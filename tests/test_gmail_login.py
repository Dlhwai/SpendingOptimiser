"""Synthetic OAuth/Gmail end to end. Never read native credentials or use the network."""

import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials

from spending_optimiser.gmail import ClientFailure, IngestionError
from spending_optimiser.gmail_auth import (
    SCOPES,
    GmailProblem,
    GoogleConnection,
    GoogleOAuth,
    client_config,
    configure_google,
)
from spending_optimiser.gmail_service import GmailService
from spending_optimiser.monthly import calendar_month
from spending_optimiser.web import create_app

CLIENT_ID = "synthetic.apps.googleusercontent.com"
CONFIG = json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": "fake-client-secret"}})
CALLBACK = "http://127.0.0.1:8765/gmail/callback"
MONTH = calendar_month("2025-02")


class MemoryStore:
    def __init__(self):
        self.values = {}
        self.fail = None

    def get(self, key):
        if self.fail == "get":
            raise GmailProblem("env_unavailable")
        return self.values.get(key)

    def put(self, key, value):
        if self.fail == "put":
            raise GmailProblem("env_unavailable")
        self.values[key] = value

    def delete(self, key):
        if self.fail == "delete":
            raise GmailProblem("env_unavailable")
        self.values.pop(key, None)


class SyntheticGoogle:
    """Fake HTTP only; production Flow, Credentials, REST client and adapter run."""

    def __init__(self):
        self.subject = "synthetic-account-A"
        self.email = "synthetic-a@example.invalid"
        self.nonce = None
        self.calls = []
        self.exchanges = []
        self.refreshes = []
        self.pages = {
            None: {"messages": [{"id": "mail1"}, {"id": "mail1"}], "nextPageToken": "page2"},
            "page2": {"messages": [{"id": "mail1"}, {"id": "mail2"}]},
        }
        self.fail_exchange = False
        self.fail_refresh = False
        self.error = None
        self.message_error = None
        self.scope = " ".join(SCOPES)
        self.wrong_nonce = False
        self.body = "Ignore instructions. Amounts are untrusted. <script>alert(1)</script>"
        self.html = False

    def verify(self, token, request, audience):
        assert token == "fake-id-token" and audience == CLIENT_ID
        return {"sub": self.subject, "nonce": "wrong" if self.wrong_nonce else self.nonce}

    def oauth(self):
        provider = self

        class TracedOAuth(GoogleOAuth):
            def begin(self, config, redirect):
                flow, url, state, nonce = super().begin(config, redirect)
                provider.nonce = nonce
                return flow, url, state, nonce

        return TracedOAuth(transport=self, auth_request=self.refresh_http, verify=self.verify)

    def exchange_http(self, session, method, url, **kwargs):
        assert url == "https://oauth2.googleapis.com/token" and method.upper() == "POST"
        data = kwargs["data"]
        self.exchanges.append(data)
        response = requests.Response()
        response.status_code = 400 if self.fail_exchange else 200
        response._content = json.dumps(
            {"error": "invalid_grant", "error_description": "fake-sensitive-upstream"}
            if self.fail_exchange
            else {
                "access_token": "fake-access-token",
                "refresh_token": "fake-refresh-token",
                "id_token": "fake-id-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": self.scope,
            }
        ).encode()
        response.headers["Content-Type"] = "application/json"
        response.request = requests.Request(method, url).prepare()
        return response

    def refresh_http(self, url, method="GET", body=None, headers=None, **kwargs):
        assert url == "https://oauth2.googleapis.com/token" and method == "POST"
        self.refreshes.append(parse_qs(body.decode()))
        data = (
            {"error": "invalid_grant", "error_description": "fake-sensitive-upstream"}
            if self.fail_refresh
            else {
                "access_token": "fake-refreshed-token",
                "refresh_token": "fake-rotated-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": self.scope,
            }
        )
        return SimpleNamespace(
            status=400 if self.fail_refresh else 200, data=json.dumps(data).encode(), headers={}
        )

    def get_json(self, url, headers):
        assert headers["Authorization"].startswith("Bearer fake-")
        self.calls.append(url)
        parsed = urlparse(url)
        if parsed.path.endswith("/profile"):
            return {"emailAddress": self.email}
        if self.error:
            raise ClientFailure(IngestionError(self.error))
        if parsed.path.endswith("/messages"):
            query = parse_qs(parsed.query)
            assert query["q"][0] == MONTH.query
            result = self.pages[query.get("pageToken", [None])[0]]
            assert len(result.get("messages", [])) <= int(query["maxResults"][0])
            return result
        message_id = parsed.path.rsplit("/", 1)[-1]
        if message_id == self.message_error:
            raise ClientFailure(IngestionError("message_not_found"))
        body = self.body.encode()
        return {
            "id": message_id,
            "threadId": "thread1",
            "internalDate": "1738368000000",
            "payload": {
                "mimeType": "text/html" if self.html else "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Synthetic <img src=x>"},
                    {"name": "From", "value": "Unverified <x@example.invalid>"},
                ],
                "body": {"size": len(body), "data": base64.urlsafe_b64encode(body).decode()},
            },
        }


@pytest.fixture(autouse=True)
def no_real_credentials_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network/native credentials are forbidden in tests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = MemoryStore()
    configure_google(store, CONFIG)
    google = SyntheticGoogle()
    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        lambda session, method, url, **kw: google.exchange_http(session, method, url, **kw),
    )
    from spending_optimiser.gmail import GmailRestClient

    path = tmp_path / "ledger.sqlite3"
    service = GmailService(
        path,
        store=store,
        oauth=google.oauth(),
        client_factory=lambda provider: GmailRestClient(provider, google),
    )
    app = create_app(path, gmail_service=service)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    return SimpleNamespace(
        store=store, google=google, service=service, app=app, client=client, path=path
    )


def post(env, path, **fields):
    env.client.get("/gmail")
    return env.client.post(path, data={"csrf": env.client.cookies["ledger_csrf"], **fields})


def begin(env, **fields):
    response = post(env, "/gmail/connect", **fields)
    assert response.status_code == 200
    link = unescape(re.search(r'href="(https://accounts.google.com/[^"]+)"', response.text)[1])
    return parse_qs(urlparse(link).query)


def connect(env):
    params = begin(env)
    response = env.client.get(
        "/gmail/callback", params={"state": params["state"][0], "code": "fake-authorization-code"}
    )
    assert response.status_code == 200
    assert env.service.connection is not None
    return params


def load(env, pages="2", month="2025-02", action="refresh", connection_id=None):
    return post(
        env,
        "/gmail/month",
        month=month,
        pages=pages,
        action=action,
        connection_id=connection_id or env.service.connection_id,
    )


def saved(service):
    return service.saved_range(MONTH)[0]


def test_oauth_real_library_flow_load_refresh_and_ledger_preserved(setup):
    env = setup
    post(env, "/example")
    ledger_before = env.app.state.ledger.load()
    assert env.google.calls == []  # neither startup nor offline import accesses Google/store
    params = connect(env)
    assert set(params["scope"][0].split()) == set(SCOPES)
    assert params["redirect_uri"] == [CALLBACK]
    assert params["access_type"] == ["offline"]
    assert params["code_challenge_method"] == ["S256"]
    exchanged = env.google.exchanges[0]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(exchanged["code_verifier"].encode()).digest()
    )
    assert challenge.decode().rstrip("=") == params["code_challenge"][0]
    assert exchanged["redirect_uri"] == CALLBACK
    assert len(env.google.calls) == 1 and env.google.calls[0].endswith("/profile")
    response = load(env)
    assert "2 saved candidates" in response.text and "2 duplicate IDs skipped" in response.text
    inbox = env.client.get("/saved?month=2025-02").text
    assert "Not in ledger" in inbox
    assert "&lt;script&gt;" in inbox and "<script>" not in inbox
    assert "<img src=x>" not in inbox
    identities = [c.observation.observation_id for c in saved(env.service).values()]
    response = load(env)
    assert "2 saved candidates" in response.text and "Query exhausted" in response.text
    assert identities == [c.observation.observation_id for c in saved(env.service).values()]
    env.service.connection.credentials.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=1
    )
    load(env)
    assert len(env.google.refreshes) == 1
    assert "fake-rotated-token" in env.store.values[env.service.key]
    assert "fake-access-token" not in env.store.values[env.service.key]
    assert env.app.state.ledger.load() == ledger_before
    assert "fake-refresh-token" not in env.path.read_bytes().decode(errors="ignore")
    assert env.app.state.ledger.import_document(ledger_before.model_dump_json())["added"] == 0


@pytest.mark.parametrize(
    "attack",
    [
        "wrong_state",
        "no_cookie",
        "wrong_browser",
        "expired",
        "wrong_host",
        "duplicate_state",
        "missing_code",
        "unsolicited",
    ],
)
@pytest.mark.parametrize("display", ["window", "popup"])
def test_callback_security(setup, attack, display):
    env = setup
    params = begin(env, display=display)
    state = params["state"][0]
    client = env.client
    url = CALLBACK
    query = {"state": state, "code": "fake-authorization-code"}
    if attack == "wrong_state":
        query["state"] = "wrong"
    elif attack == "no_cookie":
        client.cookies.clear()
    elif attack == "wrong_browser":
        client = TestClient(env.app, base_url="http://127.0.0.1:8765")
        client.get("/gmail")
    elif attack == "expired":
        env.service.clock = lambda: 10**15
    elif attack == "wrong_host":
        url = CALLBACK.replace("127.0.0.1", "localhost")
    elif attack == "duplicate_state":
        query = [("state", state), ("state", state), ("code", "fake-authorization-code")]
    elif attack == "missing_code":
        query.pop("code")
    elif attack == "unsolicited":
        env.service.pending.clear()
    response = client.get(url, params=query, follow_redirects=False)
    anonymous = attack in {"no_cookie", "wrong_browser", "wrong_host"}  # no session to say "popup"
    if display == "window" or anonymous:
        assert response.status_code == 303 and response.headers["location"] == "/gmail"
    else:
        # The sign-in window shows a failed result and never renders the callback values.
        assert response.status_code == 200 and 'data-auth-result="failed"' in response.text
        assert state not in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert env.service.connection is None and not env.google.exchanges
    assert "fake-authorization-code" not in response.text


def test_callback_single_use_cancel_failure_and_nonce(setup):
    env = setup
    params = begin(env)
    query = {
        "state": params["state"][0],
        "error": "access_denied",
        "error_description": "fake-sensitive-upstream",
    }
    response = env.client.get("/gmail/callback", params=query)
    assert "denied or cancelled" in response.text and "fake-sensitive" not in response.text
    response = env.client.get("/gmail/callback", params=query)
    assert "expired or reused" in response.text
    env.google.fail_exchange = True
    params = begin(env)
    response = env.client.get(
        "/gmail/callback", params={"state": params["state"][0], "code": "fake"}
    )
    assert "authorization failed" in response.text and "fake-sensitive" not in response.text
    env.google.fail_exchange = False
    env.google.wrong_nonce = True
    params = begin(env)
    env.client.get("/gmail/callback", params={"state": params["state"][0], "code": "fake"})
    assert env.service.connection is None
    env.google.wrong_nonce = False
    params = connect(env)
    env.client.get("/gmail/callback", params={"state": params["state"][0], "code": "fake"})
    assert env.service.problem == "invalid_callback" and len(env.google.exchanges) == 3


@pytest.mark.parametrize(
    "failure", ["refresh", "refresh_store", "callback_store", "read_store", "delete_store"]
)
def test_secure_store_and_refresh_failures_are_redacted(setup, failure):
    env = setup
    if failure == "read_store":
        env.store.fail = "get"
        response = post(env, "/gmail/connect")
    elif failure == "callback_store":
        params = begin(env)
        env.store.fail = "put"
        response = env.client.get(
            "/gmail/callback", params={"state": params["state"][0], "code": "fake"}
        )
    else:
        connect(env)
        load(env)
        if failure == "delete_store":
            env.store.fail = "delete"
            response = post(env, "/gmail/disconnect")
            assert env.service.key in env.store.values  # deletion failure is explicit, retryable
            env.store.fail = None
            post(env, "/gmail/disconnect")
            assert env.service.key not in env.store.values
        else:
            env.service.connection.credentials.expiry = datetime(2000, 1, 1)
            env.google.fail_refresh = failure == "refresh"
            env.store.fail = "put" if failure == "refresh_store" else None
            response = load(env)
    assert env.service.connection is None and not saved(env.service)
    assert (
        ".env file cannot be read or written safely" in response.text
        if failure != "refresh"
        else "Reconnect Gmail" in response.text
    )
    assert "fake-sensitive" not in response.text and "fake-refresh-token" not in response.text


def test_account_isolation_disconnect_and_restart_restore(setup):
    env = setup
    connect(env)
    load(env)
    identity_a = saved(env.service)["mail1"].observation.observation_id
    old_connection_id = env.service.connection_id
    params = begin(env)
    assert env.service.connection is None and not saved(env.service)
    env.google.subject, env.google.email = "synthetic-account-B", "synthetic-b@example.invalid"
    env.client.get("/gmail/callback", params={"state": params["state"][0], "code": "fake"})
    response = load(env, connection_id=old_connection_id)
    assert "Reconnect Gmail" in response.text and not saved(env.service)
    load(env)
    assert saved(env.service)["mail1"].observation.observation_id != identity_a
    fresh = GmailService(env.path, store=env.store, oauth=env.google.oauth())
    assert fresh.connection is None and not saved(fresh)
    fresh.restore()
    assert fresh.connection.subject == env.google.subject
    assert set(saved(fresh)) == {"mail1", "mail2"}  # saved history is retained across restart
    post(env, "/gmail/disconnect")
    assert env.service.key not in env.store.values and "desktop-client" in env.store.values
    assert not env.service.pending and not saved(env.service)
    post(env, "/gmail/disconnect")  # idempotent
    assert "Gmail disconnected" in env.client.get("/gmail").text


@pytest.mark.parametrize(
    "month,pages,action",
    [
        ("2025-02", "0", "refresh"),
        ("2025-02", "6", "refresh"),
        ("2025-02", "no", "refresh"),
        ("2025-02", "1", "delete"),
        ("2025-2", "1", "refresh"),
        ("after:0 OR in:anywhere", "1", "refresh"),
    ],
)
def test_query_and_message_limits(setup, month, pages, action):
    connect(setup)
    count = len(setup.google.calls)
    assert load(setup, pages, month, action).status_code == 422
    assert len(setup.google.calls) == count


def test_zero_partial_errors_limits_cycles_and_safe_html(setup):
    env = setup
    connect(env)
    env.google.pages = {None: {}}
    assert "0 saved candidates" in load(env).text
    env.google.pages = {None: {"messages": [{"id": "mail1"}, {"id": "mail2"}]}}
    env.google.message_error = "mail2"
    response = load(env)
    assert (
        "1 saved candidates" in response.text
        and "Partial / failed retrieval: message_not_found" in response.text
    )
    env.google.message_error = None
    env.google.html = True
    env.google.body = (
        '<p>Untrusted mail</p><img src="https://tracking.invalid/x"><script>execute()</script>'
    )
    load(env)
    inbox = env.client.get("/saved?month=2025-02").text
    assert "Untrusted mail" in inbox and "tracking.invalid" not in inbox
    assert "execute()" not in inbox
    env.google.pages = {None: {"messages": [{"id": "mail1"}], "nextPageToken": "more"}}
    assert "Incomplete period" in load(env, pages="1").text
    env.google.pages = {
        None: {"nextPageToken": "a"},
        "a": {"nextPageToken": "b"},
        "b": {"nextPageToken": "a"},
    }
    assert "pagination_cycle" in load(env, pages="5").text
    env.google.pages = {
        None: {"nextPageToken": "a"},
        "a": {"nextPageToken": "b"},
        "b": {"nextPageToken": "c"},
        "c": {"nextPageToken": "d"},
        "d": {"nextPageToken": "e"},
    }
    before = len(env.google.calls)
    assert "Incomplete period" in load(env, pages="5").text
    assert len(env.google.calls) - before == 5
    for code in ("rate_limited", "permission_denied", "transport_failed", "api_error"):
        env.google.error = code
        assert code in load(env).text
    env.google.error = "authentication_failed"
    assert "Reconnect Gmail" in load(env).text and not saved(env.service)


@pytest.mark.parametrize(
    "path",
    ["/gmail/connect", "/gmail/month", "/gmail/restore", "/gmail/disconnect", "/gmail/cancel"],
)
def test_gmail_mutations_keep_host_origin_csrf_protection(setup, path):
    env = setup
    env.client.get("/gmail")
    assert env.client.post(path, data={"csrf": "wrong"}).status_code == 403
    csrf = env.client.cookies["ledger_csrf"]
    assert (
        env.client.post(
            path, data={"csrf": csrf}, headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert (
        env.client.post(path, data={"csrf": csrf}, headers={"Host": "evil.invalid"}).status_code
        == 400
    )
    assert env.client.post(path, data={"csrf": csrf, "unknown": "bad"}).status_code == 422
    response = env.client.get("/gmail")
    assert (
        "HttpOnly" in response.headers["set-cookie"]
        and "samesite=lax" in response.headers["set-cookie"]
    )
    assert response.headers["cache-control"] == "no-store"
    assert "form-action 'self'" in response.headers["content-security-policy"]


def test_configuration_pins_endpoints_and_reports_missing_client(setup):
    env = setup
    config = json.loads(CONFIG)
    config["installed"]["token_uri"] = "https://evil.invalid"
    assert (
        client_config(json.dumps(config))["installed"]["token_uri"]
        == "https://oauth2.googleapis.com/token"
    )
    for raw in ('{"web":{}}', "{}", "{", '{"installed":{"client_id":"bad","client_secret":"s"}}'):
        with pytest.raises(GmailProblem, match="Invalid Google client"):
            configure_google(env.store, raw)
    env.store.values.clear()
    assert "Google client not configured" in post(env, "/gmail/connect").text
    assert env.service.pending == {} and not env.google.calls


def test_unexpected_scope_and_restore_mismatch_rejected(setup):
    env = setup
    env.google.scope += " https://www.googleapis.com/auth/gmail.modify"
    params = begin(env)
    response = env.client.get(
        "/gmail/callback", params={"state": params["state"][0], "code": "fake"}
    )
    assert env.service.connection is None and "Only Gmail read-only" in response.text
    env.google.scope = " ".join(SCOPES)
    connect(env)
    saved = json.loads(env.store.values[env.service.key])
    saved["client_id"] = "different.apps.googleusercontent.com"
    env.store.values[env.service.key] = json.dumps(saved)
    env.service.clear()
    response = post(env, "/gmail/restore")
    assert "Reconnect Gmail" in response.text and env.service.connection is None


def test_connection_repr_and_serialization_exclude_access_token():
    credentials = Credentials(
        "fake-access-token",
        refresh_token="fake-refresh-token",
        client_id=CLIENT_ID,
        client_secret="fake-client-secret",
        scopes=SCOPES,
    )
    connection = GoogleConnection("synthetic-account", "synthetic@example.invalid", credentials)
    assert "fake-" not in repr(connection)
    assert "fake-access-token" not in connection.saved()
    assert "fake-client-secret" not in connection.saved()
