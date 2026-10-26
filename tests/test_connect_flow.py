"""Provider sign-in windows: initiation, completion, cancel, error and fallbacks (synthetic)."""

import importlib.util
import threading
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from spending_optimiser.ai_service import AIService
from spending_optimiser.codex_client import AIProblem, CodexClient
from spending_optimiser.gmail_auth import GmailProblem
from spending_optimiser.gmail_service import GmailService
from spending_optimiser.ledger import Ledger
from spending_optimiser.monthly import calendar_month
from spending_optimiser.web import create_app

SPEC = importlib.util.spec_from_file_location(
    "connect_ui_preview", Path(__file__).parents[1] / "examples" / "ui_preview.py"
)
PREVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREVIEW)
BASE = "http://127.0.0.1:8876"
MONTH = calendar_month("2025-02")


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements, self.text = [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)

    def attrs(self, tag, **attrs):
        return [
            a
            for t, a in self.elements
            if t == tag and all(k in a and a[k] == v for k, v in attrs.items())
        ]


def post(client, path, **data):
    client.get("/gmail")
    return client.post(path, data={"csrf": client.cookies["ledger_csrf"], **data})


def status(client, provider):
    response = client.get(f"/{provider}/status")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


def provider_link(response):
    link = Page(response.text).attrs("a", rel="noreferrer")[0]["href"]
    refresh = Page(response.text).attrs("meta", **{"http-equiv": "refresh"})[0]["content"]
    assert refresh == f"0;url={link}"  # the automatic hop and the manual link agree
    return link


def approve_gmail(client, link, decision="code"):
    state = parse_qs(urlparse(link).query)["state"][0]
    params = {"state": state, decision: "synthetic" if decision == "code" else "access_denied"}
    return client.get("/gmail/callback", params=params, follow_redirects=False)


@pytest.fixture
def app(tmp_path):
    return PREVIEW.preview_app(tmp_path, ai_signed_in=False)


@pytest.fixture
def client(app):
    return TestClient(app, base_url=BASE)


# ---- nothing starts on its own ----------------------------------------------------------


def test_startup_page_views_and_status_polls_never_start_a_provider_session(tmp_path):
    calls = []

    class Counting:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError(f"Provider session touched: {name}")

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    gmail = GmailService(ledger.path, store=Counting(), oauth=Counting())
    ai = AIService(ledger, binary="synthetic", client_factory=Counting())
    app = create_app(ledger.path, gmail_service=gmail, ai_service=ai)
    client = TestClient(app, base_url=BASE)
    for path in (
        "/",
        "/gmail",
        "/saved",
        "/ai",
        "/ledger",
        "/review",
        "/public",
        "/imports",
        "/storage",
        "/gmail/status",
        "/ai/status",
        "/ai/status",
        "/static/provider-connect.js",
    ):
        assert client.get(path).status_code == 200
    assert calls == [] and ai.client is None and not gmail.pending
    assert status(client, "gmail") == {"attempt": 0, "state": "idle"}
    assert status(client, "ai") == {"attempt": 0, "state": "idle"}


def test_connect_buttons_target_one_named_window_with_an_explicit_fallback(client):
    for path, action, name in (
        ("/gmail", "/gmail/connect", "gmail"),
        ("/ai", "/ai/connect", "chatgpt"),
    ):
        page = Page(client.get(path).text)
        forms = page.attrs("form", **{"data-provider-connect": name})
        assert len(forms) == 1
        form = forms[0]
        assert form["action"] == action and form["method"] == "post"
        assert form["target"] == "provider-auth"  # one reusable sign-in window
        assert form["data-status"] == action.replace("connect", "status")
        assert page.attrs("input", name="display", value="popup", type="hidden")
        assert page.attrs("input", name="display", value="window", type="hidden")  # <noscript>
        blocked = page.attrs("div", **{"data-auth-blocked": name})
        assert blocked and blocked[0]["role"] == "alert" and "hidden" in blocked[0]
        assert page.attrs("script") == [{"src": "/static/provider-connect.js"}]


def test_script_is_served_same_origin_without_caching_or_sniffing(client):
    response = client.get("/static/provider-connect.js")
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.text.strip().endswith("})();")
    assert client.get("/static/other.js").status_code == 404
    assert client.post("/static/provider-connect.js").status_code in {403, 405}


# ---- Gmail --------------------------------------------------------------------------------


def test_gmail_popup_completes_only_in_the_app_and_exposes_no_callback_secrets(client, app):
    response = post(client, "/gmail/connect", display="popup")
    assert response.status_code == 200
    link = provider_link(response)
    assert link.startswith("/preview-consent?state=")
    assert status(client, "gmail") == {"attempt": 1, "state": "pending"}
    assert app.state.gmail.connection is None  # opening a window is not a connection
    assert "Waiting for Google sign-in" in client.get("/gmail").text
    assert Page(client.get("/gmail").text).attrs("div", **{"data-auth-resume": None})
    result = approve_gmail(client, link)
    assert result.status_code == 200
    page = Page(result.text)
    assert page.attrs("main", **{"data-auth-result": "connected"})
    assert "Gmail connected" in result.text
    assert result.headers["referrer-policy"] == "no-referrer"
    assert result.headers["cache-control"] == "no-store"
    assert "synthetic-only" not in result.text and "code=" not in result.text
    assert "state=" not in result.text and "fake-refresh-token" not in result.text
    assert page.attrs("a", href="/gmail") and page.attrs("button", **{"data-close-window": None})
    assert status(client, "gmail") == {"attempt": 1, "state": "idle"}
    assert app.state.gmail.connection is not None
    assert app.state.gmail.saved_month(MONTH)[0] == {}  # signing in loads no mail
    assert "alex.preview@example.invalid" in client.get("/gmail").text


def test_gmail_popup_denial_failure_and_missing_client_return_clear_states(tmp_path):
    app = PREVIEW.preview_app(tmp_path)
    client = TestClient(app, base_url=BASE)
    link = provider_link(post(client, "/gmail/connect", display="popup"))
    denied = approve_gmail(client, link, decision="error")
    assert Page(denied.text).attrs("main", **{"data-auth-result": "cancelled"})
    assert "denied or cancelled" in denied.text and app.state.gmail.connection is None
    assert "denied or cancelled" in client.get("/gmail").text  # the app shows it too
    assert status(client, "gmail")["state"] == "idle"
    link = provider_link(post(client, "/gmail/connect", display="popup"))
    state = parse_qs(urlparse(link).query)["state"][0]
    failed = client.get("/gmail/callback", params={"state": state, "code": "wrong"})
    assert Page(failed.text).attrs("main", **{"data-auth-result": "failed"})
    assert "Google authorization failed" in failed.text
    replay = client.get("/gmail/callback", params={"state": state, "code": "synthetic"})
    assert Page(replay.text).attrs("main", **{"data-auth-result": "failed"})
    assert "expired or reused" in replay.text and app.state.gmail.connection is None


def test_gmail_popup_without_a_google_client_reports_the_missing_configuration(tmp_path):
    app = PREVIEW.preview_app(tmp_path, configured=False)
    client = TestClient(app, base_url=BASE)
    before = status(client, "gmail")
    response = post(client, "/gmail/connect", display="popup")
    page = Page(response.text)
    assert page.attrs("main", **{"data-auth-result": "failed"})
    assert (
        "SPENDING_GOOGLE_CLIENT_ID" in response.text
        and "SPENDING_GOOGLE_CLIENT_SECRET" in response.text
    )
    assert not page.attrs("a", rel="noreferrer")  # no provider page is offered
    assert not page.attrs("meta", **{"http-equiv": "refresh"})
    after = status(client, "gmail")
    assert after["state"] == "idle" and after["attempt"] == before["attempt"] + 1
    assert "SPENDING_GOOGLE_CLIENT_ID" in client.get("/gmail").text  # the opener reports it too
    assert app.state.gmail.connection is None and not app.state.gmail.pending


def test_gmail_cancel_clears_the_attempt_and_the_abandoned_window_cannot_finish(client, app):
    link = provider_link(post(client, "/gmail/connect", display="popup"))
    assert status(client, "gmail")["state"] == "pending"
    response = post(client, "/gmail/cancel")
    assert response.status_code == 200 and "Sign-in cancelled" in response.text
    assert status(client, "gmail") == {"attempt": 1, "state": "idle"}
    late = approve_gmail(client, link)
    assert Page(late.text).attrs("main", **{"data-auth-result": "failed"})
    assert app.state.gmail.connection is None
    assert post(client, "/gmail/cancel").status_code == 200  # idempotent


def test_gmail_expired_attempt_becomes_idle_with_an_explicit_message(client, app):
    link = provider_link(post(client, "/gmail/connect", display="popup"))
    app.state.gmail.clock = lambda: 10**15
    assert status(client, "gmail") == {"attempt": 1, "state": "idle"}
    assert "Sign-in expired" in client.get("/gmail").text
    assert approve_gmail(client, link).status_code == 200 and app.state.gmail.connection is None


def test_blocked_popup_continuation_completes_in_the_same_window(client, app):
    response = post(client, "/gmail/connect", display="window")
    link = provider_link(response)
    result = approve_gmail(client, link)
    assert result.status_code == 303 and result.headers["location"] == "/gmail"
    assert result.headers["referrer-policy"] == "no-referrer"
    assert app.state.gmail.connection is not None
    # A later popup attempt is not affected by the earlier same-window one.
    link = provider_link(post(client, "/gmail/connect", display="popup"))
    assert approve_gmail(client, link).status_code == 200


def test_sign_in_display_must_be_a_known_mode_and_mutations_stay_protected(client):
    for path in ("/gmail/connect", "/ai/connect"):
        assert post(client, path, display="tab").status_code == 422
        assert client.post(path, data={"csrf": "wrong", "display": "popup"}).status_code == 403
        csrf = client.cookies["ledger_csrf"]
        evil = {"Origin": "https://evil.invalid"}
        assert client.post(path, data={"csrf": csrf}, headers=evil).status_code == 403
        assert (
            client.post(path, data={"csrf": csrf}, headers={"Host": "evil.invalid"}).status_code
            == 400
        )
    for path in ("/gmail/cancel", "/ai/cancel"):
        assert client.post(path, data={"csrf": "wrong"}).status_code == 403
        assert post(client, path, display="popup").status_code == 422
    assert not client.app.state.gmail.pending and client.app.state.ai.client is None


def test_status_requires_no_secrets_and_never_leaks_account_details(client, app):
    approve = provider_link(post(client, "/gmail/connect", display="popup"))
    approve_gmail(client, approve)
    body = client.get("/gmail/status").text
    assert "alex.preview" not in body and "token" not in body
    assert set(client.get("/gmail/status").json()) == {"attempt", "state"}
    assert set(client.get("/ai/status").json()) == {"attempt", "state"}


# ---- ChatGPT ------------------------------------------------------------------------------


def test_chatgpt_popup_is_connected_only_after_the_official_client_reports_an_account(client, app):
    response = post(client, "/ai/connect", display="popup")
    assert provider_link(response) == "/preview-ai-consent"
    ai = app.state.ai
    assert ai.connection is None and ai.client.inputs == []
    assert status(client, "ai") == {"attempt": 1, "state": "pending"}
    waiting = client.get("/ai").text
    assert "Waiting for ChatGPT sign-in" in waiting and "AI disconnected" in waiting
    # The popup was opened and the user consented there, but nothing changed in the app yet.
    assert client.get("/preview-ai-consent/approve").status_code == 200
    assert ai.connection is None
    connection_id = ai.connection_id
    assert status(client, "ai") == {"attempt": 1, "state": "idle"}  # the poll noticed
    assert ai.connection == {"email": "fresh.preview@example.invalid", "plan": "plus"}
    assert ai.connection_id != connection_id and ai.authorization_url is None
    connected = client.get("/ai").text
    assert "ChatGPT connected" in connected and "fresh.preview@example.invalid" in connected
    assert ai.client.inputs == []  # signing in and polling never reach a model
    assert not app.state.ledger.load().transactions


def test_chatgpt_denied_cancelled_and_expired_sign_ins_return_to_the_app(client, app):
    ai = app.state.ai
    post(client, "/ai/connect", display="popup")
    transport = ai.client
    client.get("/preview-ai-consent/deny")
    assert status(client, "ai")["state"] == "idle"
    assert ai.client is None and transport.closed and ai.connection is None
    assert "ChatGPT sign-in did not complete" in client.get("/ai").text
    post(client, "/ai/connect", display="popup")
    transport = ai.client
    response = post(client, "/ai/cancel")
    assert "Sign-in cancelled" in response.text
    assert transport.cancelled and transport.closed and ai.client is None
    assert status(client, "ai") == {"attempt": 2, "state": "idle"}
    post(client, "/ai/connect", display="popup")
    ai.clock = lambda: 10**15
    assert status(client, "ai")["state"] == "idle"
    assert "Sign-in expired" in client.get("/ai").text and ai.client is None
    assert post(client, "/ai/cancel").status_code == 200  # nothing pending: harmless
    assert ai.client is None


def test_chatgpt_popup_reports_missing_client_and_connection_failures(tmp_path, monkeypatch):
    plain = TestClient(create_app(tmp_path / "plain.sqlite3"), base_url=BASE)
    response = post(plain, "/ai/connect", display="popup")
    assert Page(response.text).attrs("main", **{"data-auth-result": "failed"})
    assert "Codex is not configured" in response.text and "SPENDING_CODEX_CLIENT" in response.text
    assert status(plain, "ai") == {"attempt": 1, "state": "idle"}
    assert "Not configured" in plain.get("/ai").text
    app = PREVIEW.preview_app(tmp_path)

    def fail(self):
        raise AIProblem("connection_failed")

    monkeypatch.setattr(PREVIEW.PreviewAIClient, "start", fail)
    client = TestClient(app, base_url=BASE)
    response = post(client, "/ai/connect", display="popup")
    assert "Official client connection failed" in response.text
    assert app.state.ai.client is None and status(client, "ai")["state"] == "idle"


def test_chatgpt_cached_subscription_connects_without_opening_a_provider_page(tmp_path):
    app = PREVIEW.preview_app(tmp_path)  # a saved official-client session exists
    client = TestClient(app, base_url=BASE)
    response = post(client, "/ai/connect", display="popup")
    page = Page(response.text)
    assert page.attrs("main", **{"data-auth-result": "connected"})
    assert not page.attrs("meta", **{"http-equiv": "refresh"})
    assert app.state.ai.connection["email"] == "ai.preview@example.invalid"
    window = post(client, "/ai/connect", display="window")
    assert window.status_code == 200 and "ChatGPT connected" in window.text  # redirected to /ai


def test_chatgpt_window_fallback_and_manual_check_complete_without_a_popup(client, app):
    response = post(client, "/ai/connect", display="window")
    assert provider_link(response) == "/preview-ai-consent"
    early = client.post("/ai/confirm", data={"csrf": client.cookies["ledger_csrf"]})
    assert "No eligible saved ChatGPT subscription session" in early.text
    assert app.state.ai.connection is None  # not yet signed in: explicit refusal, still waiting
    client.get("/preview-ai-consent/approve")
    page = client.post("/ai/confirm", data={"csrf": client.cookies["ledger_csrf"]})
    assert "fresh.preview@example.invalid" in page.text


def test_status_poll_never_waits_for_a_running_analysis(client, app):
    post(client, "/ai/connect", display="popup")
    ai = app.state.ai
    release, entered = threading.Event(), threading.Event()

    def hold():
        with ai.lock:
            entered.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(5)
        client.get("/preview-ai-consent/approve")
        result = {}
        poller = threading.Thread(target=lambda: result.update(client.get("/ai/status").json()))
        poller.start()
        poller.join(5)
        assert not poller.is_alive() and result["state"] == "pending"
        assert ai.connection is None  # skipped, not blocked
    finally:
        release.set()
        thread.join(5)
    assert status(client, "ai")["state"] == "idle" and ai.connection is not None


# ---- the official client's login protocol --------------------------------------------------


def fake_codex(tmp_path, responses):
    native = CodexClient("/fake", tmp_path)
    native.calls = []

    def request(method, params=None, timeout=20):
        native.calls.append((method, params))
        return responses[method]

    native.request = request
    return native


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.openai.com/oauth/authorize",
        "https://evil.invalid/oauth/authorize",
        "https://auth.openai.com@evil.invalid/",
        "https://auth.openai.com:8443/oauth",
        "https://user:pw@auth.openai.com/oauth",
        "javascript:alert(1)",
        "",
    ],
)
def test_only_the_official_openai_login_page_is_ever_offered(tmp_path, url):
    native = fake_codex(tmp_path, {"account/login/start": {"authUrl": url, "loginId": "id"}})
    with pytest.raises(AIProblem, match="connection failed"):
        native.login()


def test_login_completion_and_cancel_use_the_official_clients_own_messages(tmp_path):
    ok = "https://auth.openai.com/oauth/authorize?state=x"
    native = fake_codex(
        tmp_path,
        {
            "account/login/start": {"authUrl": ok, "loginId": "login-1", "type": "chatgpt"},
            "account/login/cancel": {"status": "canceled"},
        },
    )
    assert native.login() == ok
    assert native.login_finished() is None
    native.pending = [
        {"method": "account/login/completed", "params": {"loginId": "other", "success": False}},
        {"method": "turn/completed", "params": {}},
    ]
    assert native.login_finished() is None and native.pending == []
    native.pending = [
        {"method": "account/login/completed", "params": {"loginId": "login-1", "success": True}}
    ]
    assert native.login_finished() is True
    native.pending = [
        {"method": "account/login/completed", "params": {"success": False, "error": "x"}}
    ]
    assert native.login_finished() is False
    native.cancel_login()
    native.cancel_login()  # a second cancel sends nothing
    assert native.calls[-1] == ("account/login/cancel", {"loginId": "login-1"})
    assert [c for c, _ in native.calls].count("account/login/cancel") == 1


def test_gmail_service_surfaces_a_failed_begin_before_it_stops_counting_as_busy(tmp_path):
    class Failing:
        def get(self, key):
            return None

    service = GmailService(tmp_path / "ledger.sqlite3", store=Failing())
    with pytest.raises(GmailProblem):
        service.begin("session", "http://127.0.0.1:8765/gmail/callback")
    assert service.problem == "not_configured" and not service.busy
    assert service.status("session") == {"attempt": 1, "state": "idle"}
