"""One Refresh: which days it asks Gmail for, when it may call the mail loaded, how it resumes.

Synthetic mailboxes and fake transports only. A windowed fake honours Gmail's after:/before:
query, pages newest first and can be told to fail, so interval and resume logic is exercised
the way the real search behaves.
"""

import base64
import json
import re
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from saved_view import outcomes
from test_monthly import MemoryStore

from spending_optimiser import gmail_service
from spending_optimiser.gmail import ClientFailure, IngestionError
from spending_optimiser.gmail_auth import GmailProblem, GoogleConnection
from spending_optimiser.gmail_service import GmailService
from spending_optimiser.ledger import heads
from spending_optimiser.monthly import SINGAPORE, calendar_month, date_range, split_range
from spending_optimiser.processing import REVISION
from spending_optimiser.web import create_app

BASE = "http://127.0.0.1:8877"
TODAY = date(2025, 3, 20)
PURCHASE = (
    "Type: Purchase\nAmount: SGD {amount}\nEvent date: {day}\n"
    "Account: wallet-blue\nMerchant: Fictional Cafe"
)


def ms(year, month, day, hour=9, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=SINGAPORE).timestamp() * 1000)


class WindowedMail:
    """Newest-first Gmail search over a synthetic mailbox, honouring after:/before:."""

    def __init__(self, mailbox=None, size=20):
        self.mailbox = dict(mailbox or {})  # id -> receipt in milliseconds
        self.size = size
        self.calls = []
        self.queries = []
        self.fail_get = set()  # ids whose fetch fails permanently
        self.fail_list = None  # an error raised by the next listing, then cleared
        self.bodies = {}

    def window(self, query):
        after, before = (int(n) for n in re.findall(r":(\d+)", query))
        found = [i for i, at in self.mailbox.items() if after < at // 1000 < before]
        return sorted(found, key=lambda i: (-self.mailbox[i], i))

    def list_messages(self, *, query, page_token, page_size):
        self.calls.append(("list", page_token))
        self.queries.append(query)
        if self.fail_list is not None:
            error, self.fail_list = self.fail_list, None
            raise ClientFailure(IngestionError(error, True))
        ids = self.window(query)
        start = int(page_token or 0)
        page = ids[start : start + min(page_size, self.size)]
        result = {"messages": [{"id": i} for i in page]}
        if start + len(page) < len(ids):
            result["nextPageToken"] = str(start + len(page))
        return result

    def get_message(self, identity):
        self.calls.append(("get", identity))
        raw = self.bodies.get(identity, "Synthetic notice, no supported financial facts.").encode()
        return {
            "id": identity,
            "threadId": None if identity in self.fail_get else "thread",
            "internalDate": str(self.mailbox[identity]),
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": identity}],
                "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
            },
        }

    def gets(self):
        return [call[1] for call in self.calls if call[0] == "get"]

    def windows(self):
        """Inclusive Singapore days each recorded query asked for."""
        days = []
        for query in self.queries:
            after, before = (int(n) for n in re.findall(r":(\d+)", query))
            first = datetime.fromtimestamp(after + 1, SINGAPORE).date()
            last = datetime.fromtimestamp(before - 1, SINGAPORE).date()
            days.append((first, last))
        return days


@pytest.fixture(autouse=True)
def synthetic_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Only disposable synthetic stores and transports allowed")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)


def service_for(path, fake, subject="account-A", today=TODAY):
    service = GmailService(path, store=MemoryStore(), client_factory=lambda _: fake)
    service.connection = GoogleConnection(
        subject, f"{subject}@example.invalid", Credentials("fake")
    )
    service.today = lambda: today
    return service


def refresh(service, today=None):
    service.refresh(service.connection_id, today)
    return service.cache.refresh_state(service.namespace)


from google.oauth2.credentials import Credentials  # noqa: E402


def test_a_new_account_loads_the_current_singapore_month_only(tmp_path):
    fake = WindowedMail(
        {
            "last-month": ms(2025, 2, 28, 23, 59),
            "first-second": ms(2025, 3, 1, 0, 0),
            "today": ms(2025, 3, 20, 8),
        }
    )
    service = service_for(tmp_path / "new.sqlite3", fake)
    state = refresh(service)
    assert fake.windows() == [(date(2025, 3, 1), date(2025, 3, 20))]
    assert set(fake.gets()) == {"first-second", "today"}
    assert state["outcome"] == "complete" and state["through"] == "2025-03-20"
    view = service.refresh_view()
    assert view["message"] == "All recent emails are loaded. 2 new." and not view["alert"]


def test_an_existing_account_loads_from_its_latest_saved_mail_day_through_today(tmp_path):
    path = tmp_path / "existing.sqlite3"
    fake = WindowedMail({"saved": ms(2025, 3, 5, 14), "older": ms(2025, 3, 2, 8)})
    first = service_for(path, fake, today=date(2025, 3, 6))
    refresh(first)
    # Wipe the refresh state, as if the mail was saved by an earlier version of the app.
    with first.cache.ledger.connection() as db:
        db.execute("DELETE FROM mail_refresh")
        db.execute("DELETE FROM mail_scans")
    fake.mailbox.update(
        {
            "day-before": ms(2025, 3, 4, 23, 59),
            "fresh": ms(2025, 3, 19, 10),
            "same-day": ms(2025, 3, 5, 0, 0),
        }
    )
    fake.queries.clear()
    fake.calls.clear()
    service = service_for(path, fake)
    refresh(service)
    # The latest saved mail arrived on 5 March: that whole day through today is requested.
    assert fake.windows() == [(date(2025, 3, 5), date(2025, 3, 20))]
    assert set(fake.gets()) == {"fresh", "same-day"}  # nothing saved is fetched again, 4th excluded
    assert "day-before" not in service.saved_range(date_range("2025-03-01", "2025-03-20"))[0]


def test_a_long_gap_is_covered_in_bounded_subranges_and_never_clamped(tmp_path):
    path = tmp_path / "gap.sqlite3"
    fake = WindowedMail({"early": ms(2024, 8, 14, 9), "mid": ms(2024, 11, 30, 9)})
    first = service_for(path, fake, today=date(2024, 8, 14))
    refresh(first)
    fake.mailbox.update({"late": ms(2025, 3, 18, 9)})
    fake.queries.clear()
    service = service_for(path, fake, today=date(2025, 3, 20))
    state = refresh(service)
    windows = sorted(fake.windows())
    assert windows[0][0] == date(2024, 8, 14) and windows[-1][1] == date(2025, 3, 20)
    for (_, previous_last), (next_first, _) in zip(windows, windows[1:], strict=False):
        assert (next_first - previous_last).days == 1  # no gap between subranges
    assert len(windows) == len(split_range(date(2024, 8, 14), date(2025, 3, 20))) == 3
    assert {"early", "mid", "late"} <= {
        key
        for key in service.saved_range(date_range("2024-08-14", "2024-11-13"))[0]
        | service.saved_range(date_range("2024-11-14", "2025-02-13"))[0]
        | service.saved_range(date_range("2025-02-14", "2025-03-20"))[0]
    }
    assert state["outcome"] == "complete"


def test_split_range_covers_every_day_once_with_calendar_bounded_chunks():
    chunks = split_range(date(2024, 11, 30), date(2025, 6, 1))
    assert chunks[0].start == date(2024, 11, 30) and chunks[-1].last == date(2025, 6, 1)
    assert all(a.end == b.start for a, b in zip(chunks, chunks[1:], strict=False))
    assert split_range(date(2025, 3, 20), date(2025, 3, 20))[0].key == "2025-03-20..2025-03-20"
    with pytest.raises(ValueError):
        split_range(date(2025, 3, 21), date(2025, 3, 20))


def test_zero_results_are_a_complete_refresh_not_a_failure(tmp_path):
    fake = WindowedMail()
    service = service_for(tmp_path / "zero.sqlite3", fake)
    state = refresh(service)
    assert state["outcome"] == "complete" and state["loaded"] == 0
    assert service.refresh_view()["message"] == "All recent emails are loaded. No new emails."
    assert fake.gets() == []


def test_an_interrupted_refresh_resumes_older_pages_instead_of_skipping_them(tmp_path, monkeypatch):
    path = tmp_path / "resume.sqlite3"
    mailbox = {f"m{i:02}": ms(2025, 3, 1 + i % 18, 8, i) for i in range(30)}
    fake = WindowedMail(mailbox, size=10)
    monkeypatch.setattr(gmail_service, "REFRESH_PAGES", 1)
    service = service_for(path, fake)
    state = refresh(service)
    assert state["outcome"] == "more" and state["through"] is None
    assert len(fake.gets()) == 10 and "resume_from" in state
    newest = max(mailbox, key=mailbox.get)
    assert newest in service.saved_range(calendar_month("2025-03"))[0]
    # The newest mail is saved, yet the next Refresh still starts from the original day.
    assert service.refresh_start(service.namespace, TODAY) == date(2025, 3, 1)
    assert "press Refresh" in service.refresh_view()["message"]
    restarted = service_for(path, fake)  # a new process picks up where the last one stopped
    fake.queries.clear()
    refresh(restarted)
    assert fake.windows()[0][0] == date(2025, 3, 1)
    assert [call for call in fake.calls if call[0] == "list"][1:2] == [("list", "10")]
    monkeypatch.setattr(gmail_service, "REFRESH_PAGES", 25)
    state = refresh(restarted)
    assert state["outcome"] == "complete" and state["through"] == "2025-03-20"
    assert set(restarted.saved_range(calendar_month("2025-03"))[0]) == set(mailbox)
    assert sorted(fake.gets()) == sorted(mailbox)  # every message fetched exactly once


def test_an_unloadable_message_is_reported_without_holding_back_later_mail(tmp_path):
    path = tmp_path / "failed.sqlite3"
    fake = WindowedMail({"good": ms(2025, 3, 18), "bad": ms(2025, 3, 20, 7), "old": ms(2025, 3, 3)})
    fake.fail_get = {"bad"}
    service = service_for(path, fake)
    state = refresh(service)
    assert state["outcome"] == "failed" and state["failed"] == 1
    assert state["through"] == "2025-03-20" and "resume_from" not in state
    assert set(service.saved_range(calendar_month("2025-03"))[0]) == {"good", "old"}
    view = service.refresh_view()
    assert view["alert"] and view["message"] == (
        "1 emails could not be loaded from Gmail. All other recent emails are loaded."
    )
    # The next day still covers the failed message's day: it is reported, not fetched again.
    fake.mailbox["next-day"] = ms(2025, 3, 21, 9)
    fake.calls.clear()
    fake.queries.clear()
    state = refresh(service, date(2025, 3, 21))
    assert fake.windows() == [(date(2025, 3, 20), date(2025, 3, 21))]
    assert fake.gets() == ["next-day"]
    assert state["outcome"] == "failed" and state["failed"] == 1 and state["loaded"] == 1
    assert state["through"] == "2025-03-21"
    # Once the interval has moved past it, Refresh lists only new days and completes.
    fake.mailbox["later"] = ms(2025, 3, 22, 9)
    fake.calls.clear()
    fake.queries.clear()
    service.today = lambda: date(2025, 3, 22)
    state = refresh(service)
    assert fake.windows() == [(date(2025, 3, 21), date(2025, 3, 22))]
    assert fake.gets() == ["later"] and "bad" not in [call[1] for call in fake.calls]
    assert state["outcome"] == "complete" and state["through"] == "2025-03-22"
    assert service.refresh_view()["message"] == "All recent emails are loaded. 1 new."
    # The failure record itself is kept.
    assert service.cache.failed_ids(service.namespace) == {"bad": "invalid_message"}
    assert "bad" not in service.saved_range(date_range("2025-03-01", "2025-03-22"))[0]


def test_an_interrupted_refresh_resumes_its_page_after_the_day_changes(tmp_path, monkeypatch):
    path = tmp_path / "large.sqlite3"
    mailbox = {f"m{i:02}": ms(2025, 3, 1 + i % 18, 8, i) for i in range(30)}
    fake = WindowedMail(mailbox, size=10)
    monkeypatch.setattr(gmail_service, "REFRESH_PAGES", 1)
    service = service_for(path, fake)
    assert refresh(service)["outcome"] == "more"
    fake.mailbox["new-day"] = ms(2025, 3, 21, 9)
    fake.calls.clear()
    fake.queries.clear()
    monkeypatch.setattr(gmail_service, "REFRESH_PAGES", 2)
    state = refresh(service, date(2025, 3, 21))
    # The new day is its own chunk; the unfinished chunk continues from its second page.
    assert fake.windows() == [
        (date(2025, 3, 21), date(2025, 3, 21)),
        (date(2025, 3, 1), date(2025, 3, 20)),
    ]
    assert [call for call in fake.calls if call[0] == "list"] == [("list", None), ("list", "10")]
    assert state["outcome"] == "more" and state["resume_from"] == "2025-03-01"
    fake.calls.clear()
    monkeypatch.setattr(gmail_service, "REFRESH_PAGES", 25)
    state = refresh(service, date(2025, 3, 22))
    assert ("list", "20") in fake.calls and ("list", "10") not in fake.calls
    assert state["outcome"] == "complete" and state["through"] == "2025-03-22"
    assert set(service.saved_range(date_range("2025-03-01", "2025-03-22"))[0]) == set(fake.mailbox)
    assert sorted(fake.gets()) == sorted(set(fake.gets()))  # nothing fetched twice


def test_scan_progress_does_not_copy_every_saved_message(tmp_path):
    fake = WindowedMail({f"m{i:02}": ms(2025, 3, 1 + i % 19, 8, i) for i in range(40)})
    service = service_for(tmp_path / "bounded.sqlite3", fake)
    refresh(service)
    for day in (21, 22, 23):
        fake.mailbox[f"d{day}"] = ms(2025, 3, day, 9)
        state = refresh(service, date(2025, 3, day))
        assert state["outcome"] == "complete" and state["loaded"] == 1
    with service.cache.ledger.connection() as db:
        rows = db.execute("SELECT month, payload FROM mail_scans").fetchall()
    for key, payload in rows:
        if key != "2025-03-01..2025-03-20":
            assert len(json.loads(payload)["seen"]) <= 2
    assert len(service.cache.saved_ids(service.namespace)) == 43
    assert sorted(fake.gets()) == sorted(fake.mailbox)


def test_a_gmail_error_is_actionable_and_the_next_refresh_continues(tmp_path):
    fake = WindowedMail({"a": ms(2025, 3, 3), "b": ms(2025, 3, 4)})
    service = service_for(tmp_path / "error.sqlite3", fake)
    fake.fail_list = "rate_limited"
    state = refresh(service)
    assert state["outcome"] == "error" and state["through"] is None
    view = service.refresh_view()
    assert view["alert"] and "limiting requests" in view["message"]
    state = refresh(service)
    assert state["outcome"] == "complete" and set(fake.gets()) == {"a", "b"}
    fake.fail_list = "transport_failed"
    state = refresh(service)
    assert state["outcome"] == "error"
    assert "Gmail could not be reached" in service.refresh_view()["message"]


def test_refreshing_twice_on_one_day_neither_duplicates_nor_misses_new_mail(tmp_path):
    fake = WindowedMail({"a": ms(2025, 3, 20, 8)})
    service = service_for(tmp_path / "same-day.sqlite3", fake)
    refresh(service)

    def versions():
        with service.cache.ledger.connection() as db:
            return db.execute("SELECT count(*) FROM mail_versions").fetchone()[0]

    assert versions() == 1
    state = refresh(service)
    assert state["loaded"] == 0 and versions() == 1
    assert service.refresh_view()["message"].endswith("No new emails.")
    fake.mailbox["b"] = ms(2025, 3, 20, 16)
    state = refresh(service)
    assert state["loaded"] == 1 and versions() == 2 and state["outcome"] == "complete"
    # The next day starts at the completion boundary, today, so the day is looked at again.
    fake.queries.clear()
    refresh(service, date(2025, 3, 21))
    assert fake.windows() == [(date(2025, 3, 20), date(2025, 3, 21))]
    assert service.cache.state(service.namespace, date_range("2025-03-20", "2025-03-21"))[
        "exhausted"
    ]


def test_accounts_keep_separate_boundaries_and_mail(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    fake = WindowedMail({"a-mail": ms(2025, 3, 5)})
    one = service_for(path, fake, "account-A")
    refresh(one)
    other_fake = WindowedMail({"b-mail": ms(2025, 3, 7)})
    two = service_for(path, other_fake, "account-B")
    assert two.refresh_view()["state"] == "new"
    assert two.refresh_start(two.namespace, TODAY) == date(2025, 3, 1)  # a new mailbox
    refresh(two)
    assert set(two.saved_range(calendar_month("2025-03"))[0]) == {"b-mail"}
    assert set(one.saved_range(calendar_month("2025-03"))[0]) == {"a-mail"}
    later = date(2025, 3, 25)
    assert one.refresh_start(one.namespace, later) == two.refresh_start(two.namespace, later)
    assert one.namespace != two.namespace
    with pytest.raises(GmailProblem):
        two.refresh(one.connection_id)  # another account's form
    assert other_fake.gets() == ["b-mail"]


def test_refresh_processes_all_saved_mail_not_just_one_batch(tmp_path):
    fake = WindowedMail({f"m{i:03}": ms(2025, 3, 2, 8, i % 59) for i in range(130)})
    for key in fake.mailbox:
        fake.bodies[key] = PURCHASE.format(amount=f"{int(key[1:]) + 1}.00", day="2025-03-02")
    service = service_for(tmp_path / "bulk.sqlite3", fake)
    state = refresh(service)
    assert state["outcome"] == "complete"
    assert service.cache.pending_count(service.namespace, REVISION) == 0
    assert len(heads(service.cache.ledger.load())) == 130
    assert not service.processing_error


def test_unqualified_mail_does_not_stop_the_mail_from_being_loaded(tmp_path):
    fake = WindowedMail({"note": ms(2025, 3, 4), "buy": ms(2025, 3, 5)})
    fake.bodies["buy"] = PURCHASE.format(amount="21.40", day="2025-03-05")
    service = service_for(tmp_path / "mixed.sqlite3", fake)
    state = refresh(service)
    assert state["outcome"] == "complete"
    _, results = outcomes(service, calendar_month("2025-03"))
    assert results["note"]["outcome"] == "unsupported" and results["buy"]["outcome"] == "accepted"


def test_processing_failure_is_reported_not_called_complete(tmp_path, monkeypatch):
    fake = WindowedMail({"buy": ms(2025, 3, 5)})
    fake.bodies["buy"] = PURCHASE.format(amount="21.40", day="2025-03-05")
    service = service_for(tmp_path / "processing.sqlite3", fake)
    monkeypatch.setattr(gmail_service, "process_saved", lambda cache, namespace: 1)
    state = refresh(service)
    assert state["outcome"] == "processing" and state["through"] == "2025-03-20"
    view = service.refresh_view()
    assert view["alert"] and "could not be processed" in view["message"]


def test_a_stale_completion_is_not_presented_as_current(tmp_path):
    fake = WindowedMail({"a": ms(2025, 3, 5)})
    service = service_for(tmp_path / "stale.sqlite3", fake)
    refresh(service)
    service.today = lambda: date(2025, 3, 22)
    view = service.refresh_view()
    assert view["state"] == "stale" and "Last refreshed 20 Mar 2025" in view["message"]


def test_a_final_old_chunk_is_not_listed_again(tmp_path):
    path = tmp_path / "final.sqlite3"
    fake = WindowedMail({"early": ms(2024, 12, 20), "late": ms(2025, 3, 18)})
    # A failure in the newest chunk keeps the run open, so older finished chunks are revisited.
    service = service_for(path, fake, today=date(2025, 3, 20))
    fake.fail_get = {"late"}
    refresh(service)
    fake.queries.clear()
    fake.fail_get = set()
    refresh(service)
    old = [w for w in fake.windows() if w[1] < date(2025, 3, 1)]
    assert old == []  # the finished historical chunk stays final


def test_web_refresh_reports_through_the_page_and_never_loads_on_get(tmp_path):
    path = tmp_path / "web.sqlite3"
    fake = WindowedMail({"a": ms(2025, 3, 5)})
    service = service_for(path, fake)
    client = TestClient(create_app(path, gmail_service=service), base_url=BASE)
    for page in ("/", "/gmail", "/cards", "/ledger", "/storage"):
        assert client.get(page).status_code == 200
    assert fake.calls == []
    page = client.get("/gmail").text
    assert "Press Refresh to load this month" in page and "/gmail/refresh" in page
    fields = {"csrf": client.cookies["ledger_csrf"], "connection_id": service.connection_id}
    assert client.post("/gmail/refresh", data={**fields, "csrf": "wrong"}).status_code == 403
    assert (
        client.post(
            "/gmail/refresh", data=fields, headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    stale = client.post("/gmail/refresh", data={**fields, "connection_id": "stale"})
    assert stale.status_code == 200 and fake.calls == [] and "Reconnect Gmail" in stale.text
    done = client.post("/gmail/refresh", data=fields)
    assert done.status_code == 200 and done.url.path == "/gmail"
    assert "All recent emails are loaded. 1 new." in done.text
    visible = re.sub(r"(?s)<style>.*?</style>", "", done.text)
    for removed in (
        "Inspect",
        "Continue period",
        "Load period",
        "Pages per action",
        "Retry failed",
    ):
        assert removed not in visible
    assert json.loads(client.get("/gmail/status").text)["state"] == "idle"


def test_an_old_abandoned_manual_scan_does_not_widen_the_first_refresh(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    old = {f"jan{i}": ms(2025, 1, 2 + i, 8) for i in range(6)}
    fake = WindowedMail({**old, "recent": ms(2025, 3, 12, 8)}, size=2)
    first = service_for(path, fake)
    january = calendar_month("2025-01")
    first.load_range(january, 1, first.connection_id, "continue")  # an earlier version's load
    first.load_range(calendar_month("2025-03"), 1, first.connection_id, "continue")
    assert not first.saved_range(january)[1]["exhausted"]
    with first.cache.ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM mail_refresh").fetchone()[0] == 0
    fake.mailbox["newer"] = ms(2025, 3, 19, 8)
    fake.queries.clear()
    fake.calls.clear()
    state = refresh(first)
    assert fake.windows() == [(date(2025, 3, 12), TODAY)]
    assert fake.gets() == ["newer"] and state["outcome"] == "complete"
    # The old record is preserved as it was.
    january_state = first.saved_range(january)[1]
    assert january_state["started"] and not january_state["exhausted"]


def test_mail_source_pages_keep_untrusted_mail_inert(tmp_path):
    from test_processing import setup

    hostile = "Type: Purchase\nAmount: SGD 3.50\nEvent date: 2025-02-03\nAccount: wallet-blue\n"
    hostile += "Merchant: <script>steal()</script><img src=x>"
    service, _, client = setup(tmp_path, {"one": hostile})
    service.load_range(calendar_month("2025-02"), 1, service.connection_id, "continue")
    (transaction,) = heads(service.cache.ledger.load())
    ref = transaction.amount.evidence[0]
    page = client.get(
        "/mail-source",
        params={"observation_id": ref.evidence_revision_id, "version": ref.locator.split(":")[1]},
    )
    assert page.status_code == 200
    assert "&lt;script&gt;steal()&lt;/script&gt;" in page.text and "<script>steal" not in page.text
    assert "<img src=x>" not in page.text
    assert 'href="/ledger"' in page.text and "Inspect" not in re.sub(
        r"(?s)<style>.*?</style>", "", page.text
    )


def test_a_database_from_before_this_version_gains_its_tables_without_losing_mail(tmp_path):
    path = tmp_path / "before.sqlite3"
    fake = WindowedMail({"kept": ms(2025, 3, 6, 8)})
    older = service_for(path, fake, today=date(2025, 3, 6))
    refresh(older)
    with older.cache.ledger.connection() as db:
        for table in ("mail_refresh", "card_sources", "card_choices"):
            db.execute(f"DROP TABLE {table}")
    service = service_for(path, fake)  # opening the database recreates what is missing
    assert set(service.saved_range(calendar_month("2025-03"))[0]) == {"kept"}
    assert service.cache.refresh_state(service.namespace) == {"through": None}
    assert service.cache.cards.sources(service.namespace) == {}
    state = refresh(service)  # an existing account continues from its latest saved mail day
    assert fake.windows()[-1] == (date(2025, 3, 6), TODAY) and state["outcome"] == "complete"
