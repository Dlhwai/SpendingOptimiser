"""Minimal invented messages exercising generalized contracts, not copied mail.

No private customer values, source identifiers, full messages or corpus assertions.
"""

from dataclasses import replace
from datetime import date

import pytest
from test_local_templates import candidate, definitions
from test_processing import BODY, Notifications, load, setup, total

from spending_optimiser.gmail import GmailAdapter
from spending_optimiser.ledger import heads
from spending_optimiser.local_templates import LocalTemplates, apply_generic
from spending_optimiser.processing import REVISION, extract

CARD = (
    "Card Transaction Alert\nTransaction Ref: EXAMPLE-REF-A\n"
    "We refer to your card transaction request dated 03/02/25. "
    "We are pleased to confirm that the transaction was completed.\n"
    "Date & Time: 03 FEB 09:15 (SGT)\nAmount: SGD12.34\n"
    "From: DBS/POSB card ending 9999\nTo: Fictional Merchant\n"
)
WALLET = (
    "Transaction Alerts\nTransaction Ref: EXAMPLE-WALLET-A\n"
    "We refer to your PayLah! Scan & Pay Transfer dated 03 Feb. "
    "We are pleased to confirm that the transaction was completed.\n"
    "Date & Time:\n03 Feb 09:15 (SGT)\nAmount:\nSGD7.89\n"
    "From:\nPayLah! Wallet (Mobile ending 8888)\nTo:\nFictional Recipient\n"
)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invented profile tests must remain local")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("spending_optimiser.extraction.NotificationExtractor.extract", forbidden)


def test_completed_card_profile_accepts_source_reference_not_underlying_card_mapping(tmp_path):
    service, fake, client = setup(tmp_path, {"one": CARD})
    document = load(service)
    (transaction,) = heads(document)
    assert transaction.review.status == "accepted" and total(document) == 1234
    assert transaction.kind.value == "purchase" and transaction.direction.value == "outflow"
    assert transaction.occurred_on.value == date(2025, 2, 3)
    assert transaction.card_id.value is transaction.channel.value is None
    assert document.accounts[0].kind == "other"
    assert "9999" not in document.accounts[0].label
    # Status stays within the heading column, not the narrow mobile expander column.
    assert '<span class="badge">accepted</span></span>' in client.get("/saved?month=2025-02").text
    source = candidate(service, fake)
    facts, _, outcome, _ = extract(source, "fictional-version", source.observation)
    assert outcome == "accepted"
    for fact in facts.values():
        for ref in fact.evidence:
            start, end = map(int, ref.locator.rsplit(":", 2)[1:])
            assert source.parts[0].text[start:end] == ref.quote
    other = source.observation.model_copy(update={"source_namespace": "another-fictional-owner"})
    altered = replace(source, observation=other)
    other_facts, _, _, _ = extract(altered, "fictional-version", other)
    assert other_facts["account_id"].value != facts["account_id"].value
    fake.bodies["two"] = (
        CARD.replace("EXAMPLE-REF-A", "EXAMPLE-REF-B")
        .replace("12.34", "5.67")
        .replace("09:15", "10:20")
        .replace("Fictional Merchant", "Another Fictional Shop")
    )
    new = candidate(service, fake, "two")
    result = LocalTemplates(service.cache).apply(
        service.namespace, "two", "new-version", new, new.observation
    )
    assert result[1] == "accepted" and definitions(service) == 1
    fake.pages = {None: {"messages": [{"id": "one"}, {"id": "two"}]}}
    document = load(service, "refresh")
    assert total(document) == 1801 and definitions(service) == 1
    assert load(service, "refresh") == document


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace("was completed", "was declined"),
        lambda text: text.replace(
            "From: DBS/POSB card ending 9999", "From: Other issuer ending 9999"
        ),
        lambda text: text.replace("To: Fictional Merchant", "To: account 12345678"),
        lambda text: text.replace("To: Fictional Merchant", "To: Fictional ATM"),
        lambda text: text.replace("To: Fictional Merchant", "To: Wallet top-up"),
        lambda text: text.replace("Transaction Ref: EXAMPLE-REF-A\n", ""),
        lambda text: text.replace("Date & Time: 03 FEB 09:15 (SGT)", "Date & Time: unknown"),
        lambda text: text.replace("03/02/25", "00/02/25"),
        lambda text: text.replace("SGD12.34", "XYZ12.34"),
    ],
)
def test_incomplete_changed_or_non_purchase_contracts_never_gain_authority(tmp_path, change):
    service, _, _ = setup(tmp_path, {"one": change(CARD)})
    document = load(service)
    assert total(document) == 0 and all(t.review.status != "accepted" for t in heads(document))


@pytest.mark.parametrize(
    "body, kind",
    [(CARD, "purchase"), (WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15"), "transfer")],
)
def test_completed_profiles_reconcile_all_explicit_currency_evidence(tmp_path, body, kind):
    service, fake, _ = setup(tmp_path, {"one": body + "\nCurrency: SGD"})
    assert heads(load(service))[0].review.status == "accepted"
    for currency in ("USD", "XYZ", "dollars"):
        fake.bodies["one"] = body + "\nCurrency: " + currency
        document = load(service, "refresh")
        transaction = heads(document)[0]
        assert transaction.review.status == "pending"
        assert total(document, kind=kind) == 0 and transaction.review.reason


@pytest.mark.parametrize(
    "body, before, after, reason, kind, minor",
    [
        (CARD, "EXAMPLE-REF-A", "EXAMPLE-REF-B", "notice_reference_conflicting", "purchase", 1234),
        (CARD, "EXAMPLE-REF-A", "example-ref-a", "notice_reference_conflicting", "purchase", 1234),
        (
            WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15"),
            "EXAMPLE-WALLET-A",
            "EXAMPLE-WALLET-B",
            "notice_reference_conflicting",
            "transfer",
            789,
        ),
        (
            WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15"),
            "Fictional Recipient",
            "Another Fictional Recipient",
            "payee_conflicting",
            "transfer",
            789,
        ),
        (
            WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15"),
            "Fictional Recipient",
            "fictional Recipient",
            "payee_conflicting",
            "transfer",
            789,
        ),
        (
            CARD,
            "Fictional Merchant",
            "Another Fictional Merchant",
            "merchant_conflicting",
            "purchase",
            1234,
        ),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_qualifying_profile_alternatives_must_agree_before_projection(
    tmp_path, body, before, after, reason, kind, minor, reverse
):
    import base64
    from html import escape

    service, fake, client = setup(tmp_path, {"one": body})
    single = load(service)
    assert heads(single)[0].review.status == "accepted" and total(single, kind=kind) == minor
    original_message = fake.get_message
    alternative = body.replace(before, "  " + before + "  ")

    def with_alternative(identity):
        message = original_message(identity)
        raw = "".join(f"<p>{escape(line)}</p>" for line in alternative.splitlines()).encode()
        parts = [
            message["payload"],
            {
                "mimeType": "text/html",
                "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
            },
        ]
        message["payload"] = {
            "mimeType": "multipart/alternative",
            "parts": parts[::-1] if reverse else parts,
        }
        return message

    fake.get_message = with_alternative
    compatible = load(service, "refresh")
    assert (
        heads(compatible)[0].review.status == "accepted" and total(compatible, kind=kind) == minor
    )
    learned_count = definitions(service)
    alternative = body.replace(before, "  " + after + "  ")
    saved = candidate(service, fake)
    facts, _, outcome, reasons = extract(saved, "synthetic-conflicting-profile", saved.observation)
    assert outcome == "uncertain" and reason in reasons
    assert facts["amount"].value.minor_units == minor
    document = load(service, "refresh")
    transaction = heads(document)[0]
    assert transaction.review.status == "pending" and reason in transaction.review.reason
    assert total(document, kind=kind) == 0
    assert transaction.account_id.value == heads(compatible)[0].account_id.value
    assert transaction.kind.value == kind and transaction.card_id.value is None
    if kind == "transfer":
        assert transaction.merchant.value is None and transaction.transfer_purpose.value is None
    assert document.transactions[:2] == compatible.transactions
    assert definitions(service) == learned_count
    parts = {part.locator: part for part in saved.parts}
    for fact in (transaction.amount, transaction.occurred_on, transaction.account_id):
        for ref in fact.evidence:
            _, digest, locator, _, start, end = ref.locator.split(":")
            assert parts[locator].text[int(start) : int(end)] == ref.quote
    templates = LocalTemplates(service.cache)
    assert reason in templates.apply(service.namespace, "one", digest, saved, saved.observation)[2]
    results = [
        apply_generic(form, saved, digest, saved.observation)
        for form in templates.generic_forms()["forms"]
    ]
    assert all(result[1] != "accepted" for result in results)
    assert any(reason in result[2] for result in results)
    assert service.cache.version(service.namespace, "one", digest).parts == saved.parts
    response = client.get(
        "/mail-source",
        params={"observation_id": transaction.origins[0].observation_id, "version": digest},
    )
    assert response.status_code == 200 and before in response.text and after in response.text
    assert "Excluded from totals" in client.get("/saved?month=2025-02").text
    assert load(service, "refresh") == document


def test_profile_agreement_does_not_reconcile_unrelated_transport_recipients():
    import base64

    body = BODY + "\nTo: first@example.invalid\nTransaction Ref: HEADER-A"
    alternative = body.replace("first@example.invalid", "other@example.invalid").replace(
        "HEADER-A", "HEADER-B"
    )
    fake = Notifications({"one": body})
    original_message = fake.get_message

    def with_alternative(identity):
        message = original_message(identity)
        raw = alternative.encode()
        message["payload"] = {
            "mimeType": "multipart/alternative",
            "parts": [
                message["payload"],
                {
                    "mimeType": "text/html",
                    "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
                },
            ],
        }
        return message

    fake.get_message = with_alternative
    saved = GmailAdapter(fake, "synthetic-non-profile-metadata").get_candidate("one").candidate
    facts, _, outcome, reasons = extract(saved, "non-profile-control", saved.observation)
    assert outcome == "accepted" and not reasons and facts["amount"].value.minor_units == 1234


INVALID_CONTRACT_FIELDS = [
    ("From", "DBS/POSB card ending 12345"),
    ("From", "PayLah! Wallet (Mobile ending 12345)"),
    ("From", "unsupported source"),
    ("From", ""),
    ("Date & Time", "x" * 129),
    ("Date & Time", ""),
    ("To", "x" * 129),
    ("To", ""),
    ("Transaction Ref", "unsupported/reference"),
    ("Transaction Ref", "x" * 129),
    ("Transaction Ref", ""),
]


@pytest.mark.parametrize("body", [CARD, WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15")])
@pytest.mark.parametrize("label, value", INVALID_CONTRACT_FIELDS)
def test_every_profile_contract_field_participates_in_completeness(body, label, value):
    fake = Notifications({"one": body})
    adapter = GmailAdapter(fake, "synthetic-contract-review")
    control = adapter.get_candidate("one").candidate
    assert extract(control, "valid-contract", control.observation)[2] == "accepted"
    fake.bodies["one"] = body + f"\n{label}: {value}"
    saved = adapter.get_candidate("one").candidate
    _, _, outcome, reasons = extract(saved, "invalid-contract", saved.observation)
    assert outcome == "uncertain" and "invalid_notification_contract" in reasons


@pytest.mark.parametrize(
    "claim_text",
    [
        "card transaction request dated unknown",
        "card transaction request dated 03/02/25e2",
        "card transaction request dated 2025-02-03",
        "card transaction request dated",
        "PayLah! Scan & Pay Transfer dated unknown",
        "PayLah! Scan & Pay Transfer dated 03 February 2025",
        "PayLah! Scan & Pay Transfer dated",
    ],
)
@pytest.mark.parametrize("body", [CARD, WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15"), BODY])
def test_unsupported_date_claims_cannot_hide_beside_valid_financial_facts(body, claim_text):
    fake = Notifications({"one": body})
    adapter = GmailAdapter(fake, "synthetic-claim-review")
    control = adapter.get_candidate("one").candidate
    assert extract(control, "valid-claim-control", control.observation)[2] == "accepted"
    fake.bodies["one"] = (
        body
        + "\nWe refer to your "
        + claim_text
        + ". We are pleased to confirm that the transaction was completed."
    )
    saved = adapter.get_candidate("one").candidate
    _, _, outcome, reasons = extract(saved, "invalid-claim", saved.observation)
    assert outcome == "uncertain" and "invalid_notification_contract" in reasons


@pytest.mark.parametrize(
    "body, competing_source, kind",
    [
        (CARD, "PayLah! Wallet (Mobile ending 8888)", "purchase"),
        (
            WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15"),
            "DBS/POSB card ending 9999",
            "transfer",
        ),
    ],
)
def test_profile_source_types_cannot_bypass_single_source_attribution(
    tmp_path, body, competing_source, kind
):
    service, fake, _ = setup(tmp_path, {"one": body})
    original = load(service)
    assert heads(original)[0].review.status == "accepted"
    fake.bodies["one"] = body + "\nFrom: " + competing_source
    document = load(service, "refresh")
    assert heads(document)[0].review.status == "pending" and total(document, kind=kind) == 0
    assert "incomplete_notification_contract" in heads(document)[0].review.reason
    assert document.transactions[0] == original.transactions[0]


@pytest.mark.parametrize(
    "body, kind",
    [(CARD, "purchase"), (WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15"), "transfer")],
)
@pytest.mark.parametrize("only_field", [False, True])
def test_profile_completeness_checks_every_mime_representation(tmp_path, body, kind, only_field):
    import base64
    from html import escape

    service, fake, client = setup(tmp_path, {"one": body})
    original_message = fake.get_message
    alternative = body

    def with_alternative(identity):
        message = original_message(identity)
        raw = "".join(f"<p>{escape(line)}</p>" for line in alternative.splitlines()).encode()
        message["payload"] = {
            "mimeType": "multipart/alternative",
            "parts": [
                message["payload"],
                {
                    "mimeType": "text/html",
                    "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
                },
            ],
        }
        return message

    fake.get_message = with_alternative
    original = load(service)
    assert heads(original)[0].review.status == "accepted"
    alternative = ("" if only_field else body) + "\nFrom: unsupported source"
    document = load(service, "refresh")
    transaction = heads(document)[0]
    assert transaction.review.status == "pending" and total(document, kind=kind) == 0
    assert "invalid_notification_contract" in transaction.review.reason
    assert document.transactions[0] == original.transactions[0]
    assert "Excluded from totals" in client.get("/saved?month=2025-02").text
    saved = candidate(service, fake)
    parts = {part.locator: part for part in saved.parts}
    for fact in (transaction.amount, transaction.occurred_on, transaction.account_id):
        for ref in fact.evidence:
            _, _, locator, _, start, end = ref.locator.split(":")
            assert parts[locator].text[int(start) : int(end)] == ref.quote
    assert load(service, "refresh") == document


def test_profile_completeness_does_not_reclassify_ordinary_transport_metadata(tmp_path):
    body = BODY + (
        "\nFrom: ordinary@example.invalid\nTo: recipient@example.invalid"
        "\nDate & Time: unsupported transport date\nTransaction Ref: unsupported/reference"
    )
    service, _, _ = setup(tmp_path, {"one": body})
    document = load(service)
    transaction = heads(document)[0]
    assert transaction.review.status == "accepted" and total(document) == 1234
    assert transaction.direction.value is transaction.card_id.value is None


def test_known_date_order_requires_corresponding_financial_day_month(tmp_path):
    service, _, _ = setup(tmp_path, {"one": CARD.replace("03 FEB", "04 FEB")})
    transaction = heads(load(service))[0]
    assert (
        transaction.review.status == "pending" and transaction.occurred_on.status == "conflicting"
    )
    assert set(transaction.occurred_on.alternatives) == {date(2025, 2, 3), date(2025, 2, 4)}


def test_fixed_two_digit_year_convention_is_not_a_sliding_receipt_year(tmp_path):
    service, fake, _ = setup(tmp_path, {"one": CARD.replace("03/02/25", "03/02/99")})
    source = candidate(service, fake)
    facts, _, outcome, _ = extract(source, "fictional-version", source.observation)
    assert outcome == "accepted" and facts["occurred_on"].value == date(1999, 2, 3)
    fake.bodies["one"] = CARD.replace("03/02/25", "03/02/68")
    source = candidate(service, fake)
    facts, _, outcome, reasons = extract(source, "fictional-version", source.observation)
    assert facts["occurred_on"].value == date(2068, 2, 3)
    assert outcome == "uncertain" and "completed_event_after_observation" in reasons
    fake.bodies["one"] = CARD.replace("03/02/25", "03/02/99").replace(
        "03 FEB 09:15", "03 FEB 1999 09:15"
    )
    source = candidate(service, fake)
    assert extract(source, "fictional-version", source.observation)[0]["occurred_on"].value == date(
        1999, 2, 3
    )


def test_yearless_wallet_transfer_retains_unknown_year_even_with_received_timestamp(tmp_path):
    service, _, _ = setup(tmp_path, {"one": WALLET})
    document = load(service)
    transaction = heads(document)[0]
    assert transaction.kind.value == "transfer" and transaction.direction.value == "outflow"
    assert transaction.account_id.value and transaction.occurred_on.value is None
    assert transaction.review.status == "pending" and total(document) == 0
    assert "financial_event_year_not_stated" in transaction.review.reason
    assert transaction.transfer_purpose.value is None and transaction.card_id.value is None


def test_explicit_wallet_financial_year_can_qualify_without_receipt_substitution(tmp_path):
    service, _, _ = setup(tmp_path, {"one": WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15")})
    transaction = heads(load(service))[0]
    assert transaction.review.status == "accepted" and transaction.kind.value == "transfer"
    assert transaction.occurred_on.value == date(2025, 2, 3)
    assert transaction.merchant.value is transaction.transfer_purpose.value is None


@pytest.mark.parametrize(
    "body",
    ["Fwd: fictional\n" + CARD, CARD + "\n" + CARD.replace("EXAMPLE-REF-A", "EXAMPLE-REF-B")],
)
def test_forwarded_or_combined_notices_are_not_folded_into_one_purchase(tmp_path, body):
    service, _, _ = setup(tmp_path, {"one": body})
    document = load(service)
    assert total(document) == 0 and len(heads(document)) == 1
    assert "forwarded_or_multiple_financial_events" in heads(document)[0].review.reason


@pytest.mark.parametrize(
    "body",
    [
        "Your latest eStatement is ready for viewing. Fictional product, no transactions attached.",
        "We refer to your request to update your email address. This is not a payment.",
        "You're almost done setting Google up on a fictional device.\nFictional LLC 1234",
        "You've sent a recovery contact request. No financial activity.",
    ],
)
def test_explicit_administrative_notices_are_non_transactions_without_ledger_noise(tmp_path, body):
    service, _, _ = setup(tmp_path, {"one": body})
    assert not load(service).transactions
    assert (
        next(iter(service.cache.outcomes(service.namespace, REVISION).values()))["outcome"]
        == "non_transaction"
    )


def test_inert_html_padding_does_not_make_a_short_field_oversized(tmp_path):
    service, _, _ = setup(
        tmp_path, {"one": CARD.replace("Transaction Ref:", "\n" * 700 + "Transaction Ref:")}
    )
    assert heads(load(service))[0].review.status == "accepted"


def test_html_profile_normalization_preserves_financial_bindings(tmp_path):
    import base64
    from html import escape

    service, fake, _ = setup(
        tmp_path,
        {
            "one": "".join(f"<p>{escape(line)}</p>" for line in CARD.splitlines())
            + "<script>Amount: USD999.99</script>"
        },
    )
    original = fake.get_message

    def html_message(identity):
        message = original(identity)
        message["payload"]["mimeType"] = "text/html"
        assert base64.urlsafe_b64decode(message["payload"]["body"]["data"])
        return message

    fake.get_message = html_message
    assert heads(load(service))[0].review.status == "accepted"


@pytest.mark.parametrize("financial", [False, True])
def test_authentication_subject_is_nonfinancial_or_a_financial_conflict(tmp_path, financial):
    service, fake, _ = setup(
        tmp_path,
        {"one": CARD if financial else "Fictional sign-in diagnostic, no credential value"},
    )
    original = fake.get_message

    def auth_subject(identity):
        message = original(identity)
        message["payload"]["headers"] = [{"name": "Subject", "value": "Verification code"}]
        return message

    fake.get_message = auth_subject
    document = load(service)
    assert total(document) == 0
    if financial:
        assert heads(document)[0].review.status == "pending"
    else:
        assert not document.transactions


def test_learning_and_financial_revision_upgrade_retains_previous_private_and_ledger_history(
    tmp_path, monkeypatch
):
    service, _, _ = setup(tmp_path, {"one": CARD})
    with monkeypatch.context() as old:
        old.setattr(
            "spending_optimiser.local_templates.LEARNING_REVISION", "fictional-legacy-learning"
        )
        old.setattr("spending_optimiser.processing.REVISION", "fictional-legacy-processing")
        original = load(service)
    with service.cache.ledger.connection() as db:
        prior = db.execute(
            "SELECT * FROM mail_template_sources WHERE revision='fictional-legacy-learning'"
        ).fetchall()
    current = load(service, "refresh")
    assert len(heads(current)) == 1 and len(current.transactions) == 2
    assert current.transactions[0] == original.transactions[0]
    assert total(current) == 1234
    with service.cache.ledger.connection() as db:
        assert (
            db.execute(
                "SELECT * FROM mail_template_sources WHERE revision='fictional-legacy-learning'"
            ).fetchall()
            == prior
        )
        assert db.execute("SELECT count(*) FROM mail_template_sources").fetchone()[0] == 2
