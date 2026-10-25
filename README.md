# SpendingOptimiser

A planned personal spending tracker for maximizing credit-card rewards while keeping monthly cash flow and budgets visible.

**Status: deterministic desktop monthly views, account-scoped saved Gmail candidates, and the local import/review ledger are implemented. Monthly loading is explicit, bounded and resumable. All page views read saved data only. OAuth/retrieval are synthetic-tested; live Gmail still needs your setup and consent. No live AI provider or qualified bank-email extraction is integrated.**

Start with the [monthly workflow, inclusion rules and safe preview](docs/monthly.md).

## Project context

- [Current summary](summary.md): original brief, confirmed follow-up requirements, proposed safeguards, open inputs, and build order. Start here.
- [Visual architecture proposal](.lavish/project-architecture.html): component responsibilities, card onboarding, adaptive transaction extraction, and daily news/policy maintenance. Open the HTML file in a browser; its Tailwind/DaisyUI styling uses external CDNs.

## Original roadmap (not current implemented scope)

- Maybank Rewards XL, DBS yuu Credit Card Visa, and Trust Freedom Card.
- Gmail-based transaction ingestion with automated adaptation to changing bank email formats.
- Chatbot-driven discovery of card terms and evidence-backed, versioned reward rules.
- Daily [MileLion](https://milelion.com/blog/) monitoring, followed by official-term verification before financial rules change.
- Monthly spending, cash flow, reward/promotion progress, and remaining daily allowance.
- A local executable/service with a browser dashboard and manual transaction refresh.

## Implemented milestone

An importable `spending_optimiser` package provides typed Pydantic models for transaction revisions, account/card identities, uncertain card rules, source evidence, and self-contained JSON snapshots. It includes synthetic fixtures, round-trip and validation tests, a pinned uv setup, and CI. A local FastAPI browser service now imports these validated snapshots transactionally into SQLite, displays a persisted event ledger, and preserves auditable review/correction revisions. It does not calculate rewards, extract financial facts from emails, or reconcile bank statements. A separate [constrained extraction use case](docs/extraction.md) accepts notification data and an injected model callable, validates bounded evidence spans, and returns pending-review proposals using synthetic fixtures. No provider client or qualified real-bank parser is implemented. A separate read-only Gmail adapter lists and retrieves private notification candidates, with pagination, MIME normalization, stable observation identities, and redacted errors. It creates no accepted transactions. The three card names remain unresolved; no real terms have been researched.

See [schema usage and limits](docs/schemas.md). Quick start (Python 3.11.9, uv 0.11.8):

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

## Run the local application

After `uv sync --locked`, start the loopback-only service:

```sh
uv run --locked spending-optimiser
```

Open **http://127.0.0.1:8765** to choose a calendar month, inspect canonical
transactions and view purchase charts by explicit card/account/avenue and event
date. Separate currencies and unsigned event kinds are never netted. **Mail inbox**
guides Gmail setup, month selection and explicit Load / Continue / Refresh. For an offline walkthrough, choose **Import data**,
click **Import bundled synthetic example**, then open **Review queue** to inspect
evidence and correct/review the purchase. No credentials,
real financial data, model, or Internet access are needed after dependency installation.
Ledger refresh rereads the local database. Gmail has separate Connect and Load/Refresh mail actions; sign-in never loads mail automatically.

Data defaults to `~/.local/share/spending-optimiser/ledger.sqlite3`. Override with
`--database /absolute/private/path/ledger.sqlite3` or `SPENDING_OPTIMISER_DB`.
See [local setup, full synthetic walkthrough, storage/review guarantees, and limits](docs/local-ledger.md).

## Gmail adapter

See [exact Google Cloud/client setup, connect/load/disconnect, privacy and limits](docs/gmail.md). Gmail sign-in uses Google's Desktop authorization-code flow with PKCE, native OS credential storage, and a loopback callback. Save a Singapore-time calendar month (UTC+08:00) in bounded pages only when you request it; Continue until the query is exhausted. Undecodable messages are kept as unresolved IDs with a targeted retry, so later pages still load. Candidates and checkpoints persist by stable Google account identity, separate from ledger facts. Incomplete retrieval is labeled; exhaustion is not proof of financial coverage. Disconnect hides saved mail and forgets authorization, not the local cache. Saved mail is readable local SQLite with no app-level encryption and no deletion action.
Run the synthetic example without credentials or network access:

```sh
uv run --locked python examples/gmail_synthetic.py
```

Live Gmail and actual bank-notification compatibility have **not** been verified.
The app implements consent, refresh and native-only credential storage; the standalone
adapter still does not read/change stores. Follow the setup guide to import your
Desktop client JSON privately with `--configure-google`, then start the app and
choose **Mail inbox > Connect Gmail**. No credentials are needed for offline imports.

## Safe synthetic monthly desktop preview

```sh
uv run --locked python examples/monthly_preview.py --port 8877 --state partial
```

Open **http://127.0.0.1:8877/?month=2025-02** for fictional corrected ledger facts,
separate SGD/JPY/KWD/USD charts and an incomplete mail scan. Continue loading in
Mail inbox to exhaust it. Use `--state full` or `--state empty` for other states.
The seeded ledger is **not AI-extracted mail**. Every dependency is fake; no native
credentials, real mail, default ledger or external model is touched. Ctrl-C removes
the disposable database. [Exact fixture counts and limits](docs/monthly.md).

For the separate first-run Google-setup/offline-review walkthrough:

```sh
uv run --locked python examples/ui_preview.py --port 8876
```

Open **http://127.0.0.1:8876**. This runs the actual UI against a fresh disposable
database, in-memory fake credential store, local synthetic consent page and fake
Gmail transport. It never reads your normal ledger or OS credentials and makes no
Google requests. Every screen is labeled synthetic. Use `--unconfigured` to inspect
the setup-required state or `--populated` for the bundled ledger example. Connect,
approve synthetic consent, choose February 2025, then Load month for four fictional
candidates, including long text. Ctrl-C removes the temporary database. Port 8765 is explicitly refused.

## Technical direction

One Python service uses FastAPI, SQLite, and server-rendered Jinja2 forms, without
JavaScript dependencies or a frontend build. The bounded extraction use case and
read-only Gmail adapter remain separate modules; only Gmail authorization and
candidate retrieval are wired into the service. Mail is not sent to the extractor. Deterministic monthly projections use saved canonical ledger facts independently
of ingestion and AI. Provider integration is deferred pending a decision about
whether selected saved content may leave the machine or processing must be local.

## Privacy

Commit only source code, documentation, and sanitized fixtures. Keep actual financial emails, account details, OAuth credentials, databases, and local review/session state out of Git. Sending transaction content to external models requires explicit opt-in.

## Next step

The local schema-document ingestion/review path with persistence is implemented and tested. Next, qualify a real notification ingestion adapter against sanitized source examples. AI/provider integration, adaptive extraction, signed financial reconciliation, rewards and exact card-term research remain separate milestones. The current desktop monthly workflow deliberately does not guess those meanings.
