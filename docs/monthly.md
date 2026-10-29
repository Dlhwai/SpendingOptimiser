# Periods, saved mail and charts

Step 3, **Understand this month**, summarises **saved ledger facts** for a period you
choose: a single day, a calendar month or any run of days up to three calendar months.
It is not a Gmail parser, a bank statement or an AI dashboard. Every page view reads
local data only, never calls Gmail or a model, and never changes saved data. There is
no chatbot or background sync. Explicit load actions create supported automatic
transactions; merely viewing this overview never processes mail. The optional
[safe-reference AI workflow](subscription-ai.md) is a separate tab.

## Choosing a period

Every page that shows month data (Connect Gmail, Saved mail, Transactions, Storage's
restore form and this overview) has the same control:

- **From** and **To** date inputs. They are the browser's own date inputs, so the
  calendar opens from the field and the keyboard works: type the digits, or use the
  arrow keys in the calendar.
- Dates are **inclusive** and are **Asia/Singapore** calendar days. There is no time
  zone setting.
- Quick links: **This month**, **Last month** and **Last 3 months** (this month and
  the two before it). The default is the current Singapore month.
- One day (From equals To) up to three calendar months.

### The three-month limit

Internally a period is the half-open interval `[start, end)`: `end` is the day after
the chosen last day. A range may start on any day, so it is measured on the calendar
and never as 30 or 90 days:

> The latest allowed last day is the day before *start plus three calendar months*,
> where that date keeps the start's day number or, if the month is shorter, is moved
> back to the last day of that month.

| Start | Latest last day | Days | Reason |
| --- | --- | --- | --- |
| 1 Jan 2025 | 31 Mar 2025 | 90 | three whole months |
| 31 Jan 2025 | 29 Apr 2025 | 89 | 31 Jan + 3 months = 30 Apr (April has no 31st) |
| 1 Aug 2025 | 31 Oct 2025 | 92 | longer than 90 days is fine |
| 1 Dec 2023 | 29 Feb 2024 | 91 | leap year |
| 1 Dec 2024 | 28 Feb 2025 | 90 | not a leap year |
| 29 Nov 2023 | 28 Feb 2024 | 92 | 29 Nov + 3 months = 29 Feb, so the last day is the 28th |
| 30 Nov 2024 | 27 Feb 2025 | 90 | 30 Nov + 3 months clamps to 28 Feb |

Both ends may fall in differently named months or years, such as 30 Dec 2024 to
2 Jan 2025. Years run from 1970 to 9998.

### Links and validation

- A period that is exactly one calendar month keeps its month identity:
  `/?month=2025-02`. Choosing 1 to 28 February 2025 in the date inputs is the same
  period, shares its saved retrieval progress and displays as **February 2025**.
  1 to 28 February 2024 is not (2024 has a 29th), so it is a range.
- Any other period is `/?start=2025-01-31&end=2025-03-01`, with both dates inclusive.
- Existing `?month=YYYY-MM` links, forms and saved retrieval progress keep working.
- The server rejects with a 422 error, on every page and on every form post: a
  reversed range, a malformed or impossible date (`2025-02-30`), a year outside
  1970-9998, a range over the limit (the message names the latest allowed day), a
  start without an end, an end without a start, and `month` together with
  `start`/`end`. A form post must name its period; there is no silent default.
- In the browser the To field's `min` and `max` follow the From date, so the browser's
  own calendar and form validation enforce the limit and the form will not submit past
  it. A small script (`static/period-picker.js`, no build step or external library)
  keeps `min` and `max` in step when From changes. Without it the form still works and
  the server decides.

## Loading mail for a period

1. Start `uv run --locked spending-optimiser` and open http://127.0.0.1:8765.
2. In **Connect Gmail** (step 1), complete the [one-time Gmail setup](gmail.md) and
   connect through the sign-in window. No mail is fetched at sign-in or startup.
3. Choose a period and **View**. This reads SQLite only. **Load period** downloads up
   to the chosen 1-5 pages of 20 message IDs (default 2). Duplicate IDs count toward
   that bound.
4. **Continue period** until **Gmail search ended**. **Retry failed messages** re-fetches
   only messages that were proven undecodable (`invalid_message`) or too large
   (`response_too_large`), up to 20 per selected page. **Refresh period** restarts the
   query while keeping and deduplicating saved mail.

Rules that apply to every period:

- The Gmail query uses epoch seconds for the Singapore midnights that open and close
  the period, excluding spam and trash. A local half-open filter on Gmail's receipt
  time then decides membership. Mail received at 00:30 on 1 March in Singapore belongs
  to 1 March even though its UTC time is 28 February.
- **Progress belongs to one Google account and exactly one period.** The checkpoint
  (page token, successful IDs, unresolved IDs with codes, page and duplicate counts,
  errors, exhaustion) is stored under the account and the period's key. A form for
  another account or a stale connection is refused before any request is made.
- **Coverage never spreads.** A scan covers only the period it loaded. Loading a
  whole month does not mark one day inside it, or a range across it, as loaded, and
  two loaded neighbouring months do not make a range across them loaded. Mail already
  saved for any period is still listed wherever it falls, but an unscanned period says
  **Not loaded** or **More mail to load** until it is exhausted itself.
- Successful candidates survive failures. A failed listing, network, rate-limit or
  authentication error never advances the page, so Continue retries it. Unsuccessful
  retries move to the end of the persisted order, so repeated bounded retries reach
  later messages even across restarts. Exhaustion with unresolved messages is labelled
  incomplete, and exhaustion is never proof of complete financial coverage or a stable
  mailbox snapshot.
- There are no automatic retries, mailbox backfills, watches or schedules.
- Receipt time is Gmail's `internalDate`. It is not the financial event date, and
  user-supplied financial facts keep their own date-only event dates.

## What the overview includes

- A period is `[start, end)` over each event's `occurred_on` date, a calendar date
  with no time zone. Posting date, Gmail receipt, observation and revision times are
  never substituted. A transaction dated 1 March belongs to a period only if that day
  is selected.
- Only the latest canonical revision is read. Corrections replace earlier facts in the
  projection, not in history. Every dated record in the period appears in the table
  with its decision.
- Summaries count an accepted event with a resolved kind, amount, event date and
  account. Explicit duplicates, conflicting or rejected facts, pending or corrected
  records and rejected records are excluded and listed with the reason. Review
  acceptance does not verify bank truth.
- Records with no known event date cannot be placed in any period. Their ledger-wide
  count and links are under **Coverage**.
- Records are grouped by the explicit `(card_id, account_id, channel)` tuple. An
  unknown card or channel stays **Card unassigned / Avenue unknown**. Nothing is
  guessed from mail, product names or account type.
- Money is integer minor units in the schema's 0, 2 or 3 place exponents. Each
  currency has its own summary and charts; there is no FX or mixed-currency total.
- Amounts are unsigned magnitudes, not bank postings. Purchases, refunds, reversals,
  income and transfers are shown separately and never netted. Charts show purchases
  only. No balance, net spend, reward or budget is invented.
- The bar chart, the daily timeline, the totals, the transactions table and the
  **Transactions** list use the same records. Each chart has an exact-data table.
  The daily chart omits days without purchases. An empty period shows **0 results**.
- Gmail-derived events and their history/source views are scoped to the connected
  Google namespace and hidden on disconnect/account switch. Non-Gmail imported
  events remain an independent local workspace.

## Extraction and privacy boundary

[Automatic local processing](automatic-transactions.md) now creates canonical
accepted transactions through private local template discovery/application after
explicit Load/Continue/Refresh/Retry. Supported narrative, labelled and HTML layouts
still require unambiguous financial fields and explicit Account evidence. Each action
handles up to 100 undiscovered saved versions and 100 current candidates. Historical
versions teach structure only; Continue handles a remaining local backlog
after search ends without refetch. Unsupported/uncertain mail is inline and excluded.
The optional model-span use case and accepted safe-layout diagnostic proposals stay
separate; neither can broaden automatic acceptance. No private mail goes to the
[AI workflow](subscription-ai.md). Page views never change ledger facts.

## Saved mail and storage

Saved mail (headers, text and every retained content version) is readable SQLite data
protected only by OS file access, with no application-level encryption. Mail is
stored per Google account by message ID. Disconnect hides saved mail and removes the
saved authorization from `.env`, but never deletes saved mail or ledger history. See
[Storage](storage.md) for automatic compression, restoration and recovery. The
compression window (the current Singapore month and the two before it) is fixed and
does not move when you choose a different period.

## Synthetic desktop preview

```sh
uv sync --locked
uv run --locked python examples/monthly_preview.py --port 8877 --state partial
```

Open http://127.0.0.1:8877/?month=2025-02, or any period such as
`/?start=2025-01-31&end=2025-03-01`. The preview runs the real application against a
disposable database, a temporary `.env`, fake authorization and a fake Gmail transport
that answers each query from its time window. No normal ledger, real `.env`, real
consent or network is used. Port 8765 is refused and Ctrl-C deletes the database.
Use `--state full` or `--state empty` for other starting states.

The seeded ledger is fictional schema records, not extracted mail:

| Period | Result |
| --- | --- |
| February 2025 | 15 dated records: 12 accepted (one explicit duplicate excluded), 2 pending, 1 rejected; 1 undated record. SGD purchases SGD 158.80; JPY 1,234, KWD 1.234 and USD 12.99 stay separate; refund 5.00, reversal 1.00, income 2,500.00 and transfer 99.99 SGD are not netted. |
| 31 Jan to 1 Mar 2025 | 17 dated records (the two boundary days add SGD 9,000.00 each); SGD purchases SGD 18,158.80. |
| 8 Feb 2025 | 2 records, SGD 131.50. |
| 1 to 3 Jun 2025 | 0 results. |

In Connect Gmail, loading February 2025 continues three pages to 5 unique messages
and 1 duplicate. Loading 31 Jan to 1 Mar 2025 is a separate scan that finds 7 fictional
messages, including one received at 23:30 on 31 January and one at 00:30 on 1 March.

## Validation

```sh
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

The tests use disposable databases, fake stores and transports, and no real OAuth.
They cover the boundaries above (single day, the exact three-month limit with
end-of-month clamping, leap and ordinary years, cross-month and cross-year ranges,
Singapore midnight edges, invalid and over-limit input), period-scoped continuation,
retry, refresh and account switching, historical month data, and agreement between
totals, charts and tables. A browser script test compares the date picker's limit with
the server for every day across two years. Live Gmail and real-bank extraction
compatibility are not verified; see [Gmail](gmail.md#validation-readiness-versus-live-verification).
