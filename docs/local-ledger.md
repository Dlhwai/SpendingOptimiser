# Local import and review milestone

This is an offline schema-document importer with a persisted notification-event
ledger. It is **not** a bank-email parser, Gmail connector, complete bank
reconciliation, reward calculator, or live card-rule catalog. This offline ledger
workflow makes no network or model calls. The separate [Gmail connection and
candidate-loading UI](gmail.md) can now make user-authorized Google requests;
it never imports mail as transactions or alters financial review decisions. All bundled data is synthetic, including its unverified card-rule
examples. Supplied real card names remain unresolved labels.

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

1. From the **Overview**, open **Import data** and click **Import bundled synthetic
   example**. This imports the exact synthetic schema fixture bundled with the package. Alternatively, upload
   `tests/fixtures/synthetic.json` using **Import a schema document**. Both paths
   run the same validation and persistence code. Paste JSON or upload a file, not
   both; the complete request must fit within 2 MiB including form overhead.
2. Expect **11 new records** in the import notice. Open **Transactions** to see
   **1 event** and **1 need review**. Counts include public products/evidence/rules
   and private accounts/cards/observations/events. The purchase is SGD 1234
   **minor units** (SGD 12.34). Its monthly inclusion follows the
   [monthly workflow](monthly.md#inclusion-rules-and-financial-meaning).
3. Open **Review queue**, then **test-purchase**. Inspect per-field uncertainty,
   evidence locators, the private synthetic observation, and the original history.
   Unknown posting date, merchant, MCC, and other optional values are not inferred.
4. Choose **Review this event**, enter a reviewer and reason, and expand **Correct
   a fact (optional, advanced JSON)**. Choose `merchant` and paste the following
   complete replacement Fact JSON:

   ```json
   {"status":"proposed","value":"Synthetic cafe"}
   ```

   Leave the action as **Save correction, keep in review queue** and submit.
   Expect a new `local-...` revision, a correction audit entry, and the unchanged
   original revision. `proposed` is intentional: a correction is not automatically
   verified source truth. To retain or add field evidence, include an `evidence`
   array with existing source/observation ids and locators.
5. On the refreshed detail page, enter a reviewer and reason again, leave the
   correction field and JSON empty, select **Accept resolved event**, and submit.
   Expect a third review revision and **0 results** in the review queue.
6. Return to **Import data** and reimport the bundled example. Expect **0 new records**,
   **11 unchanged records**, one canonical event, and the manual merchant still
   present. Stop and restart the service with the same database path; all three
   transaction revisions and the accepted head remain.

**Transactions > Refresh local data** rereads SQLite. It never pretends to fetch Gmail.
The separate **Card evidence** view excludes private accounts,
observations, transaction quotes, and correction histories. Public evidence
referenced by a transaction is visible in its detail, while private observations
cannot be referenced by public records under the existing schema validation.

## Workspace navigation

- **Overview**: calendar-month canonical transactions, decision counts, separate
  currency/kind magnitudes, purchase grouping and daily charts, with exclusions.
  All data comes from saved records, never page-triggered retrieval or AI.
- **Mail inbox**: one-time setup guidance, Connect/Restore, explicit bounded month
  Load/Continue/Refresh, persistent account-scoped candidates and retrieval state.
  Mail stays separate from ledger events. See [monthly workflow](monthly.md).
- **Transactions**: all canonical ledger events, including accepted/rejected ones.
  Open an event for facts, source evidence, review/correction and immutable history.
- **Review queue**: events needing a decision. Reviewer, reason and action are
  primary; complete Fact JSON correction is available in an optional disclosure.
- **Import data** and **Card evidence**: advanced snapshot import and public
  reference data, separate from the main onboarding journey.

The interface uses local system fonts, server-rendered semantic forms, visible
keyboard focus and responsive layouts without scripts, remote assets or a frontend
build. See the README's disposable synthetic preview for safe UI exploration.

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
  standalone documents yield 422. Database failures yield 503 with no partial save.
- Only a validated linear chain's latest transaction revision is shown as the
  ledger event. Older revisions remain available in detail history. Explicit
  `duplicate_of` links are retained and labeled, not silently merged or totaled.
- Review writes a new revision with the same event id and origins, a new revision
  id, `supersedes`, increasing timestamp, actor, reason, and review metadata. A
  changed fact also records `Correction` metadata and a field review. No-op
  corrections and edits to identity/origins are rejected. Only one fact is edited
  per action; multiple missing facts can be corrected in successive revisions.
- Pending/corrected events and events with conflicting fields appear in the
  queue, unless the current revision rejects the event. Rejection is event-level:
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

There is no multi-user login, encryption at rest, retention/deletion UI, migrations beyond
local database format 1, automatic backups, ledger-table pagination, or production hosting
support. Do not expose the service through a proxy, LAN bind, or tunnel; the local
OS user and other software on the machine remain trusted. All database readers
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
