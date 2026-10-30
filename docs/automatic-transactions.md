# Automatic local transactions and learned email templates

An explicit **Refresh** saves Gmail messages first, then derives reusable templates from
saved emails, applies them locally and validates source-linked financial facts.
Unambiguous supported transactions are accepted without an import or review click.
Uncertain, conflicting, unsupported and nonfinancial messages stay saved and excluded,
and are reachable as evidence from a transaction's detail page.
The application's processing never sends email, private intermediate templates or
financial records to AI.

## What learning establishes

The app discovers a private structural skeleton and a closed field-role plan from
saved text, including normalized HTML and compatible MIME alternatives. Values in
recognized slots are replaced by typed placeholders, so another message with the
same layout can reuse the definition with different financial values. Definitions
and source-version receipts are account-scoped local data. Changed structure needs
new discovery; a previous self-match or repeated layout is not qualification.

Every application rechecks explicit financial semantics, money occurrences,
conflicts and representations. Learning a layout does **not** establish the meaning
of an arbitrary number, a bank/account relationship, a date or a transaction kind.
Unknown semantics remain unknown, rather than becoming guessed accepted facts.
Recognizable partial dates and masked identifiers can be reusable structural slots
without becoming financial dates or account mappings. Two-digit years do not acquire
a century from the receipt clock, and a generic Transaction Alert is not a purchase.
This is deterministic on-device learning, not an LLM, arbitrary generated parser
code, a universal bank parser, or independent bank-statement verification.

The current semantic vocabulary supports:

- Explicit Type / Transaction type fields and purchase, refund, reversal, income,
  salary or transfer meanings. A generic transaction, debit or credit alone does not
  establish purchase/income. Nonfinancial Type values are excluded.
- Amount fields with explicit supported ISO currency or `S$`; separate Currency
  and numeric Amount fields; or currency explicitly in an Amount label. Plain `$`
  is ambiguous. Grouped thousands must be syntactically valid. Conversion uses
  integer minor units with the schema's currency-specific precision and no rounding.
- Financial date fields, including date/time tables, ISO dates, English month names,
  and numeric dates only when their ordering is unambiguous. `03/04/2025` stays
  unresolved; neither receipt time nor an untrusted mail Date header substitutes.
  Unassigned or century-ambiguous date tokens retain evidence and fixed uncertainty
  reasons. Semantic label whitespace is handled without changing source offsets.
- Explicit narrative assertions such as the **fictional** example below, not
  arbitrary free prose. An assertion can bind kind/amount, while `on` and `using
  account reference` bind a date and reference in that same assertion.
- Opaque Account / Account reference fields. A complete numeric account reference
  additionally needs explicit Bank / Institution evidence. A sender label, Gmail
  address, masked suffix or card reference does not establish an underlying account.
  Account records are kind `other`; no underlying bank-account/card/product link is inferred.
- Optional Merchant and Posting date fields. Unknown direction, card, MCC, channel,
  posting date and other optional values remain explicitly unknown.

```text
Dear Fictional Reader,
Your purchase of USD 19.25 on 3 February 2025 using account reference travel-wallet.
Thank you for using this fictional service.
```

### Finite qualified notification contracts

`notification_profiles.py` also defines reusable completed card-payment and wallet-transfer
contracts. These require the complete confirmation, transaction-reference field, explicit
provider-qualified **From** reference, **To** field and financial Date & Time row in each
representation. They do not use the Gmail address, sender label or amount sign as authority.
Only the known outgoing card-payment contract can establish purchase semantics; withdrawals,
fees, repayment/top-up cues, changed/incomplete contracts and forwarded/multi-event bundles
are excluded. Wallet transfers remain transfers with unknown purpose, not assumed purchases.

A provider-qualified card/wallet reference identifies **notification source attribution only**.
It is hashed together with the mailbox namespace into an Account of kind `other`, never a
verified underlying bank account, credit/debit type, CardInstance or product association.
A bare masked identifier outside that complete contract still cannot establish attribution.

The qualified card request uses known `DD/MM/YY` ordering, corroborated by the separate
financial day/month row. An explicit full financial year takes precedence; otherwise this
finite contract uses the fixed POSIX `%y` convention (1969-2068), not a sliding receipt-year
window. Contradictory dates or completed activity after observation remain excluded. This
format convention is shown in date evidence notes. Generic two-digit dates remain unresolved.
A wallet date without a stated financial year remains unknown, even when the receipt timestamp
or an email header supplies a year. Source references are never decoded into financial dates.

Administrative contact/setup/security and statement-ready notices are classified nonfinancial
when there is no contradictory financial content. They do not create noisy ledger events.
Uppercase month names and company abbreviations are not mistaken for supported currency codes.

This example was authored for synthetic testing, not copied from a bank. Other
layouts can be learned when their slot meanings fit the supported semantics. A
currency-bearing value without field authority, multiple events, ambiguous account
attribution, conflicting/partial representations or non-final activity stays uncertain
or unsupported.

Financial representations must independently establish kind, amount/currency, event
date and account attribution and agree before facts are combined. Recognized card/account
fields and transaction headings cannot be ignored as irrelevant alternatives. Incomplete
financial alternatives yield uncertainty without projected facts, rather than supplying
each other's missing fields. Bare date/time/masked-identifier slots and irrelevant text
do not by themselves make a part financial. Identical complete plain-text/HTML alternatives
represent one event; multiple same-type financial parts are not silently deduplicated.

An adapter `ambiguous_subject` warning also prevents automatic acceptance. It is not
equivalent to a genuinely absent subject; the retained headers remain inspectable.

Work is bounded to eight inline parts, 16,384 normalized characters per part and
65,536 total characters. Mail markup stays inert; scripts, images and external links
are not executed/fetched. Attachments and broader natural-language interpretation
remain unsupported. Accepted facts stay `proposed`, not `verified`.

## Ordinary trigger and continuation

During an explicit Refresh the app repeats batches of up to **100 previously
undiscovered saved versions**, including historical versions, then up to **100 current
saved messages** from that account, until nothing is pending or one batch fails (at
most 30 rounds per Refresh). A current candidate is also learned on demand if it was
not reached by the historical discovery batch. Historical versions teach structure but
never create another transaction or override a decision.

This local backlog is account-wide and independent of the days loaded. Refresh finishes
it without refetching mail; if it stops early the status says so and the next Refresh
continues. No new import/template-preparation/review step is required. Nothing learns
or processes on GET/page viewing, startup, sign-in, restore-connection, timers or idle
background jobs. Opening a page reads progress only.

Mail and per-message financial commits are separate. Parsing/write failures preserve
committed mail and retryable failure outcomes. Unreadable archived/current/legacy
payloads are isolated through processing and ordinary views, never deleted or allowed
to block valid mail and continuation. Per-message canonical writes and successful
processing markers commit or roll back together. One post-batch maintenance pass
covers template and transaction writes, including partial-failure batches. Historical
discovery failures remain retryable behind unattempted versions, preserving damaged bytes
and fair bounded progress. A recovered readable version can establish its missing private
provenance without changing financial processing heads. Learning revisions retain older definitions.

## History, inclusion and evidence

Identity uses Google's stable opaque mailbox namespace, Gmail message ID and the
existing event key, not amount/date/content, sender or thread. Overlapping periods,
retries, reconnects and restarts do not duplicate canonical events. Separate Gmail
namespaces remain isolated. Non-Gmail imports remain a local workspace.

All source versions remain available. Facts cite the stable Observation, retained
version digest, exact normalized character span and MIME-part locator. Transaction
detail links to retained source versions, including checked compressed mail. Changes
append a linear canonical revision; unsupported replacements exclude a formerly
automatic event, and returning to an earlier source version does not revive two heads.
Absence/deletion from Gmail does not erase saved data.

Human and imported histories are protected. Reprocessing retains their decisions,
including rejection and corrections. Multiple imported event keys under one source
remain individually visible and counted correctly. Review/correction is optional at
record detail, not a mandatory queue. Different source IDs are not heuristically
reconciled into one bank transaction; explicit duplicate links or rejection remain
available in the existing ledger model.

A failed/deferred changed source may leave an older accepted head counted until a
successful update. The stored outcome of each message distinguishes **current source processing** from
**previous canonical inclusion** (the stored `mail_processing` and ledger heads), so a
newer version is never claimed to be applied while the older head still counts.

Receipt periods use Singapore boundaries; totals use date-only financial Event date.
Only latest accepted, resolved, nonduplicate records enter the same exact tables and
charts. Currencies and unsigned purchase/refund/reversal/income/transfer magnitudes
stay separate, without netting, balances, rewards or budget assumptions.

**Gmail search ended** only means no further search results. Failed/unreadable IDs,
unsupported/uncertain messages, processing failures and accepted transactions are
separate outcomes. Even a fully readable search is not complete financial coverage.

## Private state and generic Git assets

Private structural anchors, source evidence/backreferences and account-scoped learned
state stay in the local database, outside public schema exports and AI analysis.
Normal whole-database backup retains this state. No public-example or manual
sample-review prerequisite is imposed on local discovery.

The shareable `src/spending_optimiser/examples/email_templates.json` package asset is
locally reconstructed from a closed schema of MIME enums and known grammar/field-role
tokens only. It contains no
raw text, actual values, identifiers, source fingerprints, private examples, paths or
hidden metadata. Both generic generation and consumption reject unknown fields or
strings before any model-visible read. Synthetic sentinel tests exercise this
boundary. Masking, repeated text or hashing does not make an arbitrary literal public.

These generic forms are reusable structural hints via `bundled_forms` / `apply_generic`,
not bank-format certification or acceptance authority. Applications still validate all
required evidence. There is no sharing/export UI, online learning service or plugin
platform. Accepted AI placeholder layouts remain a separate optional diagnostic path;
private mail never goes to that model.

See [storage](storage.md) for automatic compression, retention and lossless recovery,
and [local ledger](local-ledger.md) for optional snapshot import and corrections.

## Synthetic proof

```sh
uv run --locked pytest tests/test_local_templates.py tests/test_template_qualification.py
uv run --locked python examples/automatic_preview.py --port 8897
# Minimal invented qualified-card and uncertain wallet/bundle fixtures:
uv run --locked python examples/automatic_preview.py --port 8897 --profiles
```

Use an unused loopback port, never 8765. The preview begins with an empty ledger,
fictional connection and fake Gmail transport. Load February 2025 to learn and reuse
non-labelled/HTML layouts, create eligible transactions and show inline exclusions;
Refresh demonstrates idempotency. No real sign-in/model/network access occurs.
Existing chart previews preseed financial fixtures and are not extraction proof.
Protected corpus qualification is separate developer verification with fixed aggregate
output and a query-only consistent backup, never a live deployment. Private verification
inputs and sample-specific findings are not public fixtures or validation-agent evidence.

## Cards, wallets and transfers

A completed-payment notice whose `From:` line states a card ("DBS/POSB card ending
NNNN") records one **card reference** per mailbox, issuer and ending, once however many
alerts repeat it. A `From:` line naming a wallet ("PayLah! Wallet (Mobile ending
NNNN)") records a **wallet reference**. Only card references are offered on
**Cards**; a wallet, mobile number, account reference or masked suffix from any other
format is never a card choice, and a full card number is never requested.

The transaction still keeps its source account reference as before. The product you
choose is stored beside the ledger, never inferred, and never rewrites a fact or
accepts an uncertain transaction. See [rewards](rewards.md).

A transfer from a wallet whose direction is explicitly outflow is recorded as a
transfer with `direction: outflow`. It counts once as spending, apart from card
purchases, unless the transfer purpose is marked as between your own accounts or a card
repayment. It has no card and earns no card reward. A year absent from the notice is
never taken from the email's receipt date, so such a notice stays uncertain and
excluded, exactly as before.
