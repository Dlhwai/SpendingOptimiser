"""Disposable synthetic storage only: no default database, mail, network or keychain."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_monthly import FIXTURE, connected, load

from spending_optimiser.document import SchemaDocument
from spending_optimiser.ledger import Ledger
from spending_optimiser.monthly import calendar_month, monthly_report
from spending_optimiser.storage import GB, ArchiveError, Storage, hot_start, pack, unpack
from spending_optimiser.web import create_app

NOW = datetime(2025, 5, 1, tzinfo=UTC)
FEB = calendar_month("2025-02")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Synthetic dependencies only")

    monkeypatch.setattr("spending_optimiser.gmail_auth.NativeStore._backend", forbidden)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)


def seed(tmp_path, subject="account-A"):
    service, fake = connected(tmp_path / "synthetic.sqlite3", subject=subject)
    fake.body = "Synthetic <script>inert</script> evidence 日 " * 1000
    fake.pages = {None: {"messages": [{"id": "old"}]}}
    load(service)
    return service, fake, Storage(service.cache.ledger)


def over_trigger(storage, value=GB + 1_000_000):
    # Sparse, disposable sidecar. Measures actual logical bytes without allocating
    # a gigabyte or touching a real ledger. This database does not use WAL mode.
    sidecar = storage.paths[2]
    with sidecar.open("wb") as out:
        out.truncate(max(0, value - storage.ledger.path.stat().st_size))
    return sidecar


def versions(service):
    with service.cache.ledger.connection() as db:
        return db.execute(
            "SELECT namespace, message_id, digest, payload FROM mail_versions "
            "ORDER BY namespace, message_id, digest"
        ).fetchall()


@pytest.mark.parametrize(
    "instant,expected",
    [
        (datetime(2025, 2, 28, 15, 59, 59, tzinfo=UTC), date(2024, 12, 1)),
        (datetime(2025, 2, 28, 16, tzinfo=UTC), date(2025, 1, 1)),
        (datetime(2024, 3, 31, 16, tzinfo=UTC), date(2024, 2, 1)),
        (datetime(2025, 12, 31, 16, tzinfo=UTC), date(2025, 11, 1)),
    ],
)
def test_hot_months_are_singapore_calendar_months(instant, expected):
    assert hot_start(instant) == expected


def test_exact_decimal_gb_threshold_and_explicit_paths(tmp_path):
    service, _, storage = seed(tmp_path)
    assert GB == 1_000_000_000
    original = versions(service)
    assert storage.compress_if_needed(now=NOW)["compressed"] == 0
    sidecar = over_trigger(storage, GB)
    assert storage.status()["bytes"] == GB
    assert storage.compress_if_needed(now=NOW)["compressed"] == 0
    assert versions(service) == original
    over_trigger(storage, GB + 1)
    assert storage.status()["bytes"] == GB + 1
    result = storage.compress_if_needed(now=NOW)
    assert result["compressed"] == 1 and result["compacted"]
    assert isinstance(versions(service)[0][3], bytes)
    assert len(storage.status()["paths"]) == 4
    assert {Path(p["path"]).name for p in storage.status()["paths"]} == {
        "synthetic.sqlite3",
        "synthetic.sqlite3-wal",
        "synthetic.sqlite3-shm",
        "synthetic.sqlite3-journal",
    }
    sidecar.unlink(missing_ok=True)


def test_hot_boundary_and_future_details_never_compressed(tmp_path):
    service, fake, storage = seed(tmp_path)
    fake.pages = {
        None: {
            "messages": [
                {"id": "old"},
                {"id": "start"},
                {"id": "april"},
                {"id": "may"},
                {"id": "future"},
            ]
        }
    }
    fake.receipts = {
        "old": "1740758399999",  # 2025-02-28 23:59:59.999 Singapore
        "start": "1740758400000",  # 2025-03-01 00:00 Singapore
        "april": "1743436800000",
        "may": "1746028800000",
        "future": "1767196800000",
    }
    for month in ("2025-02", "2025-03", "2025-04", "2025-05", "2026-01"):
        load(service, month=month, action="refresh")
    before = versions(service)
    over_trigger(storage)
    storage.compress_if_needed(now=NOW)
    after = versions(service)
    assert all(isinstance(p, str) for _, identity, _, p in after if identity != "old")
    assert all(isinstance(p, bytes) for _, identity, _, p in after if identity == "old")
    assert [r[:3] for r in before] == [r[:3] for r in after]
    assert [unpack(r[3]) for r in after] == [r[3] for r in before]


def test_all_mail_versions_roundtrip_account_separation_and_disconnect(tmp_path):
    service, fake, storage = seed(tmp_path)
    fake.body += " changed content"
    load(service, action="refresh")
    other, _ = connected(service.cache.ledger.path, fake, subject="account-B")
    load(other)
    original = versions(service)
    before = service.saved_month(FEB)
    over_trigger(storage)
    storage.compress_if_needed(now=NOW)
    assert len(original) == 3 and storage.status()["mail_archived"] == 3
    assert service.saved_month(FEB) == before
    assert storage.mail_archived(service.namespace, FEB) == 2
    assert storage.restore_mail(service.namespace, FEB) == 2
    assert storage.restore_mail(service.namespace, FEB) == 0  # idempotent
    assert storage.mail_archived(other.namespace, FEB) == 1
    assert all(
        unpack(a[3]) == b[3] and a[:3] == b[:3]
        for a, b in zip(versions(service), original, strict=True)
    )
    service.disconnect()
    assert service.saved_month(FEB) == ({}, None)
    assert len(versions(service)) == 3
    with pytest.raises(ValueError, match="Connect"):
        storage.restore_mail(None, FEB)
    assert set(other.saved_month(FEB)[0]) == {"old"}


def test_financial_history_evidence_imports_and_late_corrections_remain_exact(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    original = FIXTURE.monthly_document()
    ledger.import_document(original.model_dump_json())
    evidence = SchemaDocument.model_validate_json(
        (Path(__file__).parent / "fixtures/synthetic.json").read_text()
    )
    ledger.import_document(evidence.model_dump_json())
    storage = Storage(ledger)
    with ledger.connection() as db:
        receipts = dict(db.execute("SELECT digest, document FROM imports"))
    before = ledger.load()
    over_trigger(storage)
    assert storage.compress_if_needed(now=NOW)["compressed"] == 1  # undated fixture import excluded
    assert ledger.load() == before
    for digest, text in receipts.items():
        assert ledger.import_snapshot(digest) == text
    feb_before = monthly_report(before, FEB)
    assert {c["currency"] for c in feb_before["charts"]} == {"SGD", "JPY", "KWD", "USD"}
    ledger.review(
        "weekly-groceries-v2",
        actor="Synthetic",
        reason="Late date correction",
        status="accepted",
        field="occurred_on",
        fact_json='{"status":"proposed","value":"2025-03-01"}',
    )
    feb_after = monthly_report(ledger.load(), FEB)

    def total(report):
        return next(c for c in report["charts"] if c["currency"] == "SGD")["totals"]["purchase"]

    assert total(feb_after) == total(feb_before) - 4950
    latest = next(
        t
        for t in ledger.load().transactions
        if t.event_id == "weekly-groceries" and t.supersedes == "weekly-groceries-v2"
    )
    ledger.review(latest.revision_id, actor="Synthetic", reason="Late rejection", status="rejected")
    march = monthly_report(ledger.load(), calendar_month("2025-03"))
    assert march["counts"]["rejected"] == 1
    assert ledger.load().evidence == before.evidence
    assert ledger.load().observations == before.observations
    assert len(ledger.load().transactions) == len(before.transactions) + 2
    assert all(t in ledger.load().transactions for t in before.transactions)
    # Late import and imported canonical revision are recalculated, not frozen.
    data = original.model_dump(mode="json")
    revised = json.loads(json.dumps(data["transactions"][0]))
    revised.update(
        revision_id="late-imported-v2",
        supersedes=revised["revision_id"],
        recorded_at="2025-04-03T00:00:00Z",
    )
    revised["amount"]["value"]["minor_units"] = 9999
    data["transactions"] = [data["transactions"][0], revised]
    data["observations"] = [data["observations"][0]]
    ledger.import_document(json.dumps(data))
    assert total(monthly_report(ledger.load(), FEB)) == total(feb_after) + 9999 - 1280


def test_write_failure_rolls_back_all_compression_and_restoration(tmp_path):
    service, fake, storage = seed(tmp_path)
    fake.body += " version two"
    load(service, action="refresh")
    original = versions(service)
    over_trigger(storage)
    with service.cache.ledger.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_archive BEFORE UPDATE ON mail_versions "
            "WHEN (SELECT count(*) FROM mail_versions WHERE typeof(payload)='blob')=1 "
            "BEGIN SELECT RAISE(ABORT, 'synthetic full disk'); END"
        )
    with pytest.raises(sqlite3.Error):
        storage.compress_if_needed(now=NOW)
    assert versions(service) == original
    with service.cache.ledger.connection() as db:
        db.execute("DROP TRIGGER fail_archive")
    storage.compress_if_needed(now=NOW)
    archived = versions(service)
    with service.cache.ledger.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_restore BEFORE UPDATE ON mail_versions "
            "WHEN (SELECT count(*) FROM mail_versions WHERE typeof(payload)='text')=1 "
            "BEGIN SELECT RAISE(ABORT, 'synthetic interruption'); END"
        )
    with pytest.raises(sqlite3.Error):
        storage.restore_mail(service.namespace, FEB)
    assert versions(service) == archived


def test_process_interruption_retains_usable_original(tmp_path):
    service, _, storage = seed(tmp_path)
    original = versions(service)
    code = """
import os, sqlite3, sys
from spending_optimiser.storage import pack
with sqlite3.connect(sys.argv[1]) as db:
    db.execute('BEGIN IMMEDIATE')
    payload = db.execute('SELECT payload FROM mail_versions').fetchone()[0]
    db.execute('UPDATE mail_versions SET payload=?', (pack(payload),))
    os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(storage.ledger.path)], env=os.environ.copy(), check=False
    )
    assert result.returncode == 17
    assert versions(service) == original
    assert service.saved_month(FEB)[0]["old"].text


@pytest.mark.parametrize(
    "corrupt",
    [
        b"unknown",
        pack("synthetic")[:-1],
        pack("synthetic") + b"trailing",
        pack("synthetic")[:20] + b"broken",
    ],
)
def test_corrupt_payloads_are_detected_and_never_replaced(corrupt):
    with pytest.raises(ArchiveError, match="not changed"):
        unpack(corrupt)


def test_corrupt_archive_is_retained_and_restore_is_atomic(tmp_path):
    service, fake, storage = seed(tmp_path)
    fake.body += " second"
    load(service, action="refresh")
    over_trigger(storage)
    storage.compress_if_needed(now=NOW)
    with service.cache.ledger.connection() as db:
        digest = db.execute(
            "SELECT digest FROM mail_versions ORDER BY digest DESC LIMIT 1"
        ).fetchone()[0]
        db.execute("UPDATE mail_versions SET payload=? WHERE digest=?", (b"SOZ1corrupt", digest))
    damaged = versions(service)
    assert storage.compress_if_needed(now=NOW)["skipped"] == 1
    assert versions(service) == damaged
    with pytest.raises(ArchiveError):
        storage.restore_mail(service.namespace, FEB)
    assert versions(service) == damaged
    assert service.cache.ledger.load().transactions == ()


def test_unrelated_cold_corruption_does_not_block_hot_month(tmp_path):
    service, fake, storage = seed(tmp_path)
    fake.pages = {None: {"messages": [{"id": "hot"}]}}
    fake.receipts["hot"] = "1746057600000"
    load(service, month="2025-05")
    over_trigger(storage)
    storage.compress_if_needed(now=NOW)
    with service.cache.ledger.connection() as db:
        db.execute("UPDATE mail_versions SET payload=? WHERE message_id='old'", (b"SOZ1corrupt",))
    hot = calendar_month("2025-05")
    assert set(service.saved_month(hot)[0]) == {"hot"}
    assert storage.mail_archived(service.namespace, hot) == 0
    assert storage.restore_mail(service.namespace, hot) == 0
    with pytest.raises(ArchiveError):
        service.saved_month(FEB)


def test_archive_checksum_and_expansion_limit(monkeypatch):
    original = pack("Synthetic evidence 日" * 1000)
    with pytest.raises(ArchiveError):
        unpack(original[:12] + bytes(32) + original[44:])
    monkeypatch.setattr("spending_optimiser.storage.MAX_PAYLOAD", 10)
    with pytest.raises(ArchiveError):
        unpack(original)
    with pytest.raises(ValueError, match="original retained"):
        pack("x" * 11)


def test_above_threshold_without_eligible_data_is_honest_and_non_destructive(tmp_path):
    service, _, storage = seed(tmp_path)
    original = versions(service)
    over_trigger(storage)
    result = storage.compress_if_needed(now=datetime(2025, 3, 1, tzinfo=UTC))
    assert result == {"compressed": 0, "skipped": 0, "compacted": False, "above": True}
    assert versions(service) == original


def test_hot_financial_provenance_and_unsupported_content_stay_intact(tmp_path):
    service, _, storage = seed(tmp_path)
    candidate = service.saved_month(FEB)[0]["old"]
    data = FIXTURE.monthly_document().model_dump(mode="json")
    data["observations"] = [candidate.observation.model_dump(mode="json")]
    transaction = data["transactions"][0]
    transaction["origins"][0]["observation_id"] = candidate.observation.observation_id
    transaction["occurred_on"]["value"] = "2025-05-01"
    transaction["recorded_at"] = "2090-01-01T00:00:00Z"
    data["transactions"] = [transaction]
    service.cache.ledger.import_document(json.dumps(data))
    with service.cache.ledger.connection() as db:
        db.execute(
            "INSERT INTO mail_versions (namespace, message_id, digest, payload) "
            "VALUES (?, ?, ?, ?)",
            (service.namespace, "unsupported", "unknown", '{"future_format":true}'),
        )
    original = versions(service)
    over_trigger(storage)
    result = storage.compress_if_needed(now=NOW)
    assert result["compressed"] == 0 and result["skipped"] == 1
    assert versions(service) == original


@pytest.mark.parametrize("interrupted", [False, True])
def test_compaction_failure_preserves_archive_and_can_retry(tmp_path, monkeypatch, interrupted):
    from contextlib import contextmanager

    service, _, storage = seed(tmp_path)
    original = versions(service)
    over_trigger(storage)
    connect = storage.ledger.connection

    class VacuumFailure:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if sql == "VACUUM":
                if interrupted:
                    raise RuntimeError("synthetic interruption before compaction")
                raise sqlite3.OperationalError("synthetic disk full")
            return self.db.execute(sql, *args)

    @contextmanager
    def connection():
        with connect() as db:
            yield VacuumFailure(db)

    monkeypatch.setattr(storage.ledger, "connection", connection)
    if interrupted:
        with pytest.raises(RuntimeError, match="interruption"):
            storage.compress_if_needed(now=NOW)
    else:
        result = storage.compress_if_needed(now=NOW)
        assert result["compressed"] == 1 and not result["compacted"] and result["above"]
        assert "Completed writes remain saved" in result["warning"]
    assert unpack(versions(service)[0][3]) == original[0][3]
    assert service.saved_month(FEB)[0]["old"].text
    with storage.ledger.connection() as db:
        assert db.execute("PRAGMA freelist_count").fetchone()[0] > 0
    allocated = storage.ledger.path.stat().st_size
    monkeypatch.setattr(storage.ledger, "connection", connect)
    result = storage.compress_if_needed(now=NOW)
    assert result == {"compressed": 0, "skipped": 0, "compacted": True, "above": True}
    assert storage.ledger.path.stat().st_size < allocated
    assert unpack(versions(service)[0][3]) == original[0][3]
    assert storage.compress_if_needed(now=NOW) == {
        "compressed": 0,
        "skipped": 0,
        "compacted": False,
        "above": True,
    }


@pytest.mark.parametrize("operation", ["import", "example", "review", "mail", "retry"])
@pytest.mark.parametrize("failure", ["compression", "compression_io", "compaction", "precommit"])
def test_write_responses_distinguish_commits_from_maintenance_failures(
    tmp_path, monkeypatch, operation, failure
):
    service, fake, storage = seed(tmp_path)
    app = create_app(storage.ledger.path, gmail_service=service)
    ledger = app.state.ledger
    document = FIXTURE.monthly_document()
    if operation == "review":
        ledger.import_document(document.model_dump_json())
    if operation == "retry":
        fake.pages = {None: {"messages": [{"id": "old"}, {"id": "new"}]}}
        fake.broken = {"new"}
        load(service, action="refresh")
        fake.broken.clear()
    fake.pages = {None: {"messages": [{"id": "old"}, {"id": "new"}]}}
    before = ledger.load()
    before_versions = versions(service)
    before_scan = service.saved_month(FEB)[1]
    client = TestClient(app, base_url="http://127.0.0.1:8898")
    client.get("/imports")
    fields = {"csrf": client.cookies["ledger_csrf"]}
    if operation == "import":
        path = "/import"
        fields["document"] = document.model_dump_json()
    elif operation == "example":
        path = "/example"
        document = SchemaDocument.model_validate_json(
            (Path(__file__).parent / "fixtures/synthetic.json").read_text()
        )
    elif operation == "review":
        path = "/review"
        fields.update(
            revision_id="weekly-groceries-v2", actor="Synthetic", reason="Checked", status="rejected"
        )
    else:
        path = "/gmail/month"
        fields.update(
            month=FEB.key,
            pages="1",
            connection_id=service.connection_id,
            action="retry" if operation == "retry" else "refresh",
        )

    with monkeypatch.context() as patch:
        if failure == "precommit":
            table = (
                "imports"
                if operation in {"import", "example"}
                else "private_records"
                if operation == "review"
                else "mail_scans"
            )
            with ledger.connection() as db:
                db.execute(
                    f"CREATE TRIGGER fail_write BEFORE INSERT ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic disk full'); END"
                )
        else:
            patch.setattr("spending_optimiser.storage.GB", 1)
            if failure == "compaction":
                connect = sqlite3.connect

                class VacuumFailure(sqlite3.Connection):
                    def execute(self, sql, *args):
                        if sql == "VACUUM":
                            raise sqlite3.OperationalError("synthetic disk full")
                        return super().execute(sql, *args)

                def connection(*args, **kwargs):
                    return connect(*args, **kwargs, factory=VacuumFailure)

                patch.setattr(sqlite3, "connect", connection)
            else:

                def fail_pack(text):
                    if failure == "compression_io":
                        raise OSError("synthetic disk full")
                    raise sqlite3.OperationalError("synthetic disk full")

                patch.setattr("spending_optimiser.storage.pack", fail_pack)
        response = client.post(path, data=fields)

    persisted = Ledger(ledger.path)
    if failure == "precommit":
        assert response.status_code == 503 and "Request not saved" in response.text
        assert "Storage maintenance could not finish" not in response.text
        assert persisted.load() == before
        assert versions(service) == before_versions
        assert service.saved_month(FEB)[1] == before_scan
        return
    assert response.status_code == 200 and "Request not saved" not in response.text
    assert "Storage maintenance could not finish" in response.text
    assert "Completed writes remain saved" in response.text
    if operation in {"import", "example"}:
        assert persisted.load() == document
        assert "new records" in response.text
        with persisted.connection() as db:
            assert db.execute("SELECT count(*) FROM imports").fetchone()[0] == 1
    elif operation == "review":
        transactions = persisted.load().transactions
        assert len(transactions) == len(before.transactions) + 1
        assert sum(t.supersedes == "weekly-groceries-v2" for t in transactions) == 1
        assert "revisions preserved" in response.text
    else:
        assert set(service.saved_month(FEB)[0]) == {"old", "new"}
        assert len(versions(service)) == len(before_versions) + 1
        assert "2 saved candidates" in response.text
        state = service.saved_month(FEB)[1]
        assert state["exhausted"] and state["pages"] == 1 and state["failed"] == {}
    response = client.post("/storage/compress", data={"csrf": client.cookies["ledger_csrf"]})
    assert response.status_code == 200
    assert "Storage maintenance could not finish" not in response.text


def test_existing_v1_migration_and_automatic_write_path(tmp_path, monkeypatch):
    service, fake, storage = seed(tmp_path)
    original = versions(service)
    legacy = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.execute(
            "CREATE TABLE mail_versions (namespace TEXT NOT NULL, message_id TEXT NOT NULL, "
            "digest TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,message_id,digest))"
        )
        db.executemany("INSERT INTO mail_versions VALUES (?, ?, ?, ?)", original)
        db.execute(
            "CREATE TABLE mail_current (namespace TEXT, message_id TEXT, digest TEXT, "
            "PRIMARY KEY(namespace,message_id))"
        )
        db.executemany("INSERT INTO mail_current VALUES (?, ?, ?)", [row[:3] for row in original])
        db.execute("PRAGMA user_version=1")
    from spending_optimiser.mail_cache import MailCache

    migrated = MailCache(legacy)
    assert migrated.candidates(service.namespace, FEB) == service.saved_month(FEB)[0]
    with migrated.ledger.connection() as db:
        assert db.execute("SELECT receipt_month FROM mail_versions").fetchone()[0] is None
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    Ledger(storage.ledger.path)
    assert versions(service) == original
    with service.cache.ledger.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    monkeypatch.setattr("spending_optimiser.storage.GB", 1)
    fake.body += " trigger automatic maintenance"
    load(service, action="refresh")
    assert storage.status()["mail_archived"] == 2
    assert service.saved_month(FEB)[0]["old"].text == fake.body


def test_failed_schema_upgrade_rolls_back_and_v1_remains_recoverable(tmp_path, monkeypatch):
    path = tmp_path / "interrupted-v1.sqlite3"
    connect = sqlite3.connect
    with connect(path) as db:
        db.execute(
            "CREATE TABLE mail_versions "
            "(namespace TEXT, message_id TEXT, digest TEXT, payload TEXT)"
        )
        db.execute("INSERT INTO mail_versions VALUES ('synthetic', 'old', 'digest', 'original')")
        db.execute("PRAGMA user_version=1")

    def fail_upgrade(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_authorizer(
            lambda action, *unused: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_ALTER_TABLE
            else sqlite3.SQLITE_OK
        )
        return db

    with monkeypatch.context() as patch:
        patch.setattr("spending_optimiser.ledger.sqlite3.connect", fail_upgrade)
        with pytest.raises(sqlite3.DatabaseError):
            Ledger(path)
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert len(db.execute("PRAGMA table_info(mail_versions)").fetchall()) == 4
        assert db.execute("SELECT payload FROM mail_versions").fetchone()[0] == "original"
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='private_records'").fetchone()
    Ledger(path)
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT payload FROM mail_versions").fetchone()[0] == "original"


def test_real_desktop_routes_restore_protections_and_inert_mail(tmp_path):
    service, _, storage = seed(tmp_path)
    app = create_app(storage.ledger.path, gmail_service=service)
    client = TestClient(app, base_url="http://127.0.0.1:8898")
    over_trigger(storage)
    storage.compress_if_needed(now=NOW)
    page = client.get("/storage?month=2025-02")
    assert page.status_code == 200 and "Storage remains above 1 GB" in page.text
    assert (
        "1 compressed mail versions" in page.text and "No application-level encryption" in page.text
    )
    mail = client.get("/gmail?month=2025-02").text
    assert "1 older mail versions are compressed" in mail
    assert "&lt;script&gt;inert&lt;/script&gt;" in mail and "<script>" not in mail
    assert client.post("/storage/compress", data={"csrf": ""}).status_code == 403
    csrf = client.cookies["ledger_csrf"]
    fields = {"csrf": csrf, "month": "2025-02", "connection_id": service.connection_id}
    assert (
        client.post(
            "/storage/restore", data=fields, headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.post("/storage/restore", data={**fields, "connection_id": "stale"}).status_code
        == 409
    )
    response = client.post("/storage/restore", data=fields)
    assert response.status_code == 200 and "Restored 1 saved mail versions" in response.text
    assert storage.mail_archived(service.namespace, FEB) == 0
    assert service.saved_month(FEB)[0]["old"].text
    assert client.get("/storage", headers={"host": "evil.invalid"}).status_code == 400
    assert (
        client.post("/storage/restore", data={**fields, "namespace": "other-account"}).status_code
        == 422
    )
