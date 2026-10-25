"""Reproducible, fictional saved ledger. Not the output of an AI extraction flow."""

from spending_optimiser.document import SchemaDocument


def proposed(value):
    return {"status": "proposed", "value": value}


def monthly_document():
    data = {
        "products": [{"product_id": "preview-product", "supplied_name": "Fictional everyday card"}],
        "accounts": [
            {"account_id": "everyday", "kind": "credit_card", "label": "Everyday card"},
            {"account_id": "bank", "kind": "deposit", "label": "Daily banking"},
            {"account_id": "wallet", "kind": "wallet", "label": "Travel wallet"},
        ],
        "cards": [
            {"card_id": "everyday-card", "product_id": "preview-product", "account_id": "everyday"}
        ],
        "observations": [],
        "transactions": [],
    }

    def event(
        identity,
        day,
        amount,
        *,
        currency="SGD",
        kind="purchase",
        account="everyday",
        card="everyday-card",
        channel="card",
        status="accepted",
        merchant=None,
    ):
        data["observations"].append(
            {
                "observation_id": "source-" + identity,
                "source_system": "synthetic",
                "source_namespace": "monthly-preview",
                "source_record_id": identity,
                "observed_at": "2025-04-01T00:00:00Z",
            }
        )
        transaction = {
            "event_id": identity,
            "revision_id": identity + "-v1",
            "recorded_at": "2025-04-01T00:01:00Z",
            "origins": [
                {
                    "observation_id": "source-" + identity,
                    "event_key": "primary",
                    "extractor_revision": "synthetic-fixture",
                    "processing_run_id": "demo",
                }
            ],
            "kind": proposed(kind),
            "account_id": proposed(account),
            "merchant": proposed(merchant or identity.replace("-", " ").title()),
            "review": {
                "status": status,
                "reviewer": "Synthetic reviewer",
                "reviewed_at": "2025-04-01T00:01:00Z",
                "reason": "Fictional fixture only",
            },
        }
        if day:
            transaction["occurred_on"] = proposed(day)
        if amount is not None:
            transaction["amount"] = proposed({"currency": currency, "minor_units": amount})
        if card:
            transaction["card_id"] = proposed(card)
        if channel:
            transaction["channel"] = proposed(channel)
        data["transactions"].append(transaction)
        return transaction

    event("orchard-coffee", "2025-02-01", 1280)
    old = event("weekly-groceries", "2025-02-08", 4250)
    revised = {
        **old,
        "revision_id": "weekly-groceries-v2",
        "supersedes": old["revision_id"],
        "recorded_at": "2025-04-02T00:01:00Z",
        "amount": proposed({"currency": "SGD", "minor_units": 4950}),
        "corrections": [
            {
                "field": "amount",
                "actor": "Synthetic reviewer",
                "reason": "Fictional receipt correction",
                "corrected_at": "2025-04-02T00:01:00Z",
            }
        ],
    }
    data["transactions"].append(revised)
    event("dinner-paynow", "2025-02-08", 8200, account="bank", card=None, channel="paynow")
    event("train-wallet", "2025-02-28", 1450, account="wallet", card=None, channel=None)
    event(
        "tokyo-bookshop",
        "2025-02-15",
        1234,
        currency="JPY",
        account="wallet",
        card=None,
        channel=None,
    )
    event(
        "kuwait-cafe", "2025-02-18", 1234, currency="KWD", account="wallet", card=None, channel=None
    )
    event("online-subscription", "2025-02-20", 1299, currency="USD")
    event("returned-item", "2025-02-21", 500, kind="refund")
    event("reversed-hold", "2025-02-22", 100, kind="reversal")
    event(
        "salary",
        "2025-02-25",
        250000,
        kind="income",
        account="bank",
        card=None,
        channel="bank_transfer",
    )
    event(
        "account-transfer",
        "2025-02-26",
        9999,
        kind="transfer",
        account="bank",
        card=None,
        channel="bank_transfer",
    )
    event("uncertain-notice", "2025-02-11", 99000, status="pending", card=None, channel=None)
    event("rejected-notice", "2025-02-13", 49000, status="rejected")
    event("unknown-amount", "2025-02-14", None, status="pending", card=None, channel=None)
    event("unknown-date", None, 1234, status="pending")
    duplicate = event("duplicate-coffee", "2025-02-01", 1280)
    duplicate["duplicate_of"] = "orchard-coffee"
    event("previous-month", "2025-01-31", 900000)
    event("next-month", "2025-03-01", 900000)
    return SchemaDocument.model_validate(data)
