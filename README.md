# SpendingOptimiser

A planned personal spending tracker for maximizing credit-card rewards while keeping monthly cash flow and budgets visible.

**Status: schemas, fixture-tested extraction proposals, local import/persistence/review, and Google sign-in with explicit read-only Gmail loading are implemented. OAuth and retrieval are synthetic end-to-end tested; a live account still requires your Cloud configuration and browser consent. No live model integration or qualified bank-email extraction is implemented.**

## Project context

- [Current summary](summary.md): original brief, confirmed follow-up requirements, proposed safeguards, open inputs, and build order. Start here.
- [Visual architecture proposal](.lavish/project-architecture.html): component responsibilities, card onboarding, adaptive transaction extraction, and daily news/policy maintenance. Open the HTML file in a browser; its Tailwind/DaisyUI styling uses external CDNs.

## Initial scope

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

Open **http://127.0.0.1:8765** to see your workspace overview. **Mail inbox** guides
Gmail setup and explicit loading. For an offline walkthrough, choose **Import data**,
click **Import bundled synthetic example**, then open **Review queue** to inspect
evidence and correct/review the purchase. No credentials,
real financial data, model, or Internet access are needed after dependency installation.
Ledger refresh rereads the local database. Gmail has separate Connect and Load/Refresh mail actions; sign-in never loads mail automatically.

Data defaults to `~/.local/share/spending-optimiser/ledger.sqlite3`. Override with
`--database /absolute/private/path/ledger.sqlite3` or `SPENDING_OPTIMISER_DB`.
See [local setup, full synthetic walkthrough, storage/review guarantees, and limits](docs/local-ledger.md).

## Gmail adapter

See [exact Google Cloud/client setup, connect/load/disconnect, privacy and limits](docs/gmail.md). Gmail sign-in uses Google's Desktop authorization-code flow with PKCE, native OS credential storage, and a loopback callback. Load a bounded recent-message batch only when you request it; candidates remain separate from validated ledger events.
Run the synthetic example without credentials or network access:

```sh
uv run --locked python examples/gmail_synthetic.py
```

Live Gmail and actual bank-notification compatibility have **not** been verified.
The app implements consent, refresh and native-only credential storage; the standalone
adapter still does not read/change stores. Follow the setup guide to import your
Desktop client JSON privately with `--configure-google`, then start the app and
choose **Mail inbox > Connect Gmail**. No credentials are needed for offline imports.

## Safe synthetic UI preview

```sh
uv run --locked python examples/ui_preview.py --port 8876
```

Open **http://127.0.0.1:8876**. This runs the actual UI against a fresh disposable
database, in-memory fake credential store, local synthetic consent page and fake
Gmail transport. It never reads your normal ledger or OS credentials and makes no
Google requests. Every screen is labeled synthetic. Use `--unconfigured` to inspect
the setup-required state or `--populated` for the bundled ledger example. Connect,
approve synthetic consent, then Load mail for four fictional candidates, including
long text. Ctrl-C removes the temporary database. Port 8765 is explicitly refused.

## Technical direction

One Python service uses FastAPI, SQLite, and server-rendered Jinja2 forms, without
JavaScript dependencies or a frontend build. The bounded extraction use case and
read-only Gmail adapter remain separate modules; only Gmail authorization and
candidate retrieval are wired into the service. Mail is not sent to the extractor. Deterministic financial calculations remain a later capability,
independent of ingestion, model providers, persistence, and UI clients.

## Privacy

Commit only source code, documentation, and sanitized fixtures. Keep actual financial emails, account details, OAuth credentials, databases, and local review/session state out of Git. Sending transaction content to external models requires explicit opt-in.

## Next step

The local schema-document ingestion/review path with persistence is implemented and tested. Next, qualify a real notification ingestion adapter against sanitized source examples. Live integrations, adaptive extraction, financial calculations, and exact card-term research remain separate milestones.
