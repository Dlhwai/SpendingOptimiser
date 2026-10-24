# SpendingOptimiser

A planned personal spending tracker for maximizing credit-card rewards while keeping monthly cash flow and budgets visible.

**Status: foundational schemas, constrained fixture-tested extraction proposals, an offline local import/persistence/review application, and an offline-tested read-only Gmail candidate adapter are implemented. There is no live model integration yet. Live Gmail access requires authorization.**

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

An importable `spending_optimiser` package provides typed Pydantic models for transaction revisions, account/card identities, uncertain card rules, source evidence, and self-contained JSON snapshots. It includes synthetic fixtures, round-trip and validation tests, a pinned uv setup, and CI. A local FastAPI browser service now imports these validated snapshots transactionally into SQLite, displays a persisted event ledger, and preserves auditable review/correction revisions. It does not calculate rewards, ingest emails through the service, or reconcile bank statements. A separate [constrained extraction use case](docs/extraction.md) accepts notification data and an injected model callable, validates bounded evidence spans, and returns pending-review proposals using synthetic fixtures. No provider client or qualified real-bank parser is implemented. A separate read-only Gmail adapter lists and retrieves private notification candidates, with pagination, MIME normalization, stable observation identities, and redacted errors. It creates no accepted transactions. The three card names remain unresolved; no real terms have been researched.

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

Open **http://127.0.0.1:8765**, click **Import bundled synthetic example**, and open
**Review queue** to inspect evidence and correct/review the purchase. No credentials,
real financial data, model, or Internet access are needed after dependency installation.
Refresh rereads the local database, not Gmail.

Data defaults to `~/.local/share/spending-optimiser/ledger.sqlite3`. Override with
`--database /absolute/private/path/ledger.sqlite3` or `SPENDING_OPTIMISER_DB`.
See [local setup, full synthetic walkthrough, storage/review guarantees, and limits](docs/local-ledger.md).

## Gmail adapter

See [Gmail usage, privacy, limits and future authorized OAuth setup](docs/gmail.md).
Run the synthetic example without credentials or network access:

```sh
uv run --locked python examples/gmail_synthetic.py
```

Live Gmail and actual bank-notification compatibility have **not** been verified.
OAuth consent, refresh and OS credential storage remain host responsibilities;
this adapter does not read or change credential stores.

## Technical direction

One Python service uses FastAPI, SQLite, and server-rendered Jinja2 forms, without
JavaScript dependencies or a frontend build. The bounded extraction use case and
read-only Gmail adapter are implemented separately and not yet wired into the
service. Deterministic financial calculations remain a later capability,
independent of ingestion, model providers, persistence, and UI clients.

## Privacy

Commit only source code, documentation, and sanitized fixtures. Keep actual financial emails, account details, OAuth credentials, databases, and local review/session state out of Git. Sending transaction content to external models requires explicit opt-in.

## Next step

The local schema-document ingestion/review path with persistence is implemented and tested. Next, qualify a real notification ingestion adapter against sanitized source examples. Live integrations, adaptive extraction, financial calculations, and exact card-term research remain separate milestones.
