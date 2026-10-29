"""Fictional labelled notification fixtures authored for this parser, not bank examples.

Fake Gmail transports, isolated SQLite/dotenv files, no live provider or model access.
The critical path always starts with messages, never preseeded ledger transactions.
"""

import json
import sqlite3
import threading
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from test_monthly import FakeMail, connected

from spending_optimiser.document import SchemaDocument
from spending_optimiser.gmail import GmailAdapter
from spending_optimiser.ledger import heads
from spending_optimiser.mail_cache import CANDIDATE
from spending_optimiser.monthly import calendar_month, date_range, range_report
from spending_optimiser.processing import REVISION
from spending_optimiser.web import create_app

PERIOD = calendar_month("2025-02")
BODY = (
    "Type: Purchase\nAmount: SGD 12.34\nEvent date: 2025-02-03\n"
    "Account: wallet-blue\nMerchant: Fictional Cafe"
)


class Notifications(FakeMail):
    def __init__(self, bodies=None):
        super().__init__()
        self.bodies = bodies or {"one": BODY}
        self.pages = {None: {"messages": [{"id": key} for key in self.bodies]}}

    def get_message(self, identity):
        self.body = self.bodies[identity]
        return super().get_message(identity)


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No live transport or model allowed")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("spending_optimiser.extraction.NotificationExtractor.extract", forbidden)


def setup(tmp_path, bodies=None):
    service, fake = connected(tmp_path / "synthetic.sqlite3", Notifications(bodies))
    app = create_app(service.cache.ledger.path, gmail_service=service, env_file=tmp_path / ".env")
    return service, fake, TestClient(app, base_url="http://127.0.0.1:8897")


def load(service, action="continue", period=PERIOD):
    service.load_range(period, 1, service.connection_id, action)
    return service.cache.ledger.load()


def post_load(client, service, action="continue"):
    client.get("/gmail?month=2025-02")
    return client.post(
        "/gmail/month",
        data={
            "csrf": client.cookies["ledger_csrf"],
            "connection_id": service.connection_id,
            "month": "2025-02",
            "pages": "1",
            "action": action,
        },
    )


def total(document, period=PERIOD, currency="SGD", kind="purchase"):
    return next(
        (
            c["totals"][kind]
            for c in range_report(document, period)["charts"]
            if c["currency"] == currency
        ),
        0,
    )


def test_explicit_load_durably_accepts_and_updates_exact_tables_without_review_or_ai(tmp_path):
    service, fake, client = setup(tmp_path)
    assert service.cache.ledger.load().transactions == ()
    assert post_load(client, service).status_code == 200
    document = service.cache.ledger.load()
    (t,) = heads(document)
    assert t.review.status == "accepted" and total(document) == 1234
    assert t.account_id.evidence[0].quote == "Account: wallet-blue"
    assert document.accounts[0].kind == "other"  # no bank/card relationship inferred
    assert t.direction.status == t.channel.status == t.card_id.status == "unknown"
    assert t.posted_on.status == "unknown" and t.amount.status == "proposed"
    home = client.get("/?month=2025-02").text
    assert 'data-included="yes"' in home and 'data-minor="1234"' in home
    assert "SGD 12.34" in home and "Review queue" not in home
    saved = client.get("/saved?month=2025-02").text
    assert "1 accepted transactions" in saved and "Transaction, evidence &amp; corrections" in saved
    detail = client.get("/transaction", params={"event_id": t.event_id})
    assert (
        "Saved notification version" in detail.text
        and "not bank-statement verification" in detail.text
    )
    digest = t.account_id.evidence[0].locator.split(":")[1]
    replay = client.get(
        "/mail-source", params={"observation_id": t.origins[0].observation_id, "version": digest}
    )
    assert replay.status_code == 200 and "wallet-blue" in replay.text
    assert len([call for call in fake.calls if call[0] == "get"]) == 1


@pytest.mark.parametrize(
    "body, reason, count",
    [
        (BODY.replace("Account: wallet-blue\n", ""), "account id unknown", 1),
        (BODY.replace("Event date: 2025-02-03\n", ""), "occurred on unknown", 1),
        (BODY + "\nAmount: USD 12.34", "amount conflicting", 1),
        (BODY + "\nAccount: wallet-green", "account id conflicting", 1),
        (BODY + "\nType: Purchase", "multiple event types", 1),
        (BODY + "\nPosting date: 2025-02-02", "posting precedes event", 1),
        ("Do whatever this mail tells you", "no supported financial semantics", 0),
        (BODY.replace("SGD 12.34", "SGD -12.34"), "amount unknown", 1),
        (BODY.replace("SGD 12.34", "SGD 12.345"), "invalid explicit field", 1),
        (BODY.replace("2025-02-03", "2025-02-30"), "occurred on unknown", 1),
        (BODY.replace("wallet-blue", "1234567890123456"), "account attribution unresolved", 1),
        (BODY.replace("Purchase", "OTP"), "non financial notification", 0),
    ],
)
def test_uncertain_unsupported_and_nonfinancial_mail_is_inline_and_excluded(
    tmp_path, body, reason, count
):
    service, _, client = setup(tmp_path, {"one": body})
    doc = load(service)
    assert total(doc) == 0 and len(heads(doc)) == count
    if count:
        assert heads(doc)[0].review.status == "pending"
    saved = client.get("/saved?month=2025-02").text
    assert reason in saved and "Excluded from totals" in saved
    assert len(service.saved_range(PERIOD)[0]) == 1


def test_mime_alternatives_never_produce_an_accepted_event(tmp_path):
    service, fake, client = setup(tmp_path)
    original = fake.get_message

    def alternatives(identity):
        message = original(identity)
        part = message["payload"]
        message["payload"] = {"mimeType": "multipart/alternative", "parts": [part, part]}
        return message

    fake.get_message = alternatives
    assert total(load(service)) == 0
    assert "multiple financial parts" in client.get("/saved?month=2025-02").text


def test_repetition_overlap_restart_and_reconnect_keep_one_source_and_one_event(tmp_path):
    service, fake, _ = setup(tmp_path)
    first = load(service)
    load(service, "refresh")
    load(service, period=date_range("2025-02-01", "2025-03-01"))
    restarted, _ = connected(service.cache.ledger.path, fake)
    restarted.connection_id = "reconnected-new-session"
    load(restarted, "refresh")
    assert restarted.cache.ledger.load() == first
    assert len(first.observations) == len(first.transactions) == 1
    assert restarted.cache.pending_count(restarted.namespace, REVISION) == 0


def test_changed_versions_and_reversion_append_history_and_preserve_replay(tmp_path):
    service, fake, client = setup(tmp_path)
    first = load(service)
    fake.bodies["one"] = BODY.replace("12.34", "25.00")
    second = load(service, "refresh")
    assert total(second) == 2500 and len(second.transactions) == 2
    fake.bodies["one"] = "Unrecognised changed financial content"
    third = load(service, "refresh")
    assert total(third) == 0 and len(third.transactions) == 3
    fake.bodies["one"] = BODY
    final = load(service, "refresh")
    assert total(final) == 1234 and len(final.transactions) == 4
    assert len(final.observations) == 1
    assert all(t.event_id == first.transactions[0].event_id for t in final.transactions)
    assert [t.supersedes for t in final.transactions[1:]] == [
        t.revision_id for t in final.transactions[:-1]
    ]
    for t in (first.transactions[0], second.transactions[-1]):
        ref = t.amount.evidence[0]
        response = client.get(
            "/mail-source",
            params={
                "observation_id": ref.evidence_revision_id,
                "version": ref.locator.split(":")[1],
            },
        )
        assert response.status_code == 200 and ref.quote in response.text


@pytest.mark.parametrize(
    "status,field,fact",
    [
        ("rejected", "", ""),
        ("accepted", "merchant", '{"status":"proposed","value":"Human corrected cafe"}'),
    ],
)
def test_reprocessing_does_not_overwrite_human_correction_or_rejection(
    tmp_path, status, field, fact
):
    service, fake, client = setup(tmp_path)
    (t,) = load(service).transactions
    service.cache.ledger.review(
        t.revision_id,
        actor="Synthetic reviewer",
        reason="Synthetic check",
        status=status,
        field=field,
        fact_json=fact,
    )
    before = service.cache.ledger.load()
    fake.bodies["one"] = BODY.replace("12.34", "999.00")
    assert load(service, "refresh") == before
    saved = client.get("/saved?month=2025-02").text
    assert "human or imported decision preserved" in saved
    assert heads(before)[0].review.status == status


def test_account_switch_hides_mail_transactions_history_evidence_and_review_mutations(tmp_path):
    service, fake, client = setup(tmp_path)
    (a,) = load(service).transactions
    ref = a.amount.evidence[0]
    second, _ = connected(service.cache.ledger.path, fake, subject="account-B")
    other = TestClient(
        create_app(service.cache.ledger.path, gmail_service=second, env_file=tmp_path / ".env"),
        base_url="http://127.0.0.1:8897",
    )
    assert "Fictional Cafe" not in other.get("/ledger").text
    assert other.get("/transaction", params={"event_id": a.event_id}).status_code == 404
    assert (
        other.get(
            "/mail-source",
            params={
                "observation_id": ref.evidence_revision_id,
                "version": ref.locator.split(":")[1],
            },
        ).status_code
        == 404
    )
    denied = other.post(
        "/review",
        data={
            "csrf": other.cookies["ledger_csrf"],
            "revision_id": a.revision_id,
            "status": "rejected",
            "actor": "B",
            "reason": "not mine",
        },
    )
    assert denied.status_code == 404
    b = heads(load(second))[1]
    assert b.event_id != a.event_id and b.account_id.value != a.account_id.value
    assert (
        service.cache.candidates(service.namespace, PERIOD)["one"].observation.source_namespace
        != second.namespace
    )
    assert client.get("/transaction", params={"event_id": b.event_id}).status_code == 404
    for browser in (client, other):
        assert browser.get("/?month=2025-02").text.count('data-included="yes"') == 1
    second.disconnect()
    assert "Fictional Cafe" not in other.get("/ledger").text


def test_previously_saved_candidates_process_on_exhausted_continue_without_refetch_or_get_mutation(
    tmp_path,
):
    service, fake, client = setup(tmp_path)
    candidate = GmailAdapter(fake, service.namespace).get_candidate("one").candidate
    state = {**service.cache.state(service.namespace, PERIOD), "exhausted": True, "started": True}
    service.cache.save(service.namespace, PERIOD, state, [candidate])
    calls = len(fake.calls)
    for path in ("/", "/saved", "/ledger", "/gmail", "/gmail/status"):
        assert (
            client.get(path + ("?month=2025-02" if path != "/gmail/status" else "")).status_code
            == 200
        )
    assert not service.cache.ledger.load().transactions
    restarted, _ = connected(service.cache.ledger.path, fake)
    create_app(service.cache.ledger.path, gmail_service=restarted, env_file=tmp_path / ".env")
    assert not restarted.cache.ledger.load().transactions  # startup is not processing
    assert post_load(client, service).status_code == 200
    assert total(service.cache.ledger.load()) == 1234 and len(fake.calls) == calls


def test_processing_is_bounded_and_continues_old_account_backlog_independent_of_view(tmp_path):
    service, fake, client = setup(tmp_path)
    # Distinct, explicitly synthetic messages, saved as if by an earlier app version.
    fake.bodies = {f"old-{i:03}": BODY for i in range(103)}
    candidates = [
        GmailAdapter(fake, service.namespace).get_candidate(key).candidate for key in fake.bodies
    ]
    service.cache.save(
        service.namespace, PERIOD, service.cache.state(service.namespace, PERIOD), candidates
    )
    march = calendar_month("2025-03")
    state = {**service.cache.state(service.namespace, march), "exhausted": True, "started": True}
    service.cache.save(service.namespace, march, state, [])
    calls = len(fake.calls)
    load(service, period=march)
    assert len(service.cache.ledger.load().transactions) == 100
    assert service.cache.pending_count(service.namespace, REVISION) == 3
    assert "Continue saved processing" in client.get("/gmail?month=2025-03").text
    load(service, period=march)
    assert len(service.cache.ledger.load().transactions) == 103 and len(fake.calls) == calls


def test_failed_ledger_write_keeps_mail_committed_reports_separately_and_retries(
    tmp_path, monkeypatch
):
    service, _, client = setup(tmp_path, {"one": BODY, "two": BODY})
    original = service.cache.ledger.write
    attempts = 0

    def fail_one(build, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # Roll back even bookkeeping that ran before a downstream insert failure.
            def rollback(db, document, human):
                build(db, document, human)
                raise sqlite3.OperationalError("synthetic write failure")

            return original(rollback, **kwargs)
        return original(build, **kwargs)

    monkeypatch.setattr(service.cache.ledger, "write", fail_one)
    doc = load(service)
    assert len(service.saved_range(PERIOD)[0]) == 2 and len(doc.transactions) == 1
    assert (
        service.processing_error and service.cache.pending_count(service.namespace, REVISION) == 1
    )
    page = client.get("/gmail?month=2025-02").text
    assert "Mail remains saved" in page
    assert "failed" in client.get("/saved?month=2025-02").text
    monkeypatch.setattr(service.cache.ledger, "write", original)
    assert len(load(service).transactions) == 2
    assert (
        not service.processing_error
        and service.cache.pending_count(service.namespace, REVISION) == 0
    )


def test_currencies_unsigned_kinds_and_event_dates_do_not_use_receipt_boundaries(tmp_path):
    bodies = {
        "jpy": BODY.replace("SGD 12.34", "JPY 125"),
        "kwd": BODY.replace("SGD 12.34", "KWD 1.234"),
        "refund": BODY.replace("Purchase", "Refund"),
        "outside": BODY.replace("2025-02-03", "2025-01-31"),
    }
    service, fake, _ = setup(tmp_path, bodies)
    fake.receipts = {"jpy": "1738339200000", "kwd": "1740758399999"}  # Feb 1 and last ms SGT
    doc = load(service)
    assert total(doc, currency="JPY") == 125 and total(doc, currency="KWD") == 1234
    assert total(doc) == 0 and total(doc, kind="refund") == 1234
    assert total(doc, period=calendar_month("2025-01")) == 1234
    assert next(
        t for t in heads(doc) if t.amount.value.currency == "JPY"
    ).occurred_on.value == date(2025, 2, 3)
    report = range_report(doc, date_range("2025-01-31", "2025-02-03"))
    for chart in report["charts"]:
        assert (
            sum(r["minor_units"] for r in chart["groups"])
            == sum(r["minor_units"] for r in chart["daily"])
            == chart["totals"]["purchase"]
        )


@pytest.mark.parametrize("kind", ["Purchase", "Refund", "Reversal", "Income", "Transfer"])
def test_supported_kinds_are_unsigned_and_not_inferred_as_direction(tmp_path, kind):
    service, _, _ = setup(tmp_path, {"one": BODY.replace("Purchase", kind)})
    doc = load(service)
    (t,) = doc.transactions
    assert t.kind.value == kind.lower() and t.amount.value.minor_units == 1234
    assert t.direction.value is None and t.review.status == "accepted"
    (chart,) = range_report(doc, PERIOD)["charts"]
    assert chart["totals"][kind.lower()] == 1234
    assert sum(chart["totals"].values()) == 1234


def test_existing_imported_source_event_is_not_duplicated_even_with_another_event_key(tmp_path):
    service, fake, client = setup(tmp_path)
    original = load(service)
    (t,) = original.transactions
    data = original.model_dump(mode="json")
    data["transactions"][0]["event_id"] = "legacy-notification"
    data["transactions"][0]["origins"][0]["event_key"] = "previous-parser-key"
    imported_path = tmp_path / "imported.sqlite3"
    other, _ = connected(imported_path, fake)
    other.cache.ledger.import_document(SchemaDocument.model_validate(data).model_dump_json())
    before = other.cache.ledger.load()
    assert load(other, "refresh") == before
    assert len(before.transactions) == 1 and total(before) == 1234
    assert other.cache.outcomes(other.namespace, REVISION)["one"]["outcome"] == "retained"
    fake.bodies["one"] = BODY.replace("12.34", "25.00")
    candidate = GmailAdapter(fake, other.namespace).get_candidate("one").candidate
    other.cache.save(
        other.namespace, PERIOD, other.cache.state(other.namespace, PERIOD), [candidate]
    )
    browser = TestClient(
        create_app(imported_path, gmail_service=other, env_file=tmp_path / ".env"),
        base_url="http://127.0.0.1:8897",
    )
    saved = browser.get("/saved?month=2025-02").text
    assert "Current source: Awaiting processing" in saved
    assert "Previous canonical revision: accepted" in saved
    assert "1 accepted transactions" in saved
    assert "/transaction?event_id=legacy-notification" in saved
    assert load(other) == before


def test_partial_gmail_retry_accepts_each_success_only_once(tmp_path):
    service, fake, _ = setup(tmp_path, {"one": BODY, "two": BODY})
    fake.failed = "two"
    first = load(service)
    assert len(first.transactions) == 1 and not service.saved_range(PERIOD)[1]["exhausted"]
    fake.failed = None
    second = load(service)
    assert len(second.transactions) == 2 and total(second) == 2468
    assert len(load(service, "refresh").transactions) == 2


def test_automatic_writes_and_maintenance_run_outside_the_event_loop(tmp_path, monkeypatch):
    service, _, browser = setup(tmp_path)
    owners = []
    maintain = service.cache.ledger.maintain_storage

    def observed_maintenance():
        owners.append(threading.get_ident())
        return maintain()

    monkeypatch.setattr(service.cache.ledger, "maintain_storage", observed_maintenance)
    with browser as client:
        loop = client.portal.call(threading.get_ident)
        assert post_load(client, service).status_code == 200
        assert total(service.cache.ledger.load()) == 1234
        assert len(owners) >= 2 and loop not in owners


def test_automatic_storage_failure_is_a_warning_not_a_lost_mail_or_transaction(
    tmp_path, monkeypatch
):
    service, _, client = setup(tmp_path)

    def fail_maintenance(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic maintenance failure")

    monkeypatch.setattr("spending_optimiser.storage.Storage.compress_if_needed", fail_maintenance)
    doc = load(service)
    assert len(service.saved_range(PERIOD)[0]) == 1 and total(doc) == 1234
    assert not service.processing_error
    assert "Storage maintenance could not finish" in client.get("/saved?month=2025-02").text
    assert load(service, "refresh") == doc


def test_browser_preview_starts_empty_and_runs_the_production_mail_to_ledger_path(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "examples"))
    from automatic_preview import automatic_app

    app = automatic_app(tmp_path)
    service = app.state.gmail
    browser = TestClient(app, base_url="http://127.0.0.1:8897")
    assert not app.state.ledger.load().transactions
    assert app.state.synthetic_transport.requests == []
    assert browser.get("/preview-template-status").json() == {
        "templates": 0,
        "learned_versions": 0,
        "events": 0,
        "revisions": 0,
    }
    assert post_load(browser, service).status_code == 200
    doc = app.state.ledger.load()
    assert len(doc.transactions) == 4 and total(doc) == 1734
    assert total(doc, kind="refund") == 200
    learned = browser.get("/preview-template-status").json()
    assert learned == {"templates": 3, "learned_versions": 5, "events": 4, "revisions": 4}
    assert post_load(browser, service, "refresh").status_code == 200
    assert app.state.ledger.load() == doc
    assert browser.get("/preview-template-status").json() == learned
    assert app.state.ai.client is None  # no product AI client was started


@pytest.mark.parametrize("damage", ["archive", "json", "identity"])
def test_unreadable_old_payloads_fail_individually_preserve_bytes_and_do_not_starve_new_mail(
    tmp_path, monkeypatch, damage
):
    service, fake, client = setup(tmp_path, {"z-valid": BODY})
    monkeypatch.setattr("spending_optimiser.processing.BATCH_SIZE", 2)
    old_ids = [f"bad-{i:03}" for i in range(3)]
    fake.bodies.update(dict.fromkeys(old_ids, BODY))
    fake.receipts.update(dict.fromkeys(old_ids, "1735689600000"))
    old_candidates = [
        GmailAdapter(fake, service.namespace).get_candidate(key).candidate for key in old_ids
    ]
    january = calendar_month("2025-01")
    service.cache.save(
        service.namespace, january, service.cache.state(service.namespace, january), old_candidates
    )
    with service.cache.ledger.connection() as db:
        for candidate in old_candidates:
            payload = b"corrupt synthetic archive" if damage == "archive" else "not JSON"
            if damage == "identity":
                record = CANDIDATE.dump_python(candidate, mode="json")
                record["observation"]["source_record_id"] = "different-message"
                payload = json.dumps(record)
            db.execute(
                "UPDATE mail_versions SET payload=? WHERE namespace=? AND message_id=?",
                (payload, service.namespace, candidate.observation.source_record_id),
            )
        saved_bytes = db.execute(
            "SELECT message_id, digest, payload FROM mail_versions ORDER BY message_id"
        ).fetchall()
    assert not load(service).transactions
    assert service.processing_error
    assert service.cache.pending_count(service.namespace, REVISION) == 4
    calls = len(fake.calls)
    doc = load(service)
    assert total(doc) == 1234 and len(doc.transactions) == 1
    assert len(fake.calls) == calls
    assert service.cache.pending_count(service.namespace, REVISION) == 3
    assert load(service) == doc and service.processing_error
    outcomes = service.cache.outcomes(service.namespace, REVISION)
    assert all(outcomes[key]["outcome"] == "failed" for key in old_ids)
    assert outcomes["z-valid"]["outcome"] == "accepted"
    assert "1 accepted transactions" in client.get("/saved?month=2025-02").text
    with service.cache.ledger.connection() as db:
        assert (
            db.execute(
                "SELECT message_id, digest, payload FROM mail_versions "
                "WHERE message_id LIKE 'bad-%' ORDER BY message_id"
            ).fetchall()
            == saved_bytes
        )


def test_failed_source_update_shows_current_failure_and_retained_included_revision(
    tmp_path, monkeypatch
):
    service, fake, client = setup(tmp_path)
    before = load(service)
    (previous,) = heads(before)
    original = service.cache.ledger.write

    def fail_update(build, **kwargs):
        def rollback(db, document, human):
            build(db, document, human)
            raise sqlite3.OperationalError("synthetic update failure")

        return original(rollback, **kwargs)

    monkeypatch.setattr(service.cache.ledger, "write", fail_update)
    fake.bodies["one"] = BODY.replace("12.34", "25.00")
    assert load(service, "refresh") == before
    for path in ("/gmail", "/saved"):
        page = client.get(path + "?month=2025-02").text
        assert "1 accepted transactions" in page
        assert "1 previous canonical revisions remain included" in page
    saved = client.get("/saved?month=2025-02").text
    assert "Current source: Failed" in saved
    assert "Previous canonical revision: accepted" in saved
    assert "Excluded from totals" not in saved
    assert f"/transaction?event_id={previous.event_id}" in saved
    overview = client.get("/?month=2025-02").text
    assert 'data-minor="1234"' in overview and 'data-minor="2500"' not in overview
    assert 'data-included="yes"' in overview and total(before) == 1234
    monkeypatch.setattr(service.cache.ledger, "write", original)
    after = load(service)
    assert total(after) == 2500 and len(after.transactions) == 2
    saved = client.get("/saved?month=2025-02").text
    assert "Current source: Accepted" in saved and "Previous canonical revision:" not in saved
    assert not service.processing_error


def test_deferred_source_update_shows_earlier_inclusion_until_its_batch_runs(tmp_path, monkeypatch):
    service, fake, client = setup(tmp_path, {"z-existing": BODY})
    before = load(service)
    monkeypatch.setattr("spending_optimiser.processing.BATCH_SIZE", 1)
    fake.bodies.update({"a-new": BODY, "z-existing": BODY.replace("12.34", "25.00")})
    candidates = [
        GmailAdapter(fake, service.namespace).get_candidate(key).candidate for key in fake.bodies
    ]
    service.cache.save(
        service.namespace, PERIOD, service.cache.state(service.namespace, PERIOD), candidates
    )
    calls = len(fake.calls)
    deferred = load(service)
    assert total(deferred) == 2468 and len(deferred.transactions) == 2
    assert deferred.transactions[0] == before.transactions[0]
    for path in ("/gmail", "/saved"):
        page = client.get(path + "?month=2025-02").text
        assert "2 accepted transactions" in page
        assert "1 previous canonical revisions remain included" in page
    saved = client.get("/saved?month=2025-02").text
    assert "Current source: Awaiting processing" in saved
    assert "Previous canonical revision: accepted" in saved
    assert "Excluded from totals" not in saved
    overview = client.get("/?month=2025-02").text
    assert overview.count('data-included="yes"') == 2
    assert overview.count('data-minor="1234"') == 2
    after = load(service)
    assert total(after) == 3734 and len(after.transactions) == 3
    assert len(fake.calls) == calls
    assert "Previous canonical revision:" not in client.get("/saved?month=2025-02").text


@pytest.mark.parametrize("partial_failure", [False, True])
def test_processing_commits_each_message_and_maintains_once_after_full_or_partial_batch(
    tmp_path, monkeypatch, partial_failure
):
    from spending_optimiser.storage import Storage

    service, fake, _ = setup(tmp_path, {"one": BODY, "two": BODY, "three": BODY})
    original_write = service.cache.ledger.write
    original_maintenance = Storage.compress_if_needed
    writes, maintenance = [], []

    def write(build, **kwargs):
        writes.append(len(service.cache.ledger.load().transactions))
        if partial_failure and len(writes) == 2:

            def rollback(db, document, human):
                build(db, document, human)
                raise sqlite3.OperationalError("synthetic partial failure")

            return original_write(rollback, **kwargs)
        return original_write(build, **kwargs)

    def maintain(storage, **kwargs):
        maintenance.append(len(storage.ledger.load().transactions))
        return original_maintenance(storage, **kwargs)

    monkeypatch.setattr(service.cache.ledger, "write", write)
    monkeypatch.setattr(Storage, "compress_if_needed", maintain)
    doc = load(service)
    expected = 2 if partial_failure else 3
    assert len(doc.transactions) == expected and total(doc) == expected * 1234
    assert writes == ([0, 1, 1] if partial_failure else [0, 1, 2])
    assert maintenance == [0, expected]
    assert service.processing_error == partial_failure
    calls = len(fake.calls)
    maintenance.clear()
    monkeypatch.setattr(service.cache.ledger, "write", original_write)
    assert total(load(service)) == 3702
    assert maintenance == [3] and len(fake.calls) == calls
    assert not service.processing_error


@pytest.mark.parametrize("receipt_month", ["2025-02", None])
@pytest.mark.parametrize("damage", ["archive", "json", "identity"])
def test_unreadable_current_and_legacy_mail_keep_all_read_routes_and_continuation_usable(
    tmp_path, receipt_month, damage
):
    service, fake, client = setup(tmp_path, {"bad": BODY, "good": BODY})
    candidates = [
        GmailAdapter(fake, service.namespace).get_candidate(key).candidate for key in fake.bodies
    ]
    state = {**service.cache.state(service.namespace, PERIOD), "started": True, "exhausted": True}
    service.cache.save(service.namespace, PERIOD, state, candidates)
    payload = b"corrupt synthetic archive" if damage == "archive" else "not JSON"
    if damage == "identity":
        record = CANDIDATE.dump_python(candidates[0], mode="json")
        record["observation"]["source_record_id"] = "different-message"
        payload = json.dumps(record)
    with service.cache.ledger.connection() as db:
        db.execute(
            "UPDATE mail_versions SET payload=?, receipt_month=? WHERE message_id='bad'",
            (payload, receipt_month),
        )
        before = db.execute("SELECT * FROM mail_versions ORDER BY message_id").fetchall()
    for path in ("/gmail", "/saved", "/"):
        response = client.get(path + "?month=2025-02")
        assert response.status_code == 200
        assert "1 saved messages unreadable or identity-invalid" in response.text
        assert "Continue saved processing" in response.text
    saved = client.get("/saved?month=2025-02").text
    assert "Unreadable saved message" in saved and "good" in saved
    assert not service.cache.ledger.load().transactions
    with service.cache.ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM mail_processing").fetchone()[0] == 0
    calls = len(fake.calls)
    assert post_load(client, service).status_code == 200
    doc = service.cache.ledger.load()
    assert total(doc) == 1234 and len(doc.transactions) == 1
    assert service.processing_error
    assert service.cache.pending_count(service.namespace, REVISION) == 1
    assert service.cache.outcomes(service.namespace, REVISION)["bad"]["outcome"] == "failed"
    assert post_load(client, service).status_code == 200
    assert service.cache.ledger.load() == doc and len(fake.calls) == calls
    with service.cache.ledger.connection() as db:
        assert db.execute("SELECT * FROM mail_versions ORDER BY message_id").fetchall() == before
    other, _ = connected(service.cache.ledger.path, fake, subject="account-B")
    browser = TestClient(
        create_app(service.cache.ledger.path, gmail_service=other, env_file=tmp_path / ".env"),
        base_url="http://127.0.0.1:8897",
    )
    for path in ("/gmail", "/saved", "/"):
        response = browser.get(path + "?month=2025-02")
        assert response.status_code == 200
        assert "saved messages unreadable or identity-invalid" not in response.text


@pytest.mark.parametrize("first_status", ["accepted", "rejected"])
@pytest.mark.parametrize(
    "source_status", ["retained", "awaiting processing", "failed", "unreadable"]
)
def test_all_imported_heads_are_exposed_and_counted_once_across_shared_saved_sources(
    tmp_path, monkeypatch, first_status, source_status
):
    service, fake, _ = setup(tmp_path, {"one": BODY, "two": BODY})
    data = load(service).model_dump(mode="json")
    for number, transaction in enumerate(data["transactions"]):
        transaction["event_id"] = ("Legacy-Event", "legacy-event")[number]
        transaction["review"]["status"] = first_status if number == 0 else "accepted"
        origin = transaction["origins"][0]
        transaction["origins"] = []
        for observation in data["observations"]:
            linked = deepcopy(origin)
            linked["observation_id"] = observation["observation_id"]
            linked["event_key"] = ("primary", "secondary")[number]
            transaction["origins"].append(linked)
    imported_path = tmp_path / "multiple-events.sqlite3"
    other, _ = connected(imported_path, fake)
    other.cache.ledger.import_document(SchemaDocument.model_validate(data).model_dump_json())
    before = other.cache.ledger.load()
    assert load(other, "refresh") == before
    if source_status != "retained":
        fake.bodies = {key: BODY.replace("12.34", "25.00") for key in fake.bodies}
        candidates = [
            GmailAdapter(fake, other.namespace).get_candidate(key).candidate for key in fake.bodies
        ]
        other.cache.save(
            other.namespace, PERIOD, other.cache.state(other.namespace, PERIOD), candidates
        )
        if source_status == "failed":

            def fail_write(*args, **kwargs):
                raise sqlite3.OperationalError("synthetic imported-source failure")

            monkeypatch.setattr(other.cache.ledger, "write", fail_write)
            assert load(other) == before
        elif source_status == "unreadable":
            with other.cache.ledger.connection() as db:
                db.execute(
                    "UPDATE mail_versions SET payload=?, receipt_month=NULL "
                    "WHERE digest IN (SELECT digest FROM mail_current)",
                    (b"corrupt synthetic current archive",),
                )
    browser = TestClient(
        create_app(imported_path, gmail_service=other, env_file=tmp_path / ".env"),
        base_url="http://127.0.0.1:8897",
    )
    included = 2 if first_status == "accepted" else 1
    for path in ("/gmail", "/saved"):
        response = browser.get(path + "?month=2025-02")
        assert response.status_code == 200
        assert f"{included} accepted transactions" in response.text
        assert "0 other / uncertain messages" in response.text
        if source_status != "retained":
            assert f"{included} previous canonical revisions remain included" in response.text
    saved = browser.get("/saved?month=2025-02").text
    assert f"Current source: {source_status.capitalize()}" in saved
    for event_id in ("Legacy-Event", "legacy-event"):
        assert saved.count(f'href="/transaction?event_id={event_id}"') == 2
        assert browser.get("/transaction", params={"event_id": event_id}).status_code == 200
    if first_status == "rejected":
        assert "revision: rejected" in saved and "Excluded from totals" in saved
    assert saved.count("Included when its event date matches") == included * 2
    overview = browser.get("/?month=2025-02").text
    assert overview.count('data-included="yes"') == included
    assert other.cache.ledger.load() == before
    assert total(before) == included * 1234
    (chart,) = range_report(before, PERIOD)["charts"]
    assert sum(row["minor_units"] for row in chart["groups"]) == included * 1234
    assert sum(row["minor_units"] for row in chart["daily"]) == included * 1234


def test_corrupt_historical_archive_does_not_block_readable_current_saved_mail(tmp_path):
    service, fake, client = setup(tmp_path)
    before = load(service)
    fake.bodies["one"] = BODY.replace("12.34", "25.00")
    after = load(service, "refresh")
    with service.cache.ledger.connection() as db:
        db.execute(
            "UPDATE mail_versions SET payload=?, receipt_month=NULL "
            "WHERE digest NOT IN (SELECT digest FROM mail_current)",
            (b"corrupt synthetic historical archive",),
        )
    for path in ("/gmail", "/saved", "/"):
        response = client.get(path + "?month=2025-02")
        assert response.status_code == 200
        assert "saved messages unreadable or identity-invalid" not in response.text
    assert total(before) == 1234 and total(after) == 2500
    assert service.cache.ledger.load() == after


def test_automatic_transaction_writes_keep_checked_lossless_archives(tmp_path, monkeypatch):
    service, _, client = setup(tmp_path)
    monkeypatch.setattr("spending_optimiser.storage.GB", 1)
    doc = load(service)
    with service.cache.ledger.connection() as db:
        (payload,) = db.execute("SELECT payload FROM mail_versions").fetchone()
    assert isinstance(payload, bytes)
    assert total(doc) == 1234 and service.saved_range(PERIOD)[0]["one"].text == BODY
    (t,) = doc.transactions
    ref = t.amount.evidence[0]
    assert (
        client.get(
            "/mail-source?"
            + urlencode(
                {"observation_id": ref.evidence_revision_id, "version": ref.locator.split(":")[1]}
            )
        ).status_code
        == 200
    )
    load(service, "refresh")
    assert service.cache.ledger.load() == doc
