"""Self-contained JSON document with local identity, history and evidence integrity checks."""

from collections.abc import Iterator
from typing import Literal, Self

from pydantic import BaseModel, model_validator

from .common import EvidenceRef, Fact, Model
from .evidence import SourceEvidence
from .rules import CardProduct, RuleRevision
from .transactions import Account, CardInstance, Observation, Transaction


def walk(value: object) -> Iterator[BaseModel]:
    if isinstance(value, BaseModel):
        yield value
        for name in type(value).model_fields:
            yield from walk(getattr(value, name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)


def index(items: tuple, field: str) -> dict:
    result = {getattr(item, field): item for item in items}
    if len(result) != len(items):
        raise ValueError(f"duplicate {field}")
    return result


def check_history(items: dict, identity: str, timestamp: str) -> None:
    roots: set[str] = set()
    successors: set[str] = set()
    for item in items.values():
        if item.supersedes is None:
            key = getattr(item, identity)
            if key in roots:
                raise ValueError("history needs one root per stable identity")
            roots.add(key)
            continue
        if item.supersedes in successors:
            raise ValueError("history must not fork; record conflicting facts within a revision")
        successors.add(item.supersedes)
        previous = items.get(item.supersedes)
        if previous is None:
            raise ValueError("superseded revision must be included in document")
        if getattr(item, identity) != getattr(previous, identity):
            raise ValueError("superseded revision belongs to a different identity")
        if getattr(item, timestamp) <= getattr(previous, timestamp):
            raise ValueError("revision times must strictly increase along supersedes")
        if isinstance(item, RuleRevision) and (
            item.scope != previous.scope or item.product_id != previous.product_id
        ):
            raise ValueError("superseded rule must have the same product and scope")


class SchemaDocument(Model):
    schema_version: Literal["1.0"] = "1.0"
    products: tuple[CardProduct, ...] = ()
    accounts: tuple[Account, ...] = ()
    cards: tuple[CardInstance, ...] = ()
    observations: tuple[Observation, ...] = ()
    evidence: tuple[SourceEvidence, ...] = ()
    rules: tuple[RuleRevision, ...] = ()
    transactions: tuple[Transaction, ...] = ()

    @model_validator(mode="after")
    def integrity(self) -> Self:
        products = index(self.products, "product_id")
        accounts = index(self.accounts, "account_id")
        cards = index(self.cards, "card_id")
        observations = index(self.observations, "observation_id")
        sources = index(self.evidence, "revision_id")
        rules = index(self.rules, "revision_id")
        transactions = index(self.transactions, "revision_id")
        events = {t.event_id for t in self.transactions}
        source_keys = [
            (o.source_system, o.source_namespace, o.source_record_id) for o in self.observations
        ]
        if len(set(source_keys)) != len(source_keys):
            raise ValueError("one observation identity per source record")
        check_history(sources, "source_id", "retrieved_at")
        check_history(rules, "rule_id", "compiled_at")
        check_history(transactions, "event_id", "recorded_at")
        for model in walk(self):
            if isinstance(model, EvidenceRef):
                targets = sources if model.target == "source_evidence" else observations
                if model.evidence_revision_id not in targets:
                    raise ValueError("dangling evidence reference")
            if isinstance(model, Fact) and model.status == "verified":
                # Fact verification is a recorded assertion, not a substitute for source review.
                if model.review.status == "rejected":
                    raise ValueError("a rejected fact cannot be verified")
        for card in self.cards:
            if card.product_id not in products or card.account_id not in accounts:
                raise ValueError("card needs a known product and account")
            if accounts[card.account_id].kind != "credit_card":
                raise ValueError("card instance must map to a credit-card account")
        for rule in self.rules:
            if rule.product_id not in products:
                raise ValueError("rule needs a known product")
            for ref in (m for m in walk(rule) if isinstance(m, EvidenceRef)):
                if ref.target != "source_evidence":
                    raise ValueError("public rules cannot cite private transaction observations")
                if sources[ref.evidence_revision_id].retrieved_at > rule.compiled_at:
                    raise ValueError("rule cannot cite evidence retrieved after compilation")
        origin_owners: dict[tuple[str, str], set[str]] = {}
        duplicate_edges: dict[str, set[str]] = {}
        for txn in self.transactions:
            for origin in txn.origins:
                observation = observations.get(origin.observation_id)
                if observation is None or observation.observed_at > txn.recorded_at:
                    raise ValueError("transaction needs a known observation available at recording")
                origin_owners.setdefault((origin.observation_id, origin.event_key), set()).add(
                    txn.event_id
                )
            for fact in (txn.account_id, txn.counterparty_account_id):
                values = (() if fact.value is None else (fact.value,)) + fact.alternatives
                if any(v not in accounts for v in values):
                    raise ValueError("transaction references an unknown account")
            card_values = (() if txn.card_id.value is None else (txn.card_id.value,)) + (
                txn.card_id.alternatives
            )
            if any(v not in cards for v in card_values):
                raise ValueError("transaction references an unknown card")
            if txn.card_id.value is not None and txn.account_id.value is not None:
                if cards[txn.card_id.value].account_id != txn.account_id.value:
                    raise ValueError("transaction card/account mismatch")
            related = (
                () if txn.related_event_id.value is None else (txn.related_event_id.value,)
            ) + txn.related_event_id.alternatives
            if any(v not in events or v == txn.event_id for v in related):
                raise ValueError("related event must exist and not be self")
            if txn.duplicate_of is not None:
                if txn.duplicate_of not in events:
                    raise ValueError("duplicate target must exist")
                duplicate_edges.setdefault(txn.event_id, set()).add(txn.duplicate_of)
            if any(c.corrected_at > txn.recorded_at for c in txn.corrections):
                raise ValueError("correction cannot occur after its transaction revision")

        # Duplicate links are descriptive, but must not create loops, even through revisions.
        def visit(event: str, path: frozenset[str]) -> None:
            if event in path:
                raise ValueError("duplicate links cannot form a cycle")
            for target in duplicate_edges.get(event, ()):
                visit(target, path | {event})

        for event in duplicate_edges:
            visit(event, frozenset())
        for owners in origin_owners.values():
            if len(owners) > 1:
                roots = [event for event in owners if not duplicate_edges.get(event)]
                if len(roots) != 1 or any(
                    event != roots[0] and roots[0] not in duplicate_edges.get(event, set())
                    for event in owners
                ):
                    raise ValueError(
                        "shared source event needs explicit duplicate links to one root"
                    )
        return self
