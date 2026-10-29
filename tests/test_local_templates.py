"""Genuinely synthetic notification layouts, never private-derived examples.

Production discovery is exercised before reuse; no ready template or ledger is seeded.
"""

import base64
import json

import pytest
from test_monthly import connected
from test_processing import BODY, PERIOD, Notifications, load, setup, total

from spending_optimiser.gmail import GmailAdapter
from spending_optimiser.ledger import heads
from spending_optimiser.local_templates import (
    LocalTemplates,
    apply_generic,
    bundled_forms,
    discovery_pending,
    generic_asset,
    validate_asset,
    validate_generic,
)
from spending_optimiser.mail_cache import CANDIDATE
from spending_optimiser.processing import extract

NARRATIVE = (
    "Dear Fictional Reader,\nYour purchase of USD 19.25 on 3 February 2025 "
    "using account reference travel-wallet.\nThank you for using this fictional service."
)
HTML = (
    "<html><body><h1>Fictional notification</h1><table>"
    "<tr><td>Transaction type</td><td>Purchase</td></tr>"
    "<tr><td>Transaction amount</td><td>SGD 12.34</td></tr>"
    "<tr><td>Transaction date</td><td>2025-02-03</td></tr>"
    "<tr><td>Account reference</td><td>wallet-blue</td></tr>"
    "</table><script>throw new Error('synthetic sentinel')</script>"
    "<img src='https://never-fetch.invalid/pixel'></body></html>"
)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Synthetic local templates must not call external services")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("spending_optimiser.extraction.NotificationExtractor.extract", forbidden)


def definitions(service):
    with service.cache.ledger.connection() as db:
        return db.execute("SELECT count(*) FROM mail_templates").fetchone()[0]


def candidate(service, fake, key="one"):
    return GmailAdapter(fake, service.namespace).get_candidate(key).candidate


def save_candidate(service, saved):
    service.cache.save(
        service.namespace, PERIOD, service.cache.state(service.namespace, PERIOD), [saved]
    )
    with service.cache.ledger.connection() as db:
        return db.execute(
            "SELECT digest FROM mail_current WHERE namespace=? AND message_id=?",
            (service.namespace, saved.observation.source_record_id),
        ).fetchone()[0]


def test_actual_load_learns_then_reuses_non_labelled_layout_across_values_and_restart(tmp_path):
    service, fake, client = setup(tmp_path, {"one": NARRATIVE})
    assert not service.cache.ledger.load().transactions
    doc = load(service)
    (first,) = heads(doc)
    assert first.review.status == "accepted" and total(doc, currency="USD") == 1925
    assert first.direction.value is first.card_id.value is first.posted_on.value is None
    assert definitions(service) == 1
    fake.bodies["two"] = (
        NARRATIVE.replace("19.25", "88.50")
        .replace("3 February", "7 February")
        .replace("travel-wallet", "daily-wallet")
    )
    new = candidate(service, fake, "two")
    templates = LocalTemplates(service.cache)
    # Apply to a distinct message BEFORE learning it: the definition really is reusable.
    facts, outcome, reasons = templates.apply(
        service.namespace, "two", "synthetic-version", new, new.observation
    )
    assert outcome == "accepted" and not reasons and facts["amount"].value.minor_units == 8850
    fake.pages = {None: {"messages": [{"id": "one"}, {"id": "two"}]}}
    doc = load(service, "refresh")
    assert definitions(service) == 1 and len(doc.transactions) == 2
    assert total(doc, currency="USD") == 10775
    restarted, _ = connected(service.cache.ledger.path, fake)
    assert load(restarted, "refresh") == doc
    assert "2 accepted transactions" in client.get("/saved?month=2025-02").text
    with service.cache.ledger.connection() as db:
        assert db.execute("SELECT count(*) FROM mail_template_sources").fetchone()[0] == 2


def test_changed_structure_requires_new_discovery_and_never_invents_missing_slots(tmp_path):
    service, fake, _ = setup(tmp_path, {"one": NARRATIVE})
    load(service)
    fake.bodies["two"] = "New fictional heading\n" + NARRATIVE
    new = candidate(service, fake, "two")
    templates = LocalTemplates(service.cache)
    assert templates.apply(service.namespace, "two", "new-version", new, new.observation)[2] == (
        "layout_not_discovered",
    )
    digest = save_candidate(service, new)
    templates.learn(service.namespace, "two", digest, new)
    assert templates.apply(service.namespace, "two", digest, new, new.observation)[1] == "accepted"
    assert definitions(service) == 2
    fake.bodies["three"] = NARRATIVE.replace(" on 3 February 2025", "")
    incomplete = candidate(service, fake, "three")
    digest = save_candidate(service, incomplete)
    templates.learn(service.namespace, "three", digest, incomplete)
    facts, outcome, _ = templates.apply(
        service.namespace, "three", digest, incomplete, incomplete.observation
    )
    assert outcome == "uncertain" and facts["occurred_on"].value is None


def with_html(fake, *, plain=False, conflicting=False):
    original = fake.get_message

    def get_message(identity):
        message = original(identity)
        raw = HTML.replace("12.34", "99.00") if conflicting else HTML
        raw = raw.encode()
        html = {
            "mimeType": "text/html",
            "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
        }
        if plain:
            message["payload"] = {
                "mimeType": "multipart/alternative",
                "parts": [message["payload"], html],
            }
        else:
            message["payload"] = html
        return message

    fake.get_message = get_message


@pytest.mark.parametrize("plain", [False, True])
def test_html_tables_and_matching_alternatives_are_inert_single_events(tmp_path, plain):
    service, fake, client = setup(tmp_path)
    with_html(fake, plain=plain)
    doc = load(service)
    assert len(doc.transactions) == 1 and total(doc) == 1234
    assert definitions(service) == 1
    (t,) = doc.transactions
    assert t.amount.evidence and all("mail-version:" in ref.locator for ref in t.amount.evidence)
    text = client.get("/saved?month=2025-02").text
    assert "synthetic sentinel" not in text and "never-fetch.invalid" not in text
    assert load(service, "refresh") == doc


def test_conflicting_alternatives_are_preserved_and_excluded(tmp_path):
    service, fake, _ = setup(tmp_path)
    with_html(fake, plain=True, conflicting=True)
    doc = load(service)
    (t,) = doc.transactions
    assert t.review.status == "pending" and t.amount.status == "conflicting" and total(doc) == 0
    assert {m.minor_units for m in t.amount.alternatives} == {1234, 9900}
    assert len({ref.locator for ref in t.amount.evidence}) == 2


@pytest.mark.parametrize(
    "body",
    [
        NARRATIVE.replace("USD", "$"),
        NARRATIVE.replace("3 February 2025", "03/02/2025"),
        NARRATIVE.replace("travel-wallet", "ending-1234"),
        NARRATIVE.replace("purchase", "transaction"),
        NARRATIVE + "\nAvailable balance: USD 999.00",
        NARRATIVE.replace("19.25", "19.2345"),
        NARRATIVE.replace("19.25", "19.2.5"),
        NARRATIVE.replace(".\nThank", " is pending.\nThank"),
        "Example: " + NARRATIVE.split("\n")[1],
    ],
)
def test_structure_or_self_match_does_not_supply_missing_or_ambiguous_semantics(tmp_path, body):
    service, _, _ = setup(tmp_path, {"one": body})
    doc = load(service)
    assert total(doc, currency="USD") == 0
    assert all(t.review.status != "accepted" for t in doc.transactions)


def test_complete_account_requires_institution_and_never_maps_a_masked_card(tmp_path):
    full = BODY.replace("wallet-blue", "123456789012") + "\nBank: Fictional Bank"
    service, fake, _ = setup(tmp_path, {"one": full})
    doc = load(service)
    (t,) = doc.transactions
    assert t.review.status == "accepted" and t.card_id.value is None
    assert "123456789012" not in doc.accounts[0].label
    assert len(t.account_id.evidence) == 2
    fake.bodies["one"] = full.replace("123456789012", "****1234")
    changed = load(service, "refresh")
    assert total(changed) == 0 and heads(changed)[0].account_id.value is None


def test_all_saved_versions_teach_boundedly_but_only_current_version_creates_facts(tmp_path):
    service, fake, client = setup(tmp_path)
    for amount in range(1, 104):
        fake.bodies["one"] = BODY.replace("12.34", f"{amount}.00")
        saved = candidate(service, fake)
        state = {
            **service.cache.state(service.namespace, PERIOD),
            "started": True,
            "exhausted": True,
        }
        service.cache.save(service.namespace, PERIOD, state, [saved])
    assert discovery_pending(service.cache, service.namespace) == 103
    assert (
        "103 saved versions awaiting local template discovery"
        in client.get("/gmail?month=2025-02").text
    )
    assert not service.cache.ledger.load().transactions
    calls = len(fake.calls)
    doc = load(service)
    assert len(doc.transactions) == 1 and total(doc) == 10300
    assert discovery_pending(service.cache, service.namespace) == 2
    assert "Continue saved processing" in client.get("/gmail?month=2025-02").text
    assert load(service) == doc and len(fake.calls) == calls
    assert discovery_pending(service.cache, service.namespace) == 0
    assert definitions(service) == 1


def test_namespace_isolation_and_human_rejection_survive_learning_new_versions(tmp_path):
    service, fake, _ = setup(tmp_path, {"one": NARRATIVE})
    original = load(service)
    (t,) = original.transactions
    service.cache.ledger.review(
        t.revision_id, actor="Synthetic person", reason="Synthetic rejection", status="rejected"
    )
    fake.bodies["one"] = NARRATIVE.replace("19.25", "99.00")
    assert heads(load(service, "refresh"))[0].review.status == "rejected"
    other, _ = connected(service.cache.ledger.path, fake, subject="other-synthetic-account")
    templates = LocalTemplates(other.cache)
    new = candidate(other, fake)
    assert templates.apply(other.namespace, "one", "new-version", new, new.observation)[2] == (
        "layout_not_discovered",
    )
    load(other)
    assert definitions(other) == 2


def test_generic_representation_is_reusable_and_cannot_export_private_literals(tmp_path):
    sentinels = (
        "UniqueSyntheticPerson",
        "private@example.invalid",
        "https://private.invalid/secret?id=secret",
        "SECRET_VALUE_DO_NOT_EXPORT",
    )
    body = "\n".join(sentinels) + "\n" + NARRATIVE
    service, fake, _ = setup(tmp_path, {"one": body})
    load(service)
    asset = LocalTemplates(service.cache).generic_forms()
    encoded = json.dumps(asset)
    for sentinel in (
        *sentinels,
        "19.25",
        "3 February",
        "travel-wallet",
        service.namespace,
        "sha256",
    ):
        assert sentinel not in encoded
    assert set(asset) == {"version", "forms"} and len(asset["forms"]) == 1
    (form,) = asset["forms"]
    fake.bodies["two"] = NARRATIVE.replace("19.25", "21.00")
    second = candidate(service, fake, "two")
    facts, outcome, _ = apply_generic(form, second, "synthetic-second-version", second.observation)
    assert outcome == "accepted" and facts["amount"].value.minor_units == 2100
    validated = validate_generic(form)
    bypass = validated.model_copy(
        update={"parts": (validated.parts[0].model_copy(update={"rules": (sentinels[0],)}),)}
    )
    for injected in (
        bypass,
        {**form, "comment": sentinels[0]},
        {**form, "source": service.namespace},
        {"version": form["version"], "parts": [{"mime": "text/plain", "rules": [sentinels[0]]}]},
        {
            "version": form["version"],
            "parts": [{"mime": "text/plain", "rules": ["kind_label"], "example": body}],
        },
    ):
        with pytest.raises(ValueError, match="^invalid_generic_template$"):
            validate_generic(injected)
        with pytest.raises(ValueError, match="^invalid_generic_template$"):
            generic_asset([injected])


def test_discovery_binds_explicit_grid_aliases_and_separate_currency_without_guessing(tmp_path):
    body = (
        "Type of transaction: Retail purchase\nTransaction amount: 1,234.50\n"
        "Transaction currency: SGD\nTransaction date & Time: 23/02/2025 09:10:00 SGT\n"
        "Account reference: travel-wallet"
    )
    service, fake, _ = setup(tmp_path, {"one": body})
    doc = load(service)
    assert total(doc) == 123450 and heads(doc)[0].occurred_on.value.isoformat() == "2025-02-23"
    assert len(heads(doc)[0].amount.evidence) == 2
    fake.bodies["one"] = body.replace("23/02", "03/02")
    changed = load(service, "refresh")
    assert total(changed) == 0 and heads(changed)[0].occurred_on.value is None
    fake.bodies["one"] = body + "\nCurrency: USD"
    assert total(load(service, "refresh")) == 0


def test_explicit_currency_in_label_and_masked_card_ambiguity(tmp_path):
    body = BODY.replace("Amount: SGD 12.34", "Amount (SGD): 12.34")
    service, fake, _ = setup(tmp_path, {"one": body})
    assert total(load(service)) == 1234
    fake.bodies["one"] = body.replace("Account: wallet-blue", "Card/Acct No.: ****1234")
    changed = load(service, "refresh")
    assert total(changed) == 0 and heads(changed)[0].account_id.value is None


def test_assertion_reuse_never_turns_a_debit_into_a_purchase_without_type_evidence(tmp_path):
    body = (
        "An amount of USD 9.50 was debited on 23 February 2025 "
        "using account reference travel-wallet."
    )
    service, fake, _ = setup(tmp_path, {"one": body})
    first = load(service)
    assert total(first, currency="USD") == 0 and heads(first)[0].kind.value is None
    fake.bodies["one"] = "Purchase confirmation\n" + body
    assert total(load(service, "refresh"), currency="USD") == 950
    fake.bodies["one"] = NARRATIVE.replace("Your purchase", "You have made a purchase")
    assert total(load(service, "refresh"), currency="USD") == 1925


def test_bundled_generic_assets_are_closed_and_functionally_reusable(tmp_path):
    service, fake, _ = setup(tmp_path, {"one": NARRATIVE, "two": BODY})
    forms = bundled_forms()
    for key in ("one", "two"):
        saved = candidate(service, fake, key)
        assert any(
            apply_generic(form, saved, "synthetic-version", saved.observation)[1] == "accepted"
            for form in forms
        )
    with pytest.raises(ValueError, match="^invalid_generic_template$"):
        validate_asset(
            {"version": "local-structural-v1", "forms": [], "comment": "SYNTHETIC_PRIVATE_SENTINEL"}
        )


def test_persistent_learning_requires_a_real_saved_version(tmp_path):
    service, fake, _ = setup(tmp_path)
    saved = candidate(service, fake)
    templates = LocalTemplates(service.cache)
    with pytest.raises(ValueError, match="^template_source_version_missing$"):
        templates.learn(service.namespace, "one", "not-a-saved-version", saved)
    assert definitions(service) == 0


def test_semantic_html_whitespace_does_not_drop_explicit_roles(tmp_path):
    body = (
        BODY.replace("Type:", "Transaction\u00a0type:")
        .replace("Event date:", "Transaction\u00a0date:")
        .replace("Account:", "Account\u00a0reference:")
        .replace("2025-02-03", "3\u00a0February\u00a02025")
        .replace("Purchase", "Retail\u00a0purchase")
    )
    service, _, _ = setup(tmp_path, {"one": body})
    doc = load(service)
    assert total(doc) == 1234 and heads(doc)[0].occurred_on.value.isoformat() == "2025-02-03"


def test_partial_date_and_masked_slots_are_reusable_but_not_financial_authority(tmp_path):
    body = "Transaction Alert\nAmount: SGD 19.25\nAt 03-Feb-25 using card ****1234."
    service, fake, _ = setup(tmp_path, {"one": body})
    doc = load(service)
    (first,) = heads(doc)
    assert (
        total(doc) == 0
        and first.kind.value is first.occurred_on.value is first.account_id.value is None
    )
    assert first.occurred_on.evidence and first.account_id.evidence and first.kind.evidence
    fake.bodies["two"] = (
        body.replace("19.25", "20.00").replace("03-Feb-25", "04-Mar-26").replace("1234", "5678")
    )
    second = candidate(service, fake, "two")
    templates = LocalTemplates(service.cache)
    facts, outcome, reasons = templates.apply(
        service.namespace, "two", "synthetic-version", second, second.observation
    )
    assert outcome == "uncertain" and facts["occurred_on"].value is None
    assert "date_semantics_or_century_unresolved" in reasons
    assert "masked_identifier_not_account_mapping" in reasons
    assert definitions(service) == 1
    shared = json.dumps(templates.generic_forms())
    assert "unassigned_date_token" in shared and "masked_identifier_token" in shared
    assert "03-Feb-25" not in shared and "1234" not in shared


def test_unassigned_date_does_not_replace_financial_date_or_explicit_conflicts(tmp_path):
    body = BODY.replace("Event date: 2025-02-03\n", "") + "\nGenerated: 2025-02-03"
    service, fake, _ = setup(tmp_path, {"one": body})
    assert total(load(service)) == 0
    fake.bodies["one"] = BODY.replace("Event date:", "Date:")
    assert heads(load(service, "refresh"))[0].occurred_on.value is None
    fake.bodies["one"] = BODY + "\nEvent date: 2025-02-04\nGenerated: 03-Feb-25"
    doc = load(service, "refresh")
    assert heads(doc)[0].occurred_on.status == "conflicting" and total(doc) == 0
    fake.bodies["one"] = (
        BODY + "\nAccount: wallet-green\nCard ending ****1234\nType: Refund\nTransaction Alert"
    )
    doc = load(service, "refresh")
    t = heads(doc)[0]
    assert t.account_id.status == t.kind.status == "conflicting" and total(doc) == 0


def test_template_provenance_survives_lossless_archiving_restore_and_restart(tmp_path, monkeypatch):
    from spending_optimiser.storage import Storage

    monkeypatch.setattr("spending_optimiser.storage.GB", 1)
    service, fake, _ = setup(tmp_path, {"one": NARRATIVE})
    doc = load(service)
    with service.cache.ledger.connection() as db:
        before = db.execute(
            "SELECT namespace, identity, revision, shape, generic FROM mail_templates"
        ).fetchall()
        sources = db.execute("SELECT * FROM mail_template_sources").fetchall()
        assert isinstance(db.execute("SELECT payload FROM mail_versions").fetchone()[0], bytes)
    assert Storage(service.cache.ledger).restore_mail(service.namespace, PERIOD) == 1
    with service.cache.ledger.connection() as db:
        assert (
            db.execute(
                "SELECT namespace, identity, revision, shape, generic FROM mail_templates"
            ).fetchall()
            == before
        )
        assert db.execute("SELECT * FROM mail_template_sources").fetchall() == sources
    restarted, _ = connected(service.cache.ledger.path, fake)
    assert load(restarted, "refresh") == doc and definitions(restarted) == 1


def test_historical_discovery_failures_retry_fairly_and_repaired_provenance_is_retained(tmp_path):
    service, fake, _ = setup(
        tmp_path, {"bad-a": NARRATIVE, "bad-b": NARRATIVE, "z-valid": NARRATIVE}
    )
    saved = {key: candidate(service, fake, key) for key in fake.bodies}
    for value in saved.values():
        save_candidate(service, value)
    with service.cache.ledger.connection() as db:
        db.execute(
            "UPDATE mail_versions SET payload=? WHERE message_id IN ('bad-a','bad-b')",
            (b"synthetic corrupt archive",),
        )
    templates = LocalTemplates(service.cache)
    assert templates.discover(service.namespace, 1) == 1 and templates.discovery_failed == 1
    assert templates.discover(service.namespace, 1) == 1 and templates.discovery_failed == 1
    assert templates.discover(service.namespace, 1) == 1 and templates.discovery_failed == 0
    assert definitions(service) == 1 and discovery_pending(service.cache, service.namespace) == 2
    with service.cache.ledger.connection() as db:
        assert (
            db.execute("SELECT payload FROM mail_versions WHERE message_id='bad-a'").fetchone()[0]
            == b"synthetic corrupt archive"
        )
        assert db.execute("SELECT count(*) FROM mail_processed_current").fetchone()[0] == 0
        # Simulate an operator restoring the known-good SYNTHETIC version, not a
        # production repair that overwrites damaged user history.
        db.execute(
            "UPDATE mail_versions SET payload=? WHERE message_id='bad-a'",
            (CANDIDATE.dump_json(saved["bad-a"]).decode(),),
        )
    assert templates.discover(service.namespace, 1) == 1 and templates.discovery_failed == 0
    assert discovery_pending(service.cache, service.namespace) == 1
    with service.cache.ledger.connection() as db:
        assert (
            db.execute(
                "SELECT identity FROM mail_template_sources WHERE message_id='bad-a'"
            ).fetchone()[0]
            is not None
        )
        assert db.execute("SELECT count(*) FROM mail_processed_current").fetchone()[0] == 0
    assert not service.cache.ledger.load().transactions


@pytest.mark.parametrize(
    "body, currency",
    [
        (BODY, "SGD"),
        (BODY.replace("Amount: SGD 12.34", "Amount (SGD): 12.34"), "SGD"),
        (BODY.replace("Amount: SGD 12.34", "Amount: 12.34\nCurrency: SGD"), "SGD"),
        (NARRATIVE, "USD"),
        (NARRATIVE.replace("Your purchase", "Your refund"), "USD"),
        (NARRATIVE.replace("Your purchase", "Your transfer"), "USD"),
        (
            "Purchase confirmation\n"
            + NARRATIVE.split("\n")[1]
            .replace("Your purchase of", "An amount of")
            .replace(" on ", " was debited on "),
            "USD",
        ),
    ],
)
@pytest.mark.parametrize("label", ["Currency", "Transaction currency"])
def test_every_amount_path_reconciles_explicit_currency(tmp_path, body, currency, label):
    service, fake, _ = setup(tmp_path, {"one": body + f"\n{label}: {currency}"})
    original = load(service)
    assert heads(original)[0].review.status == "accepted"
    for competing in ("USD" if currency == "SGD" else "SGD", "XYZ", "dollars"):
        fake.bodies["one"] = body + f"\n{label}: {competing}"
        document = load(service, "refresh")
        transaction = heads(document)[0]
        assert transaction.review.status == "pending"
        assert total(document, currency=currency, kind=transaction.kind.value) == 0
        assert document.transactions[0] == original.transactions[0]
        assert transaction.review.reason


def test_mime_currency_signatures_agree_with_extraction(tmp_path):
    service, fake, _ = setup(tmp_path, {"one": BODY + "\nCurrency: SGD"})
    with_html(fake, plain=True)
    assert total(load(service)) == 1234
    fake.bodies["one"] = BODY + "\nCurrency: USD"
    document = load(service, "refresh")
    transaction = heads(document)[0]
    assert total(document) == 0 and transaction.review.status == "pending"
    assert "amount_currency_conflicting" in transaction.review.reason
    assert "conflicting_or_incomplete_representations" in transaction.review.reason


@pytest.mark.parametrize("reference", ["1234 5678 9012 3456", "1234-5678-9012-3456"])
@pytest.mark.parametrize("narrative", [False, True])
def test_separated_numeric_accounts_require_explicit_institution(tmp_path, reference, narrative):
    body = (
        NARRATIVE.replace("travel-wallet", reference)
        if narrative
        else BODY.replace("wallet-blue", reference)
    )
    service, fake, _ = setup(tmp_path, {"one": body})
    document = load(service)
    assert heads(document)[0].account_id.value is None
    assert heads(document)[0].review.status == "pending"
    assert not document.accounts
    fake.bodies["one"] = body + "\nInstitution: Fictional Bank"
    document = load(service, "refresh")
    transaction = heads(document)[0]
    assert transaction.review.status == "accepted"
    assert len(transaction.account_id.evidence) == 2
    assert transaction.card_id.value is None and document.accounts[0].kind == "other"
    assert total(document, currency="USD" if narrative else "SGD") == (1925 if narrative else 1234)


@pytest.mark.parametrize(
    "reference",
    ["1234.5678.9012", "1234_5678_9012", "1234.5678_9012", "1234/5678/9012"],
)
@pytest.mark.parametrize("narrative", [False, True])
@pytest.mark.parametrize("bank", [False, True])
def test_unsupported_numeric_formats_never_create_canonical_accounts(
    tmp_path, reference, narrative, bank
):
    control = NARRATIVE if narrative else BODY
    service, fake, _ = setup(tmp_path, {"one": control})
    original = load(service)
    assert heads(original)[0].review.status == "accepted"
    fake.bodies["one"] = control.replace("travel-wallet" if narrative else "wallet-blue", reference)
    if bank:
        fake.bodies["one"] += "\nBank: Fictional Bank"
    document = load(service, "refresh")
    transaction = heads(document)[0]
    assert transaction.review.status == "pending"
    assert transaction.account_id.value is None and not transaction.account_id.alternatives
    assert document.accounts == original.accounts
    assert total(document, currency="USD" if narrative else "SGD") == 0
    assert document.transactions[0] == original.transactions[0]


@pytest.mark.parametrize("reference", ["1234.5678.9012", "1234_5678_9012"])
@pytest.mark.parametrize("other", ["wallet-green", "1234 5678 9012"])
@pytest.mark.parametrize("reverse", [False, True])
def test_account_alternatives_cannot_map_unsupported_numeric_identities(
    tmp_path, reference, other, reverse
):
    references = [reference, other] if not reverse else [other, reference]
    body = BODY.replace(
        "Account: wallet-blue", "\n".join("Account: " + value for value in references)
    )
    service, _, _ = setup(tmp_path, {"one": body + "\nBank: Fictional Bank"})
    document = load(service)
    transaction = heads(document)[0]
    assert transaction.review.status == "pending"
    assert transaction.account_id.value is None and not transaction.account_id.alternatives
    assert not document.accounts and total(document) == 0


ASSERTIONS = [
    ("Your purchase of", "", "purchase"),
    ("You made a purchase for", "", "purchase"),
    ("You have made a purchase of", "", "purchase"),
    ("Your refund of", "", "refund"),
    ("Your transfer for", "", "transfer"),
    ("Your PayNow transfer of", "", "transfer"),
    ("An amount of", " was debited", "purchase"),
    ("An amount of", " was credited", "purchase"),
]
MALFORMED_MAGNITUDES = [
    "19.25e2",
    "19.25E+2",
    "19.25-",
    "19.25+",
    "19.25.6",
    "19,25",
    "-19.25",
    "+19.25",
    "19.25/2",
    "19.25_suffix",
    "1 234.50",
    "1  234.50",
    "1\t234.50",
    "1\u00a0234.50",
    "1\u202f234.50",
    "1\n234.50",
    "1\r\n234.50",
    "1 234 567",
    "19. 25",
    "19 .25",
    "19 . 25",
    "1, 234.50",
    "1 ,234.50",
    "19.25 -1",
    "19.25 + 1",
]


@pytest.mark.parametrize("prefix, verb, kind", ASSERTIONS)
@pytest.mark.parametrize("magnitude", MALFORMED_MAGNITUDES)
def test_financial_assertions_validate_whole_unsigned_amounts(prefix, verb, kind, magnitude):
    control = f"{prefix} USD 19.25{verb} on 3 February 2025 using account reference travel.wallet."
    if verb:
        control = "Purchase confirmation\n" + control
    fake = Notifications({"one": control})
    adapter = GmailAdapter(fake, "synthetic-assertion-review")
    saved = adapter.get_candidate("one").candidate
    facts, _, outcome, reasons = extract(saved, "valid-assertion", saved.observation)
    assert outcome == "accepted" and not reasons
    assert facts["amount"].value.minor_units == 1925 and facts["kind"].value == kind
    invalid = control.replace("19.25", magnitude)
    for body in (invalid, BODY + "\n" + invalid):
        fake.bodies["one"] = body
        saved = adapter.get_candidate("one").candidate
        facts, _, outcome, reasons = extract(saved, "invalid-assertion", saved.observation)
        assert outcome != "accepted" and reasons
        assert (
            not facts or facts["amount"].value is None or facts["amount"].value.minor_units == 1234
        )


@pytest.mark.parametrize("verb", ["debited", "credited"])
@pytest.mark.parametrize("value", ["XYZ -99.00", "$-99.00", "USD\n-99.00", "nonsense", ""])
def test_unsupported_competing_debit_credit_assertions_cannot_disappear(verb, value):
    fake = Notifications({"one": BODY + f"\nAn amount of {value} was {verb}."})
    saved = GmailAdapter(fake, "synthetic-debit-review").get_candidate("one").candidate
    facts, _, outcome, reasons = extract(saved, "invalid-debit", saved.observation)
    assert outcome == "uncertain" and "invalid_explicit_field" in reasons
    assert facts["amount"].value.minor_units == 1234


@pytest.mark.parametrize(
    "amount_field",
    ["Amount: SGD {number}", "Amount: {number}\nCurrency: SGD", "Amount (SGD): {number}"],
)
@pytest.mark.parametrize("number", ["1\n234.50", "1\n\t234.50", "1.\n25", "1\n\n234.50"])
def test_numeric_continuations_cannot_escape_labelled_amount_values(amount_field, number):
    control = BODY.replace("Amount: SGD 12.34", amount_field.format(number="1,234.50"))
    fake = Notifications({"one": control})
    adapter = GmailAdapter(fake, "synthetic-multiline-money")
    saved = adapter.get_candidate("one").candidate
    facts, _, outcome, _ = extract(saved, "valid-labelled-grouping", saved.observation)
    assert outcome == "accepted" and facts["amount"].value.minor_units == 123450
    fake.bodies["one"] = BODY.replace("Amount: SGD 12.34", amount_field.format(number=number))
    saved = adapter.get_candidate("one").candidate
    facts, _, outcome, reasons = extract(saved, "continuing-labelled-number", saved.observation)
    assert outcome == "uncertain" and facts["amount"].value is None
    assert "invalid_explicit_field" in reasons
    if amount_field.startswith("Amount: SGD"):
        assert "unbound_monetary_value" in reasons


@pytest.mark.parametrize("whitespace", [" ", "\t", "\u00a0", "\u202f", "\n"])
@pytest.mark.parametrize("representation", ["plain", "html", "alternative"])
def test_saved_numeric_continuations_are_inspectable_and_never_counted(
    tmp_path, whitespace, representation
):
    from html import escape

    service, fake, client = setup(
        tmp_path, {"one": NARRATIVE, "two": NARRATIVE.replace("19.25", "10.00")}
    )
    original_message = fake.get_message

    def represent(identity):
        message = original_message(identity)
        if representation != "plain":
            raw = "".join(
                f"<p>{escape(line)}</p>" for line in fake.bodies[identity].splitlines()
            ).encode()
            html = {
                "mimeType": "text/html",
                "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
            }
            message["payload"] = (
                html
                if representation == "html"
                else {"mimeType": "multipart/alternative", "parts": [message["payload"], html]}
            )
        return message

    fake.get_message = represent
    original = load(service)
    assert total(original, currency="USD") == 2925
    previous = next(t for t in heads(original) if t.amount.value.minor_units == 1925)
    old_digest = previous.amount.evidence[0].locator.split(":")[1]
    fake.bodies["one"] = NARRATIVE.replace("19.25", "1" + whitespace + "234.50")
    document = load(service, "refresh")
    changed = next(t for t in heads(document) if t.event_id == previous.event_id)
    assert changed.review.status == "pending" and changed.amount.value is None
    assert total(document, currency="USD") == 1000
    assert document.transactions[:2] == original.transactions
    assert "Excluded from totals" in client.get("/saved?month=2025-02").text
    digest = changed.kind.evidence[0].locator.split(":")[1]
    saved = service.cache.version(service.namespace, "one", digest)
    assert saved.parts == candidate(service, fake).parts
    templates = LocalTemplates(service.cache)
    result = templates.apply(service.namespace, "one", digest, saved, saved.observation)
    assert result[1] != "accepted" and "invalid_financial_assertion" in result[2]
    assert all(
        apply_generic(form, saved, digest, saved.observation)[1] != "accepted"
        for form in templates.generic_forms()["forms"]
    )
    for version in (old_digest, digest):
        response = client.get(
            "/mail-source",
            params={"observation_id": previous.origins[0].observation_id, "version": version},
        )
        assert response.status_code == 200
        assert ("19.25" if version == old_digest else "234.50") in response.text
    assert load(service, "refresh") == document


def test_unspaced_purchase_assertion_cannot_hide_an_unsupported_amount():
    fake = Notifications({"one": NARRATIVE.replace("of USD", "ofUSD")})
    adapter = GmailAdapter(fake, "synthetic-unspaced-review")
    control = adapter.get_candidate("one").candidate
    assert extract(control, "valid-unspaced", control.observation)[2] == "accepted"
    fake.bodies["one"] = BODY + "\n" + NARRATIVE.replace("of USD 19.25", "ofXYZ -99.00")
    saved = adapter.get_candidate("one").candidate
    assert extract(saved, "invalid-unspaced", saved.observation)[2] == "uncertain"


@pytest.mark.parametrize(
    "body, invalid, currency",
    [
        (NARRATIVE, NARRATIVE.replace("19.25", "19.25e2"), "USD"),
        (NARRATIVE, NARRATIVE.replace("19.25", "19.25-"), "USD"),
        (BODY, BODY + "\nAn amount of XYZ\n-99.00 was credited.", "SGD"),
    ],
)
def test_invalid_assertions_invalidate_totals_without_losing_source_history(
    tmp_path, body, invalid, currency
):
    service, fake, client = setup(tmp_path, {"one": body})
    original = load(service)
    assert heads(original)[0].review.status == "accepted"
    fake.bodies["one"] = invalid
    document = load(service, "refresh")
    assert heads(document)[0].review.status == "pending" and total(document, currency=currency) == 0
    assert document.transactions[0] == original.transactions[0]
    assert "Excluded from totals" in client.get("/saved?month=2025-02").text
    assert load(service, "refresh") == document


@pytest.mark.parametrize("magnitude", MALFORMED_MAGNITUDES)
def test_monetary_scanner_retains_unsupported_competing_magnitudes(magnitude):
    fake = Notifications({"one": BODY + "\nAvailable balance: USD " + magnitude})
    saved = GmailAdapter(fake, "synthetic-money-review").get_candidate("one").candidate
    facts, _, outcome, reasons = extract(saved, "invalid-magnitude", saved.observation)
    assert outcome == "uncertain" and "unbound_monetary_value" in reasons
    assert facts["amount"].value.minor_units == 1234


@pytest.mark.parametrize("punctuation", ["", ".", ";", ":", "!", "?"])
@pytest.mark.parametrize(
    "money, code, minor",
    [
        ("USD 1,234.56", "USD", 123456),
        ("JPY 19", "JPY", 19),
        ("KWD 19.250", "KWD", 19250),
        ("USD 1", "USD", 100),
        ("USD 1234.50", "USD", 123450),
        ("JPY 1,234", "JPY", 1234),
        ("USD\u00a01,234.50", "USD", 123450),
        ("S$ 12.34", "SGD", 1234),
    ],
)
def test_valid_narrative_money_precision_boundaries_and_spans(punctuation, money, code, minor):
    body = (
        f"Your purchase of {money}{punctuation} on 3 February 2025 "
        "using account reference travel.wallet."
    )
    saved = (
        GmailAdapter(Notifications({"one": body}), "synthetic-money-control")
        .get_candidate("one")
        .candidate
    )
    facts, _, outcome, reasons = extract(saved, "valid-money", saved.observation)
    assert outcome == "accepted" and not reasons
    assert facts["amount"].value.currency == code and facts["amount"].value.minor_units == minor
    assert facts["direction"].value is facts["posted_on"].value is None
    for fact in facts.values():
        for ref in fact.evidence:
            start, end = map(int, ref.locator.rsplit(":", 2)[1:])
            assert saved.parts[0].text[start:end] == ref.quote


@pytest.mark.parametrize("kind", ["purchase", "refund", "transfer", "debit"])
def test_complete_narrative_references_keep_distinct_processing_identities(tmp_path, kind):
    body = NARRATIVE.replace("Your purchase", "Your " + kind)
    if kind == "debit":
        body = "Purchase confirmation\n" + body.replace("Your debit of", "An amount of").replace(
            " on ", " was debited on "
        )
    service, _, _ = setup(
        tmp_path,
        {
            "one": body.replace("travel-wallet", "travel.wallet"),
            "two": body.replace("travel-wallet", "travel.savings"),
        },
    )
    document = load(service)
    transactions = heads(document)
    assert len(transactions) == len(document.accounts) == 2
    assert len({t.account_id.value for t in transactions}) == 2
    assert all(t.review.status == "accepted" for t in transactions)
    assert {a.label for a in document.accounts} == {"travel.wallet", "travel.savings"}
    assert total(document, currency="USD", kind="purchase" if kind == "debit" else kind) == 3850
    for transaction in transactions:
        ref = transaction.account_id.evidence[0]
        assert "travel.wallet" in ref.quote or "travel.savings" in ref.quote
    assert load(service, "refresh") == document


@pytest.mark.parametrize(
    "reference",
    [
        "travel.wallet/extra",
        "travel.wallet@extra",
        "travel.wallet+extra",
        "travel.walleté",
        "a" * 65,
        "travel.wallet.",
    ],
)
def test_continuing_or_incomplete_narrative_reference_cannot_bind_a_prefix(tmp_path, reference):
    service, _, _ = setup(tmp_path, {"one": NARRATIVE.replace("travel-wallet", reference)})
    document = load(service)
    transaction = heads(document)[0]
    assert transaction.review.status == "pending" and transaction.account_id.value is None
    assert not document.accounts and total(document, currency="USD") == 0


INVALID_LABELLED_VALUES = [
    *[
        (label, "Purchase!")
        for label in ("Type", "Transaction type", "Type of transaction", "Event type")
    ],
    *[
        (label, "SGD -99.00")
        for label in (
            "Amount",
            "Transaction amount",
            "Purchase amount",
            "Transfer amount",
            "Amount debited",
            "Amount credited",
        )
    ],
    ("Amount", "-99.00"),
    ("Transaction amount", "12.34oops"),
    ("Amount (SGD)", "-99.00"),
    ("Transaction amount (SGD)", "12.34oops"),
    ("Amount (dollars)", "99.00"),
    *[
        (label, "04/02/25")
        for label in (
            "Event date",
            "Transaction date",
            "Purchase date",
            "Transfer date",
            "Date of transaction",
            "Transaction date & Time",
            "Posting date",
            "Posted on",
        )
    ],
    *[
        (label, "a" * 129)
        for label in ("Account", "Account ID", "Account reference", "Account number", "Account No.")
    ],
    ("Bank", "b" * 65),
    ("Institution", "b" * 65),
    *[(label, "m" * 129) for label in ("Merchant", "Merchant name", "Transaction description")],
    ("Currency", "dollars"),
    ("Transaction currency", "dollars"),
    *[(label, "c" * 129) for label in ("Card", "Card number", "Card No.", "Card/Acct No.")],
]


@pytest.mark.parametrize("label, value", INVALID_LABELLED_VALUES)
@pytest.mark.parametrize(
    "separator, empty",
    [(": ", False), (":\n", False), ("\t", False), (": ", True), (":\n", True)],
)
def test_recognized_labels_cannot_hide_invalid_or_empty_competing_values(
    label, value, separator, empty
):
    fake = Notifications({"one": BODY})
    adapter = GmailAdapter(fake, "synthetic-label-review")
    control = adapter.get_candidate("one").candidate
    assert extract(control, "control", control.observation)[2] == "accepted"
    fake.bodies["one"] = BODY + "\n" + label + separator + ("" if empty else value)
    saved = adapter.get_candidate("one").candidate
    facts, _, outcome, reasons = extract(saved, "competing-value", saved.observation)
    assert outcome == "uncertain" and "invalid_explicit_field" in reasons
    assert facts["amount"].value.minor_units == 1234


@pytest.mark.parametrize("bad_field", ["Amount: SGD -99.00", "Event date: 04/02/25"])
@pytest.mark.parametrize("html", [False, True])
def test_invalid_competing_fields_are_saved_inline_and_excluded(tmp_path, bad_field, html):
    from html import escape

    body = BODY + "\n" + bad_field
    if html:
        body = "".join(f"<p>{escape(line)}</p>" for line in body.splitlines())
    service, fake, client = setup(tmp_path, {"one": body})
    if html:
        original = fake.get_message

        def html_message(identity):
            message = original(identity)
            message["payload"]["mimeType"] = "text/html"
            return message

        fake.get_message = html_message
    document = load(service)
    transaction = heads(document)[0]
    assert total(document) == 0 and transaction.review.status == "pending"
    assert "invalid_explicit_field" in transaction.review.reason
    assert "Excluded from totals" in client.get("/saved?month=2025-02").text
    saved = candidate(service, fake)
    assert bad_field in saved.parts[0].text
    for fact in (transaction.amount, transaction.occurred_on, transaction.account_id):
        for ref in fact.evidence:
            start, end = map(int, ref.locator.rsplit(":", 2)[1:])
            assert saved.parts[0].text[start:end] == ref.quote


def test_parser_upgrade_requalifies_unchanged_saved_machine_acceptance(tmp_path, monkeypatch):
    from spending_optimiser import local_templates

    current_analyze = local_templates.analyze

    def legacy_analyze(saved):
        analysis = current_analyze(saved)
        analysis.issues = tuple(i for i in analysis.issues if i != "amount_currency_conflicting")
        return analysis

    service, _, _ = setup(tmp_path, {"one": BODY + "\nCurrency: USD"})
    with monkeypatch.context() as legacy:
        legacy.setattr(local_templates, "analyze", legacy_analyze)
        legacy.setattr(local_templates, "LEARNING_REVISION", "local-learning-v2")
        legacy.setattr("spending_optimiser.processing.REVISION", "local-templates-v3")
        original = load(service)
    assert total(original) == 1234 and heads(original)[0].review.status == "accepted"
    document = load(service)
    assert total(document) == 0 and heads(document)[0].review.status == "pending"
    assert len(document.transactions) == 2
    assert document.transactions[0] == original.transactions[0]
    assert load(service) == document
    with service.cache.ledger.connection() as db:
        revisions = {row[0] for row in db.execute("SELECT revision FROM mail_template_sources")}
    assert revisions == {"local-learning-v2", local_templates.LEARNING_REVISION}


def test_financial_labels_do_not_reinterpret_transport_dates_or_merchant_text(tmp_path):
    body = BODY.replace("Fictional Cafe", "Currency: dollars; Type: Purchase!") + (
        "\nDate: 04/02/25\nGenerated: 04/02/25\nNarrative says Event date: tomorrow"
    )
    service, _, _ = setup(tmp_path, {"one": body})
    document = load(service)
    assert total(document) == 1234 and heads(document)[0].review.status == "accepted"


def test_generated_private_template_data_never_enters_public_schema_exports(tmp_path):
    service, _, client = setup(tmp_path, {"one": NARRATIVE})
    load(service)
    assert "mail_templates" not in service.cache.ledger.load().model_dump()
    assert client.get("/preview-template-status").status_code == 404
    assert "travel-wallet" not in client.get("/public").text
    assert "Dear Fictional Reader" not in client.get("/public").text
