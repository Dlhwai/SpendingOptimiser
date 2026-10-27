# SpendingOptimiser

A local, single-user app that keeps a private ledger of card and bank notifications and
shows what you spent over a day, a month or up to three calendar months. It runs on
your computer at `http://127.0.0.1:8765`, and nothing leaves it unless you explicitly
ask for a Gmail load or an AI analysis.

**Status: early.** The offline ledger, saved Gmail candidates and period views are
implemented and tested with synthetic data. Live Gmail has not been verified, and a
Google sign-in problem reported on the installed app is being diagnosed separately;
this work does not change sign-in. Rewards, qualified bank-email parsing and
card-term research are not built.

## What it does

The sidebar has three steps. Each opens only saved local data unless you press a load
or analysis button.

1. **Connect Gmail**: sign in with Google in a window the app opens (read-only access),
   then explicitly **Load**, **Continue**, **Retry** or **Refresh** mail for a period.
   Nothing loads at sign-in or startup. Progress is saved per account and period.
2. **Inspect saved data**: saved mail, transactions, the review queue, card evidence,
   data import and storage, as tabs.
3. **Understand this month**: the canonical transactions for a period with purchase
   charts by card, account and avenue and by day. Currencies and event kinds are never
   mixed or netted. An optional **Safe AI analysis** tab handles public card terms only.

**Periods.** Pick From and To dates in the calendar (inclusive, Singapore time), or use
This month, Last month and Last 3 months. A period is one day up to three calendar
months, and the server enforces that limit. See [periods and charts](docs/monthly.md).

**Storage.** Once usage is above 1,000,000,000 bytes the app compresses older detail
by itself after each write. Nothing is deleted and nothing needs to be run. See
[storage](docs/storage.md).

## Set up and run

Developed and tested with Python 3.11.9 and uv 0.11.8 (the pinned setup).

```sh
uv sync --locked
uv run --locked spending-optimiser
```

Open **http://127.0.0.1:8765**. The database defaults to
`~/.local/share/spending-optimiser/ledger.sqlite3`; override it with
`--database /absolute/private/path/ledger.sqlite3` or `SPENDING_OPTIMISER_DB`.

**Try it offline** (no credentials, model or Internet after install): open step 2, choose
**Import data**, import the bundled synthetic example, then open **Review queue**.

**Use Gmail** by following [Gmail setup and use](docs/gmail.md): create a Desktop OAuth
client, put its ID and secret in the gitignored `.env` (see `.env.example`), then
choose **Connect Gmail**.

## Guides

| Guide | Covers |
| --- | --- |
| [Periods and charts](docs/monthly.md) | Date ranges and the exact three-month limit, loading mail, what the overview includes |
| [Gmail](docs/gmail.md) | Google Cloud setup, sign-in, the read-only adapter, privacy, live-verification status |
| [Local ledger](docs/local-ledger.md) | Import and review walkthrough, Card evidence explained, persistence guarantees |
| [Storage](docs/storage.md) | Automatic compression, restoring older mail, recovery |
| [Safe AI analysis](docs/subscription-ai.md) | Optional ChatGPT connection for public terms |
| [Schemas](docs/schemas.md), [extraction](docs/extraction.md) | Data model and the fixture-tested extraction use case |
| [Project summary](summary.md) | Original brief, planned scope and current status |

## Try it safely with synthetic data

Each preview runs the real app against a disposable database and fake Google and model
services. Use an unused loopback port, never 8765. Ctrl-C deletes its data.

```sh
uv run --locked python examples/monthly_preview.py --port 8877   # periods and charts
uv run --locked python examples/ui_preview.py --port 8876        # first-run sign-in walkthrough
uv run --locked python examples/storage_preview.py --port 8898   # compression and restore
uv run --locked python examples/gmail_synthetic.py               # adapter only, no UI
```

## Privacy

- Loopback only, with CSRF, Host and CSP protections. Do not expose it through a proxy
  or tunnel.
- Saved mail and the ledger are readable SQLite files with no application-level
  encryption. The Google client and refresh authorization live in the gitignored `.env`,
  as plaintext with owner-only permissions. Protect the disk and backups.
- Private mail and transactions are never sent to a model. Only app-captured official
  public terms and allowlisted placeholder structures can be analysed, on an explicit
  per-analysis action.
- Commit only code, documentation and sanitized fixtures. Keep real emails, account
  details, credentials, databases and session state out of Git.

## Current limits

- No rewards engine, budgets, bank-statement reconciliation or automatic acceptance of
  mail-derived transactions. Ledger events come from schema-document import, and you
  review and correct them by hand.
- Gmail candidates are raw saved messages, not transactions. Real bank-email formats
  are unqualified. Exhausting a query is not proof of complete financial coverage.
- Live Google sign-in and live AI accounts are unverified.
- Card evidence is reference data from imported schema documents, not a researched or
  activated rule catalog.
- Built for small local ledgers, not high-volume use. There is no deletion UI, backup
  tool, multi-user login or hosted mode.

## Development

```sh
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Tests use synthetic fixtures, disposable databases and fake transports. CI runs the
same three commands. The planned architecture and the original roadmap are in the
[project summary](summary.md) and the
[visual architecture proposal](.lavish/project-architecture.html), which loads its
styling from external CDNs.
