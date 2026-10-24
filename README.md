# SpendingOptimiser

A planned personal spending tracker for maximizing credit-card rewards while keeping monthly cash flow and budgets visible.

**Status: requirements and architecture only. There is no runnable application yet.**

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

## Proposed technical direction

A modular Python application with FastAPI, SQLite, and a lightweight Jinja2/HTMX dashboard. Keep deterministic financial calculations independent of Gmail, model providers, persistence, and UI clients. The stack is proposed, not scaffolded or installed.

## Privacy

Commit only source code, documentation, and sanitized fixtures. Keep actual financial emails, account details, OAuth credentials, databases, and local review/session state out of Git. Sending transaction content to external models requires explicit opt-in.

## Next step

Define the transaction, card-rule, source-evidence, and versioning schemas, then build and test one complete Gmail-to-ledger path.
