"""Explicit analysis of immutable safe snapshots, separate from accepted financial facts."""

import hashlib
import json
import secrets
import threading
from datetime import UTC, datetime
from uuid import uuid4

from .ai_inputs import (
    checked_snapshot,
    digest,
    family,
    format_snapshot,
    outbound,
    parse_format,
    proposal,
    result_schema,
    terms_snapshot,
)
from .codex_client import VERSION, AIProblem, CodexClient
from .common import EvidenceRef, Fact, Money, Review
from .ledger import Conflict, canonical, parse_json
from .public_terms import ISSUERS, discover, https_get
from .transactions import EventOrigin, Transaction


class AIService:
    def __init__(self, ledger, *, binary=None, client_factory=CodexClient, transport=https_get):
        self.ledger, self.binary, self.client_factory = ledger, binary, client_factory
        self.transport, self.import_result = transport, None
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

    def _insert(self, body):
        identity = digest(body)
        with self.ledger.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO ai_snapshots VALUES (?, ?, ?, ?)",
                (identity, family(parse_json(body)), body, datetime.now(UTC).isoformat()),
            )
        return identity

    def import_terms(self, issuer, query):
        """Explicit request only: bounded official retrieval, then app-owned capture."""
        found, problems, complete = discover(issuer, query, self.transport)
        saved = [
            self._insert(terms_snapshot(issuer, item.final_url, datetime.now(UTC), item.text))
            for item in found
        ]
        self.import_result = {
            "issuer": ISSUERS[issuer]["name"],
            "saved": len(saved),
            "problems": problems[:10],
            "complete": complete,
        }
        return saved

    def save_format(self, content):
        return self._insert(format_snapshot(content))

    LISTING = (
        "SELECT s.id, s.body, s.rowid = (SELECT MAX(t.rowid) FROM ai_snapshots t "
        "WHERE t.family = s.family) FROM ai_snapshots s "
    )

    def snapshots(self):
        with self.ledger.connection() as db:
            rows = db.execute(self.LISTING + "ORDER BY s.rowid DESC LIMIT 50").fetchall()
        items = []
        for identity, body, latest in rows:
            try:
                if digest(body) != identity:
                    raise ValueError()
                value = checked_snapshot(body)
            except ValueError:
                items.append({"id": identity, "status": "unsupported"})
                continue
            items.append(
                {
                    "id": identity,
                    "kind": value["kind"],
                    "label": (
                        value["evidence"]["url"] if value["kind"] == "terms" else value["content"]
                    ),
                    "status": "current" if latest else "stale",
                }
            )
        return items

    def snapshot(self, identity):
        with self.ledger.connection() as db:
            row = db.execute(self.LISTING + "WHERE s.id = ?", (identity,)).fetchone()
        if row is None or digest(row[1]) != identity:
            raise ValueError("Saved safe snapshot is missing or changed")
        checked_snapshot(row[1])
        if not row[2]:
            raise ValueError(
                "A newer official capture of this document exists. "
                "Use the current capture for preview or analysis."
            )
        return row[1]

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
            name = family(checked_snapshot(body))
            with self.ledger.connection() as db:
                parent = self._head(db, name, owner)
            identity = "ai-" + uuid4().hex
            schema = result_schema(body)
            try:
                raw = self.client.analyse(text, schema)
            except AIProblem:
                self.close()
                raise
            try:
                result = proposal(body, raw, identity, parent)
            except (ValueError, TypeError, KeyError, RecursionError):
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
                        name,
                        owner,
                        parent,
                        canonical(result),
                        datetime.now(UTC).isoformat(),
                    ),
                )
            return identity

    RECORD = (
        "SELECT p.id, p.body, COALESCE(r.action, 'pending'), p.created_at "
        "FROM ai_proposals p LEFT JOIN ai_reviews r ON p.id = r.proposal_id WHERE p.owner = ? "
    )

    @staticmethod
    def _record(row):
        identity, body, status, created = row
        return {"id": identity, "result": json.loads(body), "status": status, "created_at": created}

    def proposals(self):
        if self.owner is None:
            return []
        with self.ledger.connection() as db:
            rows = db.execute(
                self.RECORD + "ORDER BY p.rowid DESC LIMIT 50", (self.owner,)
            ).fetchall()
        return [self._record(row) for row in rows]

    def find_proposal(self, identity):
        if self.owner is None:
            return None
        with self.ledger.connection() as db:
            row = db.execute(self.RECORD + "AND p.id = ?", (self.owner, identity)).fetchone()
        return None if row is None else self._record(row)

    def accepted_layouts(self):
        """Current accepted format heads, applied locally without any model call."""
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT p.id, p.body FROM ai_proposals p JOIN ai_reviews r ON p.id = r.proposal_id "
                "WHERE r.action = 'accepted' AND p.family LIKE 'format:%' AND NOT EXISTS ("
                "SELECT 1 FROM ai_proposals c JOIN ai_reviews s ON c.id = s.proposal_id "
                "WHERE c.parent = p.id AND s.action = 'accepted') ORDER BY r.rowid"
            ).fetchall()
        return [(identity, json.loads(body)["layout"]) for identity, body in rows]

    def format_proposals(self, candidates):
        """Deterministic, unsaved transaction proposals from accepted layouts only."""
        layouts = self.accepted_layouts()
        found = {}
        for message_id, candidate in candidates.items():
            matches = {}
            for part in candidate.parts:
                for number, line in enumerate(part.text.splitlines()):
                    for layout_id, layout in layouts:
                        try:
                            values = parse_format(layout, line.strip())
                        except ValueError:
                            continue
                        matches.setdefault(
                            canonical(values),
                            (values, layout_id, f"{part.locator}:line:{number}", line.strip()),
                        )
            if len(matches) == 1:
                found[message_id] = layout_transaction(candidate, *next(iter(matches.values())))
        return found

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
                # Acceptance stores a versioned reference. Accepted layouts only yield
                # unsaved proposals; never a verified rule, ledger event or correction.


def layout_transaction(candidate, values, layout_id, locator, quote):
    observation = candidate.observation
    ref = EvidenceRef(
        target="observation",
        evidence_revision_id=observation.observation_id,
        locator=locator,
        quote=quote,
    )

    def fact(value):
        return Fact() if value is None else Fact(status="proposed", value=value, evidence=(ref,))

    source = [
        observation.source_system,
        observation.source_namespace,
        observation.source_record_id,
        "primary",
    ]
    return Transaction(
        event_id="notification-" + digest(canonical(source)),
        revision_id=layout_id + "-" + observation.observation_id,
        recorded_at=datetime.now(UTC),
        origins=(
            EventOrigin(
                observation_id=observation.observation_id,
                event_key="primary",
                extractor_revision=layout_id,
                processing_run_id=layout_id,
            ),
        ),
        amount=fact(Money.model_validate(values["money"])),
        occurred_on=fact(values["date"]),
        merchant=fact(values["merchant"]),
        review=Review(reason="Deterministic accepted-layout proposal; independent review required"),
    )
