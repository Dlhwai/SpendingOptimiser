"""Private account-scoped mail versions and resumable period scans, separate from facts."""

import hashlib
import json

from pydantic import TypeAdapter

from .gmail import Candidate
from .ledger import Ledger
from .monthly import SINGAPORE
from .storage import unpack

CANDIDATE = TypeAdapter(Candidate)


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
        # Versions are retained for evidence replay, including content changes.
        # Each observation appears once in the selected period's saved-mail view,
        # whichever scan saved it.
        marks = ", ".join("?" * len(period.months))
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT v.payload FROM mail_current c JOIN mail_versions v "
                "ON v.namespace=c.namespace AND v.message_id=c.message_id AND v.digest=c.digest "
                f"WHERE c.namespace=? AND (v.receipt_month IN ({marks}) "
                "OR v.receipt_month IS NULL) ORDER BY c.message_id",
                (namespace, *period.months),
            ).fetchall()
        latest = {}
        for (payload,) in rows:
            candidate = CANDIDATE.validate_json(unpack(payload))
            latest[candidate.observation.source_record_id] = candidate
        return {
            identity: candidate
            for identity, candidate in latest.items()
            if period.contains(candidate.gmail_internal_at)
        }
