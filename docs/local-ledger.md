# Local ledger, corrections and optional recovery

The canonical notification-event ledger receives [supported automatic transactions](automatic-transactions.md)
after explicit Gmail loading. This guide also preserves the optional offline
schema-document recovery/import walkthrough; it makes no network or model calls.
There is no complete bank reconciliation, reward calculator or live card-rule catalog. Bundled financial examples are synthetic, including the unverified card rules.
Generic template assets contain only closed MIME/field-role tokens, never source values. Supplied real card names remain unresolved labels.

## Setup and one start command

Use Python 3.11.9 and uv 0.11.8 (the repository's pinned development setup):

```sh
uv sync --locked
uv run --locked spending-optimiser
```

Open **http://127.0.0.1:8765**. The executable binds only to `127.0.0.1`, ignores
proxy forwarding headers, and disables access logs. Stop with Ctrl-C. Installed
packages and a browser are sufficient for offline operation; the first dependency
installation may need Internet access. No frontend build, CDN, credentials,
external model, or separate service is required.

The default database is
`~/.local/share/spending-optimiser/ledger.sqlite3`. Configure a different path:

```sh
uv run --locked spending-optimiser --database /absolute/private/path/ledger.sqlite3 --port 8765
```

`SPENDING_OPTIMISER_DB` also sets the path; `--database` takes precedence. Relative
paths resolve from the process working directory. New databases are created with
mode 0600; newly created containing directories use mode 0700. Existing file and
directory permissions are not changed. Use `spending-optimiser --help` for the
small command reference. Unknown flags and invalid ports fail without prompting.

Keep financial import files outside the repository, or in the Git-ignored `data/`
directory. Import filenames are not used as filesystem destinations. Uploaded
content is stored in SQLite, not as loose files. Databases, SQLite sidecars, logs,
and local runtime/data directories are ignored by Git. Do not commit real source
material, credentials, screenshots containing financial data, or exports.

## Complete synthetic browser workflow

1. For this optional developer/recovery walkthrough, open **Saved mail > Recovery & developer tools > Restore / import a schema snapshot**, then **Import bundled synthetic
   example**. This imports the exact synthetic schema fixture bundled with the package. Alternatively, upload
   `tests/fixtures/synthetic.json` using **Import a schema document**. Both paths
   run the same validation and persistence code. Paste JSON or upload a file, not
   both; the complete request must fit within 2 MiB including form overhead.
2. Expect **11 new records** in the import notice. Open **Transactions > All saved
   records** for **1 event** needing review; Overview defaults to the current
   month, while this fixture's event is dated January 2025. Import counts include
   public products/evidence/rules and private accounts/cards/observations/events.
   The purchase is SGD 1234 **minor units** (SGD 12.34). See the [monthly inclusion
   rules](monthly.md#what-the-overview-includes) for summary eligibility.
3. Open **Transactions**, then **test-purchase**. Inspect per-field uncertainty,
   evidence locators, the private synthetic observation, and the original history.
   Unknown posting date, merchant, MCC, and other optional values are not inferred.
4. Choose **Review this event**, enter a reviewer and reason, and expand **Correct
   a fact (optional, advanced JSON)**. Choose `merchant` and paste the following
   complete replacement Fact JSON:

   ```json
   {"status":"proposed","value":"Synthetic cafe"}
   ```

   Leave the action as **Save correction, keep excluded** and submit.
   Expect a new `local-...` revision, a correction audit entry, and the unchanged
   original revision. `proposed` is intentional: a correction is not automatically
   verified source truth. To retain or add field evidence, include an `evidence`
   array with existing source/observation ids and locators.
5. On the refreshed detail page, enter a reviewer and reason again, leave the
   correction field and JSON empty, select **Accept resolved event**, and submit.
   Expect a third review revision and an accepted canonical event.
6. Return to the optional snapshot-import form and reimport the bundled example. Expect **0 new records**,
   **11 unchanged records**, one canonical event, and the manual merchant still
   present. Stop and restart the service with the same database path; all three
   transaction revisions and the accepted head remain.

Reloading any page rereads SQLite. Opening a page never fetches Gmail.

## Card evidence

**Card evidence** (a tab of **Inspect saved data**) is a read-only list of the public,
card-level reference information saved in your local database. It answers "which cards
and which public sources does the app hold?", not "what did I spend?". It shows three
groups of records exactly as stored, each expandable to its full JSON:

- **Products**: the cards, by the name you supplied (for example "Maybank Rewards XL")
  plus issuer, country, variant and network. Each of those is marked unknown, proposed
  or verified. The three supplied cards are unresolved labels until their exact
  variants are researched.
- **Evidence**: the public documents the information came from. Each record keeps the
  document's URL, kind (issuer, program, campaign, editorial or synthetic), retrieval
  time, effective dates, document version and an optional content hash. It records
  where something was read, not that it is true.
- **Rules**: versioned rule sets proposed for a product: earning rates, minimum spend,
  caps, eligibility, exclusions, rounding, date basis and redemption. Every field has a
  status and cites its evidence. Older revisions are kept, never overwritten.

What it is **not**:

- Not your transactions, accounts, card numbers, mail or review history. Private
  records are deliberately excluded from this view. A transaction's own detail page
  shows the public evidence it cites.
- Not proof that a rule is correct, verified or in force. The page says so itself:
  *Reference data only: not an activated or verified rule catalog*. The app calculates
  no rewards from these rules.
- Not a researched catalogue. The app does not look cards up for you.

These public records arrive only through optional schema-document import in the
**Recovery & developer tools** disclosure; mail extraction does not create card rules. The
bundled synthetic example adds the three unresolved card names plus one fictional
product, one fictional evidence record and one fictional rule set (4 products,
1 evidence and 1 rule). Until something is imported each group shows **0 results**.
Official terms captured in **Safe AI analysis** and their proposals live in that
workflow's own saved snapshots and never appear here or change the ledger.

## Workspace navigation

The sidebar has exactly three steps; related views are tabs on their step.

1. **Connect Gmail**: Connect/Restore/Disconnect through a sign-in window, and
   explicit bounded Load/Continue/Retry/Refresh for a chosen period (a day, a month or
   up to three calendar months) with retrieval state kept per account and period.
   See [periods and charts](monthly.md).
2. **Inspect saved data**, with tabs:
   - **Saved mail**: persistent source messages with inline processing outcomes and
     links to transactions/evidence. Supported unambiguous messages are accepted
     automatically; other formats and uncertain entries are flagged and excluded.
   - **Transactions**: all canonical ledger events, including accepted/rejected ones.
     Open an event for facts, source evidence, review/correction and immutable history.
   - **Card evidence** (see above): public reference data.
   - No Review queue, Import data or Storage routine tabs. Correction/rejection is
     optional at event detail; snapshot import and storage recovery remain under
     Saved mail's **Recovery & developer tools** disclosure.
3. **Understand this month**, with tabs **Overview** (canonical transactions for the
   chosen period, decision counts, separate currency/kind magnitudes, purchase
   grouping and daily charts, with exclusions) and **Safe AI analysis**. All data comes
   from saved records, never page-triggered retrieval or AI.

The interface uses local system fonts, server-rendered semantic forms, visible
keyboard focus and responsive layouts, with no remote assets or frontend build. Its
only scripts are same-origin helpers: the sign-in window helper on the Connect pages
and the date-picker limit helper on pages with a period control. See the README's
disposable synthetic preview for safe UI exploration.

## Persistence and review contract

- Each input must be a self-contained, validated `SchemaDocument` format 1.0,
  including all predecessors and referenced records. Existing schema interfaces
  and meanings are unchanged. Duplicate JSON keys are rejected rather than
  silently selecting one value.
- Records are immutable SQLite rows, keyed by the existing collection identity:
  product/account/card/observation ids and evidence/rule/transaction revision ids.
  Public records and private records use separate tables within the same private
  database. This is logical separation, not multiple-user access control.
- The normalized original imported document is retained in the `imports` table
  with a SHA-256 digest and import time. It retains schema values/history, not
  byte-for-byte whitespace or object-key order. Repeating the same normalized
  document does not add another import record.
- One `BEGIN IMMEDIATE` transaction checks the input against **all** stored
  records and writes every addition or nothing. Identical records are unchanged.
  Different content under an existing immutable identity yields HTTP 409, never a
  replacement. Cross-import source collisions, history forks, multiple roots,
  dangling links, and other combined-document conflicts also yield 409. Invalid
  standalone documents yield 422. Pre-commit database failures yield 503 with no
  partial import/review save. For post-commit maintenance warnings, see
  [storage safety and recovery](storage.md#safety-and-migration).
- Only a validated linear chain's latest transaction revision is shown as the
  ledger event. Older revisions remain available in detail history. Explicit
  `duplicate_of` links are retained and labeled, not silently merged or totaled.
- Review writes a new revision with the same event id and origins, a new revision
  id, `supersedes`, increasing timestamp, actor, reason, and review metadata. A
  changed fact also records `Correction` metadata and a field review. No-op
  corrections and edits to identity/origins are rejected. Only one fact is edited
  per action; multiple missing facts can be corrected in successive revisions.
- Pending/corrected events and events with conflicting fields stay visible inline
  in Transactions/Saved mail, excluded from totals. No separate review step is required. Rejection is event-level:
  it carries every fact (including corrected or conflicting ones) forward
  unchanged and cannot be combined with a fact correction. Acceptance needs
  resolved kind, positive amount, event date, and known account, plus all
  existing schema integrity checks. Conflicting or explicitly
  rejected facts must be resolved before accepting. Optional unknowns can remain
  visible after acceptance; acceptance is not a claim that every field is known
  or verified. Rejection preserves history, rather than deleting the event.
- A stale review returns 409; refresh and inspect the current revision before
  retrying. A current revision at or after the local clock also returns 409 rather
  than fabricating a future action time; review needs a truthful later timestamp.
  SQLite serializes writes, preventing concurrent lost updates.
- Once an event has a **local** review/correction, any new imported revision for
  that event returns 409, even if it claims to continue the manual chain. Exact
  reimports remain harmless. There is no automatic conflict-resolution/rebase UI;
  this conservative boundary protects manual work during replay. Other events
  can still be imported in independent documents. An imported document with a
  conflicting protected event fails in its entirety.

## Local safety and limitations

Rendered imports are escaped. All state changes are POST forms with a server
nonce plus HttpOnly, SameSite=Strict cookie; cross-origin mutations and untrusted
Host names are rejected. Forms expire on restart. Responses disable caching and
external scripts/resources; source URLs are displayed as text, not fetched.
Uploaded bodies are bounded to 2 MiB. This ledger workflow makes no model calls or
transaction telemetry. The separate [safe-reference AI workflow](subscription-ai.md)
never receives imported private data. Tests and smoke validation use synthetic material.

There is no multi-user login, application-level encryption at rest, deletion UI,
automatic backups, ledger-table pagination, or production hosting support.
See [Storage](storage.md) for automatic compression, restoration of saved mail for a
period, database migration and post-write maintenance. Do not expose the service through
a proxy, LAN bind, or tunnel; the local OS user and other software on the machine
remain trusted. All database readers
can see private material. For a backup, stop the service and copy the database
(and any SQLite sidecars if present) to a private location. Protect backups like
financial source data. Large histories are loaded as one document, so this
milestone is intended for small local ledgers, not high-volume workloads.

Evidence checks establish links and chronology, not the truth of a quote or a
reviewer's financial interpretation. Deterministic monthly saved-data projections are documented in [monthly.md](monthly.md).
No statement balancing, adaptive extraction,
AI calls, scheduling, monthly budget, reward engine, or researched card terms are
implemented here. Gmail OAuth and read-only candidate loading are a separate
implemented feature, documented in [gmail.md](gmail.md). Later integration still needs sanitized bank
notification examples, credentials with an agreed privacy boundary, and verified
product/term information. Those inputs do not block this offline milestone.

## Validation

```sh
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

The service tests cover import/upload through browser endpoints, persistence
across application restart, duplicate replay, validation and SQL-failure rollback,
immutable identity/source/history conflicts, evidence separation, correction and
review history, stale submissions, manual precedence, input boundaries, CSRF,
Host/Origin checks, and HTML escaping. The original schema suite remains in place.
A real Chrome smoke uses the same synthetic import, correction, acceptance, and
reimport flow, without live financial material.
