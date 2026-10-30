"""Cards: stated card endings, an explicit and correctable product choice, wallet exclusion.

Invented notices and identifiers on fake transports. A choice is user-supplied product
information only; it never rewrites source facts or accepts an uncertain transaction.
"""

import re
from html.parser import HTMLParser
from importlib.resources import files

import pytest
from fastapi.testclient import TestClient
from test_monthly import connected
from test_processing import Notifications, load, setup

from spending_optimiser import cards, processing
from spending_optimiser.cards import CardBook, describe, product_options
from spending_optimiser.document import SchemaDocument
from spending_optimiser.ledger import Ledger, heads
from spending_optimiser.processing import REVISION, account_identity
from spending_optimiser.web import create_app

CARD = (
    "Card Transaction Alert\nTransaction Ref: EXAMPLE-REF-{ref}\n"
    "We refer to your card transaction request dated 03/02/25. "
    "We are pleased to confirm that the transaction was completed.\n"
    "Date & Time: 03 FEB 09:15 (SGT)\nAmount: SGD{amount}\n"
    "From: DBS/POSB card ending {ending}\nTo: Fictional Merchant\n"
)
WALLET = (
    "Transaction Alerts\nTransaction Ref: EXAMPLE-WALLET-{ref}\n"
    "We refer to your PayLah! Scan & Pay Transfer dated 03 Feb. "
    "We are pleased to confirm that the transaction was completed.\n"
    "Date & Time:\n03 Feb 2025 09:15 (SGT)\nAmount:\nSGD{amount}\n"
    "From:\nPayLah! Wallet (Mobile ending {ending})\nTo:\nFictional Recipient\n"
)


def card(ref="A", amount="18.60", ending="4071"):
    return CARD.format(ref=ref, amount=amount, ending=ending)


def wallet(ref="A", amount="41.20", ending="5520"):
    return WALLET.format(ref=ref, amount=amount, ending=ending)


class Fields(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)

    def find(self, tag, **attrs):
        return [a for t, a in self.tags if t == tag and attrs.items() <= a.items()]

    @property
    def words(self):
        return " ".join(" ".join(self.text).split())


def choose(client, service, account_id, product_id, **extra):
    client.get("/cards")
    return client.post(
        "/cards/choose",
        data={
            "csrf": client.cookies["ledger_csrf"],
            "connection_id": service.connection_id,
            "account_id": account_id,
            "product_id": product_id,
            **extra,
        },
        follow_redirects=False,
    )


def card_account(service):
    (account,) = [
        a for a, c in service.cache.cards.sources(service.namespace).items() if c["kind"] == "card"
    ]
    return account


def test_a_card_alert_creates_one_pending_card_and_wallets_never_do(tmp_path):
    service, _, client = setup(
        tmp_path, {"c1": card("A"), "c2": card("B", "5.00"), "w1": wallet("A"), "w2": wallet("B")}
    )
    load(service)
    sources = service.cache.cards.sources(service.namespace)
    assert sorted((s["kind"], s["suffix"], s["label"]) for s in sources.values()) == [
        ("card", "4071", "DBS/POSB card ending 4071"),
        ("wallet", "5520", "PayLah! wallet ending 5520"),
    ]
    page = Fields(client.get("/cards").text)
    assert "DBS/POSB card ending 4071" in page.words and "5520" not in page.words
    assert page.words.count("Pending") == 1  # one card, however many alerts mention it
    (select,) = page.find("select", name="product_id")
    assert "required" in select
    options = {a.get("value"): a for a in page.find("option")}
    assert "disabled" in options[""] and "selected" in options[""]
    assert {"maybank-unresolved", "dbs-unresolved", "trust-unresolved"} <= set(options)
    assert not page.find("input", type="text") and not page.find("input", type="number")


def test_wallet_references_are_not_card_identifiers(tmp_path):
    service, _, client = setup(tmp_path, {"w1": wallet("A")})
    load(service)
    (wallet_account,) = service.cache.cards.sources(service.namespace)
    assert client.get("/cards").text.count("<select") == 0
    response = choose(client, service, wallet_account, "dbs-unresolved")
    assert response.status_code == 422
    assert service.cache.cards.choices(service.namespace) == {}
    assert describe("paylah-notice-ref-5520")["kind"] == "wallet"
    assert describe("some-account") is None and describe("issuer-dbsposb-cnf-12345") is None


def test_choosing_saves_corrects_and_survives_a_restart(tmp_path):
    service, fake, client = setup(tmp_path, {"c1": card()})
    load(service)
    account = card_account(service)
    response = choose(client, service, account, "dbs-unresolved")
    assert response.status_code == 303 and response.headers["location"] == "/cards"
    page = Fields(client.get("/cards").text)
    assert page.words.count("Chosen") == 1 and "1 of 1 chosen" in page.words
    selected = [a for a in page.find("option") if "selected" in a and a.get("value")]
    assert [a["value"] for a in selected] == ["dbs-unresolved"]
    assert not any(a.get("value") == "" for a in page.find("option"))  # placeholder is gone
    assert choose(client, service, account, "dbs-unresolved").status_code == 303  # idempotent
    assert choose(client, service, account, "trust-unresolved").status_code == 303  # correction
    book = service.cache.cards
    assert book.choices(service.namespace) == {account: "trust-unresolved"}
    assert [p for p, _ in book.history(service.namespace, account)] == [
        "dbs-unresolved",
        "trust-unresolved",
    ]
    restarted, _ = connected(service.cache.ledger.path, fake)
    assert restarted.cache.cards.choices(restarted.namespace) == {account: "trust-unresolved"}
    names = {p.product_id: p.supplied_name for p in restarted.cache.ledger.load().products}
    assert names["trust-unresolved"] == "Trust Freedom Card"


def test_choices_are_validated_on_the_server_and_protected(tmp_path):
    service, _, client = setup(tmp_path, {"c1": card()})
    load(service)
    account = card_account(service)
    assert choose(client, service, account, "made-up-product").status_code == 422
    assert choose(client, service, "mail-account-unknown", "dbs-unresolved").status_code == 422
    assert choose(client, service, account, "dbs-unresolved", extra="1").status_code == 422
    stale = client.post(
        "/cards/choose",
        data={
            "csrf": client.cookies["ledger_csrf"],
            "connection_id": "stale",
            "account_id": account,
            "product_id": "dbs-unresolved",
        },
    )
    assert stale.status_code == 409
    fields = {
        "connection_id": service.connection_id,
        "account_id": account,
        "product_id": "dbs-unresolved",
    }
    assert client.post("/cards/choose", data={**fields, "csrf": "wrong"}).status_code == 403
    forged = client.post(
        "/cards/choose",
        data={**fields, "csrf": client.cookies["ledger_csrf"]},
        headers={"Origin": "https://evil.invalid"},
    )
    assert forged.status_code == 403
    assert service.cache.cards.choices(service.namespace) == {}
    assert not service.cache.ledger.load().products  # nothing was written for a rejected choice


def test_a_choice_never_rewrites_evidence_or_accepts_an_uncertain_transaction(tmp_path):
    uncertain = card("U").replace("03 FEB 09:15", "03 FEB 2024 09:15")  # contradicts the 25 stated
    service, _, client = setup(tmp_path, {"good": card("G"), "bad": uncertain})
    before = load(service)
    statuses = {t.event_id: t.review.status for t in heads(before)}
    assert sorted(statuses.values()) == ["accepted", "pending"]
    account = card_account(service)
    assert choose(client, service, account, "dbs-unresolved").status_code == 303
    after = service.cache.ledger.load()
    assert after.transactions == before.transactions and after.accounts == before.accounts
    assert [p.product_id for p in after.products] == ["dbs-unresolved"]
    assert all(t.card_id.value is None for t in heads(after))
    assert {t.event_id: t.review.status for t in heads(after)} == statuses


def test_repeat_processing_keeps_one_card_and_the_human_choice(tmp_path, monkeypatch):
    service, _, client = setup(tmp_path, {"c1": card("A"), "c2": card("B", "9.00")})
    load(service)
    account = card_account(service)
    choose(client, service, account, "maybank-unresolved")
    sources = service.cache.cards.sources(service.namespace)
    monkeypatch.setattr(processing, "REVISION", REVISION + "-next")
    monkeypatch.setattr("spending_optimiser.gmail_service.REVISION", REVISION + "-next")
    processing.process_saved(service.cache, service.namespace)
    assert service.cache.pending_count(service.namespace, REVISION + "-next") == 0
    assert service.cache.cards.sources(service.namespace) == sources
    assert service.cache.cards.choices(service.namespace) == {account: "maybank-unresolved"}
    assert len(service.cache.cards.history(service.namespace, account)) == 1


def test_mailboxes_and_issuers_do_not_share_a_card_even_with_the_same_ending(tmp_path, monkeypatch):
    path = tmp_path / "shared.sqlite3"
    first, fake = connected(path, Notifications({"c1": card(ending="4071")}), "account-A")
    second, _ = connected(path, fake, "account-B")
    load(first)
    load(second)
    a, b = card_account(first), card_account(second)
    assert a != b and a == account_identity(first.namespace, "issuer-dbsposb-cnf-4071")
    first.cache.cards.choose(first.namespace, a, "dbs-unresolved")
    assert second.cache.cards.choices(second.namespace) == {}
    with pytest.raises(ValueError):
        second.cache.cards.choose(second.namespace, a, "dbs-unresolved")  # not this mailbox's card
    # A second issuer with the same four digits is its own card in the same mailbox.
    monkeypatch.setattr(
        cards,
        "SOURCES",
        (
            *cards.SOURCES,
            ("issuer-otherbank-cnf-", "card", "otherbank", "Other Bank card ending {}"),
        ),
    )
    other = account_identity(first.namespace, "issuer-otherbank-cnf-4071")
    ledger = first.cache.ledger
    with ledger.connection() as db:
        CardBook.note(db, first.namespace, other, "issuer-otherbank-cnf-4071")
    found = first.cache.cards.sources(first.namespace)
    assert {c["label"] for c in found.values()} == {
        "DBS/POSB card ending 4071",
        "Other Bank card ending 4071",
    }
    first.cache.cards.choose(first.namespace, other, "trust-unresolved")
    assert first.cache.cards.choices(first.namespace) == {
        a: "dbs-unresolved",
        other: "trust-unresolved",
    }


def test_the_catalogue_reuses_ledger_products_and_never_conflicts_with_an_import(tmp_path):
    service, _, client = setup(tmp_path, {"c1": card()})
    load(service)
    document = SchemaDocument.model_validate(
        {"products": [{"product_id": "example-extra", "supplied_name": "Example Extra Card"}]}
    )
    service.cache.ledger.import_document(document.model_dump_json())
    options = product_options(service.cache.ledger.load())
    assert options["example-extra"] == "Example Extra Card" and "dbs-unresolved" in options
    account = card_account(service)
    assert choose(client, service, account, "example-extra").status_code == 303
    assert choose(client, service, account, "dbs-unresolved").status_code == 303
    again = files("spending_optimiser").joinpath("examples/synthetic.json").read_text()
    result = service.cache.ledger.import_document(again)
    assert result["added"] > 0  # the bundled example imports over the same product records
    ids = [p.product_id for p in service.cache.ledger.load().products]
    assert sorted(ids) == sorted(set(ids)) and "dbs-unresolved" in ids


def test_a_card_is_only_listed_for_the_connected_mailbox(tmp_path):
    path = tmp_path / "isolated.sqlite3"
    first, fake = connected(path, Notifications({"c1": card(ending="4071")}), "account-A")
    load(first)
    second, _ = connected(path, Notifications({"c2": card(ending="6250")}), "account-B")
    load(second)
    client = TestClient(create_app(path, gmail_service=second), base_url="http://127.0.0.1:8897")
    words = Fields(client.get("/cards").text).words
    assert "ending 6250" in words and "ending 4071" not in words
    assert re.search(r"1 of 1 chosen|0 of 1 chosen", words)


def test_cards_page_without_gmail_or_cards_is_a_plain_empty_state(tmp_path):
    client = TestClient(create_app(tmp_path / "empty.sqlite3"), base_url="http://127.0.0.1:8897")
    assert "Connect Gmail to see cards" in client.get("/cards").text
    service, _, client = setup(tmp_path, {"n": "Fictional unrelated notice"})
    load(service)
    assert "No cards found yet" in client.get("/cards").text
    assert Ledger(service.cache.ledger.path).load().products == ()


RULE = {
    "products": [{"product_id": "example-product", "supplied_name": "Example Rewards Card"}],
    "rules": [
        {
            "rule_id": "example-rule",
            "revision_id": "example-rule-v1",
            "product_id": "example-product",
            "compiled_at": "2025-01-02T12:00:00+08:00",
            "effective": {
                "status": "proposed",
                "value": {"start": "2025-01-01", "end": "2026-01-01"},
            },
            "date_basis": {"status": "proposed", "value": "transaction_date"},
            "eligibility": {"status": "not_applicable", "note": "Terms state none"},
            "minimum_spend": {"status": "not_applicable", "note": "Terms state none"},
            "caps": {"status": "not_applicable", "note": "Terms state none"},
            "exclusions": {"status": "not_applicable", "note": "Terms state none"},
            "rounding": {"status": "not_applicable", "note": "Terms state none"},
            "earning": {
                "status": "proposed",
                "value": [
                    {
                        "rate_id": "base",
                        "component": "base",
                        "reward": {
                            "status": "proposed",
                            "value": {
                                "unit": {"unit_id": "example-points", "kind": "points"},
                                "quantity": "3",
                            },
                        },
                        "per_spend": {
                            "status": "proposed",
                            "value": {"currency": "SGD", "minor_units": 100},
                        },
                        "eligibility": {"status": "not_applicable", "note": "Terms state none"},
                    }
                ],
            },
        }
    ],
}


def test_chosen_card_rewards_and_wallet_transfer_spending_reach_the_page(tmp_path):
    service, _, client = setup(tmp_path, {"c1": card("A", "18.60"), "w1": wallet("A", "41.20")})
    service.cache.ledger.import_document(SchemaDocument.model_validate(RULE).model_dump_json())
    load(service)
    account = card_account(service)
    before = client.get("/?month=2025-02").text
    words = Fields(before).words
    assert "Card rewards" in words and "Choose card" in words
    assert "Card purchases SGD 18.60 Transfers out SGD 41.20 Total spending SGD 59.80" in words
    assert "Transfers out by account" in words and "PayLah! wallet ending 5520" in words
    assert "Example Rewards Card" not in words  # nothing chosen yet, so no reward is shown
    assert choose(client, service, account, "example-product").status_code == 303
    after = Fields(client.get("/?month=2025-02").text)
    assert "DBS/POSB card ending 4071 (Example Rewards Card)" in after.words
    row = [a for a in after.find("tr") if a.get("data-card") == account]
    assert row and row[0]["data-status"] == "estimated"
    assert "55.80 example points" in after.words and "Estimate" in after.words  # 3 x 18.60
    assert "Estimated from saved terms, not bank-confirmed." in after.words
    assert after.words.count("example points") == 1  # the wallet earns no card reward
    assert "Optimise spending" in after.words and "Understand this month" not in after.words


def test_pending_choice_and_missing_terms_are_stated_not_zeroed(tmp_path):
    service, _, client = setup(tmp_path, {"c1": card()})
    load(service)
    account = card_account(service)
    words = Fields(client.get("/?month=2025-02").text).words
    assert "Card not chosen" in words and "Choose card" in words and "points" not in words
    choose(client, service, account, "trust-unresolved")
    words = Fields(client.get("/?month=2025-02").text).words
    assert "No reward terms for this card" in words and "points" not in words


def test_synthetic_refresh_preview_walks_refresh_cards_and_rewards(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "examples"))
    from refresh_preview import refresh_app

    app = refresh_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8899")
    service = app.state.gmail
    assert app.state.synthetic_transport.requests == []  # opening the app fetches nothing
    client.get("/gmail")
    done = client.post(
        "/gmail/refresh",
        data={"csrf": client.cookies["ledger_csrf"], "connection_id": service.connection_id},
    )
    assert "All recent emails are loaded. 4 new." in done.text
    (account,) = [
        a for a, c in service.cache.cards.sources(service.namespace).items() if c["kind"] == "card"
    ]
    assert choose(client, service, account, "preview-product").status_code == 303
    words = Fields(client.get("/?month=2025-02").text).words
    assert "78.00 preview points" in words and "Transfers out SGD 41.20" in words
