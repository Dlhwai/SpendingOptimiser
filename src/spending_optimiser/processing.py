"""Explicit saved-mail processing through private local template learning/application.

No model, network, inferred dates or inferred account/card relationships. Durable
facts remain in the canonical ledger; templates are private source-linked local data.
"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from .common import EvidenceRef, Fact, Review
from .extraction import notification_event_id
from .ledger import heads
from .local_templates import LocalTemplates, analyze, facts_from
from .mail_cache import decode_candidate
from .transactions import Account, EventOrigin, Transaction

REVISION = "local-templates-v10"
BATCH_SIZE = 100


def account_identity(namespace, reference):
    key = json.dumps([namespace, reference], ensure_ascii=True, separators=(",", ":"))
    return "mail-account-" + hashlib.sha256(key.encode()).hexdigest()


def account_facts(result, observation):
    facts, outcome, reasons = result
    if not facts:
        return {}, [], outcome, reasons
    account = facts.pop("account")
    references = (() if account.value is None else (account.value,)) + account.alternatives
    accounts = []
    for reference in references:
        identity = account_identity(observation.source_namespace, reference)
        # Never store full account numbers as display labels.
        label = (
            reference
            if not any(c.isdigit() for c in reference)
            else "Notification account " + identity[-8:]
        )
        accounts.append(Account(account_id=identity, kind="other", label=label))
    mapped = account.model_dump()
    mapped["value"] = accounts[0].account_id if account.value is not None else None
    mapped["alternatives"] = tuple(a.account_id for a in accounts) if account.alternatives else ()
    facts["account_id"] = Fact.model_validate(mapped)
    reasons = tuple(
        "account_id_" + r[len("account_") :]
        if r in {"account_unknown", "account_conflicting"}
        else r
        for r in reasons
    )
    return facts, accounts, outcome, reasons


def extract(candidate, digest, observation):
    """Pure qualification interface; runtime uses persisted discovery before application."""
    return account_facts(facts_from(analyze(candidate), digest, observation), observation)


def process_one(cache, namespace, message_id, digest, payload):
    """A single atomic outcome + ledger append, following the separately committed mail save."""
    candidate = decode_candidate(namespace, message_id, payload)
    source = candidate.observation
    templates = LocalTemplates(cache)
    templates.learn(namespace, message_id, digest, candidate)

    def build(db, document, protected):
        current = db.execute(
            "SELECT digest FROM mail_current WHERE namespace=? AND message_id=?",
            (namespace, message_id),
        ).fetchone()
        processed = db.execute(
            "SELECT digest, extractor FROM mail_processed_current "
            "WHERE namespace=? AND message_id=?",
            (namespace, message_id),
        ).fetchone()
        if current != (digest,) or processed == (digest, REVISION):
            return []
        observation = next(
            (
                o
                for o in document.observations
                if (o.source_system, o.source_namespace, o.source_record_id)
                == (source.source_system, namespace, message_id)
            ),
            source,
        )
        event_id = notification_event_id(observation)
        previous = next((t for t in heads(document) if t.event_id == event_id), None)
        # Imported or human-authored source events must never be duplicated or overwritten.
        owners = [
            t
            for t in heads(document)
            if any(o.observation_id == observation.observation_id for o in t.origins)
        ]
        if owners:
            previous = next((t for t in owners if not t.duplicate_of), owners[0])
            event_id = previous.event_id
        result = templates.apply(namespace, message_id, digest, candidate, observation)
        stated = result[0].get("account") if result[0] else None
        references = () if stated is None or stated.value is None else (stated.value,)
        facts, accounts, outcome, reasons = account_facts(result, observation)
        for reference in references:
            # An explicit From line of the notice, kept so the person can say which card it is.
            cache.cards.note(db, namespace, account_identity(namespace, reference), reference)
        additions = []
        if previous and event_id in protected:
            outcome, reasons = "retained", ("human_or_imported_decision_preserved",)
        elif facts or previous:
            now = datetime.now(UTC)
            if now < source.observed_at or (previous and now <= previous.recorded_at):
                raise ValueError("Local clock precedes saved financial history")
            if observation not in document.observations:
                additions.append(("observations", observation))
            existing_accounts = {a.account_id for a in document.accounts}
            additions.extend(
                ("accounts", a) for a in accounts if a.account_id not in existing_accounts
            )
            if not facts:
                facts = {
                    "kind": Fact(
                        evidence=(
                            EvidenceRef(
                                target="observation",
                                evidence_revision_id=observation.observation_id,
                                locator=f"mail-version:{digest}:message",
                            ),
                        ),
                        note="Current source no longer supports an unambiguous financial event",
                    )
                }
            accepted = outcome == "accepted"
            review = Review(
                status="accepted" if accepted else "pending",
                reviewer="local deterministic parser" if accepted else None,
                reviewed_at=now if accepted else None,
                reason=(
                    "Automatically accepted supported notification; not bank-statement verification"
                    if accepted
                    else ", ".join(reasons)
                ),
            )
            transaction = Transaction(
                event_id=event_id,
                revision_id="mail-" + uuid4().hex,
                supersedes=previous.revision_id if previous else None,
                recorded_at=now,
                origins=(
                    EventOrigin(
                        observation_id=observation.observation_id,
                        event_key="primary",
                        extractor_revision=REVISION,
                        processing_run_id="local-" + uuid4().hex,
                    ),
                ),
                review=review,
                **facts,
            )
            additions.append(("transactions", transaction))
        cache.record_outcome(
            db,
            namespace,
            message_id,
            digest,
            REVISION,
            outcome,
            reasons,
            event_id if previous or facts else None,
        )
        return additions

    cache.ledger.write(build)


def process_saved(cache, namespace):
    """At most 100 current saved messages per explicit load action. Failures stay retryable."""
    failed = 0
    try:
        # Historic versions participate in discovery only, never in financial creation.
        templates = LocalTemplates(cache)
        templates.discover(namespace, BATCH_SIZE)
        failed += templates.discovery_failed
        for message_id, digest, payload in cache.pending(namespace, REVISION, BATCH_SIZE):
            try:
                process_one(cache, namespace, message_id, digest, payload)
            except (ValueError, RecursionError, sqlite3.Error, OSError):
                failed += 1
                try:
                    with cache.ledger.connection() as db:
                        cache.record_outcome(
                            db,
                            namespace,
                            message_id,
                            digest,
                            REVISION,
                            "failed",
                            ("local_processing_failed_retry_load",),
                            None,
                        )
                except (sqlite3.Error, OSError):
                    pass
    finally:
        cache.ledger.maintain_storage()
    return failed
