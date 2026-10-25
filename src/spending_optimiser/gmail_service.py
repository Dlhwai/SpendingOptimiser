"""One local account, explicit bounded month scans and saved mail. No extraction/AI."""

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime

from .gmail import AuthenticationFailure, GmailAdapter, GmailRestClient
from .gmail_auth import GmailProblem, GoogleOAuth, NativeStore, client_config
from .mail_cache import MailCache, fresh_state
from .monthly import SINGAPORE, calendar_month

# Proven per-message failures: retained for targeted retry without blocking later pages.
PERSISTENT = {"invalid_message", "response_too_large"}


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
        self.cache = MailCache(database)
        self.pending = {}
        self.connection = None
        self.connection_id = secrets.token_urlsafe(24)
        self.notice_month = None
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
        self.connection_id = secrets.token_urlsafe(24)
        self.notice_month = None
        self.notice = "Disconnected. Mail hidden; saved mail remains on this device."

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
        self.notice = "Connected. No mail fetched. Choose a calendar month and Load month."

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
            "Select Load month to refresh authorization."
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

    @property
    def namespace(self):
        if self.connection is None:
            return None
        return "google-" + hashlib.sha256(self.connection.subject.encode()).hexdigest()

    def saved_month(self, month):
        namespace = self.namespace
        if namespace is None:
            return {}, None
        return self.cache.candidates(namespace, month), self.cache.state(namespace, month)

    def load_month(self, key, pages, connection_id, action):
        month = calendar_month(key)
        if self.connection is None or not secrets.compare_digest(
            connection_id.encode(), self.connection_id.encode()
        ):
            raise GmailProblem("reconnect")
        if (
            type(pages) is not int
            or not 1 <= pages <= 5
            or action not in {"continue", "refresh", "retry"}
        ):
            raise ValueError("Choose 1-5 pages and Continue, Retry failed messages or Refresh")
        connection, namespace = self.connection, self.namespace
        state = fresh_state() if action == "refresh" else self.cache.state(namespace, month)
        self.notice_month = month.key
        if action == "continue" and state["exhausted"]:
            self.notice = "Month query already exhausted. Refresh month to check for new mail."
            return
        adapter = GmailAdapter(self.client_factory(self), namespace)
        self.problem = None
        if action == "retry":
            # Targeted retry of retained per-message failures; listing state is unchanged.
            found, errors = [], []
            for message_id in list(state["failed"])[: pages * 20]:
                result = adapter.get_candidate(message_id)
                if result.candidate is not None:
                    found.append(result.candidate)
                    state["seen"].append(message_id)
                    del state["failed"][message_id]
                else:
                    state["failed"][message_id] = result.error.code
                    errors.append(result.error.code)
            self.verify(connection, errors)
            state["checked_at"] = datetime.now(SINGAPORE).isoformat(timespec="seconds")
            self.cache.save(namespace, month, state, found)
        else:
            for _ in range(pages):
                page = adapter.fetch_page(
                    query=month.query,
                    page_token=state["token"],
                    page_size=20,
                    seen_ids=[*state["seen"], *state["failed"]],
                )
                state["started"] = True
                state["checked_at"] = datetime.now(SINGAPORE).isoformat(timespec="seconds")
                retried = set(state["retry"])
                errors = [page.error.code] if page.error else []
                found = []
                for result in page.results:
                    if result.candidate is not None:
                        found.append(result.candidate)
                        state["seen"].append(result.message_id)
                    elif result.error.code in PERSISTENT:
                        state["failed"][result.message_id] = result.error.code
                    else:
                        errors.append(result.error.code)
                        continue
                    state["retry"].append(result.message_id)
                self.verify(connection, errors)
                state["errors"] = sorted(set(errors))
                if not errors:
                    token = page.next_page_token
                    if token is not None and token in state["tokens"]:
                        state["errors"] = ["pagination_cycle; refresh month to restart"]
                    else:
                        if token is not None:
                            state["tokens"].append(token)
                        state["token"] = token
                        state["exhausted"] = token is None
                        # Count each page once; IDs saved by earlier attempts are not duplicates.
                        state["pages"] += 1
                        state["duplicates"] += page.duplicate_count - len(
                            retried & set(page.duplicate_ids)
                        )
                        state["retry"] = []
                # Persist successes plus the unadvanced checkpoint on a failed page.
                # Explicit Continue retries that page, skipping resolved IDs.
                self.cache.save(namespace, month, state, found)
                if state["errors"] or state["exhausted"]:
                    break
        count = len(self.cache.candidates(namespace, month))
        failed = len(state["failed"])
        self.notice = (
            f"{count} saved candidates for {month.label}; {state['pages']} pages completed; "
            f"{state['duplicates']} duplicate IDs skipped; {failed} unresolved messages. "
            + (
                "Query exhausted, but unresolved messages are not saved. "
                "Retry failed messages; coverage is incomplete."
                if state["exhausted"] and failed
                else "Query exhausted, not proof of complete financial coverage."
                if state["exhausted"]
                else "Incomplete month. Continue loading or retry; totals are saved ledger only."
            )
        )

    def verify(self, connection, errors):
        if connection is not self.connection or any(
            code in {"authentication_failed", "refresh_failed"} for code in errors
        ):
            self.clear()
            # A refresh/store failure cleared authorization; keep its specific problem.
            raise GmailProblem(self.problem or "reconnect")
