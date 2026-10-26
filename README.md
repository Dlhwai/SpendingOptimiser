# SpendingOptimiser

A planned personal spending tracker for maximizing credit-card rewards while keeping monthly cash flow and budgets visible.

**Status: deterministic desktop monthly views, account-scoped saved Gmail candidates, and the local import/review ledger are implemented. Monthly loading is explicit, bounded and resumable. All page views read saved data only. OAuth/retrieval are synthetic-tested; live Gmail still needs your setup and consent. An optional, restricted subscription-backed Codex workflow now analyses only saved official issuer terms captured by the app on request, or allowlisted placeholder structures. Live AI accounts and qualified bank-email extraction have not been verified.**

Start with the [monthly workflow, inclusion rules and safe preview](docs/monthly.md).
[Storage](docs/storage.md) keeps historical financial views readable,
retains the current Singapore month plus two preceding mail months uncompressed,
and losslessly compresses eligible older detail only above 1,000,000,000 bytes.
This is not deletion, encryption or a hard storage cap.

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

An importable `spending_optimiser` package provides typed Pydantic models for transaction revisions, account/card identities, uncertain card rules, source evidence, and self-contained JSON snapshots. It includes synthetic fixtures, round-trip and validation tests, a pinned uv setup, and CI. A local FastAPI browser service now imports these validated snapshots transactionally into SQLite, displays a persisted event ledger, and preserves auditable review/correction revisions. It does not calculate rewards, automatically accept mail-derived transactions, or reconcile bank statements. Accepted format layouts can produce local, unsaved transaction proposals from saved mail without sending mail to models. A separate [constrained extraction use case](docs/extraction.md) accepts notification data and an injected model callable, validates bounded evidence spans, and returns pending-review proposals using synthetic fixtures. That extractor has no provider client or qualified real-bank parser; the separate safe-reference AI workflow never receives mail. A separate read-only Gmail adapter lists and retrieves private notification candidates, with pagination, MIME normalization, stable observation identities, and redacted errors. It creates no accepted transactions. The three exact card variants remain unresolved. Public-term analysis yields typed, uncertain minimum-spend proposals, not complete or qualified card terms.

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

Open **http://127.0.0.1:8765**. The sidebar has three steps:

1. **Connect Gmail**: sign in through a Google window opened from the app, then
   explicitly Load / Continue / Refresh a calendar month. Sign-in never loads mail.
2. **Inspect saved data**: saved mail, transactions, review queue, card evidence,
   data import and storage as tabs.
3. **Understand this month**: canonical transactions and purchase charts by explicit
   card/account/avenue and event date, plus the optional Safe AI analysis tab.
   Separate currencies and unsigned event kinds are never netted.

For an offline walkthrough, choose step 2 > **Import data**, click **Import bundled
synthetic example**, then open **Review queue** to inspect evidence and correct/review
the purchase. No credentials, real financial data, model, or Internet access are needed
after dependency installation. Ledger refresh rereads the local database.

Data defaults to `~/.local/share/spending-optimiser/ledger.sqlite3`. Override with
`--database /absolute/private/path/ledger.sqlite3` or `SPENDING_OPTIMISER_DB`.
See [local setup, full synthetic walkthrough, storage/review guarantees, and limits](docs/local-ledger.md).

## Gmail adapter

See [exact Google Cloud/client setup, connect/load/disconnect, privacy and limits](docs/gmail.md). Gmail sign-in uses Google's Desktop authorization-code flow with PKCE and a loopback callback, started only by the Connect button. The Google client and saved authorization live in the gitignored `.env` (plaintext, owner-only, not encryption; see `.env.example`). Save a Singapore-time calendar month (UTC+08:00) in bounded pages only when you request it; Continue until the query is exhausted. Undecodable messages are kept as unresolved IDs with a targeted retry, so later pages still load. Candidates and checkpoints persist by stable Google account identity, separate from ledger facts. Incomplete retrieval is labeled; exhaustion is not proof of financial coverage. Disconnect hides saved mail and forgets authorization, not the local cache. Saved mail is readable local SQLite with no app-level encryption and no deletion action.
Run the synthetic example without credentials or network access:

```sh
uv run --locked python examples/gmail_synthetic.py
```

Live Gmail and actual bank-notification compatibility have **not** been verified.
The app implements consent, refresh and `.env` credential storage; the standalone
adapter still does not read/change stores. Follow the setup guide to put your
Desktop client ID and secret in `.env` (or import the JSON with `--configure-google`),
then start the app and choose **Connect Gmail**. No credentials are needed for
offline imports.

## Safe synthetic monthly desktop preview

```sh
uv run --locked python examples/monthly_preview.py --port 8877 --state partial
```

Open **http://127.0.0.1:8877/?month=2025-02** for fictional corrected ledger facts,
separate SGD/JPY/KWD/USD charts and an incomplete mail scan. Continue loading in
Connect Gmail to exhaust it. Use `--state full` or `--state empty` for other states.
The seeded ledger is **not AI-extracted mail**. Every dependency is fake; no real
`.env`, credentials, real mail, default ledger or external model is touched. Ctrl-C removes
the disposable database. [Exact fixture counts and limits](docs/monthly.md).

## Subscription-backed safe AI

Choose **Safe AI analysis** (step 3) and **Connect ChatGPT**; the same kind of sign-in
window connects an eligible ChatGPT session through a pinned, restricted official
Codex client (its tokens stay in the client's own keyring, not `.env`). Capture official issuer terms on request or save
an allowlisted placeholder structure, inspect the exact safe input, explicitly request analysis,
and review a versioned proposal. Accepted format layouts parse loaded mail locally
into unsaved, review-pending proposals; nothing changes ledger facts automatically.
Default app use remains disconnected.
Claude and Gemini are honestly unavailable; no API-key billing fallback is used.
See [supported client setup, provider evidence, privacy boundary and limits](docs/subscription-ai.md).

## Safe synthetic UI preview

For the separate first-run Google-setup/offline-review walkthrough:

```sh
uv run --locked python examples/ui_preview.py --port 8876
```

Open **http://127.0.0.1:8876**. This runs the actual UI against a fresh disposable
database, a temporary `.env`, local synthetic consent pages, a fake Gmail transport,
a fake AI subscription and a fake model. It never reads your normal ledger, `.env` or
OS credentials and makes no Google or model-provider requests. Every screen is labeled
synthetic. Use `--unconfigured` to inspect the missing-client state, `--ai-signed-out`
to walk through the ChatGPT sign-in window, or `--populated` for the bundled ledger
example. Connect, approve synthetic consent in the sign-in window, choose February
2025, then Load month for four fictional candidates, including long text, and
inspect them in step 2. Ctrl-C removes the temporary database and `.env`. Port 8765 is
explicitly refused.

## Technical direction

One Python service uses FastAPI, SQLite, and server-rendered Jinja2 forms, without
JavaScript dependencies or a frontend build. The bounded extraction use case and
read-only Gmail adapter remain separate modules; Gmail authorization,
candidate retrieval and a separate safe-snapshot AI reference workflow are wired
into the service. Mail is never sent to either AI path. Deterministic monthly
projections use saved canonical ledger facts independently of ingestion and AI.

## Privacy

Commit only source code, documentation, and sanitized fixtures. Keep actual financial emails, account details, OAuth credentials, databases, and local review/session state out of Git. Private transaction content is never eligible for external models. Only app-captured official public terms or allowlisted placeholder structures may be sent, with explicit per-analysis action; confirmation alone cannot authorize excluded private data.

## Next step

The local schema-document ingestion/review path with persistence is implemented and tested. Next, qualify a real notification ingestion adapter against sanitized source examples. Account-backed AI verification, real-bank format qualification, adaptive extraction, signed financial reconciliation, rewards and complete card-term research remain separate milestones. The current desktop monthly workflow deliberately does not guess those meanings.
