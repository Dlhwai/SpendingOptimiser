"""Immutable-by-contract snapshots of public source documents, not a fetcher."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, HttpUrl, StringConstraints

from .common import EffectivePeriod, Fact, Identifier, Model, Review, Text

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", strict=True)]


class SourceEvidence(Model):
    revision_id: Identifier
    source_id: Identifier  # stable document identity across retrievals/revisions
    url: HttpUrl
    source_kind: Literal["issuer", "program", "campaign", "editorial", "synthetic"]
    retrieved_at: AwareDatetime
    effective: Fact[EffectivePeriod] = Field(default_factory=Fact[EffectivePeriod])
    document_version: Text | None = None  # None means the source did not disclose one
    content_sha256: Sha256 | None = None
    supersedes: Identifier | None = None
    review: Review = Field(default_factory=Review)
