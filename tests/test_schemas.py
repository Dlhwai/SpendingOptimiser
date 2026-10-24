import json
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from spending_optimiser import (
    EffectivePeriod,
    Fact,
    Money,
    RuleRevision,
    SchemaDocument,
    Transaction,
)
from spending_optimiser.common import CalendarDate
from spending_optimiser.rules import (
    Cap,
    Condition,
    EarningRate,
    Period,
    Redemption,
    RewardQuantity,
    RewardUnit,
    Rounding,
    Valuation,
)

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.json"


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


def proposed(value):
    return {"status": "proposed", "value": value}


def test_document_round_trip_and_unknowns(payload):
    doc = SchemaDocument.model_validate(payload)
    assert SchemaDocument.model_validate_json(doc.model_dump_json()) == doc
    assert doc.transactions[0].occurred_on.value == date(2025, 1, 1)
    assert doc.transactions[0].posted_on.status == "unknown"
    assert doc.transactions[0].mcc.value is None
    assert doc.cards[0].approved_on.status == "unknown"
    assert all(p.variant.status == "unknown" for p in doc.products)
    assert doc.rules[0].eligibility.status == "conflicting"
    assert doc.rules[0].redemption.status == "unknown"
    rate = doc.rules[0].earning.value[0]
    assert rate.reward.value.quantity == Decimal("3")
    assert rate.reward.value.unit.kind == "points"
    assert rate.date_basis.value == "transaction_date"
    assert doc.rules[0].minimum_spend.value[0].date_basis.value == "posting_date"


def test_empty_document_and_schema():
    assert SchemaDocument.model_validate_json('{"schema_version":"1.0"}') == SchemaDocument()
    schema = SchemaDocument.model_json_schema()
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert schema["additionalProperties"] is False
    assert "SchemaDocument" == schema["title"]


@pytest.mark.parametrize(
    "currency,exponent,amount",
    [
        ("SGD", 2, 1),
        ("JPY", 0, 1),
        ("KRW", 0, 123),
        ("BHD", 3, 1001),
        ("KWD", 3, 999),
        ("USD", 2, 0),
        ("EUR", 2, 2**63 - 1),
        ("GBP", 2, 123),
    ],
)
def test_currency_minor_units(currency, exponent, amount):
    money = Money(currency=currency, minor_units=amount)
    assert money.exponent == exponent
    assert Money.model_validate_json(money.model_dump_json()) == money
    assert money.minor_units == amount


@pytest.mark.parametrize("amount", [-1, 2**63, 1.1, "100", True, None])
def test_invalid_minor_units(amount):
    with pytest.raises(ValidationError):
        Money(currency="SGD", minor_units=amount)


@pytest.mark.parametrize("currency", ["sgd", "XXX", "BTC", "", " SGD"])
def test_unsupported_currency(currency):
    with pytest.raises(ValidationError):
        Money(currency=currency, minor_units=1)


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity", 0.5, True])
def test_invalid_reward_quantity(quantity):
    with pytest.raises(ValidationError):
        RewardQuantity(unit=RewardUnit(unit_id="test", kind="points"), quantity=quantity)


def test_exact_decimal_and_separate_redemption():
    points = {"unit": {"unit_id": "test-points", "kind": "points"}, "quantity": "123.456"}
    miles = {"unit": {"unit_id": "test-miles", "kind": "miles"}, "quantity": "1"}
    redemption = Redemption(from_reward=proposed(points), to_reward=proposed(miles))
    assert redemption.from_reward.value.quantity == Decimal("123.456")
    assert '"123.456"' in redemption.model_dump_json()
    assert redemption.fee.status == "unknown"  # not a fabricated zero fee
    valuation = Valuation(
        reward=points,
        money={"currency": "SGD", "minor_units": 5},
        assumed_at="2025-01-01T00:00:00Z",
        rationale="Synthetic assumption",
    )
    assert valuation.money.minor_units == 5
    assert Redemption.model_validate_json(redemption.model_dump_json()) == redemption


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "cashback"},
        {"kind": "points", "currency": "SGD"},
        {"kind": "miles", "currency": "USD"},
    ],
)
def test_reward_unit_currency_consistency(data):
    with pytest.raises(ValidationError):
        RewardUnit(unit_id="test", **data)
    assert RewardUnit(unit_id="test", kind="cashback", currency="SGD").currency == "SGD"


@pytest.mark.parametrize(
    "fact",
    [
        {"status": "verified", "value": "x"},
        {"status": "unknown", "value": "x"},
        {"status": "proposed"},
        {"status": "not_applicable"},
        {"status": "conflicting", "alternatives": ["x"], "note": "conflict"},
        {"status": "conflicting", "alternatives": ["x", "x"], "note": "conflict"},
        {"status": "conflicting", "alternatives": ["x", "y"]},
        {"status": "proposed", "value": "x", "alternatives": ["y"]},
    ],
)
def test_invalid_fact_states(fact):
    with pytest.raises(ValidationError):
        Fact[str].model_validate(fact)


def test_unknown_not_applicable_and_known_empty_are_distinct():
    unknown = Fact[tuple[str, ...]]()
    none = Fact[tuple[str, ...]](status="not_applicable", note="Not offered")
    empty = Fact[tuple[str, ...]](status="proposed", value=())
    assert unknown.value is None and none.value is None and empty.value == ()
    assert len({unknown.status, none.status, empty.status}) == 3


@pytest.mark.parametrize(
    "value",
    [
        1735689600,
        "2025-01-01T00:00:00Z",
        "20250101",
        "2025-02-30",
        datetime(2025, 1, 1),
        True,
    ],
)
def test_no_timestamp_coercion_to_event_dates(value):
    with pytest.raises(ValidationError):
        TypeAdapter(CalendarDate).validate_python(value)


@pytest.mark.parametrize("start,end", [("2025-01-01", "2025-01-01"), ("2025-02-01", "2025-01-01")])
def test_invalid_effective_period(start, end):
    with pytest.raises(ValidationError):
        EffectivePeriod(start=start, end=end)


def test_effective_period_does_not_invent_boundaries():
    assert EffectivePeriod().start is None
    assert EffectivePeriod(end="2025-01-01").start is None


@pytest.mark.parametrize("kind", ["purchase", "refund", "reversal", "income", "transfer"])
def test_all_event_kinds_and_transfer_identities(payload, kind):
    txn = payload["transactions"][0]
    txn["kind"] = proposed(kind)
    if kind in {"refund", "reversal"}:
        original = deepcopy(txn)
        original.update(event_id="original", revision_id="original-v1")
        original["origins"][0]["event_key"] = "original"
        payload["transactions"].append(original)
        txn["related_event_id"] = proposed("original")
    if kind == "income":
        txn["income_kind"] = proposed("salary")
    if kind == "transfer":
        txn["channel"] = proposed("paynow")
        txn["transfer_purpose"] = proposed("card_repayment")
        txn["counterparty_account_id"] = proposed("test-bank-account")
    doc = SchemaDocument.model_validate(payload)
    assert doc.transactions[0].kind.value == kind
    assert doc.transactions[0].amount.value.minor_units == 1234  # unsigned for every kind


@pytest.mark.parametrize("channel,direction", [("paynow", "inflow"), ("paylah", "outflow")])
def test_transfer_direction_is_not_invented_from_unsigned_amount(payload, channel, direction):
    txn = payload["transactions"][0]
    txn["kind"] = proposed("transfer")
    txn["direction"] = proposed(direction)
    txn["channel"] = proposed(channel)
    txn["transfer_purpose"] = proposed("external")
    doc = SchemaDocument.model_validate(payload)
    assert doc.transactions[0].direction.value == direction
    assert doc.transactions[0].channel.value == channel
    assert doc.transactions[0].amount.value.minor_units == 1234
    assert doc.transactions[0].counterparty_account_id.status == "unknown"


def test_incomplete_notification_is_reviewable(payload):
    txn = payload["transactions"][0]
    for field in ("kind", "amount", "occurred_on", "account_id", "card_id"):
        txn.pop(field)
    assert SchemaDocument.model_validate(payload).transactions[0].review.status == "pending"
    txn["review"] = {
        "status": "accepted",
        "reviewer": "test-reviewer",
        "reviewed_at": "2025-01-02T12:00:00Z",
        "reason": "Synthetic review",
    }
    with pytest.raises(ValidationError, match="accepted event needs"):
        SchemaDocument.model_validate(payload)


def test_reprocessing_corrections_and_duplicates(payload):
    original = payload["transactions"][0]
    corrected = deepcopy(original)
    corrected.update(
        revision_id="test-purchase-v2",
        supersedes="test-purchase-v1",
        recorded_at="2025-01-02T13:00:00+08:00",
    )
    corrected["amount"] = proposed({"currency": "SGD", "minor_units": 1200})
    corrected["origins"][0]["processing_run_id"] = "test-run-2"
    corrected["corrections"] = [
        {
            "field": "amount",
            "reason": "Synthetic correction",
            "actor": "test-reviewer",
            "corrected_at": "2025-01-02T12:00:00+08:00",
        }
    ]
    duplicate = deepcopy(original)
    duplicate.update(
        event_id="test-duplicate", revision_id="test-duplicate-v1", duplicate_of="test-purchase"
    )
    payload["transactions"].extend([corrected, duplicate])
    doc = SchemaDocument.model_validate(payload)
    assert len(doc.transactions) == 3
    assert doc.transactions[0].amount.value.minor_units == 1234
    assert doc.transactions[1].amount.value.minor_units == 1200
    assert doc.transactions[1].event_id == doc.transactions[0].event_id
    assert doc.transactions[2].duplicate_of == doc.transactions[0].event_id


def test_source_and_rule_history(payload):
    evidence = deepcopy(payload["evidence"][0])
    evidence.update(
        revision_id="test-evidence-v2",
        supersedes="test-evidence-v1",
        retrieved_at="2025-02-01T00:00:00Z",
    )
    evidence["effective"] = proposed({"start": "2025-02-01", "end": None})
    rule = deepcopy(payload["rules"][0])
    rule.update(
        revision_id="test-rules-v2",
        supersedes="test-rules-v1",
        compiled_at="2025-02-01T01:00:00Z",
        effective=evidence["effective"],
    )
    payload["evidence"].append(evidence)
    payload["rules"].append(rule)
    doc = SchemaDocument.model_validate(payload)
    assert len(doc.rules) == len(doc.evidence) == 2
    assert doc.schema_version == "1.0"  # history is not a schema migration
    assert doc.rules[0].effective.value.start < doc.rules[1].effective.value.start
    # Retroactive and future-effective terms are both representable, not selected by retrieval time.
    assert doc.rules[0].effective.value.start < doc.evidence[0].retrieved_at.date()
    rule["effective"] = proposed({"start": "2027-01-01"})
    assert SchemaDocument.model_validate(payload).rules[1].effective.value.start == date(2027, 1, 1)


@pytest.mark.parametrize(
    "collection,id_field",
    [
        ("products", "product_id"),
        ("cards", "card_id"),
        ("accounts", "account_id"),
        ("observations", "observation_id"),
        ("evidence", "revision_id"),
        ("rules", "revision_id"),
        ("transactions", "revision_id"),
    ],
)
def test_duplicate_ids(payload, collection, id_field):
    payload[collection].append(deepcopy(payload[collection][0]))
    with pytest.raises(ValidationError, match=f"duplicate {id_field}"):
        SchemaDocument.model_validate(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version="2.0"),
        lambda p: p.update(unrecognized=True),
        lambda p: p["rules"][0].update(product_id="missing"),
        lambda p: p["cards"][0].update(product_id="missing"),
        lambda p: p["cards"][0].update(account_id="test-bank-account"),
        lambda p: p["transactions"][0]["origins"][0].update(observation_id="missing"),
        lambda p: p["transactions"][0].update(account_id=proposed("missing")),
        lambda p: p["transactions"][0].update(card_id=proposed("missing")),
        lambda p: p["transactions"][0].update(account_id=proposed("test-bank-account")),
        lambda p: p["transactions"][0].update(duplicate_of="missing"),
        lambda p: p["transactions"][0].update(duplicate_of="test-purchase"),
        lambda p: p["transactions"][0].update(related_event_id=proposed("missing")),
        lambda p: p["transactions"][0].update(related_event_id=proposed("test-purchase")),
        lambda p: p["transactions"][0].update(
            amount=proposed({"currency": "SGD", "minor_units": 0})
        ),
        lambda p: p["transactions"][0].update(mcc=proposed("123")),
        lambda p: p["transactions"][0].update(transfer_purpose=proposed("own_accounts")),
        lambda p: p["transactions"][0].update(recorded_at="2025-01-01T00:00:00Z"),
        lambda p: p["transactions"][0].update(recorded_at="2025-01-03T00:00:00"),
        lambda p: p["evidence"][0].update(url="file:///tmp/private"),
        lambda p: p["evidence"][0].update(content_sha256="bad"),
        lambda p: p["rules"][0].update(compiled_at="2025-01-01T00:00:00Z"),
        lambda p: p["rules"][0].update(supersedes="missing"),
        lambda p: p["rules"][0].update(supersedes="test-rules-v1"),
        lambda p: p["transactions"][0].update(supersedes="missing"),
        lambda p: p["evidence"][0].update(supersedes="missing"),
        lambda p: p["rules"][0]["caps"]["value"][0].update(applies_to=proposed(["missing"])),
        lambda p: p["rules"][0]["minimum_spend"]["value"][0].update(
            applies_to=proposed(["missing"])
        ),
        lambda p: p["transactions"][0]["amount"]["evidence"][0].update(
            evidence_revision_id="missing"
        ),
    ],
)
def test_invalid_document_relationships(payload, mutation):
    mutation(payload)
    with pytest.raises(ValidationError):
        SchemaDocument.model_validate(payload)


@pytest.mark.parametrize(
    "collection,identity,timestamp",
    [
        ("evidence", "source_id", "retrieved_at"),
        ("rules", "rule_id", "compiled_at"),
        ("transactions", "event_id", "recorded_at"),
    ],
)
def test_history_cannot_change_identity(payload, collection, identity, timestamp):
    revised = deepcopy(payload[collection][0])
    revised.update(revision_id="new-revision", supersedes=revised["revision_id"])
    revised[identity] = "wrong-identity"
    revised[timestamp] = "2025-03-01T00:00:00Z"
    payload[collection].append(revised)
    with pytest.raises(ValidationError, match="different identity"):
        SchemaDocument.model_validate(payload)


def test_duplicate_cycle_and_unmarked_source_collision(payload):
    duplicate = deepcopy(payload["transactions"][0])
    duplicate.update(event_id="other", revision_id="other-v1")
    payload["transactions"].append(duplicate)
    with pytest.raises(ValidationError, match="shared source event"):
        SchemaDocument.model_validate(payload)
    payload["transactions"][0]["duplicate_of"] = "other"
    duplicate["duplicate_of"] = "test-purchase"
    with pytest.raises(ValidationError, match="cycle"):
        SchemaDocument.model_validate(payload)


def test_public_rules_cannot_cite_private_observations(payload):
    ref = payload["rules"][0]["earning"]["value"][0]["reward"]["evidence"][0]
    ref.update(target="observation", evidence_revision_id="test-observation")
    with pytest.raises(ValidationError, match="public records"):
        SchemaDocument.model_validate(payload)


@pytest.mark.parametrize("record", ["products", "evidence"])
def test_public_products_and_evidence_cannot_cite_private_observations(payload, record):
    private = {"target": "observation", "evidence_revision_id": "test-observation", "locator": "x"}
    fact = {**proposed("Synthetic"), "evidence": [private]}
    if record == "products":
        payload["products"][-1]["issuer"] = fact
    else:
        payload["evidence"][0]["effective"]["evidence"] = [private]
    with pytest.raises(ValidationError, match="public records"):
        SchemaDocument.model_validate(payload)


@pytest.mark.parametrize(
    "field,alternatives",
    [("transfer_purpose", ["own_accounts", "external"]), ("income_kind", ["salary", "other"])],
)
def test_kind_gated_facts_reject_conflicting_alternatives(payload, field, alternatives):
    payload["transactions"][0][field] = {
        "status": "conflicting",
        "alternatives": alternatives,
        "note": "unclear",
    }
    with pytest.raises(ValidationError, match="only applies"):
        SchemaDocument.model_validate(payload)


@pytest.mark.parametrize(
    "basis,value",
    [
        ("reward", {"currency": "SGD", "minor_units": 1}),
        ("eligible_spend", {"unit": {"unit_id": "p", "kind": "points"}, "quantity": "1"}),
    ],
)
def test_cap_basis_is_separate_from_spend(basis, value):
    with pytest.raises(ValidationError):
        Cap(cap_id="cap", basis=basis, limit=proposed(value), applies_to=proposed(["rate"]))


def test_structural_rule_constraints():
    with pytest.raises(ValidationError):
        Period(kind="calendar_month", timezone="Not/AZone")
    with pytest.raises(ValidationError):
        Condition(dimension="channel", operator="equals", operands=("a", "b"), description="test")
    with pytest.raises(ValidationError):
        EarningRate(
            rate_id="r", component="base", per_spend=proposed({"currency": "SGD", "minor_units": 0})
        )
    rounding = Rounding(stage="spend", mode="floor", increment="5", scope="transaction")
    assert rounding.increment == Decimal(5)
    rule = RuleRevision(
        rule_id="rules", revision_id="r", product_id="p", compiled_at="2025-01-01T00:00:00Z"
    )
    assert rule.earning.status == "unknown"


def test_conflicting_effective_dates_and_amounts(payload):
    payload["rules"][0]["effective"] = {
        "status": "conflicting",
        "note": "Synthetic conflicting commencement dates",
        "alternatives": [{"start": "2025-01-01"}, {"start": "2025-02-01"}],
    }
    payload["transactions"][0]["amount"] = {
        "status": "conflicting",
        "note": "Synthetic ambiguous currency",
        "alternatives": [
            {"currency": "SGD", "minor_units": 1234},
            {"currency": "USD", "minor_units": 1234},
        ],
    }
    doc = SchemaDocument.model_validate(payload)
    assert doc.rules[0].effective.value is None
    assert doc.transactions[0].amount.value is None
    payload["transactions"][0]["amount"]["alternatives"][0]["minor_units"] = 0
    with pytest.raises(ValidationError, match="event amount must be positive"):
        SchemaDocument.model_validate(payload)


def test_known_threshold_does_not_require_inventing_unknown_earning_terms(payload):
    rule = payload["rules"][0]
    rule.pop("earning")
    rule.pop("caps")
    rule["minimum_spend"]["value"][0].pop("applies_to")
    doc = SchemaDocument.model_validate(payload)
    minimum = doc.rules[0].minimum_spend.value[0]
    assert minimum.threshold.value.minor_units == 50000
    assert minimum.applies_to.status == doc.rules[0].earning.status == "unknown"


def test_duplicate_provider_identity_and_future_correction(payload):
    observation = deepcopy(payload["observations"][0])
    observation["observation_id"] = "same-provider-message"
    payload["observations"].append(observation)
    with pytest.raises(ValidationError, match="one observation identity"):
        SchemaDocument.model_validate(payload)
    payload["observations"].pop()
    payload["transactions"][0]["corrections"] = [
        {
            "field": "amount",
            "actor": "test",
            "reason": "Synthetic",
            "corrected_at": "2026-01-01T00:00:00Z",
        }
    ]
    with pytest.raises(ValidationError, match="correction cannot occur after"):
        SchemaDocument.model_validate(payload)


def test_history_must_not_fork_or_have_multiple_roots(payload):
    revised = deepcopy(payload["rules"][0])
    revised.update(revision_id="second", compiled_at="2025-02-01T00:00:00Z")
    payload["rules"].append(revised)
    with pytest.raises(ValidationError, match="one root"):
        SchemaDocument.model_validate(payload)
    revised["supersedes"] = "test-rules-v1"
    fork = deepcopy(revised)
    fork.update(revision_id="third", compiled_at="2025-03-01T00:00:00Z")
    payload["rules"].append(fork)
    with pytest.raises(ValidationError, match="must not fork"):
        SchemaDocument.model_validate(payload)


def test_frozen_model_rejects_mutation(payload):
    transaction = Transaction.model_validate(payload["transactions"][0])
    with pytest.raises(ValidationError, match="frozen"):
        transaction.revision_id = "overwrite"
