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
    outbound,
    parse_format,
    proposal,
    result_schema,
    safe_snapshot,
)
from .codex_client import VERSION, AIProblem, CodexClient
from .common import EvidenceRef, Fact, Money, Review
from .ledger import Conflict, canonical, parse_json
from .transactions import EventOrigin, Transaction


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

    def sources(self):
        """Current public evidence revisions that attest exact content by digest."""
        evidence = self.ledger.load().evidence
        superseded = {item.supersedes for item in evidence}
        return {
            item.revision_id: item
            for item in evidence
            if item.revision_id not in superseded
            and item.review.status != "rejected"
            and item.content_sha256 is not None
        }

    def save(self, revision_id, kind, content):
        source = self.sources().get(revision_id)
        if source is None:
            raise ValueError(
                "Choose a current public evidence record with a content SHA-256 from the ledger"
            )
        body = safe_snapshot(source, kind, content)
        identity = digest(body)
        with self.ledger.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO ai_snapshots VALUES (?, ?, ?, ?)",
                (identity, family(parse_json(body)), body, datetime.now(UTC).isoformat()),
            )
        return identity

    def snapshots(self):
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT id, body FROM ai_snapshots ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
        current = self.sources()
        items = []
        for identity, body in rows:
            try:
                if digest(body) != identity:
                    raise ValueError()
                value = checked_snapshot(body)
            except ValueError:
                items.append({"id": identity, "status": "unsupported"})
                continue
            evidence = value["evidence"]
            items.append(
                {
                    "id": identity,
                    "kind": value["kind"],
                    "source_id": evidence["source_id"],
                    "document_version": evidence["document_version"],
                    "status": "current" if evidence["revision_id"] in current else "stale",
                }
            )
        return items

    def snapshot(self, identity):
        with self.ledger.connection() as db:
            row = db.execute("SELECT body FROM ai_snapshots WHERE id = ?", (identity,)).fetchone()
        if row is None or digest(row[0]) != identity:
            raise ValueError("Saved safe snapshot is missing or changed")
        if checked_snapshot(row[0])["evidence"]["revision_id"] not in self.sources():
            raise ValueError(
                "This snapshot's public evidence was superseded, rejected or removed. "
                "Save the current revision before preview or analysis."
            )
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
