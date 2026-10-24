"""One local account, bounded in-memory candidates, no financial extraction/acceptance."""

import hashlib
import secrets
import time
from dataclasses import dataclass, field

from .gmail import AuthenticationFailure, GmailAdapter, GmailRestClient
from .gmail_auth import GmailProblem, GoogleOAuth, NativeStore, client_config


@dataclass(repr=False)
class Pending:
    flow: object = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    redirect: str
    expires: float


class GmailService:
    def __init__(self, database, *, store=None, oauth=None, client_factory=None, clock=None):
        self.store = store if store is not None else NativeStore()
        self.oauth = oauth if oauth is not None else GoogleOAuth()
        self.client_factory = client_factory if client_factory is not None else GmailRestClient
        self.clock = clock if clock is not None else time.monotonic
        self.key = "connection-" + hashlib.sha256(str(database).encode()).hexdigest()
        self.pending = {}
        self.connection = None
        self.connection_id = secrets.token_urlsafe(24)
        self.candidates = {}
        self.load_days = 7
        self.load_limit = 20
        self.notice = (
            "Disconnected. Connect Gmail or restore your saved connection; "
            "mail loads only on request."
        )
        self.problem = None

    def config(self):
        raw = self.store.get("desktop-client")
        if raw is None:
            raise GmailProblem("not_configured")
        return client_config(raw)

    def clear(self):
        self.connection = None
        self.candidates = {}
        self.connection_id = secrets.token_urlsafe(24)
        self.notice = "Disconnected. Temporary mail cleared; no mail loads automatically."

    def begin(self, session, redirect):
        config = self.config()
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if v.expires > now}
        if len(self.pending) >= 32:
            raise GmailProblem("oauth_failed")
        flow, url, state, nonce = self.oauth.begin(config, redirect)
        # Starting an account switch immediately hides the old account's mail.
        self.clear()
        self.problem = None
        self.pending[session] = Pending(flow, state, nonce, redirect, now + 600)
        return url

    def callback(self, session, params, redirect):
        pending = self.pending.get(session)
        state = params.get("state", "")
        if (
            pending is None
            or pending.expires <= self.clock()
            or not secrets.compare_digest(pending.state.encode(), state.encode())
            or pending.redirect != redirect
        ):
            raise GmailProblem("invalid_callback")
        del self.pending[session]  # consume before network calls, including denial/failure
        if params.get("error"):
            raise GmailProblem(
                "consent_denied" if params["error"] == "access_denied" else "oauth_failed"
            )
        code = params.get("code", "")
        if not code or len(code) > 4096:
            raise GmailProblem("invalid_callback")
        connection = self.oauth.finish(pending.flow, code, pending.nonce)
        self.store.put(self.key, connection.saved())  # fail closed before publishing connection
        self.clear()
        self.pending.clear()  # another tab cannot replace the newly selected account
        self.connection = connection
        self.problem = None
        self.notice = "Connected. No mail fetched. Choose a recent range and Load mail."

    def restore(self):
        config = self.config()
        raw = self.store.get(self.key)
        if raw is None:
            raise GmailProblem("no_saved_connection")
        connection = self.oauth.restore(raw, config)
        self.clear()
        self.pending.clear()
        self.connection = connection
        self.problem = None
        self.notice = (
            "Saved connection restored, not yet verified with Google. "
            "Load mail to refresh authorization."
        )

    def disconnect(self):
        self.clear()
        self.pending.clear()
        self.store.delete(self.key)
        self.problem = "disconnected"

    def access_token(self):
        if self.connection is None:
            raise AuthenticationFailure()
        try:
            if not self.connection.credentials.valid:
                self.oauth.refresh(self.connection)
                self.store.put(self.key, self.connection.saved())
            return self.connection.credentials.token
        except GmailProblem as exc:
            self.problem = exc.code
            self.clear()
            raise AuthenticationFailure() from None

    def load(self, days, limit, connection_id):
        if self.connection is None or not secrets.compare_digest(
            connection_id.encode(), self.connection_id.encode()
        ):
            raise GmailProblem("reconnect")
        if (
            type(days) is not int
            or days not in {1, 7, 30}
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Choose 1, 7 or 30 recent days and a message limit from 1 to 100")
        self.load_days, self.load_limit = days, limit
        connection = self.connection
        namespace = "google-" + hashlib.sha256(connection.subject.encode()).hexdigest()
        adapter = GmailAdapter(self.client_factory(self), namespace)
        seen, tokens, found, errors = set(), set(), {}, []
        token = None
        duplicates = examined = 0
        complete = False
        self.problem = None
        for _ in range(5):
            page = adapter.fetch_page(
                query=f"newer_than:{days}d",
                page_token=token,
                page_size=min(20, limit - examined),
                seen_ids=seen,
            )
            duplicates += page.duplicate_count
            examined += len(page.results) + page.duplicate_count
            if page.error:
                errors.append(page.error.code)
            for result in page.results:
                if result.candidate is not None:
                    found[result.message_id] = result.candidate
                    seen.add(result.message_id)
                else:
                    errors.append(result.error.code)
            if not page.successful:
                break  # no checkpoint of failed IDs; Refresh starts a fresh bounded scan
            if page.next_page_token is None:
                complete = True
                break
            if page.next_page_token in tokens:
                errors.append("invalid_response")
                break
            tokens.add(page.next_page_token)
            token = page.next_page_token
            if examined >= limit:
                break
        if connection is not self.connection:
            # A refresh/store failure cleared authorization; never restore stale cached mail.
            raise GmailProblem(self.problem or "reconnect")
        if any(code in {"authentication_failed", "refresh_failed"} for code in errors):
            self.clear()
            raise GmailProblem("reconnect")
        refreshed = len(set(found) & set(self.candidates))
        self.candidates = found  # bounded latest batch, never an accumulating mailbox backfill
        self.notice = (
            f"{len(found)} candidates loaded; {refreshed} refreshed; "
            f"{duplicates} duplicate IDs skipped. "
            f"Range: last {days} days; limit: {limit} messages / 5 pages. "
        )
        if errors:
            self.notice += (
                "Partial/failed load: "
                + ", ".join(sorted(set(errors)))
                + ". Refresh mail to retry."
            )
        elif complete:
            self.notice += (
                "Query exhausted (0 results means no matching candidates, not no transactions)."
            )
        else:
            self.notice += "Loading limit reached; more mail may match. Narrow the range if needed."
