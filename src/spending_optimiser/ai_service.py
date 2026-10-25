"""Explicit analysis of immutable safe snapshots, separate from accepted financial facts."""

import hashlib
import json
import secrets
import threading
from datetime import UTC, datetime
from uuid import uuid4

from .ai_inputs import checked_snapshot, digest, outbound, proposal, result_schema, safe_snapshot
from .codex_client import VERSION, AIProblem, CodexClient
from .ledger import Conflict, canonical


class AIService:
    def __init__(self, ledger, *, binary=None, client_factory=CodexClient):
        self.ledger, self.binary, self.client_factory = ledger, binary, client_factory
        namespace = hashlib.sha256(str(ledger.path).encode()).hexdigest()[:24]
        self.home = ledger.path.parent / (".spending-codex-" + namespace)
        self.client = self.connection = self.authorization_url = self.problem = None
        self.connection_id = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        with ledger.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ai_snapshots (id TEXT PRIMARY KEY, "
                "family TEXT NOT NULL, body TEXT NOT NULL, saved_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS ai_proposals (id TEXT PRIMARY KEY, "
                "snapshot_id TEXT NOT NULL, family TEXT NOT NULL, owner TEXT NOT NULL, "
                "parent TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS ai_reviews (proposal_id TEXT PRIMARY KEY, "
                "action TEXT NOT NULL, reviewed_at TEXT NOT NULL)"
            )

    @property
    def configured(self):
        return self.binary is not None

    @property
    def owner(self):
        return None if self.connection is None else digest(self.connection["email"])

    def close(self):
        with self.lock:
            if self.client is not None:
                self.client.close()
            self.client = self.connection = self.authorization_url = None
            self.connection_id = secrets.token_urlsafe(24)

    def connect(self):
        with self.lock:
            self.close()
            if not self.configured:
                raise AIProblem("not_configured")
            self.client = self.client_factory(self.binary, self.home)
            try:
                self.client.start()
                self.connection = self.client.account()
                if self.connection is None:
                    self.authorization_url = self.client.login()
                self.problem = None
            except (OSError, AIProblem):
                self.close()
                raise

    def confirm(self):
        with self.lock:
            if self.client is None:
                raise AIProblem("not_connected")
            account = self.client.account()
            if account is None:
                raise AIProblem("subscription_required")
            self.connection = account
            self.authorization_url = None
            self.connection_id = secrets.token_urlsafe(24)
            self.problem = None

    def disconnect(self):
        with self.lock:
            try:
                if self.client is not None:
                    self.client.logout()
            finally:
                self.close()

    def check_account(self):
        if self.connection is None or self.client is None:
            raise AIProblem("not_connected")
        if self.client.account() != self.connection:
            self.close()
            raise AIProblem("account_changed")

    def save(self, key):
        body = safe_snapshot(key)
        identity = digest(body)
        with self.ledger.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO ai_snapshots VALUES (?, ?, ?, ?)",
                (identity, key, body, datetime.now(UTC).isoformat()),
            )
        return identity

    def snapshots(self):
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT id, body FROM ai_snapshots ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
        return [{"id": identity, **checked_snapshot(body)} for identity, body in rows]

    def snapshot(self, identity):
        with self.ledger.connection() as db:
            row = db.execute("SELECT body FROM ai_snapshots WHERE id = ?", (identity,)).fetchone()
        if row is None or digest(row[0]) != identity:
            raise ValueError("Saved safe snapshot is missing or changed")
        checked_snapshot(row[0])
        return row[0]

    def preview(self, identity):
        return outbound(self.snapshot(identity))

    def _head(self, db, family, owner):
        row = db.execute(
            "SELECT p.id FROM ai_proposals p JOIN ai_reviews r ON p.id = r.proposal_id "
            "WHERE p.family = ? AND p.owner = ? AND r.action = 'accepted' "
            "ORDER BY r.rowid DESC LIMIT 1",
            (family, owner),
        ).fetchone()
        return None if row is None else row[0]

    def analyse(self, identity, fingerprint, connection_id, consent):
        with self.lock:
            if consent != "analyse-safe-snapshot":
                raise AIProblem("consent_required")
            body = self.snapshot(identity)
            text = outbound(body)
            if fingerprint != digest(text) or connection_id != self.connection_id:
                raise AIProblem("consent_required")
            self.check_account()
            owner = self.owner
            family = checked_snapshot(body)["key"]
            with self.ledger.connection() as db:
                parent = self._head(db, family, owner)
            identity = "ai-" + uuid4().hex
            try:
                raw = self.client.analyse(text, result_schema(body))
                result = proposal(body, raw, identity, parent)
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                if isinstance(exc, AIProblem):
                    raise
                raise AIProblem("invalid_output") from None
            # Do not retain rejected/raw model output or echo it in an error/log.
            self.check_account()
            result["provenance"] = {
                "provider": "Codex / ChatGPT",
                "client_version": VERSION,
                "snapshot_sha256": digest(body),
            }
            with self.ledger.connection() as db:
                db.execute(
                    "INSERT INTO ai_proposals VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity,
                        digest(body),
                        family,
                        owner,
                        parent,
                        canonical(result),
                        datetime.now(UTC).isoformat(),
                    ),
                )
            return identity

    def proposals(self):
        if self.owner is None:
            return []
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT p.id, p.body, COALESCE(r.action, 'pending'), p.created_at "
                "FROM ai_proposals p LEFT JOIN ai_reviews r ON p.id=r.proposal_id "
                "WHERE p.owner = ? ORDER BY p.rowid DESC LIMIT 50",
                (self.owner,),
            ).fetchall()
        return [
            {"id": identity, "result": json.loads(body), "status": status, "created_at": created}
            for identity, body, status, created in rows
        ]

    def review(self, identity, action):
        if action not in {"accepted", "rejected"}:
            raise ValueError("Choose accept or reject")
        with self.lock:
            self.check_account()
            owner = self.owner
            with self.ledger.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT family, parent, body FROM ai_proposals WHERE id = ? AND owner = ?",
                    (identity, owner),
                ).fetchone()
                if row is None:
                    raise Conflict("Proposal is missing or belongs to another connection")
                prior = db.execute(
                    "SELECT action FROM ai_reviews WHERE proposal_id = ?", (identity,)
                ).fetchone()
                if prior is not None:
                    if prior[0] != action:
                        raise Conflict("Review decisions are immutable")
                    return
                family, parent, body = row
                if action == "accepted" and (
                    not json.loads(body)["usable"] or self._head(db, family, owner) != parent
                ):
                    raise Conflict(
                        "Proposal is uncertain or stale. "
                        "Reanalyse the saved snapshot before accepting."
                    )
                db.execute(
                    "INSERT INTO ai_reviews VALUES (?, ?, ?)",
                    (identity, action, datetime.now(UTC).isoformat()),
                )
                # Acceptance stores a versioned reference, NOT an installed parser,
                # verified rewards rule, ledger event or financial correction.
