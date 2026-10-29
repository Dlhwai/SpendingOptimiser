"""Synthetic production-path regressions for representation, heading and status evidence."""

import base64
from html import escape

import pytest
from test_local_templates import NARRATIVE, candidate
from test_notification_profiles import CARD, WALLET
from test_processing import BODY, Notifications, load, setup, total

from spending_optimiser.gmail import GmailAdapter
from spending_optimiser.ledger import heads
from spending_optimiser.local_templates import LocalTemplates, apply_generic
from spending_optimiser.processing import extract

DATED_WALLET = WALLET.replace("03 Feb 09:15", "03 Feb 2025 09:15")
GENERIC_CORES = [
    ("Type: Purchase", "Amount: SGD 12.34", "Event date: 2025-02-03", "Account: wallet-blue"),
    (
        "Type: Purchase",
        "Amount: 12.34",
        "Currency: SGD",
        "Event date: 2025-02-03",
        "Account: wallet-blue",
    ),
    ("Type: Purchase", "Amount (SGD): 12.34", "Event date: 2025-02-03", "Account: wallet-blue"),
]
LAYOUTS = [(BODY, "purchase", 1234), (CARD, "purchase", 1234), (DATED_WALLET, "transfer", 789)]
HEADED_ASSERTIONS = [
    ("Purchase confirmation", "Your purchase of", "", "purchase"),
    ("Refund confirmation", "Your refund of", "", "refund"),
    ("Funds transfer confirmation", "Your transfer of", "", "transfer"),
    ("PayNow transfer notification", "Your PayNow transfer of", "", "transfer"),
    ("Purchase confirmation", "An amount of", " was debited", "purchase"),
    ("Purchase confirmation", "An amount of", " was credited", "purchase"),
]


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Synthetic qualification must not access external services")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("spending_optimiser.extraction.NotificationExtractor.extract", forbidden)


def render(fake, other=None, subject=None, reverse=False):
    state = {"other": other, "subject": subject}
    original = fake.get_message

    def message(identity):
        value = original(identity)
        if state["other"] is not None:
            raw = "".join(f"<p>{escape(line)}</p>" for line in state["other"].splitlines()).encode()
            parts = [
                value["payload"],
                {
                    "mimeType": "text/html",
                    "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
                },
            ]
            value["payload"] = {
                "mimeType": "multipart/alternative",
                "parts": parts[::-1] if reverse else parts,
            }
        value["payload"]["headers"] = (
            [] if state["subject"] is None else [{"name": "Subject", "value": state["subject"]}]
        )
        return value

    fake.get_message = message
    return state


def inspect(body, other=None, subject=None, reverse=False):
    fake = Notifications({"one": body})
    render(fake, other, subject, reverse)
    saved = GmailAdapter(fake, "synthetic-qualification").get_candidate("one").candidate
    return saved, extract(saved, "synthetic-version", saved.observation)


def assert_spans(saved, facts):
    parts = {part.locator: part.text for part in saved.parts}
    for fact in facts.values():
        for ref in fact.evidence:
            _, _, locator, _, start, end = ref.locator.split(":")
            assert parts[locator][int(start) : int(end)] == ref.quote


@pytest.mark.parametrize(
    "lines, mask",
    [(lines, mask) for lines in GENERIC_CORES for mask in range(1, 2 ** len(lines) - 1)],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_r12_complementary_generic_representations_cannot_manufacture_a_transaction(
    lines, mask, reverse
):
    complete = "\n".join(lines)
    _, control = inspect(complete, complete, reverse=reverse)
    assert control[2] == "accepted" and control[0]["amount"].value.minor_units == 1234
    left = "\n".join(line for index, line in enumerate(lines) if mask & (1 << index))
    right = "\n".join(line for index, line in enumerate(lines) if not mask & (1 << index))
    saved, result = inspect(left, right, reverse=reverse)
    assert result[2] == "uncertain" and result[3]
    assert result[0] == {} and result[1] == []
    assert_spans(saved, result[0])


@pytest.mark.parametrize(
    "fragment",
    [
        *GENERIC_CORES[0],
        "Currency: SGD",
        "Bank: Fictional Bank",
        "Merchant: Fictional Cafe",
        "Posting date: 2025-02-04",
    ],
)
def test_r12_a_complete_generic_alternative_does_not_qualify_a_partial_financial_sibling(fragment):
    _, control = inspect(BODY, BODY)
    assert control[2] == "accepted"
    _, result = inspect(BODY, fragment)
    assert result[2] != "accepted" and result[3]


def test_r12_numeric_account_institution_evidence_cannot_be_donated_by_a_sibling():
    complete = BODY.replace("wallet-blue", "1234 5678 9012") + "\nBank: Fictional Bank"
    _, control = inspect(complete, complete)
    assert control[2] == "accepted"
    _, result = inspect(complete, complete.replace("\nBank: Fictional Bank", ""))
    assert result[2] != "accepted" and result[3]


@pytest.mark.parametrize("body, kind, minor", LAYOUTS[1:])
@pytest.mark.parametrize("slot", ["claim", "source", "clock", "payee", "reference", "amount"])
def test_r12_profile_fragments_must_qualify_before_aggregation(body, kind, minor, slot):
    _, control = inspect(body, body)
    assert control[2] == "accepted" and control[0]["amount"].value.minor_units == minor
    if body == CARD:
        fields = {
            "claim": body.splitlines()[2],
            "source": "From: DBS/POSB card ending 9999",
            "clock": "Date & Time: 03 FEB 09:15 (SGT)",
            "payee": "To: Fictional Merchant",
            "reference": "Transaction Ref: EXAMPLE-REF-A",
            "amount": "Amount: SGD12.34",
        }
    else:
        fields = {
            "claim": body.splitlines()[2],
            "source": "From: PayLah! Wallet (Mobile ending 8888)",
            "clock": "Date & Time: 03 Feb 2025 09:15 (SGT)",
            "payee": "To: Fictional Recipient",
            "reference": "Transaction Ref: EXAMPLE-WALLET-A",
            "amount": "Amount: SGD7.89",
        }
    fragment = fields[slot]
    _, result = inspect(body, fragment)
    assert result[2] != "accepted" and result[3]
    _, complementary = inspect(
        fragment, "\n".join(value for key, value in fields.items() if key != slot)
    )
    assert complementary[2] != "accepted" and complementary[3]


@pytest.mark.parametrize("body, kind, minor", LAYOUTS)
@pytest.mark.parametrize(
    "fragment", ["Fictional delivery footer", "Generated: 2025-02-03", "Read at 09:15"]
)
def test_r12_irrelevant_parts_are_not_required_to_be_transactions(body, kind, minor, fragment):
    _, result = inspect(body, fragment)
    assert result[2] == "accepted" and result[0]["amount"].value.minor_units == minor


@pytest.mark.parametrize(
    "left, right, kind",
    [
        (
            "Type: Purchase\nAmount: SGD 12.34",
            "Event date: 2025-02-03\nAccount: wallet-blue",
            "purchase",
        ),
        (CARD, "Transaction Ref: EXAMPLE-REF-A", "purchase"),
        (DATED_WALLET, "To: Fictional Recipient", "transfer"),
    ],
)
def test_r12_saved_partial_alternatives_are_inspectable_and_excluded(tmp_path, left, right, kind):
    service, fake, client = setup(tmp_path, {"one": left})
    render(fake, right)
    document = load(service)
    assert total(document, kind=kind) == 0 and not document.transactions
    saved = candidate(service, fake)
    templates = LocalTemplates(service.cache)
    with service.cache.ledger.connection() as db:
        digest = db.execute("SELECT digest FROM mail_current WHERE message_id='one'").fetchone()[0]
    assert service.cache.version(service.namespace, "one", digest).parts == saved.parts
    assert (
        templates.apply(service.namespace, "one", digest, saved, saved.observation)[1] != "accepted"
    )
    results = [
        apply_generic(form, saved, digest, saved.observation)
        for form in templates.generic_forms()["forms"]
    ]
    assert results and all(result[1] != "accepted" for result in results)
    text = client.get("/saved?month=2025-02").text
    assert "Excluded from totals" in text and escape(right.splitlines()[0]) in text
    assert load(service, "refresh") == document


@pytest.mark.parametrize("heading, prefix, verb, kind", HEADED_ASSERTIONS)
@pytest.mark.parametrize("alternative", [False, True])
def test_r13_one_consistent_heading_and_assertion_remains_one_event(
    tmp_path, heading, prefix, verb, kind, alternative
):
    assertion = (
        f"{prefix} USD 19.25{verb} on 3 February 2025 using account reference travel-wallet."
    )
    control = assertion if not verb else "Type: Purchase\n" + assertion
    body = heading + "\n" + assertion
    _, positive = inspect(control, control if alternative else None)
    assert positive[2] == "accepted"
    saved, result = inspect(body, body if alternative else None)
    assert result[2] == "accepted" and not result[3]
    assert result[0]["kind"].value == kind and result[0]["amount"].value.minor_units == 1925
    assert_spans(saved, result[0])
    service, fake, _ = setup(tmp_path, {"one": body})
    render(fake, body if alternative else None)
    document = load(service)
    assert (
        heads(document)[0].review.status == "accepted"
        and total(document, currency="USD", kind=kind) == 1925
    )
    assert load(service, "refresh") == document


@pytest.mark.parametrize("kind", ["purchase", "refund", "transfer"])
@pytest.mark.parametrize("change", ["contradictory-heading", "repeated", "differing", "mixed"])
def test_r13_reconciliation_does_not_disable_conflict_or_assertion_cardinality(kind, change):
    headings = {
        "purchase": "Purchase confirmation",
        "refund": "Refund confirmation",
        "transfer": "Funds transfer confirmation",
    }
    assertion = (
        f"Your {kind} of USD 19.25 on 3 February 2025 using account reference travel-wallet."
    )
    if change == "contradictory-heading":
        body = (
            ("Refund confirmation" if kind != "refund" else "Purchase confirmation")
            + "\n"
            + assertion
        )
    else:
        second = assertion if change == "repeated" else assertion.replace("19.25", "20.00")
        if change == "mixed":
            second = "An amount of USD 19.25 was credited on 3 February 2025 using account reference travel-wallet."
        body = headings[kind] + "\n" + assertion + "\n" + second
    _, result = inspect(body)
    assert result[2] != "accepted" and result[3]


@pytest.mark.parametrize("body, kind, minor", LAYOUTS)
@pytest.mark.parametrize(
    "subject",
    [
        "Payment declined",
        "Transfer failed",
        "Purchase pending",
        "Transaction unsuccessful",
        "Payment cancelled",
        "Request rejected",
        "Payment not completed",
        "Balance update",
        "Promotional offer",
    ],
)
def test_r14_failure_subjects_exclude_success_shaped_bodies(tmp_path, body, kind, minor, subject):
    for positive_subject in (None, "Fictional successful notification"):
        _, positive = inspect(body, subject=positive_subject)
        assert positive[2] == "accepted" and positive[0]["amount"].value.minor_units == minor
    _, result = inspect(body, subject=subject)
    assert result[2] != "accepted" and result[3]
    service, fake, client = setup(tmp_path, {"one": body})
    state = render(fake)
    original = load(service)
    assert total(original, kind=kind) == minor
    state["subject"] = subject
    document = load(service, "refresh")
    assert total(document, kind=kind) == 0 and heads(document)[0].review.status == "pending"
    assert document.transactions[0] == original.transactions[0]
    saved = candidate(service, fake)
    with service.cache.ledger.connection() as db:
        digest = db.execute("SELECT digest FROM mail_current WHERE message_id='one'").fetchone()[0]
    templates = LocalTemplates(service.cache)
    assert (
        templates.apply(service.namespace, "one", digest, saved, saved.observation)[1] != "accepted"
    )
    results = [
        apply_generic(form, saved, digest, saved.observation)
        for form in templates.generic_forms()["forms"]
    ]
    assert results and all(result[1] != "accepted" for result in results)
    text = client.get("/saved?month=2025-02").text
    assert escape(subject) in text and "Excluded from totals" in text
    assert service.cache.version(service.namespace, "one", digest).subject == subject
    assert load(service, "refresh") == document


@pytest.mark.parametrize("body, kind, minor", LAYOUTS)
@pytest.mark.parametrize("fragment", ["Card/Acct No.: ****1234", "Transaction Alert"])
@pytest.mark.parametrize("reverse", [False, True])
def test_r15_all_consumed_financial_roles_require_qualified_representations(
    tmp_path, body, kind, minor, fragment, reverse
):
    for other in (None, body, "Fictional delivery footer"):
        _, control = inspect(body, other, reverse=reverse)
        assert control[2] == "accepted" and control[0]["amount"].value.minor_units == minor
    saved, result = inspect(body, fragment, reverse=reverse)
    assert result[2] == "uncertain" and "incomplete_financial_representations" in result[3]
    assert result[0] == {} and result[1] == []
    service, fake, client = setup(tmp_path, {"one": body})
    state = render(fake, body, reverse=reverse)
    original = load(service)
    assert total(original, kind=kind) == minor
    state["other"] = fragment
    document = load(service, "refresh")
    assert total(document, kind=kind) == 0 and heads(document)[0].review.status == "pending"
    assert document.transactions[0] == original.transactions[0]
    saved = candidate(service, fake)
    with service.cache.ledger.connection() as db:
        digest = db.execute("SELECT digest FROM mail_current WHERE message_id='one'").fetchone()[0]
    templates = LocalTemplates(service.cache)
    assert (
        templates.apply(service.namespace, "one", digest, saved, saved.observation)[1]
        == "uncertain"
    )
    results = [
        apply_generic(form, saved, digest, saved.observation)
        for form in templates.generic_forms()["forms"]
    ]
    assert results and all(result[1] != "accepted" for result in results)
    assert service.cache.version(service.namespace, "one", digest).parts == saved.parts
    text = client.get("/saved?month=2025-02").text
    assert "Excluded from totals" in text and fragment in text
    assert load(service, "refresh") == document


@pytest.mark.parametrize(
    "fragment", ["2025-02-03", "09:15", "****1234", "Fictional delivery footer"]
)
def test_r15_inert_structural_slots_remain_irrelevant(fragment):
    _, result = inspect(BODY, fragment)
    assert result[2] == "accepted" and result[0]["amount"].value.minor_units == 1234


@pytest.mark.parametrize("fragment", ["Card/Acct No.: ****1234", "Transaction Alert"])
def test_r15_attachments_do_not_become_financial_representations(fragment):
    fake = Notifications({"one": BODY})
    original = fake.get_message

    def with_attachment(identity):
        message = original(identity)
        raw = fragment.encode()
        message["payload"] = {
            "mimeType": "multipart/mixed",
            "parts": [
                message["payload"],
                {
                    "mimeType": "text/plain",
                    "filename": "fictional.txt",
                    "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
                },
            ],
        }
        return message

    fake.get_message = with_attachment
    saved = GmailAdapter(fake, "synthetic-attachment-control").get_candidate("one").candidate
    assert len(saved.parts) == 1
    assert extract(saved, "synthetic-attachment", saved.observation)[2] == "accepted"


@pytest.mark.parametrize("body, kind, minor", LAYOUTS)
@pytest.mark.parametrize(
    "other_subject",
    [
        "Payment declined",
        "Verification code",
        "Fwd: Fictional confirmation",
        "Refund notification",
        "Another ordinary confirmation",
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_r16_ambiguous_subject_diagnosis_remains_uncertainty(
    tmp_path, body, kind, minor, other_subject, reverse
):
    service, fake, client = setup(tmp_path, {"one": body})
    original_message = fake.get_message
    subjects = []

    def with_subjects(identity):
        message = original_message(identity)
        message["payload"]["headers"] = [{"name": "Subject", "value": value} for value in subjects]
        return message

    fake.get_message = with_subjects
    subjectless = candidate(service, fake)
    assert subjectless.subject is None and "ambiguous_subject" not in subjectless.warnings
    assert extract(subjectless, "genuinely-subjectless", subjectless.observation)[2] == "accepted"
    subjects[:] = ["Fictional successful notification"]
    original = load(service)
    assert total(original, kind=kind) == minor
    subjects[:] = ["Fictional successful notification", other_subject]
    if reverse:
        subjects.reverse()
    saved = candidate(service, fake)
    assert saved.subject is None and "ambiguous_subject" in saved.warnings
    assert [header.value for header in saved.headers if header.name == "subject"] == subjects
    facts, _, outcome, reasons = extract(saved, "ambiguous-subject", saved.observation)
    assert outcome == "uncertain" and "ambiguous_subject" in reasons
    assert facts["amount"].value.minor_units == minor
    document = load(service, "refresh")
    assert total(document, kind=kind) == 0 and heads(document)[0].review.status == "pending"
    assert document.transactions[0] == original.transactions[0]
    assert "ambiguous_subject" in heads(document)[0].review.reason
    with service.cache.ledger.connection() as db:
        digest = db.execute("SELECT digest FROM mail_current WHERE message_id='one'").fetchone()[0]
    retained = service.cache.version(service.namespace, "one", digest)
    assert retained.headers == saved.headers and retained.warnings == saved.warnings
    templates = LocalTemplates(service.cache)
    assert (
        "ambiguous_subject"
        in templates.apply(service.namespace, "one", digest, saved, saved.observation)[2]
    )
    results = [
        apply_generic(form, saved, digest, saved.observation)
        for form in templates.generic_forms()["forms"]
    ]
    assert results and all(result[1] != "accepted" for result in results)
    text = client.get("/saved?month=2025-02").text
    assert all(escape(subject) in text for subject in subjects)
    assert "ambiguous_subject" in text and "Excluded from totals" in text
    assert load(service, "refresh") == document


@pytest.mark.parametrize("missing", GENERIC_CORES[0])
def test_r14_subject_does_not_supply_missing_financial_facts(missing):
    body = "\n".join(line for line in GENERIC_CORES[0] if line != missing)
    _, result = inspect(
        body, subject="Purchase of SGD 12.34 on 3 February 2025 using account reference wallet-blue"
    )
    assert result[2] != "accepted"
