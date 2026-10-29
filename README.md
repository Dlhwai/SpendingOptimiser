# SpendingOptimiser

A local, single-user app that keeps a private ledger of card and bank notifications and
shows what you spent over a day, a month or up to three calendar months. It runs on
your computer at `http://127.0.0.1:8765`, and nothing leaves it unless you explicitly
ask for a Gmail load or an AI analysis.

**Status: early.** The offline ledger, saved Gmail candidates and period views are
implemented, including supported deterministic mail-to-ledger processing, and tested
with synthetic data. Live Gmail has not been verified,
including a certificate-trust fix for a Google sign-in failure reported on the
installed app (see [Gmail limits](docs/gmail.md)). Card rewards are estimates from saved
terms ([rewards](docs/rewards.md)); bank-statement reconciliation is not built. Local
template learning remains bounded by explicit supported financial semantics.

## What it does

The sidebar has three steps. Each opens only saved local data unless you press a load
or analysis button.

1. **Connect Gmail**: sign in with Google in a window the app opens (read-only access),
   then press **Refresh**. It loads mail from the latest saved email's day through today
   (the current month for a new account) and processes it, and says when all recent
   emails are loaded. Nothing loads at sign-in or startup.
2. **Cards**: the card endings your alerts state, such as "DBS/POSB card ending NNNN".
   Pick which card each one is from a list; the choice is saved and can be corrected.
3. **Optimise spending**: the canonical transactions for a period: card purchases and
   outgoing PayLah!/PayNow transfers as separate spending, charts by card, account and
   day, and an estimated reward per chosen card where the saved terms are complete.
   Currencies and event kinds are never mixed or netted. An optional **Safe AI
   analysis** tab handles public card terms only.

**Periods.** Pick From and To dates in the calendar (inclusive, Singapore time), or use
This month, Last month and Last 3 months. A period is one day up to three calendar
months, and the server enforces that limit. See [periods and charts](docs/monthly.md).

**Storage.** The app automatically compresses eligible older detail after supported
writes. Nothing is deleted and nothing needs to be run. See
[storage](docs/storage.md) for the trigger, retention window and recovery.

## Set up and run

Developed and tested with Python 3.11.9 and uv 0.11.8 (the pinned setup).

```sh
uv sync --locked
uv run --locked spending-optimiser
```

Open **http://127.0.0.1:8765**. The database defaults to
`~/.local/share/spending-optimiser/ledger.sqlite3`; override it with
`--database /absolute/private/path/ledger.sqlite3` or `SPENDING_OPTIMISER_DB`.

**Try automatic processing offline** (no credentials, model or Internet after install):
run `uv run --locked python examples/automatic_preview.py --port 8897`, then press
Refresh. This starts with an empty ledger, not imported transactions.
Optional snapshot import/recovery and transaction records are under **Cards > Records &
recovery**.

**Use Gmail** by following [Gmail setup and use](docs/gmail.md): create a Desktop OAuth
client, put its ID and secret in the gitignored `.env` (see `.env.example`), then
choose **Connect Gmail**.

## Guides

| Guide | Covers |
| --- | --- |
| [Periods and charts](docs/monthly.md) | Date ranges and the exact three-month limit, loading mail, what the overview includes |
| [Gmail](docs/gmail.md) | Google Cloud setup, sign-in, the read-only adapter, privacy, live-verification status |
| [Automatic transactions](docs/automatic-transactions.md) | Supported format, acceptance/uncertainty, bounded saved-mail continuation, source history |
| [Cards and rewards](docs/rewards.md) | Choosing the product of each card, estimated rewards, known gaps |
| [Local ledger](docs/local-ledger.md) | Optional recovery/import walkthrough, corrections, Card evidence, persistence guarantees |
| [Storage](docs/storage.md) | Automatic compression, restoring older mail, recovery |
| [Safe AI analysis](docs/subscription-ai.md) | Optional ChatGPT connection for public terms |
| [Schemas](docs/schemas.md), [extraction](docs/extraction.md) | Data model and the fixture-tested extraction use case |
| [Project summary](summary.md) | Original brief, planned scope and current status |

## Try it safely with synthetic data

Each preview runs the real app against a disposable database and fake Google and model
services. Use an unused loopback port, never 8765. Ctrl-C deletes its data.

```sh
uv run --locked python examples/automatic_preview.py --port 8897 # mail-to-ledger, empty ledger
uv run --locked python examples/refresh_preview.py --port 8899   # Refresh, Cards and rewards
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
- Commit only code, documentation, synthetic/public fixtures and validated generic
  template assets. Keep real emails, private intermediate templates, account
  details, credentials, databases and session state out of Git.

## Current limits

- Rewards are estimates, only where saved terms are complete, and never bank-confirmed
  (see [rewards](docs/rewards.md)). No budgets or bank-statement reconciliation. Saved local emails now
  teach reusable templates, including supported narrative/HTML forms, but structure
  alone never proves financial semantics or account identity. Unresolved formats and
  missing facts remain excluded. See [supported limits](docs/automatic-transactions.md).
- Loading and processing are bounded per Refresh. See
  [discovery limits and saved-mail continuation](docs/automatic-transactions.md#ordinary-trigger-and-continuation).
- Private templates/evidence stay local. Only closed-schema, value-free generic forms
  may be reusable Git assets; private literals, source links and fingerprints are excluded.
- Unsupported/uncertain mail stays saved and excluded. Machine acceptance does
  not verify bank truth. Gmail search ending does not establish complete financial coverage.
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
