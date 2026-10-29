"""Private account-scoped mail versions, resumable period scans and processing outcomes.

Facts live only in the ledger; the outcome table records which saved version each
extractor revision has handled, so repeated processing is a no-op.
"""

import hashlib
import json
from datetime import UTC, datetime

from pydantic import TypeAdapter

from .gmail import Candidate
from .ledger import Ledger
from .monthly import SINGAPORE
from .storage import unpack

CANDIDATE = TypeAdapter(Candidate)


def decode_candidate(namespace, message_id, payload):
    candidate = CANDIDATE.validate_json(unpack(payload))
    source = candidate.observation
    if (
        source.source_system != "gmail"
        or source.source_namespace != namespace
        or source.source_record_id != message_id
    ):
        raise ValueError("Mail source identity mismatch")
    return candidate


def fresh_state():
    return {
        "started": False,
        "exhausted": False,
        "token": None,
        "tokens": [],
        "seen": [],
        "failed": {},
        "retry": [],
        "pages": 0,
        "duplicates": 0,
        "errors": [],
    }


class MailCache:
    def __init__(self, database):
        self.ledger = Ledger(database)
        with self.ledger.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_versions (namespace TEXT NOT NULL, "
                "message_id TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, "
                "receipt_month TEXT, "
                "PRIMARY KEY(namespace, message_id, digest))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_current (namespace TEXT NOT NULL, "
                "message_id TEXT NOT NULL, digest TEXT NOT NULL, "
                "PRIMARY KEY(namespace, message_id))"
            )
            # The month column holds the scanned period's key: YYYY-MM for a calendar
            # month, which is what every earlier row already is, else START..END days.
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_scans (namespace TEXT NOT NULL, "
                "month TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace, month))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_processing (namespace TEXT NOT NULL, "
                "message_id TEXT NOT NULL, digest TEXT NOT NULL, extractor TEXT NOT NULL, "
                "outcome TEXT NOT NULL, reason TEXT NOT NULL, event_id TEXT, "
                "processed_at TEXT NOT NULL, "
                "PRIMARY KEY(namespace, message_id, digest, extractor))"
            )

            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_processed_current (namespace TEXT NOT NULL, "
                "message_id TEXT NOT NULL, digest TEXT NOT NULL, extractor TEXT NOT NULL, "
                "PRIMARY KEY(namespace, message_id))"
            )

    def state(self, namespace, period):
        # Progress belongs to one account and exactly one period, never to an overlapping one.
        with self.ledger.connection() as db:
            row = db.execute(
                "SELECT payload FROM mail_scans WHERE namespace=? AND month=?",
                (namespace, period.key),
            ).fetchone()
        return {**fresh_state(), **json.loads(row[0])} if row else fresh_state()

    def save(self, namespace, period, state, candidates):
        with self.ledger.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for candidate in candidates:
                observation = candidate.observation
                if observation.source_namespace != namespace:
                    raise ValueError("Mail account mismatch")
                snapshot = CANDIDATE.dump_python(candidate, mode="json")
                del snapshot["observation"]["observed_at"]
                digest = hashlib.sha256(
                    json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                db.execute(
                    "INSERT OR IGNORE INTO mail_versions "
                    "(namespace, message_id, digest, payload, receipt_month) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        namespace,
                        observation.source_record_id,
                        digest,
                        CANDIDATE.dump_json(candidate).decode(),
                        candidate.gmail_internal_at.astimezone(SINGAPORE).strftime("%Y-%m"),
                    ),
                )
                # Returning to a previously seen version still needs a new canonical revision.
                db.execute(
                    "DELETE FROM mail_processed_current WHERE namespace=? AND message_id=? "
                    "AND digest<>?",
                    (namespace, observation.source_record_id, digest),
                )
                db.execute(
                    "INSERT INTO mail_current VALUES (?, ?, ?) "
                    "ON CONFLICT(namespace, message_id) DO UPDATE SET digest=excluded.digest",
                    (namespace, observation.source_record_id, digest),
                )
            db.execute(
                "INSERT INTO mail_scans VALUES (?, ?, ?) "
                "ON CONFLICT(namespace, month) DO UPDATE SET payload=excluded.payload",
                (namespace, period.key, json.dumps(state)),
            )

        self.ledger.maintain_storage()

    def candidates(self, namespace, period):
        """Current receipt-period mail by saved identity; None marks an unreadable payload."""
        marks = ", ".join("?" * len(period.months))
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT c.message_id, v.payload FROM mail_current c JOIN mail_versions v "
                "ON v.namespace=c.namespace AND v.message_id=c.message_id AND v.digest=c.digest "
                f"WHERE c.namespace=? AND (v.receipt_month IN ({marks}) "
                "OR v.receipt_month IS NULL) ORDER BY c.message_id",
                (namespace, *period.months),
            ).fetchall()
        result = {}
        for message_id, payload in rows:
            try:
                candidate = decode_candidate(namespace, message_id, payload)
            except (ValueError, RecursionError):
                result[message_id] = None
                continue
            if period.contains(candidate.gmail_internal_at):
                result[message_id] = candidate
        return result

    def pending(self, namespace, extractor, limit=100):
        """Bounded raw (message_id, digest, payload) rows, oldest unattempted/retry first."""
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT c.message_id, c.digest, v.payload FROM mail_current c JOIN mail_versions v "
                "ON v.namespace=c.namespace AND v.message_id=c.message_id AND v.digest=c.digest "
                "LEFT JOIN mail_processed_current p ON p.namespace=c.namespace "
                "AND p.message_id=c.message_id AND p.digest=c.digest AND p.extractor=? "
                "LEFT JOIN mail_processing f ON f.namespace=c.namespace "
                "AND f.message_id=c.message_id "
                "AND f.digest=c.digest AND f.extractor=? "
                "WHERE c.namespace=? AND p.message_id IS NULL "
                "ORDER BY coalesce(f.processed_at, ''), c.message_id LIMIT ?",
                (extractor, extractor, namespace, limit),
            ).fetchall()
        return rows

    def pending_count(self, namespace, extractor):
        with self.ledger.connection() as db:
            return db.execute(
                "SELECT count(*) FROM mail_current c LEFT JOIN mail_processed_current p "
                "ON p.namespace=c.namespace AND p.message_id=c.message_id AND p.digest=c.digest "
                "AND p.extractor=? WHERE c.namespace=? AND p.message_id IS NULL",
                (extractor, namespace),
            ).fetchone()[0]

    def version(self, namespace, message_id, digest):
        with self.ledger.connection() as db:
            row = db.execute(
                "SELECT payload FROM mail_versions WHERE namespace=? AND message_id=? AND digest=?",
                (namespace, message_id, digest),
            ).fetchone()
        return decode_candidate(namespace, message_id, row[0]) if row else None

    def outcomes(self, namespace, extractor):
        """Processing result of each message's current version under one extractor revision."""
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT p.message_id, p.outcome, p.reason, p.event_id FROM mail_current c "
                "JOIN mail_processing p ON p.namespace=c.namespace "
                "AND p.message_id=c.message_id AND p.digest=c.digest "
                "WHERE c.namespace=? AND p.extractor=? AND (p.outcome='failed' OR EXISTS ("
                "SELECT 1 FROM mail_processed_current h WHERE h.namespace=c.namespace "
                "AND h.message_id=c.message_id AND h.digest=c.digest AND h.extractor=p.extractor))",
                (namespace, extractor),
            ).fetchall()
        return {
            message_id: {
                "outcome": outcome,
                "reasons": tuple(filter(None, reason.split(","))),
                "event_id": event_id,
            }
            for message_id, outcome, reason, event_id in rows
        }

    @staticmethod
    def record_outcome(db, namespace, message_id, digest, extractor, outcome, reasons, event_id):
        """Inside the caller's transaction, so an outcome never outlives a failed ledger write."""
        if outcome != "failed":
            db.execute(
                "INSERT OR REPLACE INTO mail_processed_current VALUES (?, ?, ?, ?)",
                (namespace, message_id, digest, extractor),
            )
        db.execute(
            "INSERT OR REPLACE INTO mail_processing VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                namespace,
                message_id,
                digest,
                extractor,
                outcome,
                ",".join(reasons),
                event_id,
                datetime.now(UTC).isoformat(),
            ),
        )
