# SpendingOptimiser

## Original brief

I would like to work on a new project that tracks my spending such that i can maximise rewards gain. I want to be
able to support the cards that I use and future ones that may come in and also one time promotions that may be
happening for new-to-bank or new-to-cards.

Spending refers to:
- credit card spend
- paynow/paylah transactions (transfers)

Rewards refer to:
- miles
- cashback
- one time rewards for sign up

The rewards often include minimum spends in order to earn the rewards, and only transaction from certain merchants earn that high rate.
Min spend is often tracked per calendar month.
One time rewards for sign up are either directly from banks or moneySmart/singsaver

This app should show my monthly cashflow
- salary
- incoming transfers
- spend for each cards
- how much i can spend per day left in the calendar month

Transactions should mainly come from my gmail that i route the transactions through.

This should work as a service for now as an executable that runs and checks each and tallys the amount each time with a refresh button.

## Confirmed follow-up requirements

### Initial cards and transaction source

- Maybank Rewards XL.
- DBS yuu Credit Card Visa.
- Trust Freedom Card.
- Gmail is the transaction-notification source for all three.
- Card names are user-supplied; exact product variants and current terms still need research.
- Preserve rewards in their native units, including program points. Earning, redemption into miles/cashback, and monetary valuation are separate calculations.

### Reusable, chatbot-driven card onboarding

Adding a card should start with telling a chatbot its name, not manually entering its rulebook.

1. Resolve the issuer, country, and exact product/network variant; clarify ambiguous names.
2. Discover official product pages, card-specific T&Cs, general exclusions, reward-program/redemption terms, and relevant campaign documents.
3. Retrieve HTML/PDF documents and preserve source URLs, document versions, retrieval times, and effective dates.
4. Extract structured rules for earning, minimum spend, eligibility, caps, exclusions, rounding, date basis, and redemption.
5. Attach evidence and an explicit status to each extracted field. Search for additional evidence when information is missing; never guess unresolved conditions.
6. Validate against the deterministic engine and verified examples, then publish a versioned card profile under the activation policy.

The onboarding service should be reusable from a chatbot, local dashboard, CLI, or another integration. Public card-product rules must be separate from personal card instances, approval dates, statement cycles, and promotion enrollments.

### Automated adaptation to unknown transaction formats

Bank email format changes should trigger automated adaptation rather than immediately require handwritten parser changes.

1. Use deterministic, versioned templates for known email formats.
2. Detect structural changes, missing fields, and changed field-label meaning, even if an old parser still returns a value.
3. Use constrained AI extraction for unfamiliar formats, preserving evidence for each proposed transaction field.
4. Validate amounts, currencies, dates, account mapping, event type, and identity. Distinguish purchases from refunds, reversals, declines, OTPs, balances, and marketing.
5. Learn bounded declarative extraction templates, qualify them against independently verified fixtures and new examples, and promote passing versions.
6. Retain rollback and replay capability. Reprocessing must not duplicate transactions or overwrite manual corrections.

Unambiguous, validated records can be accepted automatically. Ambiguous or incomplete records remain visible for review. Do not invent facts absent from a notification, such as an MCC or posting date, and do not execute arbitrary AI-generated parser code.

### Daily card-news and policy maintenance

- Monitor https://milelion.com/blog/ once daily for relevant news.
- Its RSS feed was verified at https://milelion.com/feed/, with ETag and Last-Modified support. The inspected response held 10 posts, so catch-up must not assume the feed contains every missed article.
- Match tracked card names, aliases, issuers, and reward programs, including bank-wide changes.
- Use news to trigger official-source research. MileLion is an editorial source, not a replacement for governing issuer/program terms.
- Also recheck tracked official documents directly; not every rule change will appear in the news feed.
- Compare new evidence with existing profiles. Automatically publish supported, unambiguous, officially evidenced changes under the configured update policy; send conflicts or unsupported conditions for review.
- Preserve immutable historical rules and apply changes from their effective dates, not the article's publication or retrieval date.
- Never automatically enroll the user in campaigns or assume personal eligibility from news.
- Report updates, pending reviews, failures, last successful check, and next run. Distinguish "0 relevant updates" from a failed or incomplete check.
- Use configurable daily scheduling in Asia/Singapore, idempotent jobs, and catch-up after downtime. A stopped executable or powered-off machine cannot perform unattended checks without a suitable scheduler/host.

## Proposed direction and implemented local subset

- Implemented: one local executable/service with a server-rendered browser ledger, local-data refresh, schema-document import, and auditable manual review/correction. See [local application setup and limits](docs/local-ledger.md).
- Modular monolith: domain calculations, application use cases, infrastructure adapters, web UI, and CLI. Reusable service boundaries do not initially require separate deployments.
- Implemented stack: Python, FastAPI, Pydantic, SQLite through the standard library, and Jinja2 forms. A bounded, fixture-tested notification extraction use case is also implemented. SQLAlchemy/Alembic and HTMX remain optional architectural recommendations, not installed dependencies.
- Deterministic reward and budget calculations consume validated, versioned data. AI researches documents and proposes extractions; it is not the financial source of truth.
- Store source evidence and audit history. Monthly totals are rebuildable projections, not the only retained records.
- Use durable job state for Gmail refresh, adaptive extraction, onboarding, and daily news/term checks.
- Detailed architecture artifact: [.lavish/project-architecture.html](.lavish/project-architecture.html).

## Proposed correctness and privacy defaults

- Store money in currency-appropriate integer minor units; use Decimal arithmetic for FX and rounding rather than binary floating point.
- Separate budget spending, actual account cash movement, and reward-eligible spend.
- Do not count card repayments as a second expense or own-account transfers as income.
- Separate minimum-spend eligibility from bonus eligibility, and estimated rewards from rewards actually received.
- Account for calendar/statement periods, transaction/posting dates, refunds, caps, exclusions, and rounding.
- Treat Gmail as evidence, not necessarily a complete bank ledger; support corrections and eventual statement/CSV reconciliation.
- Keep public-document research separate from private transaction extraction. The application's [safe AI privacy boundary](docs/subscription-ai.md#privacy-and-enforcement) governs eligible model inputs.
- Use read-only Gmail OAuth, OS credential storage, loopback-only serving by default, redacted logs, backups, and configurable source retention.
- Treat retrieved web/email content as untrusted data, never executable agent instructions.
- Do not commit real emails, account details, OAuth tokens, local databases, or session state.

## Current status and next steps

The foundational schema milestone is implemented as the importable `spending_optimiser` Python package with Pydantic validation, a pinned uv development/test setup, synthetic fixtures, automated tests and CI. It represents transaction/source/revision identities, purchase/refund/reversal/income/transfer events, personal accounts/cards separately from public products, per-field uncertainty and evidence, native rewards separately from redemption/valuation, and historical rule/evidence revisions. See [schema usage and limitations](docs/schemas.md).

A [constrained extraction use case](docs/extraction.md) now accepts notification data independently of Gmail, uses one injected model callable with a bounded span-selection contract, and returns existing pending-review transaction proposals. Deterministic validation covers a small labelled fixture grammar, not qualified real-bank formats or general-language model accuracy. Synthetic fixtures and fake clients require no live provider or privacy choice; private content is blocked from the callable without explicit per-call opt-in.

A runnable offline local service now imports validated schema documents atomically into SQLite, shows the persisted event ledger and evidence, and records immutable review/correction revisions. Duplicate reimports are harmless; conflicts fail explicitly, and imports cannot overwrite local manual revisions. The bundled synthetic example exercises the whole path without credentials or an external model. See [setup, synthetic walkthrough, guarantees and limitations](docs/local-ledger.md).

The read-only Gmail adapter is now wired to Google Desktop OAuth sign-in and explicit bounded, resumable Singapore-time calendar-month loading in the local browser app. Sign-in through a window opened by the Connect button, `.env` credential storage (plaintext, owner-only), refresh, account identity/isolation and disconnect are implemented; candidates are saved as readable local SQLite mail data per Google account and never become ledger transactions. A deterministic [monthly dashboard](docs/monthly.md) summarizes accepted ledger events per card/channel. Production library exchange/refresh and retrieval are synthetic end-to-end tested. Live-account consent/loading still require the user's Cloud configuration and explicit Google consent and have not been verified. See [Gmail setup, usage, privacy and limits](docs/gmail.md).

See the [subscription-backed safe-reference AI workflow](docs/subscription-ai.md) for supported providers, safe inputs, proposal review and verification limits. Rewards, qualified mail-to-ledger ingestion, scheduling and a researched card-rule catalog remain separate milestones. The event ledger is not complete bank reconciliation or a rule-publication gate.

Suggested build order:

1. Implemented: foundational transaction, card-rule, source-evidence, and versioning schemas. Validate and refine these against independently verified examples as later capabilities are built.
2. Implemented local subset: schema-document ingestion/review, persistence, and refresh UI. Later: qualify one bank's Gmail-to-ledger adapter, adaptive extraction, and basic monthly budget.
3. Research the three current cards, verify the rule schema against their terms, and add reusable chatbot onboarding.
4. Add daily MileLion/official-term monitoring and policy-gated rule publication.
5. Expand reconciliation, promotion tracking, reminders, and planned-spend recommendations.

Later integration milestones may need sanitized notification examples, personal approval/enrollment and campaign details, budget/savings assumptions, and the transaction-extraction model/privacy choice. None is required for the schema, synthetic extraction, or offline synthetic-data milestones.
