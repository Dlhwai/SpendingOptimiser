"""Notification-derived events, deliberately not double-entry bank ledger postings."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from .common import CalendarDate, Fact, Identifier, Model, Money, Review, Text
from .evidence import Sha256


class Account(Model):
    account_id: Identifier
    kind: Literal["credit_card", "deposit", "wallet", "other"]
    label: Text  # opaque/sanitized label, not a bank account number


class CardInstance(Model):
    card_id: Identifier
    product_id: Identifier
    account_id: Identifier
    approved_on: Fact[CalendarDate] = Field(default_factory=Fact[CalendarDate])
    statement_day: Fact[Annotated[int, Field(strict=True, ge=1, le=31)]] = Field(
        default_factory=Fact[Annotated[int, Field(strict=True, ge=1, le=31)]]
    )
    promotion_enrollments: Fact[tuple[Identifier, ...]] = Field(
        default_factory=Fact[tuple[Identifier, ...]]
    )


class Observation(Model):
    """Stable source identity. Reprocessing retains this id and its event_key."""

    observation_id: Identifier
    source_system: Literal["gmail", "manual", "statement", "synthetic"]
    source_namespace: Identifier  # opaque mailbox/import identity, never credentials
    source_record_id: Identifier  # provider message id, not subject/body hash
    observed_at: AwareDatetime
    content_sha256: Sha256 | None = None


class EventOrigin(Model):
    observation_id: Identifier
    event_key: Identifier  # distinguishes multiple events within one notification
    extractor_revision: Identifier
    processing_run_id: Identifier


class Correction(Model):
    field: Literal[
        "kind",
        "direction",
        "channel",
        "amount",
        "occurred_on",
        "posted_on",
        "account_id",
        "card_id",
        "counterparty_account_id",
        "merchant",
        "mcc",
        "transfer_purpose",
        "income_kind",
        "related_event_id",
    ]
    reason: Text
    actor: Text
    corrected_at: AwareDatetime


EventKind = Literal["purchase", "refund", "reversal", "income", "transfer"]


class Transaction(Model):
    event_id: Identifier  # stable canonical identity, including across manual corrections
    revision_id: Identifier  # changes on reprocessing/correction
    supersedes: Identifier | None = None
    recorded_at: AwareDatetime
    origins: Annotated[tuple[EventOrigin, ...], Field(min_length=1)]
    duplicate_of: Identifier | None = None  # another event, NOT a second expense to sum
    related_event_id: Fact[Identifier] = Field(default_factory=Fact[Identifier])
    kind: Fact[EventKind] = Field(default_factory=Fact[EventKind])
    direction: Fact[Literal["inflow", "outflow"]] = Field(
        default_factory=Fact[Literal["inflow", "outflow"]]
    )  # relative to account_id, not a claim of cash settlement
    channel: Fact[Literal["card", "paynow", "paylah", "bank_transfer", "other"]] = Field(
        default_factory=Fact[Literal["card", "paynow", "paylah", "bank_transfer", "other"]]
    )
    amount: Fact[Money] = Field(default_factory=Fact[Money])
    occurred_on: Fact[CalendarDate] = Field(default_factory=Fact[CalendarDate])
    posted_on: Fact[CalendarDate] = Field(default_factory=Fact[CalendarDate])
    account_id: Fact[Identifier] = Field(default_factory=Fact[Identifier])
    card_id: Fact[Identifier] = Field(default_factory=Fact[Identifier])
    counterparty_account_id: Fact[Identifier] = Field(default_factory=Fact[Identifier])
    transfer_purpose: Fact[Literal["own_accounts", "card_repayment", "external"]] = Field(
        default_factory=Fact[Literal["own_accounts", "card_repayment", "external"]]
    )
    income_kind: Fact[Literal["salary", "other"]] = Field(
        default_factory=Fact[Literal["salary", "other"]]
    )
    merchant: Fact[Text] = Field(default_factory=Fact[Text])
    mcc: Fact[Annotated[str, Field(pattern=r"^[0-9]{4}$", strict=True)]] = Field(
        default_factory=Fact[Annotated[str, Field(pattern=r"^[0-9]{4}$", strict=True)]]
    )
    review: Review = Field(default_factory=Review)
    corrections: tuple[Correction, ...] = ()

    @model_validator(mode="after")
    def event_constraints(self) -> Self:
        amounts = (
            () if self.amount.value is None else (self.amount.value,)
        ) + self.amount.alternatives
        if any(amount.minor_units == 0 for amount in amounts):
            raise ValueError("event amount must be positive when known; direction is separate")
        if self.duplicate_of == self.event_id or self.related_event_id.value == self.event_id:
            raise ValueError("an event cannot link to itself")
        identities = [(o.observation_id, o.event_key) for o in self.origins]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate source event identity within transaction")
        if self.kind.value != "transfer" and self.transfer_purpose.value is not None:
            raise ValueError("transfer purpose only applies to a known transfer")
        if self.kind.value != "income" and self.income_kind.value is not None:
            raise ValueError("income kind only applies to known income")
        if self.review.status == "accepted" and any(
            f.status not in {"proposed", "verified"}
            for f in (self.kind, self.amount, self.occurred_on, self.account_id)
        ):
            raise ValueError("accepted event needs resolved kind, amount, event date and account")
        return self
