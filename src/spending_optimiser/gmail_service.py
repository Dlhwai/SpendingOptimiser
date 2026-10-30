"""One local account, explicit bounded mail loading and deterministic local processing."""

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from .dotenv_store import DotenvFile, default_env_path
from .gmail import AuthenticationFailure, GmailAdapter, GmailRestClient
from .gmail_auth import EnvStore, GmailProblem, GoogleOAuth, client_config
from .local_templates import discovery_pending
from .mail_cache import MailCache, fresh_state
from .monthly import SINGAPORE, split_range
from .processing import REVISION, process_saved

# Proven per-message failures: retained for targeted retry without blocking later pages.
PERSISTENT = {"invalid_message", "response_too_large"}
ERRORS = {
    "rate_limited": "Gmail is limiting requests. Wait a minute, then press Refresh.",
    "permission_denied": "Gmail refused access. Disconnect and connect Gmail again.",
}
# One explicit Refresh makes at most this many Gmail listing pages (20 messages each) and
# processes at most this many local batches. Anything left is finished by pressing Refresh again.
REFRESH_PAGES = 25
PROCESS_ROUNDS = 30


@dataclass(repr=False)
class Pending:
    flow: object = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    redirect: str
    expires: float


class GmailService:
    def __init__(self, database, *, store=None, oauth=None, client_factory=None, clock=None):
        self.store = store if store is not None else EnvStore(DotenvFile(default_env_path()))
        self.oauth = oauth if oauth is not None else GoogleOAuth()
        self.client_factory = client_factory if client_factory is not None else GmailRestClient
        self.clock = clock if clock is not None else time.monotonic
        self.key = "connection-" + hashlib.sha256(str(database).encode()).hexdigest()
        self.cache = MailCache(database)
        self.pending = {}
        self.connection = None
        self.connection_id = secrets.token_urlsafe(24)
        self.notice_range = None
        self.notice = None
        self.problem = None
        self.processing_error = False
        self.new_mail = 0  # message versions stored by the Refresh in progress
        # Sign-in progress for the browser. `attempt` changes on every Connect request;
        # `busy` spans begin/callback so a poll never sees a half-finished attempt as idle.
        self.attempt = 0
        self.busy = 0

    def config(self):
        raw = self.store.get("desktop-client")
        if raw is None:
            raise GmailProblem("not_configured")
        return client_config(raw)

    def clear(self):
        self.connection = None
        self.connection_id = secrets.token_urlsafe(24)
        self.notice_range = None
        self.notice = None
        self.processing_error = False

    def attempted(self, action, *args):
        """Run a sign-in step, recording its failure before it stops counting as busy."""
        self.busy += 1
        try:
            return action(*args)
        except GmailProblem as exc:
            self.problem = exc.code
            raise
        finally:
            self.busy -= 1

    def begin(self, session, redirect):
        self.attempt += 1
        return self.attempted(self._begin, session, redirect)

    def _begin(self, session, redirect):
        config = self.config()
        now = self.clock()
        self.pending = {k: v for k, v in list(self.pending.items()) if v.expires > now}
        if len(self.pending) >= 32:
            raise GmailProblem("oauth_failed")
        flow, url, state, nonce = self.oauth.begin(config, redirect)
        # Starting an account switch immediately hides the old account's mail.
        self.clear()
        self.problem = None
        self.pending[session] = Pending(flow, state, nonce, redirect, now + 600)
        return url

    def callback(self, session, params, redirect):
        return self.attempted(self._callback, session, params, redirect)

    def _callback(self, session, params, redirect):
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

    def status(self, session):
        """In-memory progress only: no store, network or mail access."""
        pending = self.pending.get(session)
        if pending is not None and pending.expires <= self.clock():
            self.pending.pop(session, None)
            self.problem = "expired"
            pending = None
        waiting = bool(self.busy) or pending is not None
        return {"attempt": self.attempt, "state": "pending" if waiting else "idle"}

    def cancel(self, session):
        if self.pending.pop(session, None) is not None:
            self.problem = "cancelled"

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
        self.notice = "Saved connection restored. Not yet verified with Google."

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

    def saved_range(self, period):
        namespace = self.namespace
        if namespace is None:
            return {}, None
        return self.cache.candidates(namespace, period), self.cache.state(namespace, period)

    def load_range(self, period, pages, connection_id, action):
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
        try:
            self._load_range(period, pages, action, connection, namespace)
        finally:
            if self.connection is connection:
                # Saved mail commits first. Processing errors must not claim a mail-save failure.
                try:
                    self.processing_error = bool(process_saved(self.cache, namespace))
                except (ValueError, OSError, sqlite3.Error):
                    self.processing_error = True

    def _load_range(self, period, pages, action, connection, namespace, skip=(), known=None):
        known = known or {}
        state = fresh_state() if action == "refresh" else self.cache.state(namespace, period)
        self.notice_range = period.key
        if action == "continue" and state["exhausted"]:
            self.notice = "Gmail search ended. Saved mail processed; Refresh checks for new mail."
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
                    del state["failed"][message_id]
                    state["failed"][message_id] = result.error.code
                    errors.append(result.error.code)
            self.verify(connection, errors)
            state["checked_at"] = datetime.now(SINGAPORE).isoformat(timespec="seconds")
            self.new_mail += self.cache.save(namespace, period, state, found)
        else:
            for _ in range(pages):
                page = adapter.fetch_page(
                    query=period.query,
                    page_token=state["token"],
                    page_size=20,
                    seen_ids=[*skip, *known, *state["seen"], *state["failed"]],
                )
                for message_id in page.duplicate_ids:
                    if message_id in known:
                        state["failed"][message_id] = known[message_id]
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
                        state["errors"] = ["pagination_cycle; refresh period to restart"]
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
                self.new_mail += self.cache.save(namespace, period, state, found)
                if state["errors"] or state["exhausted"]:
                    break
        self.notice = None  # Counts and coverage are rendered from the saved scan state.

    def today(self):
        return datetime.now(SINGAPORE).date()

    def refresh_start(self, namespace, today):
        """First Singapore day this account's next Refresh must cover.

        An unfinished Refresh keeps its own start, so the newest page saved by an interrupted
        run is never taken as proof that the older pages behind it were loaded.
        """
        saved = self.cache.refresh_state(namespace)
        remembered = saved.get("resume_from") or saved.get("through")
        if remembered:
            return min(date.fromisoformat(remembered), today)
        latest = self.cache.latest_received(namespace)
        start = latest.astimezone(SINGAPORE).date() if latest else today.replace(day=1)
        return min(start, today)

    def refresh_view(self):
        """Saved facts only: what the Connect page says about this account's mail."""
        namespace = self.namespace
        if namespace is None:
            return None
        saved = self.cache.refresh_state(namespace)
        kind, loaded, failed = saved.get("outcome"), saved.get("loaded", 0), saved.get("failed", 0)
        checked = saved.get("checked_at")
        # Each stored fact is reported on its own, so none of them can hide another.
        covered = kind in {"complete", "failed", "processing"}
        fresh = covered and saved["checked_on"] == self.today().isoformat()
        unprocessed = kind == "processing" or bool(
            kind
            and (
                self.cache.pending_count(namespace, REVISION)
                or discovery_pending(self.cache, namespace)
            )
        )
        parts = []
        if kind == "error":
            parts.append(
                ERRORS.get(
                    saved.get("error"), "Gmail could not be reached. Press Refresh to try again."
                )
            )
        elif kind == "more":
            parts.append(f"{loaded} emails loaded. More are waiting: press Refresh.")
        elif kind is None:
            parts.append(
                "Press Refresh to load new emails."
                if self.cache.has_mail(namespace)
                else "Press Refresh to load this month's emails."
            )
        elif not fresh:
            parts.append(
                f"Last refreshed {date.fromisoformat(saved['checked_on']):%d %b %Y}. Press Refresh."
            )
        if unprocessed:
            parts.append(
                "Some saved emails could not be processed."
                + ("" if parts else " Press Refresh to retry.")
            )
        if failed:
            parts.append(f"{failed} emails could not be loaded from Gmail.")
        done = fresh and not unprocessed
        if done:
            parts.append(
                "All other recent emails are loaded."
                if failed
                else "All recent emails are loaded. "
                + (f"{loaded} new." if loaded else "No new emails.")
            )
        if done and not failed:
            state = "complete"
        elif covered and not fresh:
            state = "stale"
        elif covered:
            state = "processing" if unprocessed else "failed"
        else:
            state = kind or "new"
        return {
            "state": state,
            "message": " ".join(parts),
            "alert": state in {"failed", "error", "processing"} or bool(failed or unprocessed),
            "checked": f"{datetime.fromisoformat(checked):%d %b %Y, %H:%M}" if checked else None,
        }

    def refresh(self, connection_id, today=None):
        """The one routine mail action: load new mail, then process everything saved."""
        if self.connection is None or not secrets.compare_digest(
            connection_id.encode(), self.connection_id.encode()
        ):
            raise GmailProblem("reconnect")
        connection, namespace = self.connection, self.namespace
        today = self.today() if today is None else today
        outcome = None
        try:
            outcome = self._refresh(connection, namespace, today)
        finally:
            if self.connection is connection:
                # Saved mail commits first. Processing errors must not claim a mail-save failure.
                self.processing_error = self._process(namespace)
        if outcome is not None and self.connection is connection:
            self._finish_refresh(namespace, outcome)
        self.notice, self.notice_range = None, None

    def _refresh(self, connection, namespace, today):
        start = self.refresh_start(namespace, today)
        state = self.cache.refresh_state(namespace)
        # An unfinished Refresh resumes its own chunks. Mail can still arrive on its last day
        # after that listing passed it, so the last day is listed again with the newer days.
        end = min(date.fromisoformat(state.get("resume_to", today.isoformat())), today)
        periods = split_range(start, end)
        if end < today:
            periods += split_range(end, today)
        self.cache.save_refresh_state(
            namespace, {**state, "resume_from": start.isoformat(), "resume_to": end.isoformat()}
        )
        self.problem = None
        self.new_mail = 0
        budget, failed, error, done = REFRESH_PAGES, {}, None, True
        # Newest chunk first, so the freshest mail is usable even if the budget runs out.
        for period in reversed(periods):
            used, finished, unresolved, error = self._scan(
                period, budget, connection, namespace, today
            )
            budget -= used
            failed.update(unresolved)
            done &= finished
            if error or budget <= 0 and not finished:
                done = False
                break
        return {
            "complete": done and not error,
            "loaded": self.new_mail,
            "failed": failed,
            "error": error,
            "through": today.isoformat(),
        }

    def _scan(self, period, budget, connection, namespace, today):
        """Resume or restart one chunk. Returns pages used, listing finished, failed and error."""
        scan = self.cache.state(namespace, period)
        if (
            scan["exhausted"]
            and not scan["errors"]
            and scan["finished_on"] is not None
            and scan["finished_on"] > period.last.isoformat()
        ):
            return 0, True, scan["failed"], None  # ended after its last day: no new mail
        if budget <= 0:
            return 0, False, scan["failed"], None
        if scan["exhausted"] or not scan["started"]:
            # List from the newest page. Newer mail may have arrived since an earlier listing
            # ended; proven unloadable messages stay recorded and are not fetched again.
            scan = {**fresh_state(), "failed": scan["failed"]}
            self.cache.save(namespace, period, scan, [])
        saved = self.cache.saved_ids(namespace)
        known = {
            message_id: code
            for message_id, code in self.cache.failed_ids(namespace).items()
            if code in PERSISTENT
        }
        for message_id in saved:
            known.pop(message_id, None)
        used = 0
        while used < budget:
            self._load_range(period, 1, "continue", connection, namespace, saved, known)
            used += 1
            scan = self.cache.state(namespace, period)
            if scan["exhausted"] or scan["errors"]:
                break
        listed = scan["exhausted"] and not scan["errors"]
        if listed:
            scan["finished_on"] = today.isoformat()
            self.cache.save(namespace, period, scan, [])
        error = scan["errors"][0] if scan["errors"] else None
        return used, listed, scan["failed"], error

    def _process(self, namespace):
        """Process saved mail in bounded batches. True when some entries could not be processed."""
        try:
            for _ in range(PROCESS_ROUNDS):
                if process_saved(self.cache, namespace):
                    return True
                if not self.cache.pending_count(namespace, REVISION) and not discovery_pending(
                    self.cache, namespace
                ):
                    break
        except (ValueError, OSError, sqlite3.Error):
            return True
        return False

    def _finish_refresh(self, namespace, outcome):
        state = self.cache.refresh_state(namespace)
        pending = self.cache.pending_count(namespace, REVISION) or discovery_pending(
            self.cache, namespace
        )
        # Unloadable mail from any requested interval stays reported until it is saved.
        unloaded = {**state.get("unloaded", {}), **outcome["failed"]}
        for message_id in self.cache.saved_ids(namespace):
            unloaded.pop(message_id, None)
        if outcome["error"]:
            kind = "error"
        elif not outcome["complete"]:
            kind = "more"
        elif self.processing_error or pending:
            kind = "processing"
        elif unloaded:
            kind = "failed"
        else:
            kind = "complete"
        state.update(
            outcome=kind,
            loaded=outcome["loaded"],
            failed=len(unloaded),
            unloaded=unloaded,
            error=outcome["error"],
            checked_at=datetime.now(SINGAPORE).isoformat(timespec="seconds"),
            checked_on=outcome["through"],
        )
        if outcome["complete"]:
            # Everything from the start of this Refresh through today is saved.
            state["through"] = outcome["through"]
            state.pop("resume_from", None)
            state.pop("resume_to", None)
        self.cache.save_refresh_state(namespace, state)

    def verify(self, connection, errors):
        if connection is not self.connection or any(
            code in {"authentication_failed", "refresh_failed"} for code in errors
        ):
            self.clear()
            # A refresh/store failure cleared authorization; keep its specific problem.
            raise GmailProblem(self.problem or "reconnect")
