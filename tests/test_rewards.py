"""Estimated card rewards: only complete terms and known facts produce a figure.

Invented products, rules and purchases in an in-memory document. No bank terms are implied.
"""

from copy import deepcopy
from decimal import Decimal

import pytest

from spending_optimiser.document import SchemaDocument
from spending_optimiser.monthly import calendar_month, date_range, range_report
from spending_optimiser.rewards import card_rewards

CARD = "card-account-a"
SOURCES = {CARD: {"kind": "card", "label": "Example card ending 0001"}}
FEBRUARY = calendar_month("2025-02")
NONE = {"status": "not_applicable", "note": "Terms state none"}
POINTS = {"unit_id": "example-points", "kind": "points"}


def fact(value, status="proposed"):
    return {"status": status, "value": value}


def rate(rate_id="base", points="2", per="100", eligibility=None, currency="SGD"):
    return {
        "rate_id": rate_id,
        "component": "base",
        "reward": fact({"unit": POINTS, "quantity": points}),
        "per_spend": fact({"currency": currency, "minor_units": int(per)}),
        "eligibility": eligibility or NONE,
    }


def rule(**overrides):
    base = {
        "rule_id": "example-rule",
        "revision_id": "example-rule-v1",
        "product_id": "example-product",
        "compiled_at": "2025-01-02T12:00:00+08:00",
        "effective": fact({"start": "2025-01-01", "end": "2026-01-01"}),
        "date_basis": fact("transaction_date"),
        "eligibility": NONE,
        "minimum_spend": NONE,
        "caps": NONE,
        "exclusions": NONE,
        "rounding": NONE,
        "earning": fact([rate()]),
    }
    return {**base, **overrides}


def purchase(
    number, day, minor, kind="purchase", merchant=None, mcc=None, account=CARD, currency="SGD"
):
    event = f"event-{number}"
    transaction = {
        "event_id": event,
        "revision_id": f"{event}-v1",
        "recorded_at": "2025-03-01T10:00:00+08:00",
        "origins": [
            {
                "observation_id": f"obs-{number}",
                "event_key": "primary",
                "extractor_revision": "example",
                "processing_run_id": "run",
            }
        ],
        "kind": fact(kind),
        "amount": fact({"currency": currency, "minor_units": minor}),
        "occurred_on": fact(day),
        "account_id": fact(account),
        "review": {
            "status": "accepted",
            "reviewer": "test",
            "reviewed_at": "2025-03-01T10:00:00+08:00",
            "reason": "synthetic",
        },
    }
    if merchant:
        transaction["merchant"] = fact(merchant)
    if mcc:
        transaction["mcc"] = fact(mcc)
    return transaction


def document(rules=(), transactions=()):
    data = {
        "products": [{"product_id": "example-product", "supplied_name": "Example Card"}],
        "accounts": [
            {"account_id": CARD, "kind": "other", "label": "Example account"},
            {"account_id": "wallet-account", "kind": "wallet", "label": "Wallet"},
        ],
        "evidence": [],
        "observations": [
            {
                "observation_id": f"obs-{t['event_id'].split('-')[1]}",
                "source_system": "synthetic",
                "source_namespace": "example",
                "source_record_id": t["event_id"],
                "observed_at": "2025-03-01T09:00:00+08:00",
            }
            for t in transactions
        ],
        "rules": list(rules),
        "transactions": list(transactions),
    }
    return SchemaDocument.model_validate(data)


def rewards(doc, choices=None, period=FEBRUARY):
    chosen = {CARD: "example-product"} if choices is None else choices
    (result,) = card_rewards(doc, period, SOURCES, chosen)
    return result


FEB = ["2025-02-03", "2025-02-10", "2025-02-17"]


def test_a_chosen_card_with_complete_terms_gets_a_calculated_estimate():
    doc = document([rule()], [purchase(1, FEB[0], 2550), purchase(2, FEB[1], 1000)])
    result = rewards(doc)
    assert result.status == "estimated" and result.spend == {"SGD": 3550}
    # 2 points per SGD 1 on the exact amounts: 25.50 -> 51 and 10.00 -> 20.
    assert result.earned == {"example points": Decimal("71")}
    assert result.reason is None and result.product == "Example Card"


def test_an_unchosen_card_is_pending_and_earns_nothing_on_paper():
    doc = document([rule()], [purchase(1, FEB[0], 2550)])
    result = rewards(doc, choices={})
    assert (result.status, result.reason, result.earned) == ("pending", "Choose card", {})
    assert result.product is None


def test_a_product_without_terms_is_not_given_a_zero():
    doc = document([], [purchase(1, FEB[0], 2550)])
    result = rewards(doc)
    assert result.status == "no_terms" and result.earned == {}
    assert result.reason == "No reward terms for this card"


@pytest.mark.parametrize(
    "field",
    ["eligibility", "minimum_spend", "caps", "exclusions", "rounding", "date_basis", "effective"],
)
def test_unknown_terms_stop_the_calculation_with_a_stated_reason(field):
    unknown = {"status": "unknown"}
    doc = document([rule(**{field: unknown})], [purchase(1, FEB[0], 2550)])
    result = rewards(doc)
    assert result.status == "unsupported" and result.earned == {}
    assert result.reason.startswith("Terms incomplete")


def test_conflicting_eligibility_terms_are_not_resolved_by_guessing():
    conflict = {
        "status": "conflicting",
        "note": "Two documents disagree",
        "alternatives": [
            [
                {
                    "dimension": "channel",
                    "operator": "equals",
                    "operands": ["online"],
                    "description": "a",
                }
            ],
            [
                {
                    "dimension": "channel",
                    "operator": "equals",
                    "operands": ["in_store"],
                    "description": "b",
                }
            ],
        ],
    }
    result = rewards(document([rule(eligibility=conflict)], [purchase(1, FEB[0], 2550)]))
    assert result.status == "unsupported" and "eligibility" in result.reason


def test_rate_eligibility_needs_the_purchase_facts_it_depends_on():
    dining = {
        "status": "proposed",
        "value": [
            {
                "dimension": "mcc",
                "operator": "in",
                "operands": ["5812"],
                "description": "Dining",
            }
        ],
    }
    terms = rule(earning=fact([rate(eligibility=dining)]))
    unknown = rewards(document([terms], [purchase(1, FEB[0], 2550)]))
    assert unknown.status == "unsupported" and "mcc not known" in unknown.reason
    known = rewards(document([terms], [purchase(1, FEB[0], 2550, mcc="5812")]))
    assert known.status == "estimated" and known.earned == {"example points": Decimal("51")}
    other = rewards(document([terms], [purchase(1, FEB[0], 2550, mcc="5411")]))
    assert other.status == "estimated" and other.earned == {}
    assert other.reason == "Terms give nothing for these purchases"


def test_exclusions_remove_matching_purchases_but_need_known_facts():
    exclusion = {
        "status": "proposed",
        "value": [
            {
                "condition": {
                    "dimension": "merchant",
                    "operator": "in",
                    "operands": ["Example Telco"],
                    "description": "Telco",
                },
                "affects": "earning",
            }
        ],
    }
    terms = rule(exclusions=exclusion)
    doc = document(
        [terms],
        [
            purchase(1, FEB[0], 1000, merchant="Example Telco"),
            purchase(2, FEB[1], 500, merchant="Cafe"),
        ],
    )
    assert rewards(doc).earned == {"example points": Decimal("10")}
    assert rewards(document([terms], [purchase(3, FEB[0], 1000)])).status == "unsupported"


def test_minimum_spend_counts_the_whole_month_even_when_viewing_part_of_it():
    minimum = {
        "status": "proposed",
        "value": [
            {
                "requirement_id": "min",
                "threshold": fact({"currency": "SGD", "minor_units": 5000}),
                "date_basis": fact("transaction_date"),
                "period": fact({"kind": "calendar_month", "timezone": "Asia/Singapore"}),
                "qualifying_spend": NONE,
                "applies_to": fact(["base"]),
            }
        ],
    }
    terms = rule(minimum_spend=minimum)
    transactions = [purchase(1, FEB[0], 3000), purchase(2, FEB[2], 2500)]
    doc = document([terms], transactions)
    week = date_range("2025-02-17", "2025-02-17")
    inside = rewards(doc, period=week)
    assert inside.earned == {"example points": Decimal("50")}  # the month reached SGD 50.00
    low = document([terms], [purchase(1, FEB[0], 3000), purchase(2, FEB[2], 1500)])
    short = rewards(low, period=week)
    assert short.status == "estimated" and short.earned == {}  # minimum not reached


def test_caps_limit_the_month_in_order_of_purchase():
    cap = {
        "status": "proposed",
        "value": [
            {
                "cap_id": "cap",
                "basis": "reward",
                "limit": fact({"unit": POINTS, "quantity": "60"}),
                "period": fact({"kind": "calendar_month", "timezone": "Asia/Singapore"}),
                "applies_to": fact(["base"]),
            }
        ],
    }
    doc = document([rule(caps=cap)], [purchase(i, FEB[i - 1], 2500) for i in (1, 2, 3)])
    # Each purchase earns 50; the third is limited to what is left of the 60 cap.
    assert rewards(doc).earned == {"example points": Decimal("60")}
    spend_cap = {
        "status": "proposed",
        "value": [
            {
                "cap_id": "cap",
                "basis": "eligible_spend",
                "limit": fact({"currency": "SGD", "minor_units": 3000}),
                "period": fact({"kind": "calendar_month", "timezone": "Asia/Singapore"}),
                "applies_to": fact(["base"]),
            }
        ],
    }
    capped = document(
        [rule(caps=spend_cap)], [purchase(1, FEB[0], 2500), purchase(2, FEB[1], 2500)]
    )
    assert rewards(capped).earned == {"example points": Decimal("60")}


def test_rounding_terms_are_applied_per_transaction():
    rounding = {
        "status": "proposed",
        "value": [{"stage": "reward", "mode": "floor", "increment": "1", "scope": "transaction"}],
    }
    doc = document(
        [rule(rounding=rounding, earning=fact([rate(points="0.4")]))], [purchase(1, FEB[0], 2550)]
    )
    assert rewards(doc).earned == {"example points": Decimal("10")}  # 10.2 floored
    period = {
        "status": "proposed",
        "value": [{"stage": "reward", "mode": "floor", "increment": "1", "scope": "period"}],
    }
    assert (
        rewards(document([rule(rounding=period)], [purchase(1, FEB[0], 2550)])).status
        == "unsupported"
    )


def test_other_currencies_and_outside_dates_never_borrow_a_rate():
    usd = rule(earning=fact([rate(currency="USD")]))
    result = rewards(document([usd], [purchase(1, FEB[0], 2550)]))
    assert result.status == "unsupported" and result.earned == {}
    assert result.reason == "Terms incomplete: no rate for SGD purchases"
    late = rule(effective=fact({"start": "2025-03-01", "end": "2026-01-01"}))
    assert rewards(document([late], [purchase(1, FEB[0], 2550)])).status == "unsupported"
    open_ended = rule(effective=fact({"start": "2025-01-01"}))
    assert rewards(document([open_ended], [purchase(1, FEB[0], 2550)])).status == "unsupported"


def test_overlapping_rates_and_several_rules_are_ambiguous_not_summed():
    two = rule(earning=fact([rate("a"), rate("b", points="4")]))
    assert rewards(document([two], [purchase(1, FEB[0], 2550)])).status == "unsupported"
    second = deepcopy(rule(rule_id="other-rule", revision_id="other-v1"))
    assert rewards(document([rule(), second], [purchase(1, FEB[0], 2550)])).status == "unsupported"


def test_refunds_are_not_netted_so_no_estimate_is_shown():
    doc = document([rule()], [purchase(1, FEB[0], 2550), purchase(2, FEB[1], 500, kind="refund")])
    result = rewards(doc)
    assert result.status == "unsupported" and "refunds" in result.reason


def test_excluded_transactions_do_not_earn():
    transactions = [purchase(1, FEB[0], 2550), purchase(2, FEB[1], 1000)]
    transactions[1]["review"] = {"status": "pending"}
    result = rewards(document([rule()], transactions))
    assert result.spend == {"SGD": 2550} and result.earned == {"example points": Decimal("51")}


def test_a_quiet_month_is_not_reported_as_a_zero_reward():
    result = rewards(document([rule()], []))
    assert result.status == "idle" and result.reason == "No purchases" and result.earned == {}


def test_outgoing_wallet_transfers_are_spending_apart_from_card_purchases():
    transfer = purchase(2, FEB[1], 7500, kind="transfer", account="wallet-account")
    transfer["direction"] = fact("outflow")
    incoming = purchase(3, FEB[1], 4000, kind="transfer", account="wallet-account")
    incoming["direction"] = fact("inflow")
    unknown = purchase(4, FEB[1], 900, kind="transfer", account="wallet-account")
    own = purchase(5, FEB[1], 1100, kind="transfer", account="wallet-account")
    own["direction"] = fact("outflow")
    own["transfer_purpose"] = fact("own_accounts")
    doc = document([rule()], [purchase(1, FEB[0], 2550), transfer, incoming, unknown, own])
    (chart,) = range_report(doc, FEBRUARY)["charts"]
    assert chart["spending"] == {"purchase": 2550, "transfer": 7500, "total": 10050}
    assert [row["minor_units"] for row in chart["transfers"]] == [7500]
    assert chart["totals"]["transfer"] == 7500 + 4000 + 900 + 1100  # kinds are never netted
    result = rewards(doc)  # the wallet is not a card: no product, no card reward
    assert result.spend == {"SGD": 2550} and result.earned == {"example points": Decimal("51")}
    assert [c["label"] for c in SOURCES.values()] == ["Example card ending 0001"]


def test_a_duplicate_link_is_not_counted_twice_in_spending():
    transfer = purchase(1, FEB[0], 7500, kind="transfer", account="wallet-account")
    transfer["direction"] = fact("outflow")
    twin = purchase(2, FEB[0], 7500, kind="transfer", account="wallet-account")
    twin["direction"] = fact("outflow")
    twin["duplicate_of"] = "event-1"
    (chart,) = range_report(document([], [transfer, twin]), FEBRUARY)["charts"]
    assert chart["spending"]["transfer"] == 7500


def test_posting_date_terms_need_a_known_posting_date():
    terms = rule(date_basis=fact("posting_date"))
    assert rewards(document([terms], [purchase(1, FEB[0], 2550)])).status == "unsupported"
    posted = purchase(1, FEB[0], 2550)
    posted["posted_on"] = fact("2025-02-05")
    assert rewards(document([terms], [posted])).earned == {"example points": Decimal("51")}


def test_an_unrepresentable_calculation_is_reported_not_raised():
    rounding = {
        "status": "proposed",
        "value": [
            {"stage": "reward", "mode": "floor", "increment": "1E-40", "scope": "transaction"}
        ],
    }
    doc = document([rule(rounding=rounding)], [purchase(1, FEB[0], 2550)])
    result = rewards(doc)
    assert result.status == "unsupported" and "out of range" in result.reason


def currency_only(*codes):
    return {
        "status": "proposed",
        "value": [
            {
                "dimension": "currency",
                "operator": "in",
                "operands": list(codes),
                "description": "Listed currencies",
            }
        ],
    }


def test_a_purchase_in_another_currency_than_the_rate_is_unknown_not_zero():
    doc = document([rule()], [purchase(1, FEB[0], 2550, currency="USD")])
    result = rewards(doc)
    assert result.status == "unsupported" and result.earned == {}
    assert result.reason == "Terms incomplete: no rate for USD purchases"


def test_a_mixed_currency_card_is_not_given_a_partial_total():
    doc = document([rule()], [purchase(1, FEB[0], 2550), purchase(2, FEB[1], 1000, currency="USD")])
    result = rewards(doc)
    assert result.status == "unsupported" and result.earned == {}
    assert result.spend == {"SGD": 2550, "USD": 1000}


def test_a_same_currency_rate_is_used_when_other_currency_rates_exist():
    terms = rule(earning=fact([rate("sgd"), rate("usd", points="3", currency="USD")]))
    doc = document([terms], [purchase(1, FEB[0], 2550), purchase(2, FEB[1], 1000, currency="USD")])
    assert rewards(doc).earned == {"example points": Decimal("81")}


def test_explicitly_ineligible_currencies_may_earn_nothing():
    transactions = [purchase(1, FEB[0], 2550), purchase(2, FEB[1], 1000, currency="USD")]
    by_rule = rewards(document([rule(eligibility=currency_only("SGD"))], transactions))
    assert by_rule.status == "estimated" and by_rule.earned == {"example points": Decimal("51")}
    by_rate = rule(earning=fact([rate(eligibility=currency_only("SGD"))]))
    result = rewards(document([by_rate], transactions))
    assert result.status == "estimated" and result.earned == {"example points": Decimal("51")}


def test_an_undecided_purchase_outside_the_viewed_days_matters_only_through_caps():
    transactions = [purchase(1, FEB[0], 2550, currency="USD"), purchase(2, FEB[2], 1000)]
    week = date_range("2025-02-17", "2025-02-17")
    result = rewards(document([rule()], transactions), period=week)
    assert result.status == "estimated" and result.earned == {"example points": Decimal("20")}
    cap = {
        "status": "proposed",
        "value": [
            {
                "cap_id": "cap",
                "basis": "reward",
                "limit": fact({"unit": POINTS, "quantity": "60"}),
                "period": fact({"kind": "calendar_month", "timezone": "Asia/Singapore"}),
                "applies_to": fact(["base"]),
            }
        ],
    }
    capped = rewards(document([rule(caps=cap)], transactions), period=week)
    assert capped.status == "unsupported" and "no rate for USD" in capped.reason


def minimum(qualifying=NONE):
    return {
        "status": "proposed",
        "value": [
            {
                "requirement_id": "min",
                "threshold": fact({"currency": "SGD", "minor_units": 5000}),
                "date_basis": fact("transaction_date"),
                "period": fact({"kind": "calendar_month", "timezone": "Asia/Singapore"}),
                "qualifying_spend": qualifying,
                "applies_to": fact(["base"]),
            }
        ],
    }


def test_minimum_spend_does_not_ignore_other_currency_purchases():
    week = date_range("2025-02-17", "2025-02-17")
    short = [purchase(1, FEB[0], 2500, currency="USD"), purchase(2, FEB[2], 3000)]
    result = rewards(document([rule(minimum_spend=minimum())], short), period=week)
    assert result.status == "unsupported" and "minimum spend" in result.reason
    # Qualifying spend stated as SGD only: the USD purchase is excluded and the minimum unmet.
    sgd_only = rule(minimum_spend=minimum(currency_only("SGD")))
    unmet = rewards(document([sgd_only], short), period=week)
    assert unmet.status == "estimated" and unmet.earned == {}
    # Met in SGD alone: the USD purchase cannot change that.
    met = [purchase(1, FEB[0], 2500, currency="USD"), purchase(2, FEB[2], 6000)]
    reached = rewards(document([rule(minimum_spend=minimum())], met), period=week)
    assert reached.status == "estimated" and reached.earned == {"example points": Decimal("120")}
