"""Lossless, transactional cold payloads inside the existing SQLite database.

Financial records stay hot: range_report remains the only period calculation.
No files are removed, no background task runs, and compression is not encryption.
"""

import hashlib
import sqlite3
import struct
import zlib
from datetime import datetime

from .common import EvidenceRef
from .document import SchemaDocument, walk
from .monthly import SINGAPORE, calendar_month

GB = 1_000_000_000
MAINTENANCE_WARNING = (
    "Storage maintenance could not finish. Completed writes remain saved. "
    "Check disk space/access, then choose Check storage now in Storage."
)
MAGIC = b"SOZ1"
HEADER = struct.Struct(">4sQ32s")
# Larger/unknown payloads remain ordinary text, not discarded or truncated.
MAX_PAYLOAD = 64 * 1024 * 1024


class ArchiveError(ValueError):
    """A cold payload failed verification; leave it untouched for recovery."""


def unpack(payload):
    if isinstance(payload, str):
        return payload
    try:
        magic, size, digest = HEADER.unpack(payload[: HEADER.size])
        if magic != MAGIC or size > MAX_PAYLOAD:
            raise ValueError()
        decoder = zlib.decompressobj()
        # Bound expansion to the declared size. Never accept truncated streams,
        # trailing bytes, or a checksum mismatch, even if JSON would parse.
        raw = decoder.decompress(payload[HEADER.size :], size + 1)
        if (
            len(raw) != size
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
            or hashlib.sha256(raw).digest() != digest
        ):
            raise ValueError()
        return raw.decode("utf-8")
    except (ValueError, TypeError, OverflowError, struct.error, zlib.error, UnicodeError):
        raise ArchiveError(
            "Compressed detail is corrupt or unsupported. It was not changed. "
            "Recover this database from a known-good backup; do not delete the archive."
        ) from None


def pack(text):
    raw = text.encode("utf-8")
    if len(raw) > MAX_PAYLOAD:
        raise ValueError("Payload exceeds supported compression size; original retained")
    result = HEADER.pack(MAGIC, len(raw), hashlib.sha256(raw).digest()) + zlib.compress(raw)
    if unpack(result) != text:
        raise ArchiveError("Compression verification failed; original retained")
    return result


def hot_start(now=None):
    """Current Singapore calendar month plus its preceding two (not 90 days)."""
    current = calendar_month(
        (now or datetime.now(SINGAPORE)).astimezone(SINGAPORE).strftime("%Y-%m")
    )
    index = current.start.year * 12 + current.start.month - 1 - 2
    year, month = divmod(index, 12)
    return calendar_month(f"{year:04}-{month + 1:02}").start


class Storage:
    def __init__(self, ledger):
        self.ledger = ledger

    @property
    def paths(self):
        path = self.ledger.path
        return [
            path,
            *(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")),
        ]

    def status(self):
        paths = []
        for path in self.paths:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = 0
            paths.append({"path": str(path), "bytes": size})
        with self.ledger.connection() as db:
            imports = db.execute(
                "SELECT count(*) FROM imports WHERE typeof(document)='blob'"
            ).fetchone()[0]
            mail = 0
            if self._has_mail(db):
                mail = db.execute(
                    "SELECT count(*) FROM mail_versions WHERE typeof(payload)='blob'"
                ).fetchone()[0]
        used = sum(p["bytes"] for p in paths)
        return {
            "paths": paths,
            "bytes": used,
            "threshold": GB,
            "above": used > GB,
            "hot_start": hot_start().isoformat(),
            "mail_archived": mail,
            "imports_archived": imports,
        }

    @staticmethod
    def _has_mail(db):
        return (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_versions'"
            ).fetchone()
            is not None
        )

    def compress_if_needed(self, *, now=None):
        """Called after successful application writes, or by the explicit desktop action.

        Validate before replacing each original. SQLite commits all replacements or
        none; its journal also protects interrupted VACUUM. Disk-full errors retain
        committed data. Corrupt/unknown rows are retained and counted, never dropped.
        """
        if self.status()["bytes"] <= GB:
            return {"compressed": 0, "skipped": 0, "compacted": False, "above": False}
        cutoff = hot_start(now)
        compressed = skipped = 0
        with self.ledger.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            document = self.ledger._load(db)
            protected = set()
            for transaction in document.transactions:
                day = transaction.occurred_on.value
                if day is None or day >= cutoff or transaction.occurred_on.alternatives:
                    protected.update(o.observation_id for o in transaction.origins)
                    protected.update(
                        r.evidence_revision_id
                        for r in walk(transaction)
                        if isinstance(r, EvidenceRef) and r.target == "observation"
                    )
            if self._has_mail(db):
                # Imported lazily to avoid a ledger/cache/storage import cycle.
                from .mail_cache import decode_candidate

                for namespace, identity, digest, payload in db.execute(
                    "SELECT namespace, message_id, digest, payload FROM mail_versions"
                ):
                    try:
                        candidate = decode_candidate(namespace, identity, payload)
                        receipt = candidate.gmail_internal_at
                        if (
                            receipt is None
                            or receipt.astimezone(SINGAPORE).date() >= cutoff
                            or candidate.observation.observation_id in protected
                        ):
                            # Restore formerly cold content that is now in a hot month,
                            # e.g. clock rollback or a late canonical date correction.
                            if isinstance(payload, bytes):
                                db.execute(
                                    "UPDATE mail_versions SET payload=? "
                                    "WHERE namespace=? AND message_id=? AND digest=?",
                                    (unpack(payload), namespace, identity, digest),
                                )
                            continue
                        if isinstance(payload, bytes):
                            continue
                        packed = pack(payload)
                        if len(packed) < len(payload.encode("utf-8")):
                            db.execute(
                                "UPDATE mail_versions SET payload=?, receipt_month=? "
                                "WHERE namespace=? AND message_id=? AND digest=?",
                                (
                                    packed,
                                    receipt.astimezone(SINGAPORE).strftime("%Y-%m"),
                                    namespace,
                                    identity,
                                    digest,
                                ),
                            )
                            compressed += 1
                    except (ValueError, RecursionError):
                        skipped += 1
            for digest, payload in db.execute("SELECT digest, document FROM imports"):
                try:
                    text = unpack(payload)
                    imported = SchemaDocument.model_validate_json(text)
                    dates = [t.occurred_on.value for t in imported.transactions]
                    dates += [
                        o.observed_at.astimezone(SINGAPORE).date() for o in imported.observations
                    ]
                    dates += [
                        e.retrieved_at.astimezone(SINGAPORE).date() for e in imported.evidence
                    ]
                    dates += [r.compiled_at.astimezone(SINGAPORE).date() for r in imported.rules]
                    eligible = (
                        dates
                        and all(d is not None and d < cutoff for d in dates)
                        and not any(t.occurred_on.alternatives for t in imported.transactions)
                        and not any(o.observation_id in protected for o in imported.observations)
                    )
                    if not eligible:
                        if isinstance(payload, bytes):
                            db.execute(
                                "UPDATE imports SET document=? WHERE digest=?", (text, digest)
                            )
                        continue
                    if isinstance(payload, bytes):
                        continue
                    packed = pack(payload)
                    if len(packed) < len(text.encode("utf-8")):
                        db.execute("UPDATE imports SET document=? WHERE digest=?", (packed, digest))
                        compressed += 1
                except (ValueError, RecursionError):
                    skipped += 1
        compacted = False
        warning = None
        try:
            with self.ledger.connection() as db:
                if compressed or db.execute("PRAGMA freelist_count").fetchone()[0]:
                    db.execute("VACUUM")
                    compacted = True
        except sqlite3.Error:
            warning = MAINTENANCE_WARNING
        result = {
            "compressed": compressed,
            "skipped": skipped,
            "compacted": compacted,
            "above": self.status()["above"],
        }
        if warning:
            result["warning"] = warning
        return result

    def mail_archived(self, namespace, period):
        """Count only this identity's selected period, including earlier versions."""
        if namespace is None:
            return 0
        from .mail_cache import decode_candidate

        count = 0
        marks = ", ".join("?" * len(period.months))
        with self.ledger.connection() as db:
            for identity, payload in db.execute(
                "SELECT message_id, payload FROM mail_versions "
                "WHERE namespace=? AND typeof(payload)='blob' "
                f"AND (receipt_month IN ({marks}) OR receipt_month IS NULL)",
                (namespace, *period.months),
            ):
                try:
                    candidate = decode_candidate(namespace, identity, payload)
                except (ValueError, RecursionError):
                    continue
                count += period.contains(candidate.gmail_internal_at)
        return count

    def restore_mail(self, namespace, period):
        """Persist all versions of a selected account/period as ordinary text, atomically."""
        if namespace is None:
            raise ValueError("Connect the saved mail account before restoring its detail")
        from .mail_cache import decode_candidate

        restored = 0
        marks = ", ".join("?" * len(period.months))
        with self.ledger.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for identity, digest, payload in db.execute(
                "SELECT message_id, digest, payload FROM mail_versions "
                "WHERE namespace=? AND typeof(payload)='blob' "
                f"AND (receipt_month IN ({marks}) OR receipt_month IS NULL)",
                (namespace, *period.months),
            ):
                text = unpack(payload)  # any corrupt input rolls back every restoration
                candidate = decode_candidate(namespace, identity, text)
                if period.contains(candidate.gmail_internal_at):
                    db.execute(
                        "UPDATE mail_versions SET payload=? "
                        "WHERE namespace=? AND message_id=? AND digest=?",
                        (text, namespace, identity, digest),
                    )
                    restored += 1
        return restored
