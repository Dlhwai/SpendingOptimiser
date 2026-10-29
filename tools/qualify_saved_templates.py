"""Protected local qualification. Never print records, templates, paths or exception details.

Developer-only verification, not a new user workflow. Source is query-only and copied
with SQLite backup (including committed WAL), never immutable-mode/checkpointed.
Only a validated closed generic representation may leave the protected directory.
"""

import argparse
import contextlib
import io
import json
import os
import sqlite3
import stat
import tempfile
from pathlib import Path

from spending_optimiser.ledger import heads
from spending_optimiser.local_templates import LEARNING_REVISION, LocalTemplates, generic_asset
from spending_optimiser.mail_cache import MailCache, decode_candidate
from spending_optimiser.processing import account_facts

COUNTERS = (
    "versions_examined",
    "templates_learned",
    "examined",
    "protected",
    "supported",
    "uncertain",
    "unsupported",
    "non_transaction",
    "unreadable",
    "unresolved_kind",
    "unresolved_amount",
    "unresolved_event_date",
    "unresolved_account",
    "date_century_unresolved",
    "date_scope_unresolved",
    "masked_identifier_unmapped",
    "generic_notice_kind_unresolved",
    "amount_conflicting",
    "financial_event_year_not_stated",
    "forwarded_or_multiple_financial_events",
)


class QualificationError(Exception):
    pass


def qualify(source, private_directory, generic_output=None):
    source, directory = Path(source), Path(private_directory)
    if not source.exists():
        raise QualificationError("source_missing")
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise QualificationError("source_not_private_regular_file")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise QualificationError("private_directory_invalid")
    fd, snapshot = tempfile.mkstemp(prefix="qualification-", suffix=".sqlite3", dir=directory)
    os.fchmod(fd, 0o600)
    os.close(fd)
    live = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    copied = sqlite3.connect(snapshot)
    try:
        live.execute("PRAGMA query_only=ON")
        tables = {
            row[0] for row in live.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"mail_versions", "mail_current", "private_records", "public_records"} <= tables:
            raise QualificationError("source_schema_invalid")
        for table, required in (
            ("mail_versions", {"namespace", "message_id", "digest", "payload"}),
            ("mail_current", {"namespace", "message_id", "digest"}),
        ):
            if not required <= {row[1] for row in live.execute(f"PRAGMA table_info({table})")}:
                raise QualificationError("source_schema_invalid")
        live.backup(copied)
    finally:
        copied.close()
        live.close()
    cache = MailCache(snapshot)
    templates = LocalTemplates(cache)
    counts = dict.fromkeys(COUNTERS, 0)
    # No raw objects or exception diagnostics cross this function's output boundary.
    last = 0
    while True:
        with cache.ledger.connection() as db:
            rows = db.execute(
                "SELECT rowid, namespace, message_id, digest, payload FROM mail_versions "
                "WHERE rowid>? ORDER BY rowid LIMIT 100",
                (last,),
            ).fetchall()
        if not rows:
            break
        for rowid, namespace, message_id, digest, payload in rows:
            last = rowid
            counts["versions_examined"] += 1
            try:
                candidate = decode_candidate(namespace, message_id, payload)
                templates.learn(namespace, message_id, digest, candidate)
            except (ValueError, RecursionError):
                pass
    document = cache.ledger.load()
    with cache.ledger.connection() as db:
        protected_events = {
            json.loads(payload)["event_id"]
            for (payload,) in db.execute(
                "SELECT payload FROM private_records "
                "WHERE collection='transactions' AND origin<>'mail'"
            )
        }
        counts["templates_learned"] = db.execute(
            "SELECT count(*) FROM mail_templates WHERE revision=?", (LEARNING_REVISION,)
        ).fetchone()[0]
    sources = {o.observation_id: o for o in document.observations}
    protected_sources = {
        (
            sources[origin.observation_id].source_namespace,
            sources[origin.observation_id].source_record_id,
        )
        for transaction in heads(document)
        if transaction.event_id in protected_events
        for origin in transaction.origins
    }
    last = 0
    while True:
        with cache.ledger.connection() as db:
            rows = db.execute(
                "SELECT c.rowid, c.namespace, c.message_id, c.digest, v.payload "
                "FROM mail_current c "
                "JOIN mail_versions v ON v.namespace=c.namespace AND v.message_id=c.message_id "
                "AND v.digest=c.digest WHERE c.rowid>? ORDER BY c.rowid LIMIT 100",
                (last,),
            ).fetchall()
        if not rows:
            break
        for rowid, namespace, message_id, digest, payload in rows:
            last = rowid
            if (namespace, message_id) in protected_sources:
                counts["protected"] += 1
                continue
            counts["examined"] += 1
            try:
                candidate = decode_candidate(namespace, message_id, payload)
                facts, _, outcome, reasons = account_facts(
                    templates.apply(
                        namespace, message_id, digest, candidate, candidate.observation
                    ),
                    candidate.observation,
                )
                counts[{"accepted": "supported"}.get(outcome, outcome)] += 1
                counts["date_century_unresolved"] += "date_century_unresolved" in reasons
                counts["date_scope_unresolved"] += "date_scope_unresolved" in reasons
                counts["masked_identifier_unmapped"] += any(
                    r in reasons
                    for r in (
                        "masked_identifier_not_account_mapping",
                        "card_reference_is_not_account_mapping",
                    )
                )
                counts["generic_notice_kind_unresolved"] += (
                    "generic_transaction_kind_unresolved" in reasons
                )
                counts["financial_event_year_not_stated"] += (
                    "financial_event_year_not_stated" in reasons
                )
                counts["forwarded_or_multiple_financial_events"] += (
                    "forwarded_or_multiple_financial_events" in reasons
                )
                counts["amount_conflicting"] += (
                    "amount" in facts and facts["amount"].status == "conflicting"
                )
                if outcome == "uncertain":
                    for field, counter in (
                        ("kind", "unresolved_kind"),
                        ("amount", "unresolved_amount"),
                        ("occurred_on", "unresolved_event_date"),
                        ("account_id", "unresolved_account"),
                    ):
                        if field not in facts or facts[field].value is None:
                            counts[counter] += 1
            except (ValueError, RecursionError):
                counts["unreadable"] += 1
    if generic_output is not None:
        output = Path(generic_output)
        if output.parent.resolve() != directory.resolve():
            raise QualificationError("generic_output_directory_invalid")
        # This declassification boundary reconstructs ONLY closed schema primitives.
        asset = generic_asset(templates.generic_forms()["forms"])
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(asset, handle, sort_keys=True, indent=2)
            handle.write("\n")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--private-directory", type=Path, required=True)
    parser.add_argument("--generic-output", type=Path)
    args = parser.parse_args()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            counts = qualify(args.source, args.private_directory, args.generic_output)
        if set(counts) != set(COUNTERS) or any(
            type(v) is not int or v < 0 for v in counts.values()
        ):
            raise QualificationError("counter_schema_invalid")
    except QualificationError as exc:
        code = (
            exc.args[0]
            if exc.args
            and exc.args[0]
            in {
                "source_missing",
                "source_not_private_regular_file",
                "private_directory_invalid",
                "source_schema_invalid",
                "generic_output_directory_invalid",
                "counter_schema_invalid",
            }
            else "qualification_failed"
        )
        print(f"error: {code}")
        return 1
    except BaseException:
        print("error: qualification_failed")
        return 1
    for key in COUNTERS:
        print(f"{key}: {counts[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
