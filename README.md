# SpendingOptimiser

A planned personal spending tracker for maximizing credit-card rewards while keeping monthly cash flow and budgets visible.

**Status: foundational Python schemas and constrained, fixture-tested extraction proposals implemented. There is no runnable application or live model integration yet.**

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

An importable `spending_optimiser` package provides typed Pydantic models for transaction revisions, account/card identities, uncertain card rules, source evidence, and self-contained JSON snapshots. It includes synthetic fixtures, round-trip and validation tests, a pinned uv setup, and CI. It does not calculate rewards, fetch emails, or implement a bank ledger. A separate [constrained extraction use case](docs/extraction.md) accepts notification data and an injected model callable, validates bounded evidence spans, and returns pending-review proposals using synthetic fixtures. No provider client or qualified real-bank parser is implemented. The three card names remain unresolved; no real terms have been researched.

See [schema usage and limits](docs/schemas.md). Quick start (Python 3.11.9, uv 0.11.8):

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

## Proposed technical direction

A modular Python application with FastAPI, SQLite, and a lightweight Jinja2/HTMX dashboard. Keep deterministic financial calculations independent of Gmail, model providers, persistence, and UI clients. The Pydantic domain package and bounded extraction use case are implemented; the service, storage, and dashboard stack remains proposed.

## Privacy

Commit only source code, documentation, and sanitized fixtures. Keep actual financial emails, account details, OAuth credentials, databases, and local review/session state out of Git. Sending transaction content to external models requires explicit opt-in.

## Next step

Build and test one complete ingestion/review path with persistence, using the foundational schemas. Live integrations and exact card-term research remain separate milestones.
