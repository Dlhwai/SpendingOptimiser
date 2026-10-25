"""Append-only, transactional local storage for validated schema documents."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args
from uuid import uuid4

from pydantic import ValidationError

from .common import Fact
from .document import SchemaDocument
from .transactions import Correction, Transaction

COLLECTIONS = {
    "products": "product_id",
    "accounts": "account_id",
    "cards": "card_id",
    "observations": "observation_id",
    "evidence": "revision_id",
    "rules": "revision_id",
    "transactions": "revision_id",
}
PUBLIC = {"products", "evidence", "rules"}
CORRECTION_FIELDS = get_args(Correction.model_fields["field"].annotation)


class Conflict(ValueError):
    """An immutable identity or concurrent revision conflicts with persisted data."""


def parse_json(text: str) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON keys are not allowed")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=unique)
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep") from exc


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def heads(document: SchemaDocument) -> list[Transaction]:
    superseded = {t.supersedes for t in document.transactions}
    return [t for t in document.transactions if t.revision_id not in superseded]


def uncertain(transaction: Transaction) -> list[str]:
    return [
        name
        for name in CORRECTION_FIELDS
        if getattr(transaction, name).status in {"unknown", "proposed", "conflicting"}
    ]


def needs_review(transaction: Transaction) -> bool:
    # Accepted/rejected reviews close the queue, not the unknown optional fields.
    if transaction.review.status == "rejected":
        return False
    return transaction.review.status in {"pending", "corrected"} or any(
        getattr(transaction, name).status == "conflicting" for name in CORRECTION_FIELDS
    )


class Ledger:
    def __init__(self, path: str | Path):
        self.maintenance_warning = None
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Restrict new DBs before SQLite writes financial content, without changing umask.
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2}:
                raise ValueError("Unsupported local database version")
            for table in ("public_records", "private_records"):
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    "collection TEXT NOT NULL, identity TEXT NOT NULL, payload TEXT NOT NULL, "
                    "origin TEXT NOT NULL, PRIMARY KEY(collection, identity))"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS imports (digest TEXT PRIMARY KEY, "
                "imported_at TEXT NOT NULL, document TEXT NOT NULL)"
            )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_versions'"
            ).fetchone() and "receipt_month" not in {
                row[1] for row in db.execute("PRAGMA table_info(mail_versions)")
            }:
                # Existing text remains untouched. Cold rows get month metadata
                # when compressed, so unrelated cold corruption cannot block hot reads.
                db.execute("ALTER TABLE mail_versions ADD COLUMN receipt_month TEXT")
            # Version 2 permits verified compressed BLOBs in import/mail payloads.
            # Older binaries fail closed instead of mistaking BLOBs for JSON.
            db.execute("PRAGMA user_version = 2")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _load(self, db: sqlite3.Connection) -> SchemaDocument:
        data = {name: [] for name in COLLECTIONS}
        for table in ("public_records", "private_records"):
            for collection, payload in db.execute(
                f"SELECT collection, payload FROM {table} ORDER BY rowid"
            ):
                data[collection].append(json.loads(payload))
        return SchemaDocument.model_validate(data)

    def load(self) -> SchemaDocument:
        with self.connection() as db:
            db.execute("BEGIN")  # one consistent snapshot across both record tables
            return self._load(db)

    def maintain_storage(self):
        from .storage import MAINTENANCE_WARNING, Storage

        try:
            result = Storage(self).compress_if_needed()
        except (sqlite3.Error, OSError):
            self.maintenance_warning = MAINTENANCE_WARNING
            return None
        self.maintenance_warning = result.get("warning")
        return result

    def import_snapshot(self, digest: str) -> str:
        """Read the exact canonical import receipt, whether hot or compressed."""
        from .storage import unpack

        with self.connection() as db:
            row = db.execute("SELECT document FROM imports WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ValueError("Unknown import receipt")
        return unpack(row[0])

    def import_document(self, text: str) -> dict[str, int]:
        try:
            incoming = SchemaDocument.model_validate(parse_json(text))
        except RecursionError as exc:
            raise ValueError("Document relationships are too deeply nested") from exc
        incoming_data = incoming.model_dump(mode="json")
        serialized = canonical(incoming_data)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load(db)
            protected = {
                json.loads(payload)["event_id"]
                for (payload,) in db.execute(
                    "SELECT payload FROM private_records "
                    "WHERE collection = 'transactions' AND origin = 'manual'"
                )
            }
            merged = current.model_dump(mode="json")
            additions = []
            unchanged = 0
            for collection, key in COLLECTIONS.items():
                existing = {record[key]: record for record in merged[collection]}
                for record in incoming_data[collection]:
                    identity = record[key]
                    if identity in existing:
                        if record != existing[identity]:
                            raise Conflict(f"Immutable identity conflict: {collection}/{identity}")
                        unchanged += 1
                        continue
                    if collection == "transactions" and record["event_id"] in protected:
                        raise Conflict("Imported revision cannot extend a locally reviewed event")
                    merged[collection].append(record)
                    additions.append((collection, identity, canonical(record)))
            # Checks histories, identities and source associations across ALL imports.
            # Validation failure rolls back the entire import, including unrelated new rows.
            try:
                SchemaDocument.model_validate(merged)
            except (ValidationError, RecursionError) as exc:
                raise Conflict(
                    "Import conflicts with persisted identities, links, or revision history"
                ) from exc
            for collection, identity, payload in additions:
                table = "public_records" if collection in PUBLIC else "private_records"
                db.execute(
                    f"INSERT INTO {table} VALUES (?, ?, ?, 'import')",
                    (collection, identity, payload),
                )
            db.execute(
                "INSERT OR IGNORE INTO imports VALUES (?, ?, ?)",
                (digest, datetime.now(UTC).isoformat(), serialized),
            )
        self.maintain_storage()
        return {"added": len(additions), "unchanged": unchanged}

    def review(
        self,
        revision_id: str,
        *,
        actor: str,
        reason: str,
        status: Literal["accepted", "corrected", "rejected"],
        field: str = "",
        fact_json: str = "",
    ) -> Transaction:
        if status not in {"accepted", "corrected", "rejected"}:
            raise ValueError("Unknown review action")
        if field and field not in CORRECTION_FIELDS:
            raise ValueError("Unknown correction field")
        if not actor.strip() or not reason.strip():
            raise ValueError("Reviewer and reason are required")
        if not field and fact_json.strip():
            raise ValueError("Select a field for the replacement fact")
        if status == "corrected" and not field:
            raise ValueError("A correction must change a fact")
        if status == "rejected" and field:
            raise ValueError("Rejection applies to the whole event; save corrections separately")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            document = self._load(db)
            previous = next((t for t in heads(document) if t.revision_id == revision_id), None)
            if previous is None:
                raise Conflict("Revision is missing or stale; refresh before reviewing")
            now = datetime.now(UTC)
            if now <= previous.recorded_at:
                raise Conflict(
                    "Current revision timestamp is not before the local clock; "
                    "review requires a truthful, strictly later timestamp"
                )
            review = {
                "status": status,
                "reviewer": actor,
                "reason": reason,
                "reviewed_at": now.isoformat(),
            }
            revised = previous.model_dump(mode="json")
            revised.update(
                revision_id=f"local-{uuid4().hex}",
                supersedes=previous.revision_id,
                recorded_at=now.isoformat(),
                review=review,
            )
            if field:
                fact = parse_json(fact_json)
                if not isinstance(fact, dict):
                    raise ValueError("Replacement must be a Fact JSON object")
                revised[field] = {**fact, "review": review}
                revised["corrections"].append(
                    {
                        "field": field,
                        "actor": actor,
                        "reason": reason,
                        "corrected_at": now.isoformat(),
                    }
                )
            transaction = Transaction.model_validate(revised)
            if field and getattr(previous, field).model_dump(exclude={"review"}) == getattr(
                transaction, field
            ).model_dump(exclude={"review"}):
                raise ValueError("Correction must change the fact, not only its review metadata")
            if status == "accepted" and any(
                isinstance(getattr(transaction, name), Fact)
                and (
                    getattr(transaction, name).status == "conflicting"
                    or getattr(transaction, name).review.status == "rejected"
                )
                for name in CORRECTION_FIELDS
            ):
                raise ValueError("Resolve conflicting or rejected facts before accepting")
            merged = document.model_dump(mode="json")
            merged["transactions"].append(transaction.model_dump(mode="json"))
            SchemaDocument.model_validate(merged)
            db.execute(
                "INSERT INTO private_records VALUES ('transactions', ?, ?, 'manual')",
                (transaction.revision_id, canonical(transaction.model_dump(mode="json"))),
            )
        self.maintain_storage()
        return transaction
