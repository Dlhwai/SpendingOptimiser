"""All financial material here is synthetic. No network/services are used."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from spending_optimiser import SchemaDocument
from spending_optimiser.ledger import Conflict, Ledger, heads, needs_review
from spending_optimiser.web import MAX_BODY, create_app

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.json"


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "private" / "ledger.sqlite3")


def proposed(value):
    return {"status": "proposed", "value": value}


def correct(ledger, revision="test-purchase-v1", **kwargs):
    return ledger.review(
        revision,
        actor="Synthetic reviewer",
        reason="Synthetic correction",
        status="corrected",
        field="merchant",
        fact_json=json.dumps(proposed("Synthetic cafe")),
        **kwargs,
    )


def successor(payload, revision="import-v2"):
    revised = deepcopy(payload["transactions"][0])
    revised.update(
        revision_id=revision,
        supersedes="test-purchase-v1",
        recorded_at="2025-02-01T00:00:00Z",
    )
    revised["merchant"] = proposed("Synthetic new merchant")
    payload["transactions"].append(revised)
    return payload


def test_import_restart_idempotence_and_separation(ledger, payload):
    result = ledger.import_document(json.dumps(payload))
    assert result == {"added": 11, "unchanged": 0}
    original = SchemaDocument.model_validate(payload)
    assert Ledger(ledger.path).load() == original
    assert ledger.path.stat().st_mode & 0o777 == 0o600
    assert ledger.import_document(json.dumps(payload)) == {"added": 0, "unchanged": 11}
    with ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM imports").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM private_records").fetchone()[0] == 5
        assert db.execute("SELECT count(*) FROM public_records").fetchone()[0] == 6
        saved = db.execute("SELECT document FROM imports").fetchone()[0]
    assert SchemaDocument.model_validate_json(saved) == original


def test_import_history_and_evidence_links(ledger, payload):
    ledger.import_document(json.dumps(payload))
    successor(payload)
    ledger.import_document(json.dumps(payload))
    doc = ledger.load()
    assert len(doc.transactions) == 2
    assert heads(doc)[0].revision_id == "import-v2"
    fact = heads(doc)[0].amount
    assert fact.evidence[0].evidence_revision_id == doc.observations[0].observation_id
    assert fact.evidence[0].target == "observation"
    assert doc.rules[0].earning.value[0].reward.evidence[0].target == "source_evidence"


@pytest.mark.parametrize("collection", ["transactions", "accounts", "evidence", "observations"])
def test_conflict_rolls_back_every_collection(ledger, payload, collection):
    ledger.import_document(json.dumps(payload))
    before = ledger.load()
    payload["products"].append({"product_id": "must-not-save", "supplied_name": "Synthetic"})
    if collection == "accounts":
        payload[collection][0]["label"] = "Different synthetic label"
    elif collection == "transactions":
        payload[collection][0]["merchant"] = proposed("Different synthetic merchant")
    elif collection == "observations":
        payload[collection][0]["source_record_id"] = "changed-source-id"
    else:
        payload[collection][0]["document_version"] = "changed-version"
    with pytest.raises(Conflict, match="Immutable identity"):
        ledger.import_document(json.dumps(payload))
    assert ledger.load() == before
    with ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM imports").fetchone()[0] == 1


def test_cross_document_source_collision_and_fork_are_atomic(ledger, payload):
    ledger.import_document(json.dumps(successor(deepcopy(payload))))
    before = ledger.load()
    successor(payload, "fork-v2")
    with pytest.raises(Conflict, match="history"):
        ledger.import_document(json.dumps(payload))
    assert ledger.load() == before
    payload["transactions"] = []
    payload["observations"][0]["observation_id"] = "alias-for-existing-source"
    with pytest.raises(Conflict):
        ledger.import_document(json.dumps(payload))
    assert ledger.load() == before


def test_invalid_import_and_failed_sql_write_roll_back(ledger, payload):
    invalid = deepcopy(payload)
    invalid["transactions"][0]["amount"] = proposed({"currency": "SGD", "minor_units": 0})
    with pytest.raises(ValidationError):
        ledger.import_document(json.dumps(invalid))
    assert ledger.load() == SchemaDocument()
    # Force a failure after public rows have been inserted, not just prevalidation.
    with ledger.connection() as db:
        db.execute(
            "CREATE TRIGGER synthetic_failure BEFORE INSERT ON private_records "
            "BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        ledger.import_document(json.dumps(payload))
    assert ledger.load() == SchemaDocument()
    with ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM imports").fetchone()[0] == 0


def test_manual_correction_review_history_and_reimport(ledger, payload):
    ledger.import_document(json.dumps(payload))
    original = ledger.load().transactions[0]
    corrected = correct(ledger)
    assert corrected.supersedes == original.revision_id
    assert corrected.origins == original.origins
    assert corrected.amount.evidence == original.amount.evidence
    assert corrected.corrections[0].field == "merchant"
    assert corrected.merchant.review.status == "corrected"
    assert needs_review(corrected)
    accepted = ledger.review(
        corrected.revision_id,
        actor="Synthetic reviewer",
        reason="Checked synthetic source",
        status="accepted",
    )
    assert accepted.corrections == corrected.corrections
    assert not needs_review(accepted)
    assert accepted.posted_on.status == "unknown"  # no inferred facts
    assert ledger.load().transactions[0] == original
    ledger.import_document(json.dumps(payload))
    assert heads(Ledger(ledger.path).load()) == [accepted]
    assert len(ledger.load().transactions) == 3
    successor(payload)
    with pytest.raises(Conflict, match="locally reviewed"):
        ledger.import_document(json.dumps(payload))
    assert heads(ledger.load()) == [accepted]
    with pytest.raises(Conflict, match="stale"):
        correct(ledger)


@pytest.mark.parametrize("field", ["kind", "amount", "occurred_on", "account_id"])
def test_missing_required_cannot_accept(ledger, payload, field):
    payload["transactions"][0].pop(field)
    ledger.import_document(json.dumps(payload))
    with pytest.raises(ValidationError, match="accepted event needs"):
        ledger.review("test-purchase-v1", actor="Synthetic", reason="Synthetic", status="accepted")
    assert len(ledger.load().transactions) == 1


def test_contradiction_cannot_accept_until_corrected(ledger, payload):
    payload["transactions"][0]["merchant"] = {
        "status": "conflicting",
        "alternatives": ["Synthetic A", "Synthetic B"],
        "note": "Ambiguous",
    }
    ledger.import_document(json.dumps(payload))
    with pytest.raises(ValueError, match="conflicting"):
        ledger.review("test-purchase-v1", actor="Synthetic", reason="Synthetic", status="accepted")
    fixed = correct(ledger)
    accepted = ledger.review(
        fixed.revision_id, actor="Synthetic", reason="Synthetic", status="accepted"
    )
    assert not needs_review(accepted)


def test_rejection_with_conflicts_leaves_queue_and_survives_reimport(ledger, payload):
    payload["transactions"][0]["merchant"] = {
        "status": "conflicting",
        "alternatives": ["Synthetic A", "Synthetic B"],
        "note": "Ambiguous",
    }
    payload["transactions"][0]["amount"] = {
        "status": "conflicting",
        "alternatives": [
            {"currency": "SGD", "minor_units": 100},
            {"currency": "SGD", "minor_units": 200},
        ],
        "note": "Ambiguous",
    }
    ledger.import_document(json.dumps(payload))
    original = ledger.load().transactions[0]
    assert needs_review(original)
    rejected = ledger.review(
        "test-purchase-v1", actor="Synthetic", reason="Not mine", status="rejected"
    )
    assert rejected.review.status == "rejected"
    assert not needs_review(rejected)
    assert rejected.merchant == original.merchant
    assert rejected.amount == original.amount
    assert ledger.import_document(json.dumps(payload)) == {"added": 0, "unchanged": 11}
    document = Ledger(ledger.path).load()
    assert heads(document) == [rejected]
    assert document.transactions[0] == original
    with pytest.raises(ValueError, match="accepted event needs"):
        ledger.review(rejected.revision_id, actor="Synthetic", reason="x", status="accepted")
    assert heads(ledger.load()) == [rejected]
    page = client_for(ledger.path).get("/review").text
    assert "0 results" in page and "0 need review" in page


def test_rejection_does_not_alter_corrected_fact(ledger, payload):
    ledger.import_document(json.dumps(payload))
    corrected = correct(ledger)
    with pytest.raises(ValueError, match="whole event"):
        ledger.review(
            corrected.revision_id,
            actor="Synthetic",
            reason="Synthetic",
            status="rejected",
            field="merchant",
            fact_json=json.dumps(proposed("Synthetic other")),
        )
    rejected = ledger.review(
        corrected.revision_id, actor="Synthetic", reason="Synthetic", status="rejected"
    )
    assert rejected.merchant == corrected.merchant
    assert rejected.merchant.review.status == "corrected"
    assert rejected.corrections == corrected.corrections
    assert not needs_review(rejected)
    reopened = ledger.review(
        rejected.revision_id, actor="Synthetic", reason="Reconsidered", status="accepted"
    )
    assert reopened.merchant == corrected.merchant
    assert [t.review.status for t in ledger.load().transactions] == [
        "pending",
        "corrected",
        "rejected",
        "accepted",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"status": "corrected"},
        {"status": "anything"},
        {"status": "accepted", "actor": " "},
        {"status": "accepted", "reason": " "},
        {"status": "accepted", "fact_json": "{}"},
        {"status": "accepted", "field": "origins", "fact_json": "{}"},
        {
            "status": "corrected",
            "field": "amount",
            "fact_json": '{"status":"proposed","value":{"currency":"SGD","minor_units":0}}',
        },
        {
            "status": "corrected",
            "field": "merchant",
            "fact_json": '{"status":"verified","value":"x"}',
        },
        {"status": "corrected", "field": "merchant", "fact_json": '{"status":"unknown"}'},
        {"status": "corrected", "field": "merchant", "fact_json": "[]"},
        {
            "status": "corrected",
            "field": "merchant",
            "fact_json": (
                '{"status":"proposed","value":"x","evidence":[{"target":"observation",'
                '"evidence_revision_id":"missing","locator":"x"}]}'
            ),
        },
    ],
)
def test_bad_review_is_atomic(ledger, payload, change):
    ledger.import_document(json.dumps(payload))
    before = ledger.load()
    kwargs = {"actor": "Synthetic", "reason": "Synthetic", **change}
    with pytest.raises(ValueError):
        ledger.review("test-purchase-v1", **kwargs)
    assert ledger.load() == before


def client_for(path):
    return TestClient(create_app(path), base_url="http://127.0.0.1:8765")


def csrf(client):
    response = client.get("/")
    assert response.status_code == 200
    return {"csrf": client.cookies["ledger_csrf"]}


def test_browser_import_review_restart_and_public_privacy(tmp_path, payload):
    path = tmp_path / "ledger.sqlite3"
    client = client_for(path)
    data = csrf(client)
    payload["transactions"][0]["amount"]["evidence"][0]["quote"] = "Synthetic private-only quote"
    response = client.post(
        "/import", data=data, files={"file": ("synthetic.json", json.dumps(payload))}
    )
    assert response.status_code == 200
    assert "Import saved: 11 new records" in response.text
    assert "test-purchase" in client.get("/review").text
    detail = client.get("/transaction", params={"event_id": "test-purchase"})
    assert "Synthetic private-only quote" in detail.text
    assert "test-observation" in detail.text
    assert "Synthetic private-only quote" not in client.get("/public").text
    assert "test-observation" not in client.get("/public").text
    response = client.post(
        "/review",
        data={
            **data,
            "revision_id": "test-purchase-v1",
            "actor": "Synthetic",
            "reason": "Synthetic review",
            "status": "accepted",
            "field": "merchant",
            "fact_json": json.dumps(proposed("Synthetic cafe")),
        },
    )
    assert response.status_code == 200
    assert "Synthetic cafe" in response.text
    assert "2 revisions preserved" in response.text
    assert "0 results" in client.get("/review").text
    assert (
        client.post(
            "/review",
            data={
                **data,
                "revision_id": "test-purchase-v1",
                "actor": "Synthetic",
                "reason": "Synthetic",
                "status": "accepted",
            },
        ).status_code
        == 409
    )
    restarted = client_for(path)
    new_data = csrf(restarted)
    assert "test-purchase" in restarted.get("/?month=2025-01").text
    assert "Synthetic cafe" in restarted.get("/transaction?event_id=test-purchase").text
    response = restarted.post("/import", data={**new_data, "document": json.dumps(payload)})
    assert "0 new records" in response.text
    assert "Synthetic cafe" in restarted.get("/transaction?event_id=test-purchase").text
    assert len(restarted.app.state.ledger.load().transactions) == 2


def test_bundled_example_and_empty_states(tmp_path):
    client = client_for(tmp_path / "ledger.sqlite3")
    data = csrf(client)
    assert "0 results" in client.get("/").text
    assert client.post("/example", data=data).status_code == 200
    assert client.post("/example", data=data).status_code == 200
    assert len(client.app.state.ledger.load().transactions) == 1
    bundled = files("spending_optimiser").joinpath("examples/synthetic.json").read_text()
    assert SchemaDocument.model_validate_json(bundled) == SchemaDocument.model_validate_json(
        FIXTURE.read_text()
    )


def test_csrf_host_origin_and_untrusted_rendering(tmp_path, payload):
    client = client_for(tmp_path / "ledger.sqlite3")
    data = csrf(client)
    assert (
        client.post(
            "/example", data={}, headers={"Content-Type": "application/x-www-form-urlencoded"}
        ).status_code
        == 403
    )
    assert client.post("/example", data={"csrf": "☃"}).status_code == 403
    assert (
        client.post("/example", data=data, headers={"Origin": "https://evil.invalid"}).status_code
        == 403
    )
    assert (
        client.post("/example", data=data, headers={"Sec-Fetch-Site": "cross-site"}).status_code
        == 403
    )
    assert client.get("/", headers={"Host": "evil.invalid"}).status_code == 400
    payload["transactions"][0]["merchant"] = proposed('<script>alert("synthetic")</script>')
    payload["transactions"][0]["amount"]["evidence"][0]["quote"] = "<img src=x onerror=alert(1)>"
    assert client.post("/import", data={**data, "document": json.dumps(payload)}).status_code == 200
    response = client.get("/transaction?event_id=test-purchase")
    assert "<script>" not in response.text and "<img src=x" not in response.text
    assert "&lt;script&gt;" in response.text and "&lt;img" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "same-origin"
    assert (
        client.post(
            "/import",
            data={**data, "document": json.dumps(payload)},
            headers={"Origin": "http://127.0.0.1:8765"},
        ).status_code
        == 200
    )
    assert client.post("/example", data=data, headers={"Origin": "null"}).status_code == 403
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


@pytest.mark.parametrize(
    "document", ["", "not json", "{", '{"schema_version":"9"}', '{"a":1,"a":2}', "[" * 2000]
)
def test_invalid_browser_import(tmp_path, document):
    client = client_for(tmp_path / "ledger.sqlite3")
    data = csrf(client)
    response = client.post("/import", data={**data, "document": document})
    assert response.status_code == 422
    assert client.app.state.ledger.load() == SchemaDocument()


def test_form_limits_and_file_errors(tmp_path):
    client = client_for(tmp_path / "ledger.sqlite3")
    data = csrf(client)
    assert client.post("/example", json=data).status_code == 415
    assert client.post("/example", data={**data, "unknown": "bad"}).status_code == 422
    assert (
        client.post(
            "/example",
            content=f"csrf={data['csrf']}&csrf={data['csrf']}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/import", data={**data, "document": "{}"}, files={"file": ("x.json", "{}")}
        ).status_code
        == 422
    )
    assert client.post("/import", data=data, files={"file": ("x.json", b"\xff")}).status_code == 422
    assert client.post("/import", data={**data, "file": "not a file"}).status_code == 422
    assert client.post("/import", data={**data, "document": "x" * MAX_BODY}).status_code == 413
    assert client.get("/transaction?event_id=missing").status_code == 404
    assert client.get("/transaction").status_code == 422
    assert client.get("/docs").status_code == 404
    assert client.app.state.ledger.load() == SchemaDocument()


def test_browser_conflict_and_sql_failure(tmp_path, payload):
    client = client_for(tmp_path / "ledger.sqlite3")
    data = csrf(client)
    assert client.post("/example", data=data).status_code == 200
    payload["accounts"][0]["label"] = "Changed synthetic account"
    response = client.post("/import", data={**data, "document": json.dumps(payload)})
    assert response.status_code == 409
    assert "Request not saved" in response.text
    with client.app.state.ledger.connection() as db:
        db.execute(
            "CREATE TRIGGER synthetic_failure BEFORE INSERT ON private_records "
            "BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
        )
    response = client.post(
        "/review",
        data={
            **data,
            "revision_id": "test-purchase-v1",
            "actor": "Synthetic",
            "reason": "Synthetic",
            "status": "accepted",
        },
    )
    assert response.status_code == 503
    assert len(client.app.state.ledger.load().transactions) == 1


def test_concurrent_imports_and_reviews_are_serialized(ledger, payload):
    text = json.dumps(payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ledger.import_document, [text, text]))
    assert sorted(r["added"] for r in results) == [0, 11]
    assert len(ledger.load().transactions) == 1

    def attempt(_):
        try:
            return correct(ledger)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(ledger.load().transactions) == 2


def test_import_cannot_extend_manual_head_even_with_complete_history(ledger, payload):
    ledger.import_document(json.dumps(payload))
    manual = correct(ledger)
    incoming = ledger.load().model_dump(mode="json")
    future = manual.model_dump(mode="json")
    future.update(
        revision_id="import-after-manual",
        supersedes=manual.revision_id,
        recorded_at="2099-01-01T00:00:00Z",
    )
    incoming["transactions"].append(future)
    SchemaDocument.model_validate(incoming)  # valid schema, disallowed local replay policy
    with pytest.raises(Conflict, match="locally reviewed"):
        ledger.import_document(json.dumps(incoming))
    assert heads(ledger.load()) == [manual]


def test_cli_loopback_and_configuration(tmp_path, monkeypatch, application):
    from spending_optimiser import web

    calls = []
    monkeypatch.setattr(web.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    database = tmp_path / "env.sqlite3"
    monkeypatch.setenv("SPENDING_OPTIMISER_DB", str(database))
    monkeypatch.setattr("sys.argv", ["spending-optimiser", "--port", "8766"])
    web.main()
    assert calls[-1][0].state.ledger.path == database.resolve()
    assert calls[-1][1] == {
        "host": "127.0.0.1",
        "port": 8766,
        "proxy_headers": False,
        "access_log": False,
    }
    override = tmp_path / "override.sqlite3"
    monkeypatch.setattr("sys.argv", ["spending-optimiser", "--database", str(override)])
    web.main()
    assert calls[-1][0].state.ledger.path == override.resolve()
    for flags in (["--unknown"], ["--port", "0"], ["--port", "65536"], ["--env-file", "x"]):
        monkeypatch.setattr("sys.argv", ["spending-optimiser", *flags])
        with pytest.raises(SystemExit) as error:
            web.main()
        assert error.value.code == 2


def test_future_review_timestamp_is_explicit_and_atomic(ledger, payload):
    payload["transactions"][0]["recorded_at"] = "9999-12-31T23:59:59.999999Z"
    ledger.import_document(json.dumps(payload))
    with pytest.raises(Conflict, match="local clock"):
        correct(ledger)
    assert len(ledger.load().transactions) == 1


def test_deep_duplicate_graph_is_rejected_without_partial_import(ledger, payload):
    original = payload["transactions"][0]
    payload["transactions"] = []
    for i in range(1100):
        transaction = deepcopy(original)
        transaction.update(event_id=f"event-{i}", revision_id=f"revision-{i}")
        transaction["origins"][0]["event_key"] = f"source-event-{i}"
        if i < 1099:
            transaction["duplicate_of"] = f"event-{i + 1}"
        payload["transactions"].append(transaction)
    with pytest.raises(ValueError, match="too deeply nested"):
        ledger.import_document(json.dumps(payload))
    assert ledger.load() == SchemaDocument()
