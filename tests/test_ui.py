"""Rendered application contracts and real form journeys with synthetic dependencies."""

import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from spending_optimiser.monthly import calendar_month
from spending_optimiser.storage import Storage
from spending_optimiser.web import create_app

# Load the standalone, documented preview entry point without a production dependency.
SPEC = importlib.util.spec_from_file_location(
    "ui_preview", Path(__file__).parents[1] / "examples" / "ui_preview.py"
)
PREVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREVIEW)


class Page(HTMLParser):
    """Parse the rendered HTML interface, not implementation source."""

    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def attrs(self, tag, **attrs):
        return [
            a
            for t, a in self.elements
            if t == tag and all(k in a and a[k] == v for k, v in attrs.items())
        ]


def post(client, path, **data):
    client.get("/")
    return client.post(path, data={"csrf": client.cookies["ledger_csrf"], **data})


MONTH = calendar_month("2025-02")


def load(client, app, action="continue"):
    return post(
        client,
        "/gmail/month",
        month=MONTH.key,
        pages="1",
        action=action,
        connection_id=app.state.gmail.connection_id,
    )


def connect(client):
    response = post(client, "/gmail/connect")
    url = Page(response.text).attrs("a", rel="noreferrer")[0]["href"]
    state = parse_qs(urlparse(url).query)["state"][0]
    return client.get("/gmail/callback", params={"state": state, "code": "synthetic"})


def test_first_run_navigation_is_useful_offline_and_never_reads_credentials(tmp_path):
    client = TestClient(create_app(tmp_path / "offline.sqlite3"), base_url="http://127.0.0.1:8876")
    response = client.get("/")
    page = Page(response.text)
    assert response.status_code == 200
    assert page.attrs("a", href="/gmail")
    assert page.attrs("a", href="/", **{"aria-current": "page"})
    assert page.attrs("main", id="main", tabindex="-1")
    assert page.attrs("form") == [{"class": "month-picker", "action": "/", "method": "get"}]
    # The only primary form reads saved month data; imports stay secondary.
    for path in ("/ledger", "/review", "/gmail", "/imports", "/public"):
        response = client.get(path)
        page = Page(response.text)
        assert response.status_code == 200
        assert len(page.attrs("h1")) == 1
        assert page.attrs("a", href=path, **{"aria-current": "page"})
        assert not any(page.attrs(tag) for tag in ("iframe", "img", "link"))
        # Only the sign-in page carries the one same-origin script; no inline script anywhere.
        expected = [{"src": "/static/provider-connect.js"}] if path == "/gmail" else []
        assert page.attrs("script") == expected
        policy = response.headers["content-security-policy"]
        assert "default-src 'none'" in policy and "script-src 'self'" in policy
        assert "unsafe-inline" not in policy.split("style-src")[0]
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert "0 results" in client.get("/ledger").text
    imports = Page(client.get("/imports").text)
    assert {f["action"] for f in imports.attrs("form")} == {"/import", "/example"}


def test_missing_google_client_is_reported_honestly_and_preserves_offline_use(tmp_path):
    app = PREVIEW.preview_app(tmp_path, configured=False)
    client = TestClient(app, base_url="http://127.0.0.1:8876")
    response = post(client, "/gmail/connect")
    page = Page(response.text)
    assert response.status_code == 200
    assert page.attrs("div", role="alert")
    assert "Google client not configured" in response.text
    assert "SPENDING_GOOGLE_CLIENT_ID" in response.text
    assert "SPENDING_GOOGLE_CLIENT_SECRET" in response.text
    assert not page.attrs("input", type="password")
    assert not app.state.gmail.pending
    assert "11 new records" in post(client, "/example").text
    assert len(app.state.ledger.load().transactions) == 1


def test_mail_journey_readable_headers_limits_and_no_ledger_side_effects(tmp_path):
    app = PREVIEW.preview_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8876")
    response = connect(client)
    assert "alex.preview@example.invalid" in response.text
    assert app.state.gmail.saved_month(MONTH)[0] == {}
    load(client, app)
    response = client.get("/saved?month=2025-02")
    page = Page(response.text)
    assert len(page.attrs("details", **{"class": "mail-item"})) == 4
    assert (
        "Unverified sender: Orchard Preview &lt;notifications@example.invalid&gt;" in response.text
    )
    assert "01 Feb 2025" in response.text
    assert "Display truncated to 10,000 of" in response.text
    assert not page.attrs("img") and not page.attrs("script")
    assert page.attrs("input", name="month", value="2025-02")
    assert not app.state.ledger.load().transactions
    assert "4 saved candidates" in load(client, app, action="refresh").text
    assert "alex.preview@example.invalid" in client.get("/").text
    response = post(client, "/gmail/disconnect")
    assert "Gmail disconnected" in response.text
    assert "alex.preview@example.invalid" not in response.text
    assert app.state.gmail.saved_month(MONTH) == ({}, None)
    assert "No saved connection" in post(client, "/gmail/restore").text


def test_redesigned_ledger_review_history_and_duplicate_safe_import(tmp_path):
    app = PREVIEW.preview_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8876")
    assert "11 new records" in post(client, "/example").text
    assert "1 need review" in client.get("/ledger").text
    page = Page(client.get("/ledger").text)
    event_url = next(a["href"] for a in page.attrs("a") if a["href"].startswith("/transaction?"))
    response = client.get(event_url)
    page = Page(response.text)
    assert {a["href"] for a in page.attrs("a")}.issuperset(
        {"#facts", "#source-evidence", "#review-event", "#history"}
    )
    controls = page.attrs("input") + page.attrs("select") + page.attrs("textarea")
    labels = {a["for"] for a in page.attrs("label")}
    assert all(a.get("id") in labels for a in controls if a.get("type") != "hidden")
    response = post(
        client,
        "/review",
        revision_id="test-purchase-v1",
        actor="Preview reviewer",
        reason="Checked synthetic evidence",
        status="accepted",
        field="merchant",
        fact_json='{"status":"proposed","value":"Synthetic cafe"}',
    )
    assert response.status_code == 200
    assert "2 revisions preserved" in response.text
    assert "Preview reviewer" in response.text
    assert "0 results" in client.get("/review").text
    assert "Synthetic cafe" in client.get("/ledger").text
    assert "0 new records" in post(client, "/example").text
    restarted = TestClient(
        create_app(tmp_path / "synthetic.sqlite3"), base_url="http://127.0.0.1:8876"
    )
    assert "Synthetic cafe" in restarted.get(event_url).text
    response = client.get("/transaction?event_id=missing")
    assert response.status_code == 404
    assert Page(response.text).attrs("div", role="alert")


def test_mail_sender_is_decoded_unambiguous_and_marked_unverified(tmp_path, monkeypatch):
    original = PREVIEW.PreviewTransport.get_json

    def get_json(self, url, headers):
        message = original(self, url, headers)
        if message.get("id") == "preview-1":
            message["payload"]["headers"].append(
                {"name": "From", "value": "Other claim <x@evil.example.invalid>"}
            )
        if message.get("id") == "preview-2":
            for header in message["payload"]["headers"]:
                if header["name"] == "From":
                    header["value"] = "=?UTF-8?B?QmFuayDinJM=?= <n@bank.example.invalid>"
        return message

    monkeypatch.setattr(PREVIEW.PreviewTransport, "get_json", get_json)
    app = PREVIEW.preview_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8876")
    connect(client)
    load(client, app)
    response = client.get("/saved?month=2025-02")
    assert "Unverified sender: Unknown/ambiguous" in response.text
    assert "notifications@example.invalid&gt;Other claim" not in response.text
    assert "ambiguous_sender" in response.text
    assert "Unverified sender: Bank \u2713 &lt;n@bank.example.invalid&gt;" in response.text


def test_rendered_errors_preserve_exception_headers(tmp_path):
    client = TestClient(create_app(tmp_path / "offline.sqlite3"), base_url="http://127.0.0.1:8876")
    response = client.get("/gmail/month")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert Page(response.text).attrs("div", role="alert")


@pytest.mark.parametrize("operation", ["paste", "upload", "example", "review"])
def test_write_maintenance_keeps_the_event_loop_responsive(tmp_path, monkeypatch, operation):
    app = PREVIEW.preview_app(tmp_path)
    text = (Path(__file__).parent / "fixtures/synthetic.json").read_text()
    if operation == "review":
        app.state.ledger.import_document(text)
    entered = threading.Event()
    release = threading.Event()
    workers = []
    compress = Storage.compress_if_needed

    def blocked(self, **kwargs):
        workers.append(threading.get_ident())
        entered.set()
        assert release.wait(10), "Synthetic maintenance was not released"
        return compress(self, **kwargs)

    monkeypatch.setattr(Storage, "compress_if_needed", blocked)
    monkeypatch.setattr("spending_optimiser.storage.GB", 1)
    with TestClient(app, base_url="http://127.0.0.1:8876") as client:
        client.get("/imports")
        loop_thread = client.portal.call(threading.get_ident)
        fields = {"csrf": client.cookies["ledger_csrf"]}
        kwargs = {}
        if operation in {"paste", "upload"}:
            path = "/import"
            if operation == "paste":
                fields["document"] = text
            else:
                kwargs["files"] = {"file": ("synthetic.json", text, "application/json")}
        elif operation == "example":
            path = "/example"
        else:
            path = "/review"
            fields.update(
                revision_id="test-purchase-v1",
                actor="Synthetic",
                reason="Checked",
                status="accepted",
                field="merchant",
                fact_json='{"status":"proposed","value":"Synthetic cafe"}',
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            mutation = pool.submit(client.post, path, data=fields, **kwargs)
            try:
                assert entered.wait(5), "Write did not reach synthetic maintenance"
                assert len(workers) == 1 and workers[0] != loop_thread
                probe = pool.submit(client.get, "/imports")
                assert probe.result(timeout=5).status_code == 200
                assert not mutation.done()
            finally:
                release.set()
            response = mutation.result(timeout=5)
        assert response.status_code == 200
        assert len(app.state.ledger.load().transactions) == (2 if operation == "review" else 1)


@pytest.mark.parametrize("mode", ["partial", "full"])
def test_monthly_preview_uses_singapore_query_and_preserves_pagination(tmp_path, monkeypatch, mode):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "examples"))
    from monthly_preview import monthly_app

    app = monthly_app(tmp_path, mode)
    service = app.state.gmail
    candidates, state = service.saved_month(MONTH)
    expected = 2 if mode == "partial" else 5
    assert set(candidates) == {f"preview-{i}" for i in range(expected)}
    assert all(
        candidate.text and MONTH.contains(candidate.gmail_internal_at)
        for candidate in candidates.values()
    )
    assert state["pages"] == (1 if mode == "partial" else 3)
    assert state["token"] == ("second" if mode == "partial" else None)
    assert state["exhausted"] == (mode == "full")
    assert state["duplicates"] == (0 if mode == "partial" else 1)
    client = TestClient(app, base_url="http://127.0.0.1:8877")
    client.get("/gmail?month=2025-02")
    response = client.post(
        "/gmail/month",
        data={
            "csrf": client.cookies["ledger_csrf"],
            "month": MONTH.key,
            "pages": "2",
            "connection_id": service.connection_id,
            "action": "continue",
        },
    )
    assert response.status_code == 200 and "5 saved candidates" in response.text
    candidates, state = service.saved_month(MONTH)
    assert set(candidates) == {f"preview-{i}" for i in range(5)}
    assert state["exhausted"] and state["pages"] == 3 and state["duplicates"] == 1
    assert state["token"] is None and state["failed"] == {}
