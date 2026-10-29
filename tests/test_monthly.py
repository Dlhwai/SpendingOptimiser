"""Synthetic-only saved month, ledger projection and rendered chart contracts."""

import base64
import importlib.util
import sqlite3
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials

from spending_optimiser.document import SchemaDocument
from spending_optimiser.gmail import ClientFailure, IngestionError
from spending_optimiser.gmail_auth import GmailProblem, GoogleConnection
from spending_optimiser.gmail_service import GmailService
from spending_optimiser.monthly import SINGAPORE, calendar_month, format_money, range_report
from spending_optimiser.web import create_app

SPEC = importlib.util.spec_from_file_location(
    "monthly_fixture", Path(__file__).parents[1] / "examples" / "monthly_fixture.py"
)
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


@pytest.fixture(autouse=True)
def no_network_or_native_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Only disposable synthetic stores and transports allowed")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)


class MemoryStore:
    def get(self, key):
        return None

    def put(self, key, value):
        pass

    def delete(self, key):
        pass


class FakeMail:
    def __init__(self):
        self.pages = {None: {}}
        self.calls = []
        self.failed = None
        self.body = "Synthetic <script>alert(1)</script> <img src=https://tracking.invalid>"
        self.receipts = {}
        self.broken = set()

    def list_messages(self, **kwargs):
        self.calls.append(("list", kwargs))
        page = self.pages[kwargs["page_token"]]
        if isinstance(page, Exception):
            raise page
        return page

    def get_message(self, identity):
        self.calls.append(("get", identity))
        if identity == self.failed:
            raise ClientFailure(IngestionError("rate_limited", True, 429))
        raw = self.body.encode()
        return {
            "id": identity,
            "threadId": None if identity in self.broken else "same-thread",
            "internalDate": self.receipts.get(identity, "1738368000000"),
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": identity}],
                "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
            },
        }


def connected(path, fake=None, subject="account-A"):
    fake = fake or FakeMail()
    service = GmailService(path, store=MemoryStore(), client_factory=lambda _: fake)
    service.connection = GoogleConnection(
        subject, f"{subject}@example.invalid", Credentials("fake")
    )
    return service, fake


def load(service, pages=1, month="2025-02", action="continue"):
    service.load_range(calendar_month(month), pages, service.connection_id, action)
    return service.saved_range(calendar_month(month))


@pytest.mark.parametrize(
    ("initial", "count", "pages", "exhausted"),
    [("empty", 0, 0, False), ("partial", 2, 1, False), ("full", 5, 3, True)],
)
def test_monthly_preview_seed_and_continuation_use_the_singapore_month(
    tmp_path, monkeypatch, initial, count, pages, exhausted
):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "examples"))
    from monthly_preview import monthly_app

    app = monthly_app(tmp_path, initial)
    service = app.state.gmail
    month = calendar_month("2025-02")
    candidates, state = service.saved_range(month)
    assert len(candidates) == count
    assert (state["pages"], state["exhausted"]) == (pages, exhausted)
    assert state["started"] == (initial != "empty")
    ledger = app.state.ledger.load()
    client = TestClient(app, base_url="http://127.0.0.1:8877")
    assert client.get("/?month=2025-02").status_code == 200
    fields = {
        "csrf": client.cookies["ledger_csrf"],
        "month": month.key,
        "pages": "1",
        "action": "continue",
        "connection_id": service.connection_id,
    }
    for page in range(pages, 3):
        response = client.post("/gmail/month", data=fields)
        assert response.status_code == 200 and response.url.path == "/gmail"
        candidates, state = service.saved_range(month)
        assert len(candidates) == (2, 4, 5)[page]
        assert state["pages"] == page + 1
        assert state["exhausted"] == (page == 2)
    candidates, state = service.saved_range(month)
    assert set(candidates) == {f"preview-{i}" for i in range(5)}
    assert state["exhausted"] and state["duplicates"] == 1 and state["pages"] == 3
    assert all(month.contains(c.gmail_internal_at) for c in candidates.values())
    assert client.post("/gmail/month", data=fields).status_code == 200
    assert service.saved_range(month)[1] == state
    assert app.state.ledger.load() == ledger
    assert app.state.ai.client is None


def test_half_open_month_boundaries_and_query_timezone():
    feb = calendar_month("2024-02")
    assert (feb.start, feb.end) == (date(2024, 2, 1), date(2024, 3, 1))
    assert calendar_month("2025-12").end == date(2026, 1, 1)
    assert calendar_month("2025-02").query == "after:1738339199 before:1740758400"


def test_default_month_is_the_current_singapore_month(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 2, 28, 16, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("spending_optimiser.monthly.datetime", Clock)
    assert calendar_month().key == "2025-03"


@pytest.mark.parametrize(
    "value", ["2025-2", "2025-00", "2025-13", "2025-02-01", "", "all", "9999-12", "1969-12"]
)
def test_invalid_months(value):
    with pytest.raises(ValueError):
        calendar_month(value)


def test_empty_partial_full_pagination_duplicate_refresh_restart_and_account_isolation(tmp_path):
    path = tmp_path / "synthetic.sqlite3"
    service, fake = connected(path)
    month = calendar_month("2025-02")
    assert service.saved_range(month)[1]["started"] is False
    candidates, state = load(service)
    assert candidates == {} and state["exhausted"] and state["pages"] == 1
    calls = len(fake.calls)
    load(service)
    assert len(fake.calls) == calls  # exhausted Continue is idempotent, not new loading
    fake.pages = {
        None: {"messages": [{"id": "a"}, {"id": "a"}], "nextPageToken": "second"},
        "second": {"nextPageToken": "third"},  # empty nonterminal page is NOT exhausted
        "third": {"messages": [{"id": "a"}, {"id": "b"}]},
    }
    candidates, state = load(service, action="refresh")
    assert set(candidates) == {"a"} and not state["exhausted"]
    assert state["duplicates"] == 1
    restarted, _ = connected(path, fake)
    assert set(restarted.saved_range(month)[0]) == {"a"}
    candidates, state = load(restarted)
    assert not state["exhausted"] and state["token"] == "third"
    candidates, state = load(restarted)
    assert set(candidates) == {"a", "b"} and state["exhausted"]
    assert state["duplicates"] == 2
    load(restarted, pages=3, action="refresh")
    with restarted.cache.ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM mail_versions").fetchone()[0] == 2
    other, _ = connected(path, fake, subject="account-B")
    assert other.saved_range(month)[0] == {}
    load(other, pages=3)
    assert set(other.saved_range(month)[0]) == {"a", "b"}
    assert other.namespace != restarted.namespace
    restarted.disconnect()
    assert restarted.saved_range(month) == ({}, None)
    back, _ = connected(path, fake)
    assert set(back.saved_range(month)[0]) == {"a", "b"}
    assert back.cache.ledger.load().transactions == ()  # raw mail never becomes facts


def test_failed_page_is_retried_without_skipping_successes_or_reporting_full(tmp_path):
    service, fake = connected(tmp_path / "test.sqlite3")
    fake.pages = {
        None: {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "second"},
        "second": {},
    }
    fake.failed = "b"
    candidates, state = load(service, pages=5)
    assert set(candidates) == {"a"} and state["token"] is None
    assert not state["exhausted"] and state["errors"] == ["rate_limited"]
    fake.failed = None
    candidates, state = load(service, pages=2)
    assert set(candidates) == {"a", "b"} and state["exhausted"]
    assert len([call for call in fake.calls if call == ("get", "a")]) == 1
    fake.pages = {None: ClientFailure(IngestionError("transport_failed", True))}
    candidates, state = load(service, action="refresh")
    assert set(candidates) == {"a", "b"} and not state["exhausted"]
    assert state["errors"] == ["transport_failed"]


def test_bounds_cycles_expired_tokens_and_mail_versions(tmp_path):
    service, fake = connected(tmp_path / "test.sqlite3")
    fake.pages = {
        None: {"messages": [{"id": "a"}], "nextPageToken": "second"},
        "second": {"nextPageToken": "third"},
        "third": {"nextPageToken": "second"},
    }
    _, state = load(service, pages=5)
    assert not state["exhausted"] and "pagination_cycle" in state["errors"][0]
    fake.pages = {None: {"messages": [{"id": "a"}]}}
    original = service.saved_range(calendar_month("2025-02"))[0]["a"].text
    fake.body = "Changed synthetic message"
    load(service, action="refresh")
    assert service.saved_range(calendar_month("2025-02"))[0]["a"].text == fake.body
    fake.body = original
    load(service, action="refresh")
    assert service.saved_range(calendar_month("2025-02"))[0]["a"].text == original
    with service.cache.ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM mail_versions").fetchone()[0] == 2
    for pages in (0, 6, True):
        with pytest.raises(ValueError):
            load(service, pages=pages)
    fake.pages = {
        None: {"nextPageToken": "expired"},
        "expired": ClientFailure(IngestionError("api_error")),
    }
    _, state = load(service, pages=2, action="refresh")
    assert state["token"] == "expired" and not state["exhausted"]
    fake.pages = {None: {}}
    assert load(service, action="refresh")[1]["exhausted"]


def test_five_page_action_bound_and_atomic_save_on_storage_failure(tmp_path):
    service, fake = connected(tmp_path / "test.sqlite3")
    fake.pages = {
        None: {"nextPageToken": "1"},
        **{str(i): {"nextPageToken": str(i + 1)} for i in range(1, 8)},
    }
    _, state = load(service, pages=5)
    assert state["pages"] == 5 and state["token"] == "5" and not state["exhausted"]
    assert len(fake.calls) == 5
    fake.pages = {None: {"messages": [{"id": "new"}]}}
    with service.cache.ledger.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_scan BEFORE UPDATE ON mail_scans "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    with pytest.raises(sqlite3.Error):
        load(service, action="refresh")
    assert service.saved_range(calendar_month("2025-02"))[0] == {}
    assert service.saved_range(calendar_month("2025-02"))[1]["token"] == "5"
    with service.cache.ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM mail_versions").fetchone()[0] == 0


def test_singapore_receipt_boundaries_agree_across_query_saved_mail_and_report(tmp_path):
    service, fake = connected(tmp_path / "test.sqlite3")
    service.cache.ledger.import_document(FIXTURE.monthly_document().model_dump_json())
    sgt = {
        "before": datetime(2025, 1, 31, 23, 59, 59, 999000, SINGAPORE),
        "start": datetime(2025, 2, 1, tzinfo=SINGAPORE),
        "first-eight-hours": datetime(2025, 2, 1, 7, 59, 59, tzinfo=SINGAPORE),
        "last": datetime(2025, 2, 28, 23, 59, 59, 999000, SINGAPORE),
        "march-0030": datetime(2025, 3, 1, 0, 30, tzinfo=SINGAPORE),
        "march-eighth-hour": datetime(2025, 3, 1, 7, 59, tzinfo=SINGAPORE),
    }
    assert sgt["march-0030"].astimezone(UTC).date() == date(2025, 2, 28)
    fake.pages = {None: {"messages": [{"id": key} for key in sgt]}}
    fake.receipts = {key: str(int(value.timestamp() * 1000)) for key, value in sgt.items()}
    candidates, state = load(service)
    assert set(candidates) == {"start", "first-eight-hours", "last"} and state["exhausted"]
    assert not service.saved_range(calendar_month("2025-03"))[1]["started"]
    march, _ = load(service, month="2025-03")
    assert set(march) == {"march-0030", "march-eighth-hour"}
    for key in ("2025-02", "2025-03"):
        month = calendar_month(key)
        after, before = (int(part.split(":")[1]) for part in month.query.split())
        saved = service.saved_range(month)[0]
        assert saved and all(
            after < c.gmail_internal_at.timestamp() < before for c in saved.values()
        )
    report = range_report(service.cache.ledger.load(), calendar_month("2025-03"))
    assert [row["transaction"].occurred_on.value for row in report["rows"]] == [date(2025, 3, 1)]
    client = TestClient(
        create_app(tmp_path / "test.sqlite3", gmail_service=service),
        base_url="http://127.0.0.1:8877",
    )
    assert client.get("/?month=2025-03").status_code == 200
    march = service.saved_range(calendar_month("2025-03"))[0]
    assert len(march) == 2
    assert {
        c.gmail_internal_at.astimezone(SINGAPORE).strftime("%d %b %Y %H:%M") for c in march.values()
    } >= {"01 Mar 2025 00:30"}


def test_zero_value_purchase_month_has_nonzero_chart_scale_and_agreeing_totals():
    document = FIXTURE.monthly_document()
    data = document.model_dump(mode="json")
    data["transactions"][0]["amount"]["value"]["minor_units"] = 0
    with pytest.raises(ValueError, match="must be positive"):
        SchemaDocument.model_validate(data)  # imports cannot store zero event amounts

    def zero(t):
        money = t.amount.value
        if money is None or money.currency != "SGD":
            return t
        value = money.model_copy(update={"minor_units": 0})
        return t.model_copy(update={"amount": t.amount.model_copy(update={"value": value})})

    zeroed = document.model_copy(update={"transactions": tuple(map(zero, document.transactions))})
    report = range_report(zeroed, calendar_month("2025-02"))
    sgd = next(c for c in report["charts"] if c["currency"] == "SGD")
    assert sgd["totals"]["purchase"] == 0 and sgd["groups"] and sgd["daily"]
    assert sgd["maximum_group"] == sgd["maximum_day"] == 1
    assert sum(r["minor_units"] for r in sgd["groups"]) == sgd["totals"]["purchase"]
    assert sum(r["minor_units"] for r in sgd["daily"]) == sgd["totals"]["purchase"]
    assert all(r["minor_units"] * 100 // sgd["maximum_group"] == 0 for r in sgd["groups"])
    usd = next(c for c in report["charts"] if c["currency"] == "USD")
    assert usd["maximum_group"] == usd["maximum_day"] == usd["totals"]["purchase"] == 1299
    assert range_report(zeroed, calendar_month("2024-02"))["charts"] == []


def test_persistent_message_failure_does_not_block_later_pages_and_has_targeted_retry(tmp_path):
    path = tmp_path / "test.sqlite3"
    service, fake = connected(path)
    fake.pages = {
        None: {"messages": [{"id": "a"}, {"id": "bad"}], "nextPageToken": "second"},
        "second": {"messages": [{"id": "bad"}, {"id": "c"}]},
    }
    fake.broken = {"bad"}
    candidates, state = load(service)
    assert set(candidates) == {"a"} and state["token"] == "second"
    assert state["failed"] == {"bad": "invalid_message"} and state["errors"] == []
    assert state["pages"] == 1 and not state["exhausted"]
    candidates, state = load(service)
    assert set(candidates) == {"a", "c"} and state["exhausted"]
    assert state["pages"] == 2 and state["duplicates"] == 1
    calls = len(fake.calls)
    candidates, state = load(service, action="retry")
    assert fake.calls[calls:] == [("get", "bad")]
    assert state["failed"] == {"bad": "invalid_message"}
    assert (state["pages"], state["duplicates"]) == (2, 1)
    other, _ = connected(path, fake, subject="account-B")
    assert other.saved_range(calendar_month("2025-02"))[1]["failed"] == {}

    def reject(identity):
        raise ClientFailure(IngestionError("authentication_failed"))

    fake.get_message = reject
    with pytest.raises(GmailProblem):
        load(service, action="retry")
    assert service.connection is None
    del fake.get_message
    service, _ = connected(path, fake)
    assert service.saved_range(calendar_month("2025-02"))[1]["failed"] == {"bad": "invalid_message"}
    fake.broken = set()
    candidates, state = load(service, action="retry")
    assert set(candidates) == {"a", "bad", "c"} and state["failed"] == {}
    assert (state["pages"], state["duplicates"]) == (2, 1)
    calls = len(fake.calls)
    load(service)
    assert len(fake.calls) == calls
    restarted, _ = connected(path, fake)
    assert set(restarted.saved_range(calendar_month("2025-02"))[0]) == {"a", "bad", "c"}


@pytest.mark.parametrize("retry_pages", [1, 5])
def test_bounded_failed_retries_rotate_persisted_order_without_starvation(tmp_path, retry_pages):
    path = tmp_path / "synthetic.sqlite3"
    service, fake = connected(path)
    identities = [f"bad-{i:03}" for i in range(101)]
    fake.broken = set(identities)
    fake.pages = {
        None if i == 0 else str(i): {
            "messages": [{"id": identity} for identity in identities[i : i + 20]],
            **({"nextPageToken": str(i + 20)} if i + 20 < len(identities) else {}),
        }
        for i in range(0, len(identities), 20)
    }
    load(service, pages=5)
    candidates, state = load(service)
    assert candidates == {} and state["exhausted"] and state["pages"] == 6
    assert list(state["failed"]) == identities
    other, _ = connected(path, fake, subject="account-B")
    other_before = other.saved_range(calendar_month("2025-02"))
    march_before = service.saved_range(calendar_month("2025-03"))
    fake.broken.remove(identities[-1])
    calls = len(fake.calls)
    batch = retry_pages * 20
    candidates, state = load(service, pages=retry_pages, action="retry")
    assert fake.calls[calls:] == [("get", identity) for identity in identities[:batch]]
    assert list(state["failed"]) == identities[batch:] + identities[:batch]
    assert len(state["failed"]) == 101 and state["duplicates"] == 0
    service, _ = connected(path, fake)
    assert list(service.saved_range(calendar_month("2025-02"))[1]["failed"]) == list(
        state["failed"]
    )
    for _ in range((len(identities) - 1) // batch):
        candidates, state = load(service, pages=retry_pages, action="retry")
    assert set(candidates) == {identities[-1]}
    assert len(state["failed"]) == 100 and identities[-1] not in state["failed"]
    assert state["seen"] == [identities[-1]]
    assert (state["pages"], state["duplicates"], state["exhausted"]) == (6, 0, True)
    assert fake.calls[calls:].count(("get", identities[-1])) == 1
    assert all(kind == "get" for kind, _ in fake.calls[calls:])
    assert other.saved_range(calendar_month("2025-02")) == other_before
    assert service.saved_range(calendar_month("2025-03")) == march_before
    with service.cache.ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM mail_versions").fetchone()[0] == 1


def test_retried_pages_do_not_inflate_page_or_duplicate_counts(tmp_path):
    service, fake = connected(tmp_path / "test.sqlite3")
    fake.pages = {None: {"messages": [{"id": "a"}, {"id": "a"}, {"id": "bad"}, {"id": "b"}]}}
    fake.broken, fake.failed = {"bad"}, "b"
    for _ in range(3):
        candidates, state = load(service)
        assert (state["pages"], state["duplicates"]) == (0, 0)
        assert state["errors"] == ["rate_limited"] and state["failed"] == {"bad": "invalid_message"}
    fake.failed = None
    candidates, state = load(service)
    assert set(candidates) == {"a", "b"} and state["exhausted"]
    assert (state["pages"], state["duplicates"]) == (1, 1)
    assert [call for call in fake.calls if call == ("get", "a")] == [("get", "a")]
    fake.pages = {None: ClientFailure(IngestionError("transport_failed", True))}
    _, state = load(service, action="refresh")
    assert state["failed"] == {} and state["pages"] == 0 and not state["exhausted"]
    assert set(service.saved_range(calendar_month("2025-02"))[0]) == {"a", "b"}


def test_month_projection_canonical_correction_exclusions_unknowns_and_kind_semantics():
    doc = FIXTURE.monthly_document()
    report = range_report(doc, calendar_month("2025-02"))
    assert len(report["rows"]) == 15 and len(report["undated"]) == 1
    assert report["counts"] == {"accepted": 12, "pending": 2, "rejected": 1}
    assert report["excluded"] == {"Pending / uncertain": 2, "Rejected": 1, "Explicit duplicate": 1}
    charts = {chart["currency"]: chart for chart in report["charts"]}
    assert charts["SGD"]["totals"] == {
        "purchase": 15880,
        "refund": 500,
        "reversal": 100,
        "income": 250000,
        "transfer": 9999,
    }
    assert sorted(r["minor_units"] for r in charts["SGD"]["groups"]) == [1450, 6230, 8200]
    assert [(r["label"], r["minor_units"]) for r in charts["SGD"]["daily"]] == [
        ("2025-02-01", 1280),
        ("2025-02-08", 13150),
        ("2025-02-28", 1450),
    ]
    assert charts["JPY"]["totals"]["purchase"] == 1234
    assert charts["KWD"]["totals"]["purchase"] == 1234
    assert charts["USD"]["totals"]["purchase"] == 1299
    assert any(
        "Avenue unknown" in row["label"] and "Card unassigned" in row["label"]
        for row in charts["SGD"]["groups"]
    )
    for chart in charts.values():
        assert sum(row["minor_units"] for row in chart["groups"]) == chart["totals"]["purchase"]
        assert sum(row["minor_units"] for row in chart["daily"]) == chart["totals"]["purchase"]
    assert not range_report(doc, calendar_month("2024-02"))["rows"]
    assert len(range_report(doc, calendar_month("2025-01"))["rows"]) == 1
    assert len(range_report(doc, calendar_month("2025-03"))["rows"]) == 1


def test_manual_date_correction_moves_only_the_canonical_head_between_months(tmp_path):
    service, fake = connected(tmp_path / "test.sqlite3")
    ledger = service.cache.ledger
    ledger.import_document(FIXTURE.monthly_document().model_dump_json())
    before = range_report(ledger.load(), calendar_month("2025-02"))
    ledger.review(
        "weekly-groceries-v2",
        actor="Synthetic reviewer",
        reason="Synthetic date correction",
        status="accepted",
        field="occurred_on",
        fact_json='{"status":"proposed","value":"2025-03-01"}',
    )
    after = range_report(ledger.load(), calendar_month("2025-02"))
    march = range_report(ledger.load(), calendar_month("2025-03"))

    def sgd(report):
        return next(c for c in report["charts"] if c["currency"] == "SGD")

    assert sgd(after)["totals"]["purchase"] == sgd(before)["totals"]["purchase"] - 4950
    assert sum(r["transaction"].event_id == "weekly-groceries" for r in march["rows"]) == 1
    preserved = ledger.load()
    load(service)
    assert ledger.load() == preserved and fake.calls  # loading raw mail cannot change facts


def test_unknown_account_and_conflicting_or_rejected_fact_do_not_inflate_totals():
    data = FIXTURE.monthly_document().model_dump(mode="json")
    t = data["transactions"][0]
    t["review"]["status"] = "pending"
    t["account_id"] = {"status": "unknown"}
    t.pop("card_id")
    second = data["transactions"][1]
    second["channel"] = {
        "status": "conflicting",
        "alternatives": ["card", "paynow"],
        "note": "Synthetic conflict",
    }
    data["transactions"][2]["channel"] = second["channel"]
    data["transactions"][3]["amount"]["review"] = {
        "status": "rejected",
        "reviewer": "Test",
        "reason": "No support",
        "reviewed_at": "2025-04-01T00:01:00Z",
    }
    report = range_report(SchemaDocument.model_validate(data), calendar_month("2025-02"))
    assert "Account unassigned" in report["rows"][1]["group"] or any(
        "Account unassigned" in r["group"] for r in report["rows"]
    )
    assert report["excluded"]["Unresolved / rejected fact"] == 2
    assert next(c for c in report["charts"] if c["currency"] == "SGD")["totals"]["purchase"] == 1450


@pytest.mark.parametrize(
    "currency,amount,expected",
    [
        ("SGD", 1234, "SGD 12.34"),
        ("JPY", 1234, "JPY 1,234"),
        ("KWD", 1234, "KWD 1.234"),
        ("BHD", 1, "BHD 0.001"),
        ("USD", 2**63 - 1, "USD 92,233,720,368,547,758.07"),
    ],
)
def test_exact_integer_display(currency, amount, expected):
    assert format_money(currency, amount) == expected


class TableData(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.chart = None
        self.charts = {}
        self.rows = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "figure":
            self.chart = (a["data-currency"], a["data-chart"])
            self.charts[self.chart] = 0
        if tag == "tr" and "data-event" in a:
            self.rows.append(a)
        elif tag == "tr" and "data-minor" in a and self.chart:
            self.charts[self.chart] += int(a["data-minor"])

    def handle_endtag(self, tag):
        if tag == "figure":
            self.chart = None


def test_actual_gets_are_offline_and_chart_tables_exactly_agree_with_monthly_rows(tmp_path):
    service, fake = connected(tmp_path / "test.sqlite3")
    app = create_app(tmp_path / "test.sqlite3", gmail_service=service)
    app.state.ledger.import_document(FIXTURE.monthly_document().model_dump_json())
    client = TestClient(app, base_url="http://127.0.0.1:8877")
    for path in ("/?month=2025-02", "/gmail?month=2025-02", "/ledger", "/review"):
        assert client.get(path).status_code == 200
    assert fake.calls == []
    filtered = client.get("/ledger?month=2025-02")
    assert filtered.status_code == 200 and "15 events" in filtered.text
    assert "previous-month" not in filtered.text and "next-month" not in filtered.text
    assert "paynow" in filtered.text and "SGD 49.50" in filtered.text
    assert client.get("/ledger?month=all").status_code == 422
    response = client.get("/?month=2025-02")
    data = TableData(response.text)
    assert len(data.rows) == 15
    assert "Press Refresh" in response.text
    assert "Saved ledger facts only, not a complete bank statement" in response.text
    assert "1 undated" in response.text
    for currency in ("SGD", "JPY", "KWD", "USD"):
        total = sum(
            int(r["data-minor"])
            for r in data.rows
            if r["data-included"] == "yes"
            and r["data-kind"] == "purchase"
            and r["data-currency"] == currency
        )
        assert data.charts[currency, "groups"] == data.charts[currency, "daily"] == total
    assert "0 results" in client.get("/?month=2024-02").text
    assert client.get("/?month=all").status_code == 422
    assert client.post("/gmail/month", data={"month": "2025-02", "pages": "1"}).status_code == 403
    csrf = client.cookies["ledger_csrf"]
    fields = {
        "csrf": csrf,
        "month": "2025-02",
        "pages": "1",
        "action": "continue",
        "connection_id": service.connection_id,
    }
    response = client.post("/gmail/month", data=fields)
    assert response.status_code == 200
    assert service.saved_range(calendar_month("2025-02"))[1]["exhausted"]
    assert not service.saved_range(calendar_month("2025-03"))[1]["started"]
    assert (
        client.post(
            "/gmail/month", data=fields, headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    service.clear()
    assert "account-A@example.invalid" not in client.get("/gmail?month=2025-02").text
    assert client.post("/gmail/month", data=fields).status_code == 200
    assert fake.calls == [
        (
            "list",
            {"query": "after:1738339199 before:1740758400", "page_token": None, "page_size": 20},
        )
    ]
