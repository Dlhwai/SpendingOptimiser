"""Genuinely synthetic notification layouts, never private-derived examples.

Production discovery is exercised before reuse; no ready template or ledger is seeded.
"""

import base64
import json

import pytest
from test_monthly import connected
from test_processing import BODY, PERIOD, load, setup, total

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


def test_generated_private_template_data_never_enters_public_schema_exports(tmp_path):
    service, _, client = setup(tmp_path, {"one": NARRATIVE})
    load(service)
    assert "mail_templates" not in service.cache.ledger.load().model_dump()
    assert client.get("/preview-template-status").status_code == 404
    assert "travel-wallet" not in client.get("/public").text
    assert "Dear Fictional Reader" not in client.get("/public").text
