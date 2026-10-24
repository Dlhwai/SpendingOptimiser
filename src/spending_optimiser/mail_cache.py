"""Private account-scoped mail versions and resumable month scans, separate from facts."""

import hashlib
import json

from pydantic import TypeAdapter

from .gmail import Candidate
from .ledger import Ledger

CANDIDATE = TypeAdapter(Candidate)


class MailCache:
    def __init__(self, database):
        self.ledger = Ledger(database)
        with self.ledger.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_versions (namespace TEXT NOT NULL, "
                "message_id TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(namespace, message_id, digest))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_current (namespace TEXT NOT NULL, "
                "message_id TEXT NOT NULL, digest TEXT NOT NULL, "
                "PRIMARY KEY(namespace, message_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_scans (namespace TEXT NOT NULL, "
                "month TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace, month))"
            )

    def state(self, namespace, month):
        with self.ledger.connection() as db:
            row = db.execute(
                "SELECT payload FROM mail_scans WHERE namespace=? AND month=?",
                (namespace, month.key),
            ).fetchone()
        return (
            json.loads(row[0])
            if row
            else {
                "started": False,
                "exhausted": False,
                "token": None,
                "tokens": [],
                "seen": [],
                "pages": 0,
                "duplicates": 0,
                "errors": [],
            }
        )

    def save(self, namespace, month, state, candidates):
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
                    "INSERT OR IGNORE INTO mail_versions VALUES (?, ?, ?, ?)",
                    (
                        namespace,
                        observation.source_record_id,
                        digest,
                        CANDIDATE.dump_json(candidate).decode(),
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
                (namespace, month.key, json.dumps(state)),
            )

    def candidates(self, namespace, month):
        # Versions are retained for evidence replay, including content changes.
        # Each observation appears once in the selected month's saved-mail view.
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT v.payload FROM mail_current c JOIN mail_versions v "
                "ON v.namespace=c.namespace AND v.message_id=c.message_id AND v.digest=c.digest "
                "WHERE c.namespace=? ORDER BY c.message_id",
                (namespace,),
            ).fetchall()
        latest = {}
        for (payload,) in rows:
            candidate = CANDIDATE.validate_json(payload)
            latest[candidate.observation.source_record_id] = candidate
        return {
            identity: candidate
            for identity, candidate in latest.items()
            if month.start <= candidate.gmail_internal_at.date() < month.end
        }
