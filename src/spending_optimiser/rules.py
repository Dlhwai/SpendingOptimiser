"""Descriptive card terms; earning, redemption and valuation are separate data."""

from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from .common import (
    Currency,
    EffectivePeriod,
    Fact,
    Identifier,
    Model,
    Money,
    PositiveDecimal,
    Review,
    Text,
)


class CardProduct(Model):
    product_id: Identifier
    supplied_name: Text
    issuer: Fact[Text] = Field(default_factory=Fact[Text])
    country: Fact[Text] = Field(default_factory=Fact[Text])
    variant: Fact[Text] = Field(default_factory=Fact[Text])
    network: Fact[Text] = Field(default_factory=Fact[Text])


class RewardUnit(Model):
    unit_id: Identifier  # program-specific, e.g. synthetic-points, not generic "points"
    kind: Literal["points", "miles", "cashback", "other"]
    currency: Currency | None = None

    @model_validator(mode="after")
    def cash_currency(self) -> Self:
        if (self.kind == "cashback") != (self.currency is not None):
            raise ValueError("only cashback units must specify a currency")
        return self


class RewardQuantity(Model):
    unit: RewardUnit
    quantity: PositiveDecimal  # native units; cash is in major currency units, not Money


class Period(Model):
    kind: Literal["transaction", "calendar_month", "statement_cycle", "calendar_year", "campaign"]
    timezone: Text

    @model_validator(mode="after")
    def valid_timezone(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an installed IANA zone") from exc
        return self


class Condition(Model):
    """Declarative predicates only. Values retain source terminology, not executable code."""

    dimension: Literal[
        "merchant",
        "mcc",
        "channel",
        "country",
        "currency",
        "new_to_bank",
        "new_to_card",
        "enrollment",
        "card_network",
        "other",
    ]
    operator: Literal["in", "not_in", "equals"]
    operands: tuple[Text, ...] = Field(min_length=1)
    description: Text

    @model_validator(mode="after")
    def arity(self) -> Self:
        if self.operator == "equals" and len(self.operands) != 1:
            raise ValueError("equals needs exactly one operand")
        return self


class EarningRate(Model):
    rate_id: Identifier
    component: Literal["base", "bonus", "signup", "campaign"]
    reward: Fact[RewardQuantity] = Field(default_factory=Fact[RewardQuantity])
    per_spend: Fact[Money] = Field(default_factory=Fact[Money])
    date_basis: Fact[Literal["transaction_date", "posting_date"]] = Field(
        default_factory=Fact[Literal["transaction_date", "posting_date"]]
    )
    eligibility: Fact[tuple[Condition, ...]] = Field(default_factory=Fact[tuple[Condition, ...]])

    @model_validator(mode="after")
    def positive_block(self) -> Self:
        values = (() if self.per_spend.value is None else (self.per_spend.value,)) + (
            self.per_spend.alternatives
        )
        if any(value.minor_units == 0 for value in values):
            raise ValueError("earning spend block must be positive")
        return self


class MinimumSpend(Model):
    requirement_id: Identifier
    threshold: Fact[Money] = Field(default_factory=Fact[Money])
    date_basis: Fact[Literal["transaction_date", "posting_date"]] = Field(
        default_factory=Fact[Literal["transaction_date", "posting_date"]]
    )
    period: Fact[Period] = Field(default_factory=Fact[Period])
    qualifying_spend: Fact[tuple[Condition, ...]] = Field(
        default_factory=Fact[tuple[Condition, ...]]
    )
    applies_to: Fact[tuple[Identifier, ...]] = Field(
        default_factory=Fact[tuple[Identifier, ...]]
    )  # earning rate ids, unknown until the source establishes applicability


class Cap(Model):
    cap_id: Identifier
    basis: Literal["reward", "eligible_spend"]
    limit: Fact[RewardQuantity | Money] = Field(default_factory=Fact[RewardQuantity | Money])
    date_basis: Fact[Literal["transaction_date", "posting_date"]] = Field(
        default_factory=Fact[Literal["transaction_date", "posting_date"]]
    )
    period: Fact[Period] = Field(default_factory=Fact[Period])
    applies_to: Fact[tuple[Identifier, ...]] = Field(default_factory=Fact[tuple[Identifier, ...]])

    @model_validator(mode="after")
    def limit_basis(self) -> Self:
        values = (() if self.limit.value is None else (self.limit.value,)) + self.limit.alternatives
        if any(isinstance(v, RewardQuantity) != (self.basis == "reward") for v in values):
            raise ValueError("cap limit must match reward or eligible-spend basis")
        return self


class Exclusion(Model):
    condition: Condition
    affects: Literal["earning", "minimum_spend", "both"]


class Rounding(Model):
    stage: Literal["spend", "reward"]
    mode: Literal["floor", "ceiling", "half_up", "half_even", "none"]
    increment: PositiveDecimal
    scope: Literal["transaction", "period"]


class Redemption(Model):
    from_reward: Fact[RewardQuantity] = Field(default_factory=Fact[RewardQuantity])
    to_reward: Fact[RewardQuantity] = Field(default_factory=Fact[RewardQuantity])
    fee: Fact[Money] = Field(default_factory=Fact[Money])
    conditions: Fact[tuple[Condition, ...]] = Field(default_factory=Fact[tuple[Condition, ...]])


class Valuation(Model):
    """An assumption, never an earning rate or guaranteed redemption value."""

    reward: RewardQuantity
    money: Money
    assumed_at: AwareDatetime
    rationale: Text


class RuleRevision(Model):
    rule_id: Identifier  # stable rule family, including distinct campaigns for one product
    revision_id: Identifier
    product_id: Identifier
    compiled_at: AwareDatetime
    supersedes: Identifier | None = None
    effective: Fact[EffectivePeriod] = Field(default_factory=Fact[EffectivePeriod])
    scope: Literal["standard", "signup", "campaign"] = "standard"
    earning: Fact[tuple[EarningRate, ...]] = Field(default_factory=Fact[tuple[EarningRate, ...]])
    eligibility: Fact[tuple[Condition, ...]] = Field(default_factory=Fact[tuple[Condition, ...]])
    minimum_spend: Fact[tuple[MinimumSpend, ...]] = Field(
        default_factory=Fact[tuple[MinimumSpend, ...]]
    )
    caps: Fact[tuple[Cap, ...]] = Field(default_factory=Fact[tuple[Cap, ...]])
    exclusions: Fact[tuple[Exclusion, ...]] = Field(default_factory=Fact[tuple[Exclusion, ...]])
    rounding: Fact[tuple[Rounding, ...]] = Field(default_factory=Fact[tuple[Rounding, ...]])
    date_basis: Fact[Literal["transaction_date", "posting_date"]] = Field(
        default_factory=Fact[Literal["transaction_date", "posting_date"]]
    )
    redemption: Fact[tuple[Redemption, ...]] = Field(default_factory=Fact[tuple[Redemption, ...]])
    valuations: tuple[Valuation, ...] = ()
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def component_references(self) -> Self:
        earning_sets = (() if self.earning.value is None else (self.earning.value,)) + (
            self.earning.alternatives
        )
        ids: set[str] = set()
        for rates in earning_sets:
            rate_ids = [r.rate_id for r in rates]
            if len(set(rate_ids)) != len(rate_ids):
                raise ValueError("earning rate ids must be unique within an earning set")
            ids.update(rate_ids)
        # Conflicts can reference any candidate earning id, without choosing a winner.
        for field in (self.minimum_spend, self.caps):
            groups = (() if field.value is None else (field.value,)) + field.alternatives
            for components in groups:
                for component in components:
                    targets = (
                        () if component.applies_to.value is None else (component.applies_to.value,)
                    ) + component.applies_to.alternatives
                    if any(not set(target) <= ids for target in targets):
                        raise ValueError("requirement/cap references an unknown earning rate")
        return self
