"""Synthetic request responsiveness under a real SQLite VACUUM exclusive lock."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import routing
from fastapi.testclient import TestClient
from test_ui import PREVIEW

from spending_optimiser.ai_inputs import digest
from spending_optimiser.monthly import calendar_month


@pytest.fixture(autouse=True)
def synthetic_dependencies_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Only injected synthetic credentials and transports are allowed")

    for target in (
        "subprocess.Popen",
        "urllib.request.OpenerDirector.open",
        "requests.sessions.Session.request",
    ):
        monkeypatch.setattr(target, forbidden)


@pytest.mark.parametrize(
    "method,path,on_loop",
    [
        ("GET", "/storage", False),
        ("GET", "/gmail?month=2025-02", False),
        ("GET", "/ai", False),
        ("GET", "/ai?snapshot={snapshot}", False),
        ("GET", "/ai/proposal?id={proposal}", False),
        ("GET", "/?month=2025-02", False),
        ("GET", "/ledger?month=2025-02", False),
        ("GET", "/cards", False),
        ("GET", "/?start=2025-02-10&end=2025-02-10", False),
        ("GET", "/review", False),
        ("GET", "/transaction?event_id=test-purchase", False),
        ("GET", "/public", False),
        ("POST", "/ai/save", False),
        ("POST", "/storage/compress", False),
        ("GET", "/storage", True),
    ],
)
def test_database_requests_yield_during_vacuum(tmp_path, monkeypatch, method, path, on_loop):
    app = PREVIEW.preview_app(tmp_path, populated=True)
    ledger, gmail, ai = app.state.ledger, app.state.gmail, app.state.ai
    month = calendar_month("2025-02")
    gmail.connection = gmail.oauth.connection()
    gmail.load_range(month, 1, gmail.connection_id, "continue")
    ai.connect()
    snapshot = ai.save_format(
        "Purchase <CURRENCY> <AMOUNT> at <MERCHANT> on <DATE_ISO>. Card <CARD_SUFFIX>."
    )
    proposal = ai.analyse(
        snapshot, digest(ai.preview(snapshot)), ai.connection_id, "analyse-safe-snapshot"
    )
    document = ledger.load()
    saved_mail = gmail.saved_range(month)
    with ledger.connection() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    locked = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    probe_started = threading.Event()
    connections, timeouts, waiters, busy_codes, vacuum_completed = [], [], [], [], []
    connect = sqlite3.connect

    def hold_vacuum_lock():
        if locked.is_set():
            return 0
        probe = connect(ledger.path, timeout=0)
        try:
            probe.execute("SELECT count(*) FROM imports").fetchone()
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                raise
            busy_codes.append(exc.sqlite_errorcode)
            locked.set()
            return 0 if release.wait(20) else 1
        finally:
            probe.close()
        return 0

    class ObservedConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner = threading.get_ident()
            self.closed = False
            connections.append(self)

        def execute(self, sql, *args):
            assert threading.get_ident() == self.owner
            if sql == "VACUUM":
                self.set_progress_handler(hold_vacuum_lock, 1)
                try:
                    cursor = super().execute(sql, *args)
                    vacuum_completed.append(True)
                    return cursor
                finally:
                    self.set_progress_handler(None, 0)
            if locked.is_set() and not release.is_set() and sql.startswith(("SELECT", "INSERT")):
                waiters.append(threading.get_ident())
                waiting.set()
            return super().execute(sql, *args)

        def close(self):
            assert threading.get_ident() == self.owner
            super().close()
            self.closed = True

    def observed_connect(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return connect(*args, **kwargs, factory=ObservedConnection)

    if on_loop:
        endpoint = next(route.endpoint for route in app.routes if route.path == "/storage")
        dispatch = routing.run_endpoint_function

        async def inline_database_unit(*, dependant, values, is_coroutine):
            if dependant.call is endpoint:
                return dependant.call(**values)
            return await dispatch(dependant=dependant, values=values, is_coroutine=is_coroutine)

        monkeypatch.setattr(routing, "run_endpoint_function", inline_database_unit)

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    monkeypatch.setattr("spending_optimiser.storage.GB", 1)
    monkeypatch.setattr("spending_optimiser.storage.hot_start", lambda now=None: month.end)
    with TestClient(app, base_url="http://127.0.0.1:8898") as client:
        client.get("/imports")
        loop_thread = client.portal.call(threading.get_ident)
        fields = {"csrf": client.cookies["ledger_csrf"]}
        request_fields = dict(fields)
        if path == "/ai/save":
            request_fields["content"] = (
                "Card <CARD_SUFFIX>: <CURRENCY> <AMOUNT> at <MERCHANT>; date <DATE_DMY>."
            )
        path = path.format(snapshot=snapshot, proposal=proposal)

        def unrelated_page():
            probe_started.set()
            response = client.get("/imports")
            return response, release.is_set()

        with ThreadPoolExecutor(max_workers=3) as pool:
            maintenance = pool.submit(
                client.post, "/storage/compress", data=fields, follow_redirects=False
            )
            try:
                assert locked.wait(5), "Real VACUUM did not acquire an exclusive database lock"
                assert busy_codes == [sqlite3.SQLITE_BUSY]
                request = pool.submit(
                    client.request,
                    method,
                    path,
                    **({"data": request_fields} if method == "POST" else {}),
                    follow_redirects=False,
                )
                assert waiting.wait(5), "Request did not reach the locked database"
                assert not request.done() and not maintenance.done()
                assert len(waiters) == 1
                assert (waiters[0] == loop_thread) == on_loop
                probe = pool.submit(unrelated_page)
                assert probe_started.wait(5)
                if on_loop:
                    assert not probe.done()
                else:
                    response, after_release = probe.result(timeout=5)
                    assert response.status_code == 200 and not after_release
                    assert not request.done() and not maintenance.done()
            finally:
                release.set()
            assert maintenance.result(timeout=5).status_code == 303
            assert request.result(timeout=5).status_code == (303 if method == "POST" else 200)
            response, after_release = probe.result(timeout=5)
            assert response.status_code == 200 and after_release == on_loop

        assert vacuum_completed == [True]
        assert timeouts and set(timeouts) == {10}
        assert all(db.closed for db in connections)
        assert any(db.owner == loop_thread for db in connections) == on_loop
        assert app.state.storage.status()["mail_archived"] == 4
        assert ledger.load() == document and gmail.saved_range(month) == saved_mail
        assert len(ai.client.inputs) == 1
        assert ai.find_proposal(proposal)["status"] == "pending"
        assert len(ai.snapshots()) == (2 if path == "/ai/save" else 1)
