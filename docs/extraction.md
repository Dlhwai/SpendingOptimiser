# Constrained notification extraction

For the application's automatic mail-to-ledger path, see
[automatic transactions](automatic-transactions.md). The standalone model-assisted API
below remains proposal-only; its manual-review requirement does not govern that path.

`spending_optimiser.extraction.NotificationExtractor` is a fixture-tested use case,
not a live AI integration or qualified bank parser. It takes notification text, an
existing `Observation`, explicit caller-selected `AccountContext`, and revision/run
metadata. It returns existing `Transaction`/`Fact`/`Review` proposals and observation
evidence. It never saves, accepts, publishes rules, or calculates financial totals.
No Gmail class, database, model SDK, provider configuration or credentials are used.

## Inject one callable

The boundary is `Callable[[ModelRequest], str]`. The request contains:

- Fixed `system_instruction`, separately from untrusted `notification` data.
- `response_schema_json`, generated from the closed `ModelSelection` schema.
- `max_response_bytes` (32,768), also enforced when the response returns.

The response is exactly one JSON object, not Markdown, a tool call or executable
code. All keys are required; unknown or duplicate keys fail. For example:

```json
{
  "classification": "transaction",
  "fields": {
    "kind": [{"start": 0, "end": 14, "quote": "Type: Purchase"}],
    "amount": [],
    "occurred_on": [],
    "posted_on": [],
    "merchant": []
  }
}
```

That response to `Type: Purchase` produces an incomplete, pending-review
transaction, not an accepted purchase. Offsets are zero-based Python Unicode
character positions, end exclusive, relative to the exact supplied text. Evidence
selects entire labelled lines, excluding LF. There are no model-supplied financial
values or account IDs. Each of five field arrays has at most four spans; each quote
has at most 512 characters. Strict integers reject string/float/bool offsets.

A synthetic end-to-end example, run from the repository root after `uv sync --locked`:

```python
import json
from pathlib import Path
from spending_optimiser import Account, Observation
from spending_optimiser.extraction import (
    AccountContext, ExtractionRun, Notification, NotificationExtractor,
)

fixture = json.loads(Path("tests/fixtures/extraction.json").read_text())[0]

def fake_model(request):
    assert request.notification == fixture["notification"]
    return json.dumps(fixture["response"])

result = NotificationExtractor(fake_model).extract(
    Notification(
        content=fixture["notification"],
        data_class="synthetic",
        observation=Observation(
            observation_id="demo-observation",
            source_system="synthetic",
            source_namespace="demo-inbox",
            source_record_id="demo-message",
            observed_at="2025-01-02T12:00:00Z",
        ),
    ),
    AccountContext(account=Account(
        account_id="demo-account", kind="credit_card", label="Synthetic account",
    )),
    ExtractionRun(
        processing_run_id="demo-run", revision_id="demo-revision",
        recorded_at="2025-01-03T00:00:00Z",
    ),
)
assert result.outcome == "proposal"
assert result.transaction.amount.value.minor_units == 1234
assert result.transaction.review.status == "pending"
assert result.transaction.posted_on.status == "unknown"
```

The same callable shape can later wrap a provider-specific client. No such client
is implemented here. A future wrapper must enforce network timeouts, request and
output/token limits, structured-output mapping, redacted errors/logging, and the
provider's actual privacy/retention behavior. The synchronous use case cannot
interrupt a hung callable or prevent an incorrectly implemented callable from
logging data; wrappers are trusted application code, not model-generated code.

## Privacy gate

`Notification.data_class` defaults to `private`. With that default, **the callable
is never invoked** unless the caller supplies `external_model_opt_in=True` on that
particular call. Truthy strings or integers do not grant consent. This conservative
gate applies even if the injected callable might be local. It neither records nor
implies a persistent privacy choice or configures a live provider.

Only fixtures explicitly marked `data_class="synthetic"` **and** having
`observation.source_system="synthetic"` bypass the gate. These labels are caller
assertions, not automatic anonymization. Never relabel real data as synthetic.
No opt-in or private data is required for tests. The model receives notification
content only, not caller account identity/label, source namespace, or run metadata.
Results contain private field evidence when a proposal is produced; do not log or
commit those results for real notifications.

## Deliberately bounded evidence grammar

The first implementation supports plain text consisting only of these exact,
case-sensitive labels, `: `, and a value, separated by LF. Blank lines are allowed.
Unknown prose, HTML, headers, control characters, account/MCC/eligibility fields,
unknown labels or unrecognized types are unsupported. Transport adapters must not
silently normalize text after computing evidence offsets and the content digest.

- `Type`: Purchase, Refund, Reversal, Income, Transfer, Decline, OTP, Balance, Marketing.
- `Amount`: uppercase supported ISO currency, space, unsigned decimal major units,
  e.g. `SGD 12.34`, `JPY 1234`, `KWD 1.234`. Conversion uses integer arithmetic,
  currency-specific exponents, no floats, rounding or assumed currency. Zero,
  overflow, commas, signs, currency symbols, scientific notation, and excess
  fractional digits are unsupported. Currency coverage is the existing `Money` set.
- `Event date` and optional `Posting date`: real `YYYY-MM-DD` calendar dates.
  Missing event dates remain unknown; observation/recording dates are not substitutes.
- `Merchant`: literal nonblank text, at most 128 characters. Even instruction-like
  merchant names remain inert proposed text.

This grammar is an intentionally conservative fixture-format contract. An AI model
can select evidence but cannot broaden the deterministic validators. The independent
scanner checks *all* labelled lines and parses values itself; omitted contradictions,
wrong fields, duplicate spans and fabricated quotes/offsets fail. Distinct repeated
values become `Fact(status="conflicting")` alternatives with evidence. Repeated Type
lines also force review because this component represents at most one event.
Real bank emails, arbitrary free prose, multi-event messages, ambiguous date formats,
FX/settlement amounts and learned templates are **not supported or qualified**.
General-language AI accuracy cannot be inferred from these tests.

## Outcomes and provenance

- `proposal`: essential kind, amount and event date are grounded in the bounded
  grammar; account is explicitly supplied. **Still pending independent review.**
- `review`: missing/contradictory fields, mixed transaction/non-transaction types,
  repeated Type lines, or posting date before event date. Resolved fields stay
  proposed, unresolved fields stay unknown/conflicting; no accepted facts appear.
- `non_transaction`: grounded decline, OTP, balance or marketing classification,
  with classification evidence and no transaction.
- `unsupported`: abstention or unsupported format/value, with no transaction.
- `invalid_response`: malformed/extra/unbounded/ungrounded/incomplete model output,
  omitted evidence, or classification inconsistent with the notification; no transaction.
  This differs from a *notification* missing facts, which can produce `review`.
- `model_error`: one callable failure, with no retry or transaction.
- `disclosure_required`: no callable invocation or transaction.

`issues` are fixed diagnostic codes, not raw exception or model text. Invalid
application inputs (bad provenance hash, invalid metadata, too-large/empty notification,
recording before observation) raise validation/value errors before invoking the model.
Input is limited to 16,384 Unicode characters. Structured selection parsing rejects
nonfinite JSON numbers, deep malformed JSON, duplicate keys and unknown properties.

Model-extracted facts are always `proposed` or `conflicting`, never `verified`.
Every selected field carries `EvidenceRef(target="observation")`, the exact quote
and `content:chars:<start>:<end>` locator. Caller-supplied account context is explicitly
marked as such in its fact note; it has no fabricated notification evidence and is
not a verified personal mapping. Card identity, MCC, direction, channel, transfer
purpose, income subtype, related purchase, eligibility and rewards are not inferred.
Posting date remains unknown unless explicitly evidenced. Amounts are unsigned
positive magnitudes for all five event kinds, not ledger postings.

The supplied observation identity/namespace/provider record ID is retained and its
SHA-256 digest populated or verified against the exact UTF-8 content. The event ID
is derived from source system/namespace/provider record ID and fixed `primary` event
key, never from subject, amount, date, account mapping or model response. Replays
keep this event identity; the caller supplies revision ID, recording time and run ID.
The caller must store exact private text if evidence is to remain resolvable later.
Private evidence is never added to public `SourceEvidence`, products or card rules.

There is **no replay/deduplication storage or manual-correction precedence** here.
Standalone proposals intentionally have no `supersedes`; recording a replay as a later
revision requires the ledger's [history and inclusion policy](automatic-transactions.md#history-inclusion-and-evidence).

## Independently tested and remaining work

`tests/fixtures/extraction.json` contains synthetic notification/response golden
pairs; `tests/test_extraction.py` also injects controlled fake clients and adversarial
responses independently of the implementation scanner. Tests cover all five event
kinds; non-transactions; missing facts; amount/date/type conflicts; supported currency
exponents; invalid currency/precision/overflow; posting dates; source/replay identity;
private-disclosure blocking; malformed/extra/oversized output; fabricated or omitted
evidence; embedded instructions; Unicode; and redacted model failures. Returned
proposals also round-trip through the existing `SchemaDocument` validation contract.

Run `uv run --locked pytest`, `uv run --locked ruff check .`, and
`uv run --locked ruff format --check .` from the repository root.
No external-model request, real email, provider credential or user privacy decision
is involved. Before bank-format use: obtain separately authorized sanitized examples,
extend/qualify evidence grammars against independent expected facts, and define source
authenticity and account-mapping review. Before live/provider use: implement and audit
the narrow transport wrapper and an explicit disclosure/consent flow. The implemented
[Gmail connection](gmail.md), [local ledger](local-ledger.md) and
[automatic template learning/application](automatic-transactions.md) are separate from
this use case. Financial eligibility, rule publication and generic agent/tool execution
remain outside its scope.
