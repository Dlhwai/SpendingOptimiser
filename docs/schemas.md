# Schema milestone (format 1.0)

These are Pydantic validation models, not by themselves an ingestion service,
bank ledger, or reward engine. The package now also includes a separately tested
[offline local import/review service](local-ledger.md) using FastAPI, SQLite, and
Jinja2; the domain models themselves depend only on Pydantic. All examples/fixtures are
synthetic. The three supplied card names are unresolved labels, not verified
products or a live rules catalog.

## Development and usage

Use Python 3.11.9 (pinned in `.python-version`) and uv 0.11.8. From the repository:

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

`uv.lock` pins runtime and development dependencies; `pyproject.toml` also pins
the build backend. Python >=3.11 is supported by the package metadata; CI uses
3.11.9. No credentials, emails, model access, or network services are needed to
run the tests after installing dependencies.

```python
from pathlib import Path
from spending_optimiser import Money, SchemaDocument

money = Money(currency="JPY", minor_units=123)  # JPY 123, not JPY 1.23
assert money.exponent == 0

doc = SchemaDocument.model_validate_json(
    Path("tests/fixtures/synthetic.json").read_text()
)
serialized = doc.model_dump_json(indent=2)
assert SchemaDocument.model_validate_json(serialized) == doc
schema = SchemaDocument.model_json_schema()  # JSON Schema, including nested definitions
assert doc.transactions[0].posted_on.value is None
```

For example, export the machine-readable schema with:

```sh
uv run --locked python -c 'import json; from spending_optimiser import SchemaDocument; print(json.dumps(SchemaDocument.model_json_schema(), indent=2))'
```

## Representation and uncertainty

`SchemaDocument` is a self-contained JSON snapshot with `schema_version: "1.0"`
and arrays of products, accounts, personal cards, observations, public evidence,
rule revisions, and transaction revisions. Empty arrays mean **0 records**,
not a successful research/import run. Unknown keys/format versions fail validation.
Standalone models validate local shape; validate the document to check links.

Every extracted domain field uses `Fact[T]`:

- `unknown`: no value; not equivalent to zero, false, or an empty list.
- `proposed`: one typed value, not verified.
- `verified`: one typed value and at least one evidence reference.
- `conflicting`: no selected value, two or more distinct typed alternatives,
  and an explanatory note. References can cite both competing sources.
- `not_applicable`: no value and an explanation.

Each fact has independent evidence and review metadata. Verification of a
structured fact applies to the whole value; nested facts keep their own status.
No status inheritance or automatic promotion is implemented. Known empty lists
(e.g. a proposed empty exclusion set) differ from unknown exclusions.
A completed review records actor, aware time, and reason. An accepted transaction
needs resolved kind, amount, event date and account; posting date and MCC can
remain unknown. Acceptance does not imply statement reconciliation or verified
source truth.

## Transactions and identities

- `Account` is an opaque internal account identity. `CardInstance` links a
  personal credit-card account to a public `CardProduct`; approval date,
  statement day and enrollment identifiers are separately uncertain facts.
- `Observation` identifies a source record using source system, opaque namespace,
  and provider record id. Never use subjects or amount/date pairs as unique ids.
- `EventOrigin` adds an event key within that record, extractor revision, and
  processing run id. Keep the observation/event key stable during replay.
- `Transaction.event_id` is the stable canonical event identity;
  `revision_id` identifies an immutable extraction/correction revision.
  Keep prior revisions and link `supersedes`. Manual corrections carry field,
  actor, reason and time. This is audit data, not replay-protection code.
- Purchases, refunds, reversals, income and transfers have **positive unsigned
  amount magnitudes**. Kind is not a debit/credit ledger posting. `direction`
  separately records inflow/outflow relative to `account_id`, not cash settlement;
  it remains unknown if unsupported. `channel` can retain card, PayNow or PayLah.
  Transfers can identify both accounts and distinguish own-account transfers,
  card repayments and external transfers. Do not sum these records as expenses
  or income. Salary can be retained as an income kind without treating an
  incoming transfer as salary.
- Refund/reversal linkage can remain unknown; there is no invented original
  purchase. `duplicate_of` explicitly points to another canonical event id.
  Revisions of the same event may share source keys. Different events sharing
  the same source/event key must explicitly link duplicates to one root.
- Event date and posting date are independent calendar dates. No posting date,
  MCC, currency conversion, settlement amount, merchant, or account is inferred.
  Missing event kind/amount/date/account is representable for pending review.

Money uses strict integer minor units in `[0, 2^63-1]`; known event amounts and
earning spend blocks must be positive. Strings, floats and booleans are rejected
as minor units. Supported currencies are deliberately bounded: SGD, USD, EUR,
GBP (2 decimal places), JPY, KRW (0), BHD, KWD (3). Unsupported codes fail rather
than silently assuming cents. Add explicitly reviewed currency metadata before
supporting another currency. `Money` itself does no major-unit parsing, FX or ledger
balancing. The separate [constrained extraction use case](extraction.md) converts
explicit supported-currency decimal text with integer arithmetic and returns only
pending-review proposals.

## Card rules, evidence and history

`CardProduct` stores a supplied name plus independently uncertain issuer, country,
variant and network. It never stores personal enrollment or approval information.

`RuleRevision.rule_id` is a stable rule-family identity (standard or a specific
promotion); `revision_id` is an immutable revision. A rule belongs to one product
and scope. Earning rates, overall eligibility, minimum-spend qualification, caps,
exclusions, rounding, date basis and redemption are separate facts. Minimum spend
and caps reference earning-rate ids, not redemption conversions; applicability
can stay unknown even when a threshold is known. References inside conflicts are
checked against the union of candidate earning ids, not a selected combination.
Per-component
date bases allow earning and minimum-spend rules to differ; no fallback or
precedence is evaluated. Predicate operands retain source terminology, with
unknown conditions represented as facts, not invented MCCs or eligibility.

Reward quantities use finite positive Decimal values in native program-specific
units. JSON encodes decimals as strings; Python floats/bools are rejected.
Cashback identifies its currency and uses decimal **major currency reward
units**, potentially fractional accruals; transaction/cap spending money still
uses integer minor units. Redemption ratios/fees are distinct from earning;
`Valuation` is an explicitly dated assumption, not guaranteed monetary value.
No calculation, predicate interpreter, unit compatibility, rounding application,
cap aggregation, promotion qualification or redemption engine is implemented.

`SourceEvidence` has stable `source_id`, immutable `revision_id`, HTTP(S) URL,
aware retrieval timestamp, source kind, optional disclosed document version and
SHA-256 content digest, uncertain effective period, and review metadata.
`EvidenceRef` links a revision and a locator/optional quote. Transaction facts may
instead cite an observation, using `target: "observation"`; public products,
evidence and rules cannot cite private observations. Editorial/synthetic
evidence can be stored but is not authority to publish issuer terms. Schema
validation neither fetches documents nor establishes official-source authority;
the separate [safe AI importer](subscription-ai.md#what-is-supported) owns that boundary.

Both rules and source evidence have an effective-period fact. Each period is
half-open `[start, end)` in calendar dates, with `None` meaning **unknown**, not
infinity. Conflicting periods can retain alternatives. No retrieval timestamp is
substituted for an effective date; retroactive and future changes are permitted.
`compiled_at` is when a rule revision was recorded, not when it takes effect.
Cited evidence must have been retrieved by that time.

The document checks unique ids, evidence/account/product/card/event links,
source-record identity collisions, card/account compatibility, positive amounts,
period ordering, aware timestamps, supersedes identity and strictly increasing
revision times, correction chronology, and duplicate cycles. Histories are
complete, single-root linear chains per stable identity; partial exports with
missing predecessors are rejected. A document may retain overlapping effective
periods: correcting knowledge does not rewrite the old snapshot, and this schema
does not select the applicable historical rule or automatically close periods.

## Boundaries and next step

Models are frozen and use tuples, but Python validation bypasses such as
`model_construct` and unvalidated `model_copy(update=...)` are not safe input
interfaces. Immutability across documents, unique ids across runs, atomic saves,
manual-correction precedence, deduplication/reconciliation, completeness, and
actual bank/card-term correctness need application/storage enforcement.
References and chronology cannot prove that a quote supports a field, that an
MCC is correct, that caps use compatible units, or that an extracted condition
is financially meaningful. These checks are intentionally not a publication gate.

The separately tested [local application](local-ledger.md) now enforces immutable
cross-import identities, atomic SQLite persistence, deduplication of exact reimports,
and conservative manual-review precedence. These remain application guarantees,
not changes to the schema semantics. Next: qualify real ingestion adapters, research
exact card variants/terms, and validate the descriptive schema against verified
examples. No live research or real personal data is needed for the local milestone.
