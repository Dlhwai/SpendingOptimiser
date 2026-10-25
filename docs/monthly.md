# Deterministic desktop monthly workflow

The main page is a calendar-month view of **saved ledger facts**, not a Gmail
parser, complete bank statement or AI-generated dashboard. All page GETs read
local data only. There is no chatbot, provider connection, model call, automatic
acceptance, background sync or customizable dashboard configuration.

1. Start `uv run --locked spending-optimiser` and open http://127.0.0.1:8765.
2. Open **Mail inbox**, complete the [one-time Gmail setup](gmail.md), then Connect
   Gmail. Google consent and native credential access are user-performed. No mail
   is fetched during sign-in or startup. Restore the saved connection after restart.
3. Select a calendar month and **View saved month**. This GET only reads SQLite.
   **Load month** explicitly downloads up to the selected 1-5 pages of 20 IDs.
   Default: 2 pages. Duplicate IDs count toward that per-action bound.
4. Use **Continue / retry month** until **Query exhausted**. A successful empty
   nonterminal page remains incomplete. Successful candidates survive failures;
   retry replays the unadvanced page, skipping resolved IDs. Listing, network,
   rate-limit and authentication failures never advance the page. A message that
   is proven undecodable (`invalid_message`) or over the response limit
   (`response_too_large`) is retained as an unresolved ID for this account and
   month, and later pages continue. **Retry failed messages** re-fetches only
   those IDs. Exhaustion with unresolved messages is labeled incomplete
   coverage. Page and duplicate counts are added once per completed page, not
   per retry. API failures, invalid/expired tokens and pagination cycles are
   explicit. **Refresh month**
   restarts the query while preserving/deduplicating saved data. There are no
   automatic retries, mailbox backfills, watch/history sync or schedules.
5. Inspect escaped saved candidate text. These are raw candidates, including
   unrelated messages, not accepted transactions. Gmail receipt time uses a
   half-open Singapore-time month (Asia/Singapore, UTC+08:00): mail received at
   00:30 on 1 March in Singapore belongs to March, even though its UTC time is in
   February. Month selectors default to the current Singapore month. Receipt
   time is not the financial event date; ledger event dates stay date-only facts. Query exhaustion is
   not proof of financial completeness or a stable mailbox snapshot. Refresh
   does not erase previously saved mail absent from the latest provider response.
6. Open the **Overview**, choose the financial event month, and inspect latest
   canonical ledger records, decisions, purchase charts and exact chart tables.
   The ledger is the independent local workspace, containing all imported sources,
   not automatically mapped to the currently connected Gmail account. The account
   switch only changes visible saved mail, not offline ledger history.

## Current extraction and privacy boundary

The separate constrained extraction module is fixture-tested but **not wired to
Gmail or a live AI provider**. Saved mail does not yet produce proposals. Reviewed
transactions come from the existing offline schema import/review workflow.
The synthetic preview demonstrates that saved-data workflow, not AI extraction.
Provider integration remains deferred until the privacy decision: whether any
selected saved content may leave this device or processing must be fully local.
No API-key form or fake working AI connection is provided.

## Inclusion rules and financial meaning

- Calendar month is `[first day, first day of next month)` by `occurred_on`.
  Posting date, Gmail receipt date, observation time and revision time are never
  substituted. The default month is the local system calendar month.
- Read only the latest canonical revision. Corrections replace prior facts in the
  projection, not in history. All dated decisions appear in the monthly table.
- Summaries require an accepted event with resolved kind, amount, event date and
  account. Explicit duplicates, conflicting/rejected facts, pending/corrected and
  rejected records are excluded. Review acceptance does not verify bank truth.
- Unknown/unresolved event dates cannot be assigned to a month; their ledger-wide
  count and links are disclosed under Inclusion & coverage. Missing amounts and
  other exclusion reasons remain visible in the dated table and coverage details.
- Group by the explicit `(card_id, account_id, channel)` tuple. Labels include IDs
  so equally named accounts do not merge. Unknown card/channel stays **Card
  unassigned / Avenue unknown**, even if an account happens to have one known
  card. No guessing from mail, product names or account type.
- Money uses integer minor units throughout, with the schema's 0/2/3-place display
  exponents. Each currency has separate summaries and charts. No FX or mixed-
  currency total exists. Even sums exceeding an individual Money's int64 limit
  retain Python integer precision.
- The stored schema defines **positive unsigned event magnitudes**, not signed
  bank postings. Consequently purchases, refunds, reversals, income and transfers
  are displayed separately, never netted. Purchase charts show purchase
  magnitudes only. Refund/reversal linkage or `direction` does not establish
  settlement, so no negative cashflow, net spending or account balance is invented.
- Card/avenue bars and the daily timeline derive from the exact same included
  purchase records. Every chart has an accessible exact-data table. Daily charts
  omit days without included purchases, explicitly labeled. No opening balances,
  card limits, rewards, budget assumptions or bank terms are inferred.

## Saved mail and storage

The existing private SQLite database now adds `mail_versions`, `mail_current` and
`mail_scans` tables without rewriting schema records or advancing ledger format 1.
Saved mail (headers, message text and every retained content version) is
readable SQLite data protected only by OS file access. There is no app-level
encryption.
New databases retain owner-only file permissions. Protect backups and disk access.
No mail is promoted to the immutable financial observation/event tables by loading.

Cache keys use the stable Google `sub` namespace plus message ID, never email
address, subject or amount. Month checkpoints retain page tokens, successful IDs,
unresolved failed-message IDs and codes, page/duplicate counts, last action time, errors and an exhaustion flag. Changed
normalized mail snapshots retain previous versions; repeated identical content
(without counting a new retrieval clock as new content) is duplicate-safe. The
current pointer selects the most recently retrieved content even when it reverts
to an earlier version. Stored observation IDs remain stable.

Disconnect hides all saved mail, cancels sign-ins and deletes saved refresh authorization from the native store.
**It does not delete saved mail or ledger history.** The same stable Google
identity restores its cache; another account sees only its own mail. The default
page never opens the native store or authorizes a network request. Credential
store access remains explicit Connect/Restore/Load/Disconnect actions. OS access
is trusted; this is not multi-user access control. Saved mail is retained after
disconnect, and there is no retention, purge or deletion action. Calendar-month
loading is the only Gmail retrieval path.

## Reproducible synthetic desktop preview

```sh
uv sync --locked
uv run --locked python examples/monthly_preview.py --port 8877 --state partial
```

Open **http://127.0.0.1:8877/?month=2025-02**. This runs the actual product with a
disposable synthetic database, in-memory fake authorization and fake Gmail
transport. No normal ledger, native store, real OAuth consent or network is used.
Use **Mail inbox**, choose February 2025, and Continue to exhaust three synthetic
pages with 5 unique messages and 1 duplicate. The overview has 15 dated records,
12 accepted decisions (one explicit duplicate excluded), 2 pending, 1 rejected and
1 separately disclosed undated record. SGD purchases total **158.80** after the
canonical grocery correction; JPY 1,234, KWD 1.234 and USD 12.99 remain separate.
Refund 5.00, reversal 1.00, income 2,500.00 and transfer 99.99 SGD are not netted.

Use `--state full` for exhausted saved mail, or `--state empty` for an empty ledger
and not-started mail. Selecting January 2024 shows a no-results month without
changing data. Port 8765 is refused; Ctrl-C deletes the disposable database.
These facts are explicitly seeded fictional schema records, not extracted mail.

Validation: `uv run --locked pytest`, `uv run --locked ruff check .`, and
`uv run --locked ruff format --check .`. Monthly behavior tests use disposable
synthetic databases, fake stores/transports and no real OAuth. Live Gmail and
real-bank extraction compatibility remain unverified.
