"""Shared serializable value objects. No calculation or persistence behavior."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"^\S+$")]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
MinorUnits = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
Currency = Literal["SGD", "USD", "EUR", "GBP", "JPY", "KRW", "BHD", "KWD"]
CURRENCY_EXPONENTS: dict[str, int] = {
    "SGD": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "JPY": 0,
    "KRW": 0,
    "BHD": 3,
    "KWD": 3,
}


def exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("use a decimal string, Decimal or integer, never a float/bool")
    return value


PositiveDecimal = Annotated[
    Decimal, BeforeValidator(exact_decimal), Field(gt=0, allow_inf_nan=False)
]


def calendar_date(value: object) -> object:
    if isinstance(value, datetime) or not isinstance(value, (str, date)):
        raise ValueError("use a calendar date, not a timestamp or epoch")
    if isinstance(value, str) and (len(value) != 10 or value[4] != "-" or value[7] != "-"):
        raise ValueError("dates must use YYYY-MM-DD")
    return value


CalendarDate = Annotated[date, BeforeValidator(calendar_date)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class Money(Model):
    """Unsigned magnitude in a supported currency's smallest unit, not a signed ledger entry."""

    currency: Currency
    minor_units: MinorUnits

    @property
    def exponent(self) -> int:
        return CURRENCY_EXPONENTS[self.currency]


class EffectivePeriod(Model):
    """Half-open local dates [start, end); None is unknown, not infinity."""

    start: CalendarDate | None = None
    end: CalendarDate | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("effective end must be after start")
        return self


class EvidenceRef(Model):
    target: Literal["source_evidence", "observation"] = "source_evidence"
    evidence_revision_id: Identifier
    locator: Text  # e.g. page, clause, section or JSON pointer
    quote: Text | None = None


class Review(Model):
    status: Literal["pending", "accepted", "corrected", "rejected"] = "pending"
    reviewer: Text | None = None
    reviewed_at: AwareDatetime | None = None
    reason: Text | None = None

    @model_validator(mode="after")
    def reviewed(self) -> Self:
        if self.status != "pending" and (
            self.reviewer is None or self.reviewed_at is None or self.reason is None
        ):
            raise ValueError("a completed review needs reviewer, time and reason")
        return self


T = TypeVar("T")


class Fact(Model, Generic[T]):
    """Verification applies to this entire value, including any structured subfields."""

    status: Literal["unknown", "proposed", "verified", "conflicting", "not_applicable"] = "unknown"
    value: T | None = None
    alternatives: tuple[T, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    note: Text | None = None
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status in {"proposed", "verified"}:
            if self.value is None or self.alternatives:
                raise ValueError("known fact needs one value and no alternatives")
        elif self.status == "conflicting":
            if self.value is not None or len(self.alternatives) < 2:
                raise ValueError("conflict needs at least two alternatives, no selected value")
            if any(v in self.alternatives[:i] for i, v in enumerate(self.alternatives)):
                raise ValueError("conflict alternatives must differ")
        elif self.value is not None or self.alternatives:
            raise ValueError("unknown/not-applicable facts cannot carry a value")
        if self.status == "verified" and not self.evidence:
            raise ValueError("verified facts require evidence")
        if self.status in {"conflicting", "not_applicable"} and self.note is None:
            raise ValueError("conflicting/not-applicable facts need an explanation")
        return self
