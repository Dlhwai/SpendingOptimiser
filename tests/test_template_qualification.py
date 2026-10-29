"""The local-qualification output is a fixed counter/error contract, not a mail dump."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

from test_local_templates import NARRATIVE
from test_processing import PERIOD, setup

from spending_optimiser.gmail import GmailAdapter
from spending_optimiser.local_templates import validate_generic

SCRIPT = Path(__file__).parents[1] / "tools" / "qualify_saved_templates.py"
KEYS = {
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
}


def invoke(source, directory, *, output=None):
    args = [
        sys.executable,
        str(SCRIPT),
        "--source",
        str(source),
        "--private-directory",
        str(directory),
    ]
    if output:
        args += ["--generic-output", str(output)]
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def test_query_only_consistent_backup_includes_wal_and_emits_only_safe_contracts(tmp_path):
    secret = "SYNTHETIC_SECRET_NOT_DISCLOSED"
    service, fake, _ = setup(tmp_path, {"one": secret + "\n" + NARRATIVE})
    source = service.cache.ledger.path
    anchor = sqlite3.connect(source)
    try:
        assert anchor.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        saved = GmailAdapter(fake, service.namespace).get_candidate("one").candidate
        service.cache.save(service.namespace, PERIOD, {}, [saved])
        before = anchor.execute(
            "SELECT namespace, message_id, digest, payload FROM mail_versions"
        ).fetchall()
        directory = tmp_path / "protected-copy"
        directory.mkdir(mode=0o700)
        output = directory / "generic.json"
        result = invoke(source, directory, output=output)
        assert result.returncode == 0 and result.stderr == ""
        assert secret not in result.stdout and str(source) not in result.stdout
        counts = {
            key: int(value.strip())
            for key, value in (line.split(":", 1) for line in result.stdout.splitlines())
        }
        assert set(counts) == KEYS
        assert counts["examined"] == counts["supported"] == counts["versions_examined"] == 1
        assert (
            anchor.execute(
                "SELECT namespace, message_id, digest, payload FROM mail_versions"
            ).fetchall()
            == before
        )
        assert anchor.execute("SELECT count(*) FROM private_records").fetchone()[0] == 0
        assert not anchor.execute(
            "SELECT 1 FROM sqlite_master WHERE name='mail_templates'"
        ).fetchone()
        asset = json.loads(output.read_text())
        assert secret not in output.read_text() and service.namespace not in output.read_text()
        assert asset["forms"] and all(validate_generic(form) for form in asset["forms"])
        assert all(
            stat.S_IMODE(file.stat().st_mode) == 0o600
            for file in directory.iterdir()
            if file.is_file()
        )
    finally:
        anchor.close()


def test_private_invalid_rows_and_schema_failures_never_escape_as_diagnostics(tmp_path):
    service, fake, _ = setup(tmp_path)
    saved = GmailAdapter(fake, service.namespace).get_candidate("one").candidate
    service.cache.save(service.namespace, PERIOD, {}, [saved])
    secret = "SYNTHETIC_PRIVATE_ERROR_SENTINEL"
    with service.cache.ledger.connection() as db:
        db.execute("UPDATE mail_versions SET payload=?", (json.dumps({"body": secret}),))
    directory = tmp_path / "protected"
    result = invoke(service.cache.ledger.path, directory)
    assert result.returncode == 0 and result.stderr == "" and secret not in result.stdout
    assert "unreadable: 1" in result.stdout
    wrong = tmp_path / "invalid.sqlite3"
    wrong.write_text(secret)
    os.chmod(wrong, 0o600)
    error = invoke(wrong, directory)
    assert (
        error.returncode == 1
        and error.stdout == "error: qualification_failed\n"
        and error.stderr == ""
    )
    missing = invoke(tmp_path / "absent.sqlite3", directory)
    assert (
        missing.returncode == 1
        and missing.stdout == "error: source_missing\n"
        and missing.stderr == ""
    )
