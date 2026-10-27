"""Selected date ranges through Gmail loading, saved mail, the overview and storage.

Synthetic ledgers, fake Gmail transports and disposable databases only.
"""

import re
from collections import Counter
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from test_monthly import FIXTURE, TableData, connected
from test_storage import NOW, over_trigger, seed, versions

from spending_optimiser.gmail_auth import GmailProblem
from spending_optimiser.mail_cache import MailCache
from spending_optimiser.monthly import (
    SINGAPORE,
    calendar_month,
    date_range,
    range_report,
    shortcuts,
)
from spending_optimiser.web import create_app

BASE = "http://127.0.0.1:8877"


@pytest.fixture(autouse=True)
def no_network_or_native_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Only disposable synthetic stores and transports allowed")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)


def ms(*when):
    return str(int(datetime(*when, tzinfo=SINGAPORE).timestamp() * 1000))


def load(service, period, pages=1, action="continue"):
    service.load_range(period, pages, service.connection_id, action)
    return service.saved_range(period)


def web(path, service):
    client = TestClient(create_app(path, gmail_service=service), base_url=BASE)
    client.get("/imports")
    return client


def post(client, path, **data):
    token = client.cookies["ledger_csrf"]
    return client.post(path, data={"csrf": token, **data}, follow_redirects=False)


class Fields(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def find(self, tag, **attrs):
        return [a for t, a in self.tags if t == tag and attrs.items() <= a.items()]


def scans(service):
    with service.cache.ledger.connection() as db:
        return dict(db.execute("SELECT month, payload FROM mail_scans"))


# Fictional messages received around the edges of 10 February to 14 March 2025.
EDGES = {
    "day-before": ms(2025, 2, 9, 23, 59, 59),
    "first-second": ms(2025, 2, 10),
    "last-second": ms(2025, 3, 14, 23, 59, 59),
    "day-after": ms(2025, 3, 15),
}


def test_loading_a_range_asks_gmail_for_those_days_and_keeps_only_mail_received_inside(tmp_path):
    service, fake = connected(tmp_path / "ranges.sqlite3")
    period = date_range("2025-02-10", "2025-03-14")
    fake.pages = {None: {"messages": [{"id": name} for name in EDGES]}}
    fake.receipts = EDGES
    candidates, state = load(service, period)
    assert set(candidates) == {"first-second", "last-second"} and state["exhausted"]
    assert fake.calls[0] == ("list", {"query": period.query, "page_token": None, "page_size": 20})
    assert period.query != calendar_month("2025-02").query
    # Stored mail is visible through any period that contains it, scanned or not.
    assert set(service.saved_range(date_range("2025-03-01", "2025-03-31"))[0]) == {
        "last-second",
        "day-after",
    }
    assert set(service.saved_range(date_range("2025-02-10", "2025-02-10"))[0]) == {"first-second"}
    candidates, state = service.saved_range(date_range("2025-03-15", "2025-03-15"))
    assert set(candidates) == {"day-after"} and not state["started"]


def test_coverage_belongs_to_the_exact_period_that_was_loaded(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    service, fake = connected(path)
    fake.pages = {None: {"messages": [{"id": "early"}, {"id": "late"}]}}
    fake.receipts = {"early": ms(2025, 2, 5, 9), "late": ms(2025, 2, 25, 9)}
    february = calendar_month("2025-02")
    load(service, february)
    assert service.saved_range(february)[1]["exhausted"]
    first_ten = date_range("2025-02-01", "2025-02-10")
    candidates, state = service.saved_range(first_ten)
    assert set(candidates) == {"early"}  # saved mail is not hidden
    assert not state["started"] and not state["exhausted"]  # but nothing claims it was scanned
    client = web(path, service)
    assert "Not loaded" in client.get("/gmail?start=2025-02-01&end=2025-02-10").text
    saved = client.get("/saved?start=2025-02-01&end=2025-02-10").text
    assert "Period incomplete or not loaded" in saved and "Saved candidates: 1" in saved
    assert "Query exhausted" in client.get("/gmail?month=2025-02").text
    around = client.get("/?start=2025-01-15&end=2025-03-14").text
    assert "Gmail retrieval for this period is incomplete or not started" in around
    # Two exhausted neighbouring months still do not make a range across them loaded.
    load(service, calendar_month("2025-01"))
    spanning = date_range("2025-01-15", "2025-02-14")
    assert not service.saved_range(spanning)[1]["started"]
    load(service, first_ten)
    assert set(scans(service)) == {"2025-02", "2025-01", "2025-02-01..2025-02-10"}
    assert service.saved_range(february)[1]["pages"] == 1


def test_continuation_retry_and_refresh_are_scoped_to_account_and_period(tmp_path):
    path = tmp_path / "scoped.sqlite3"
    service, fake = connected(path)
    period = date_range("2025-02-10", "2025-03-14")
    fake.pages = {
        None: {"messages": [{"id": "a"}, {"id": "bad"}], "nextPageToken": "second"},
        "second": {"messages": [{"id": "b"}]},
    }
    fake.receipts = {"a": ms(2025, 2, 11), "b": ms(2025, 3, 1), "bad": ms(2025, 2, 12)}
    fake.broken = {"bad"}
    _, state = load(service, period)
    assert state["token"] == "second" and state["failed"] == {"bad": "invalid_message"}
    assert not service.saved_range(calendar_month("2025-02"))[1]["started"]
    restarted, _ = connected(path, fake)
    assert restarted.saved_range(period)[1]["token"] == "second"
    candidates, state = load(restarted, period)
    assert set(candidates) == {"a", "b"} and state["exhausted"] and state["pages"] == 2
    assert [call[1]["page_token"] for call in fake.calls if call[0] == "list"] == [None, "second"]
    assert {call[1]["query"] for call in fake.calls if call[0] == "list"} == {period.query}
    other, _ = connected(path, fake, subject="account-B")
    candidates, state = other.saved_range(period)
    assert candidates == {} and not state["started"]
    calls = len(fake.calls)
    with pytest.raises(GmailProblem):
        other.load_range(period, 1, restarted.connection_id, "continue")  # another account's form
    assert len(fake.calls) == calls
    fake.broken = set()
    _, state = load(restarted, period, action="retry")
    assert state["failed"] == {} and "bad" in restarted.saved_range(period)[0]
    assert fake.calls[calls:] == [("get", "bad")]
    _, state = load(restarted, period, pages=2, action="refresh")
    assert state["exhausted"] and state["pages"] == 2 and state["duplicates"] == 0
    assert not restarted.saved_range(calendar_month("2025-02"))[1]["started"]
    assert set(scans(restarted)) == {"2025-02-10..2025-03-14"}


def test_a_period_and_its_equal_calendar_month_share_one_stored_scan(tmp_path):
    path = tmp_path / "shared.sqlite3"
    service, fake = connected(path)
    fake.pages = {None: {"messages": [{"id": "a"}]}}
    load(service, calendar_month("2025-02"))
    client = web(path, service)
    for query in ("month=2025-02", "start=2025-02-01&end=2025-02-28"):
        page = client.get(f"/gmail?{query}").text
        assert "Query exhausted" in page and "February 2025" in page
    fields = {"connection_id": service.connection_id, "pages": "1", "action": "continue"}
    response = post(client, "/gmail/month", start="2025-02-01", end="2025-02-28", **fields)
    assert response.status_code == 303 and response.headers["location"] == "/gmail?month=2025-02"
    assert [call for call in fake.calls if call[0] == "list"] == [fake.calls[0]]  # no new loading
    assert set(scans(service)) == {"2025-02"}


def test_historical_mail_without_month_metadata_is_found_by_any_period_that_holds_it(tmp_path):
    service, fake = connected(tmp_path / "legacy.sqlite3")
    fake.pages = {None: {"messages": [{"id": "old"}]}}
    fake.receipts = {"old": ms(2025, 2, 14, 8)}
    load(service, calendar_month("2025-02"))
    with service.cache.ledger.connection() as db:
        db.execute("UPDATE mail_versions SET receipt_month=NULL")
    cache = MailCache(service.cache.ledger.path)
    wanted = [date_range("2025-02-14", "2025-02-14"), date_range("2025-01-31", "2025-04-29")]
    for period in wanted:
        assert set(cache.candidates(service.namespace, period)) == {"old"}
    assert cache.candidates(service.namespace, date_range("2025-02-15", "2025-02-20")) == {}


@pytest.mark.parametrize(
    "query",
    [
        "start=2025-02-10&end=2025-02-09",
        "start=2025-01-01&end=2025-04-01",
        "start=2025-02-30&end=2025-03-01",
        "start=2025-02-10",
        "end=2025-02-10",
        "month=2025-02&start=2025-02-01&end=2025-02-02",
        "start=&end=",
    ],
)
@pytest.mark.parametrize("path", ["/", "/gmail", "/saved", "/ledger", "/storage"])
def test_invalid_reversed_or_over_limit_views_are_rejected_by_the_server(tmp_path, path, query):
    service, fake = connected(tmp_path / "invalid.sqlite3")
    client = web(tmp_path / "invalid.sqlite3", service)
    assert client.get(f"{path}?{query}").status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(
    "fields",
    [
        {"start": "2025-02-10", "end": "2025-02-09"},
        {"start": "2025-01-01", "end": "2025-04-01"},
        {"start": "2025-02-10"},
        {"month": "2025-02", "start": "2025-02-01", "end": "2025-02-02"},
        {"month": "2025-02-01"},
        {},
    ],
)
@pytest.mark.parametrize("action", ["/gmail/month", "/storage/restore"])
def test_invalid_posted_periods_are_rejected_before_any_mail_or_storage_work(
    tmp_path, action, fields
):
    service, fake = connected(tmp_path / "posted.sqlite3")
    client = web(tmp_path / "posted.sqlite3", service)
    extra = {"pages": "1", "action": "continue"} if action == "/gmail/month" else {}
    response = post(client, action, connection_id=service.connection_id, **extra, **fields)
    assert response.status_code == 422
    assert fake.calls == [] and scans(service) == {}


def test_a_valid_range_posts_loads_and_redirects_to_the_same_range(tmp_path):
    service, fake = connected(tmp_path / "posted.sqlite3")
    fake.pages = {None: {"messages": [{"id": "a"}]}}
    fake.receipts = {"a": ms(2025, 3, 2)}
    client = web(tmp_path / "posted.sqlite3", service)
    period = date_range("2025-02-20", "2025-03-05")
    fields = {"connection_id": service.connection_id, "pages": "1", "action": "continue"}
    response = post(client, "/gmail/month", start="2025-02-20", end="2025-03-05", **fields)
    assert response.status_code == 303
    assert parse_qs(urlparse(response.headers["location"]).query) == {
        "start": ["2025-02-20"],
        "end": ["2025-03-05"],
    }
    assert fake.calls[0][1]["query"] == period.query
    page = client.get(response.headers["location"]).text
    assert "20 Feb - 5 Mar 2025" in page and "Query exhausted" in page
    form = Fields(page).find("form", action="/gmail/month")
    assert form
    hidden = {a["name"]: a["value"] for a in Fields(page).find("input", type="hidden")}
    assert (hidden["start"], hidden["end"]) == (
        "2025-02-20",
        "2025-03-05",
    ) and "month" not in hidden
    # A stale form from before an account switch loads nothing.
    calls = len(fake.calls)
    post(
        client,
        "/gmail/month",
        **{**fields, "connection_id": "stale"},
        start="2025-02-20",
        end="2025-03-05",
    )
    assert len(fake.calls) == calls
    assert not Fields(client.get("/gmail?month=2025-02").text).find(
        "input", name="start", type="hidden"
    )


def test_page_views_of_any_period_read_saved_data_only(tmp_path, monkeypatch):
    service, fake, storage = seed(tmp_path)
    monkeypatch.setattr("spending_optimiser.storage.GB", 1)  # compression would be due
    before = (versions(service), scans(service), storage.ledger.load())
    calls = len(fake.calls)
    client = web(storage.ledger.path, service)
    for query in (
        "month=2025-02",
        "start=2025-01-31&end=2025-03-01",
        "start=2025-02-14&end=2025-02-14",
    ):
        for path in ("/", "/gmail", "/saved", "/ledger", "/storage"):
            assert client.get(f"{path}?{query}").status_code == 200
    assert (versions(service), scans(service), storage.ledger.load()) == before
    assert len(fake.calls) == calls


SPAN = date_range("2025-01-31", "2025-03-01")


def kinds(report):
    return {
        (chart["currency"], kind): amount
        for chart in report["charts"]
        for kind, amount in chart["totals"].items()
    }


def test_range_report_uses_the_same_canonical_records_as_each_day_in_it():
    document = FIXTURE.monthly_document()
    report = range_report(document, SPAN)
    assert len(report["rows"]) == 17 and len(report["undated"]) == 1
    assert report["counts"] == {"accepted": 14, "pending": 2, "rejected": 1}
    assert report["excluded"] == {"Pending / uncertain": 2, "Rejected": 1, "Explicit duplicate": 1}
    sgd = next(c for c in report["charts"] if c["currency"] == "SGD")
    assert sgd["totals"]["purchase"] == 15880 + 2 * 900000
    assert [r["label"] for r in sgd["daily"]][0] == "2025-01-31"
    assert [r["label"] for r in sgd["daily"]][-1] == "2025-03-01"
    assert sum(r["minor_units"] for r in sgd["groups"]) == sgd["totals"]["purchase"]
    assert sum(r["minor_units"] for r in sgd["daily"]) == sgd["totals"]["purchase"]
    for currency in ("JPY", "KWD", "USD"):  # separate currencies are never combined
        chart = next(c for c in report["charts"] if c["currency"] == currency)
        assert chart["totals"]["purchase"] == {"JPY": 1234, "KWD": 1234, "USD": 1299}[currency]
    days = Counter()
    count = 0
    day = SPAN.start
    while day < SPAN.end:
        daily = range_report(document, date_range(day.isoformat(), day.isoformat()))
        count += len(daily["rows"])
        days.update(kinds(daily))
        day += timedelta(days=1)
    assert count == len(report["rows"])
    assert days == Counter(kinds(report))
    quarter = range_report(document, date_range("2025-01-01", "2025-03-31"))
    assert [r["transaction"].event_id for r in quarter["rows"]].count("orchard-coffee") == 1
    assert len(quarter["rows"]) == len(report["rows"])


def test_single_day_and_empty_ranges_have_exact_results_and_empty_states():
    document = FIXTURE.monthly_document()
    one_day = range_report(document, date_range("2025-02-08", "2025-02-08"))
    assert {r["transaction"].event_id for r in one_day["rows"]} == {
        "weekly-groceries",
        "dinner-paynow",
    }
    sgd = one_day["charts"][0]
    assert (sgd["currency"], sgd["totals"]["purchase"]) == ("SGD", 4950 + 8200)
    assert [(r["label"], r["minor_units"]) for r in sgd["daily"]] == [("2025-02-08", 13150)]
    empty = range_report(document, date_range("2025-06-01", "2025-06-03"))
    assert empty["rows"] == [] and empty["charts"] == [] and len(empty["undated"]) == 1


def test_overview_and_transactions_show_the_selected_range_with_exact_tables(tmp_path):
    service, fake = connected(tmp_path / "overview.sqlite3")
    client = web(tmp_path / "overview.sqlite3", service)
    service.cache.ledger.import_document(FIXTURE.monthly_document().model_dump_json())
    response = client.get("/?start=2025-01-31&end=2025-03-01")
    text = response.text
    assert response.status_code == 200 and "<h1>31 Jan - 1 Mar 2025</h1>" in text
    data = TableData(text)
    assert len(data.rows) == 17
    for currency in ("SGD", "JPY", "KWD", "USD"):
        total = sum(
            int(r["data-minor"])
            for r in data.rows
            if r["data-included"] == "yes"
            and r["data-kind"] == "purchase"
            and r["data-currency"] == currency
        )
        assert data.charts[currency, "groups"] == data.charts[currency, "daily"] == total
    assert "17 events" in client.get("/ledger?start=2025-01-31&end=2025-03-01").text
    assert "15 events" in client.get("/ledger?month=2025-02").text
    assert "17 events" not in client.get("/ledger?start=2025-02-01&end=2025-02-28").text
    assert "0 results" in client.get("/?start=2025-06-01&end=2025-06-03").text
    layout = Fields(text)
    hrefs = {a["href"] for t, a in layout.tags if t == "a" and "href" in a}
    assert {
        "/gmail?start=2025-01-31&end=2025-03-01",
        "/saved?start=2025-01-31&end=2025-03-01",
    } <= hrefs
    assert "Understand this month" in text  # the step name itself is unchanged
    assert fake.calls == []


def test_the_picker_offers_dates_up_to_three_months_and_a_current_month_shortcut(tmp_path):
    service, _ = connected(tmp_path / "picker.sqlite3")
    client = web(tmp_path / "picker.sqlite3", service)
    page = Fields(client.get("/gmail?start=2025-01-31&end=2025-03-01").text)
    (start,) = page.find("input", name="start", type="date")
    (end,) = page.find("input", name="end", type="date")
    assert (start["value"], end["value"]) == ("2025-01-31", "2025-03-01")
    assert end["min"] == "2025-01-31" and end["max"] == "2025-04-29" and "required" in end
    assert start["min"] == "1970-01-01" and start["max"] == "9998-12-31"
    links = {a["href"] for t, a in page.tags if t == "a" and "/gmail?" in a.get("href", "")}
    for _, preset in shortcuts():
        assert f"/gmail?{'&'.join(f'{k}={v}' for k, v in preset.params.items())}" in links
    marked = Fields(client.get("/gmail?month=" + shortcuts()[0][1].key).text)
    current = [a["href"] for t, a in marked.tags if t == "a" and a.get("aria-current") == "true"]
    assert current == ["/gmail?month=" + shortcuts()[0][1].key]
    unfiltered = Fields(client.get("/ledger").text)
    assert not [a for t, a in unfiltered.tags if t == "a" and a.get("aria-current") == "true"]
    assert "All saved records" in client.get("/ledger?month=2025-02").text


def test_storage_policy_ignores_the_selected_period_and_compresses_automatically(
    tmp_path, monkeypatch
):
    service, fake, storage = seed(tmp_path)
    client = web(storage.ledger.path, service)
    hot = re.compile(r"Hot months start at <strong>([^<]+)</strong>")
    default = hot.search(client.get("/storage").text)[1]
    assert default == storage.status()["hot_start"]
    for query in (
        "month=2020-01",
        "start=2025-02-01&end=2025-04-30",
        "start=2031-01-01&end=2031-01-01",
    ):
        assert hot.search(client.get(f"/storage?{query}").text)[1] == default
    # Nothing is compressed until a write, and then no manual action is needed.
    monkeypatch.setattr("spending_optimiser.storage.GB", 1)
    assert storage.status()["mail_archived"] == 0
    fake.pages = {None: {"messages": [{"id": "old"}, {"id": "newer"}]}}
    fake.receipts = {"newer": ms(2025, 2, 6)}  # "old" keeps the seeded 1 February receipt
    response = post(
        client,
        "/gmail/month",
        connection_id=service.connection_id,
        pages="1",
        action="refresh",
        start="2025-02-01",
        end="2025-03-10",
    )
    assert response.status_code == 303
    assert storage.status()["mail_archived"] == 2
    page = client.get("/saved?start=2025-02-01&end=2025-03-10").text
    assert "2 older mail versions are compressed" in page and "Saved candidates: 2" in page
    stored = service.saved_range(date_range("2025-02-01", "2025-03-10"))[0]
    assert {c.text for c in stored.values()} == {fake.body.strip()}  # readable while cold
    ledger = storage.ledger.load()
    restored = post(
        client,
        "/storage/restore",
        connection_id=service.connection_id,
        start="2025-02-06",
        end="2025-02-06",
    )
    assert restored.status_code == 303
    assert restored.headers["location"].endswith("start=2025-02-06&end=2025-02-06")
    assert storage.status()["mail_archived"] == 1
    assert storage.ledger.load() == ledger
    assert set(service.saved_range(date_range("2025-02-01", "2025-03-10"))[0]) == {"old", "newer"}


def test_restoring_a_period_restores_only_versions_received_inside_it(tmp_path):
    service, fake, storage = seed(tmp_path)  # "old", received 1 February, is already saved
    fake.body = "Synthetic evidence " * 1000
    fake.pages = {None: {"messages": [{"id": "jan"}, {"id": "feb"}, {"id": "march"}]}}
    fake.receipts = {"jan": ms(2025, 1, 20), "feb": ms(2025, 2, 20), "march": ms(2025, 3, 20)}
    load(service, date_range("2025-01-20", "2025-03-20"), action="refresh")
    over_trigger(storage)
    # March is a hot month on 1 May 2025, so only January and February detail is compressed.
    assert storage.compress_if_needed(now=NOW)["compressed"] == 3
    namespace = service.namespace
    middle = date_range("2025-02-10", "2025-02-25")
    assert storage.mail_archived(namespace, middle) == 1
    assert storage.mail_archived(namespace, date_range("2025-02-21", "2025-02-28")) == 0
    assert storage.restore_mail(namespace, date_range("2025-02-21", "2025-02-28")) == 0
    assert storage.restore_mail(namespace, middle) == 1
    cold = {identity for _, identity, _, payload in versions(service) if isinstance(payload, bytes)}
    assert cold == {"old", "jan"}
    assert storage.restore_mail(namespace, date_range("2025-01-01", "2025-03-31")) == 2
    assert not [row for row in versions(service) if isinstance(row[3], bytes)]
    with pytest.raises(ValueError):
        storage.restore_mail(None, middle)


def test_synthetic_preview_answers_each_period_query_from_its_own_time_window(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "examples"))
    from monthly_preview import monthly_app

    service = monthly_app(tmp_path, "empty").state.gmail
    candidates, state = load(service, date_range("2025-01-31", "2025-03-01"), pages=5)
    assert state["exhausted"] and state["pages"] == 4 and not state["failed"]
    assert set(candidates) == {"range-jan-31", "range-mar-01", *(f"preview-{i}" for i in range(5))}
    assert set(load(service, date_range("2025-04-10", "2025-04-10"))[0]) == {"range-apr-10"}
    candidates, state = load(service, date_range("2025-05-01", "2025-05-02"))
    assert candidates == {} and state["exhausted"]
    assert not service.saved_range(calendar_month("2025-02"))[1]["started"]
