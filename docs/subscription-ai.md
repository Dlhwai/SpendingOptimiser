# Safe, subscription-backed desktop AI

This follow-up adds a separate **Safe AI analysis** page (a tab of step 3,
**Optimise spending**). Ordinary Gmail loading,
transaction lookups and financial calculations do not invoke a model. The default
app is disconnected and needs no AI client. For saved-data charts, see the
[deterministic monthly workflow](monthly.md).

## What is supported

- A restricted, unmodified official **Codex CLI 0.164.0**, on **macOS Apple Silicon**,
  with official ChatGPT browser consent and mandatory native Keychain storage.
  This milestone qualifies unmanaged clients only: requirements beyond the app's
  own ChatGPT-only login pin cause refusal, not an attempt to override MDM,
  local-admin or cloud policy.
  The native binary checksum is pinned to the version used for enforcement proof.
- **Official public terms importer.** A closed collection of independently checked
  official issuer origins (DBS `www.dbs.com.sg`/`dbs.com.sg`, Maybank Singapore
  `www.maybank2u.com.sg`, Trust Bank `trustbank.sg`), each with official start pages.
  On an explicit **Find and capture official terms** request, the app follows
  same-issuer links best-first from those pages, making at most 8 HTTPS requests
  (redirects included) under a shared 45-second socket-I/O deadline, and
  captures pages whose own text contains your product words and "terms"/"conditions".
  Your words are matched locally and never sent as a search query. Nothing runs on
  page views or timers. New or changed documents need no code change; a newer capture
  of the same URL makes older snapshots stale.
- **Retrieval boundary.** HTTPS only, port 443, no credentials in URLs, every hop
  (including redirects, at most 3) must stay on the issuer's listed hosts. Hostnames
  must resolve only to public addresses and the connection is pinned to a checked
  address. TLS certificate chain and hostname are verified against the bundled
  `certifi` CA bundle, not the Python installation's trust store (empty on a
  python.org interpreter whose certificate step was never run). No proxy, cookies,
  authorization or session headers. Bodies are read in chunks capped at 2 MiB. Every
  connect, TLS handshake, send and socket read (status line, headers, chunk sizes and
  body) waits at most 15 s and never past the import's 45-second deadline. System
  hostname resolution uses the OS resolver's timeout, not an interruptible app
  deadline, so a stalled lookup can exceed 45 seconds; after it returns, an expired
  budget prevents connecting. There is no background fetch left running after an
  import returns. Captures made before the deadline are kept. Only
  `text/html`/`text/plain` are read, scripts/styles are dropped and extracted text
  over 24,000 characters is refused, as is premature EOF in declared-length or chunked
  HTTP bodies. PDFs, other sites, failed, malformed or oversized documents and searches
  that hit the request or time budget are shown as unavailable.
- **Authority.** Only the importer writes terms snapshots. Each stores the issuer,
  final URL, retrieval time, content SHA-256 and exact extracted text; the preview
  fingerprint covers that saved body. Imported ledger `SourceEvidence` and pasted
  text cannot create or replace an outbound terms snapshot.
- Public terms analysis: the model returns one stated minimum-spend threshold (ISO
  currency, integer minor units) and an exact quote. The app requires the quote to be
  contiguous saved text and a complete amount, read from the full saved source, whose
  span lies inside the quote. Partial matches such as `S$800` inside `US$800`,
  `S$800.50` or `S$8000` are refused. The result is a proposed
  `RuleRevision`/`MinimumSpend` citing the capture with character offsets. Period
  basis, timezone, earning rates, eligibility, applicability, caps and effective dates
  remain unknown. Uncertain answers stay null.
- Format structures: one line of distinct `<ROLE>` placeholders, words from a fixed
  ASCII notification vocabulary and allowlisted punctuation. Unrecognized Unicode
  characters, controls, names, digits, `@` and raw message text are refused.
  The model maps each placeholder to a fixed role (currency, amount, merchant,
  date_iso, date_dmy, card_suffix), never Python, shell or regex. The app requires every
  placeholder mapped exactly once, then round-trips fictional values through its fixed
  role patterns.
- Explicit review with immutable accept/reject decisions and accepted-version
  ancestry. Accepted terms are a versioned reference. The current accepted format
  layouts are applied locally to loaded Gmail candidates: a single unambiguous,
  non-zero matching line becomes an unsaved, review-pending `Transaction` proposal on
  the Mail page. Zero or invalid matches are skipped. No model is called, nothing is
  written to the ledger and no rewards are calculated.

Snapshots superseded by a newer capture are listed as stale, and rows the current
validator cannot read are listed as unsupported; neither can be previewed or analysed.
There is no web search, general crawler, terms monitoring, reward optimizer or
private-history analysis. Many issuers publish card terms only as PDFs, which are
currently unavailable rather than partially read.

## Local setup

Do not reuse another tool's login cache, extract tokens, or paste an API key.
Install the pinned official package **locally**, not globally:

```sh
npm install --prefix "$HOME/.local/share/spending-optimiser/codex-client" @openai/codex@0.164.0
```

Tell the app where the official native executable is, either in the gitignored `.env`
(see `.env.example`) or with a flag:

```sh
SPENDING_CODEX_CLIENT='/Users/you/.local/share/spending-optimiser/codex-client/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex'
uv run --locked spending-optimiser                      # reads SPENDING_CODEX_CLIENT
uv run --locked spending-optimiser --codex-client /absolute/path/to/codex   # overrides it
```

If using a non-default ledger, include the same `--database /absolute/private/path`
on each start. The official native executable, not the npm JavaScript launcher or
a shell wrapper, is required. The app checks SHA-256
`d8990e3ee7acfbc26f91265b12a1a83f0fee40d0e0f8f1f40a37d4ec21f499cf`.
Other versions, platforms and binaries fail unavailable rather than silently using
weaker controls. Do not upgrade the client without requalifying enforcement.
The app reads only this one path from `.env` at startup; it never exports `.env`
values to the official client, whose environment is a fixed allowlist.

1. Open **Safe AI analysis** from step 3.
2. Choose **Connect ChatGPT**. This opens a sign-in window exactly like Gmail's:
   the app starts the official client, which creates a private, database-specific
   client home beside the database and refuses unsafe effective configuration. No
   default Codex/worker session or ambient API/proxy variables are reused. Sign in
   and consent only on the official ChatGPT page; the client's own callback and token
   exchange complete it, so the sign-in window is closed by you afterwards. The app
   page asks its own status endpoint (a local account lookup, never a model call)
   and shows **ChatGPT connected** only once the client reports an eligible
   subscription account. If the browser blocks the window, choose **Continue in this
   window**; **Cancel sign-in** cancels the client's login, and an attempt expires
   after 10 minutes. A denied or failed login is reported in the app. Without
   JavaScript, **Check sign-in** performs the same lookup on request. An eligible
   cached subscription session restores directly on the Connect POST.
3. Capture official terms (or save a placeholder structure), inspect its complete
   application-supplied input,
   check the consent box and select **Request analysis now**. Nothing is sent by
   Save, preview, a page GET, Gmail loading, sign-in, status polling or an ordinary
   ledger lookup.
4. Review the structured result, evidence and unknowns. Accept or reject it.
   Accepted format layouts only create unsaved, review-pending Mail proposals.

The saved-session label is not a live entitlement guarantee. This project has not
performed account-backed live verification. The selected model must be available
to the signed-in account; provider/model/limit failures are reported, not routed to
an API account. A timed-out request may already have consumed subscription allowance;
it is never retried silently.

Restart clears the app connection, not the native authorization or saved public
reference history. Connect explicitly to restore it. Disconnect/forget deletes
this client's isolated native authorization and clears the app connection; if
native deletion fails, unlock Keychain, reconnect and retry. Provider-wide consent
revocation remains in the provider's account settings. Other databases' provider
sessions and proposal histories are separate. A changed account is refused until
explicit reconnection; proposals cannot be accepted under a different account.

### Where the ChatGPT authorization is stored

Gmail's client and refresh token live in the gitignored `.env` (see
[gmail.md](gmail.md)). **ChatGPT's do not, by design of the supported route.** The
official client persists its own OAuth tokens only in its mandatory native keyring
(`cli_auth_credentials_store = "keyring"`, keyed by the isolated `CODEX_HOME`), and
it has no setting that reads or writes tokens in an application's `.env`. Putting
them there would mean this app reading the client's tokens or handing it tokens
through `account/login/start` with `type: "chatgptAuthTokens"`, which the 0.164.0
protocol schema marks "[UNSTABLE] FOR OPENAI INTERNAL USE ONLY - DO NOT USE" and
which would require this app to hold access tokens and answer the client's token
refresh requests. This app refuses every client-initiated callback, so it does not
implement that bridge. Only non-secret app configuration (`SPENDING_CODEX_CLIENT`)
uses `.env`. The client's alternative `file` store would write a plaintext
`auth.json` inside the client home, not `.env`, and is rejected by the effective
configuration check. Changing that is a separate decision.

## Privacy and enforcement

No private transaction amounts, merchants, account/card identifiers, raw mail or
headers/IDs, authentication material or Gmail observations are eligible model
input. Placeholder structures contain `<AMOUNT>`, `<MERCHANT>`, `<CARD_SUFFIX>` and
other role markers, never real values. No Gmail object or ledger document is used
to construct the outbound snapshot. Source content and model instructions are
separate data; instruction-like source text grants no authority.

Before every analysis the official app-server's effective configuration is checked:

- Supported `configRequirements/read` must report explicit `requirements: null`,
  or an object whose non-null entries are exactly
  `{"allowedLoginMethods":["chatgpt"]}`. The pinned client projects the app's own
  `forced_login_method = "chatgpt"` into this response; it is not additional policy.
  Missing, malformed, empty/all-null objects, other login lists and any other
  non-null requirement are refused. This check runs on Connect (including saved
  session restore), before every analysis and again after thread startup, before
  the turn. Policy pins cannot be established by a user-config read alone.
- Named `noio` profile: filesystem root **deny**, no narrower grants/inheritance,
  network disabled, approvals **never**, permission requests disabled. Legacy
  sandbox settings are refused because they can override named profiles.
- Shell, browser/computer use, image tools/generation, apps/plugins, multi-agent,
  hooks, memories, code mode and dependency installation are disabled. Unexpected
  inherited MCP/apps/plugins/notification commands, external instructions/catalogs
  or provider overrides cause refusal before thread start. An empty MCP table
  override is not assumed to erase inherited entries.
- Environment and permission-context injection are disabled. Built-in skill
  catalogs can still inject local paths, so supported `skills/list` and
  `skills/config/write` operations disable all enabled skills in this isolated
  client; a force-reloaded catalog must confirm that none remain enabled.
- Each analysis has a fresh ephemeral thread, fixed application instructions and
  only the saved safe payload. Thread permissions are checked before the turn;
  completed threads are unsubscribed. The host never grants a server-requested
  tool, approval, external token or privilege callback.

These controls were exercised on the unmodified official client with fake loopback
model responses attempting native patch reads/writes, shell/network access, image,
MCP, browser and privilege calls. Native sandbox denials, unchanged synthetic files,
no sentinel disclosure and zero model canary requests were observed. A working
canary positive control and an inherited-MCP counterexample were included. This is
stronger than mocking the enforcement or instructing the model not to use tools,
but it is not proof for other client versions or operating systems. Permission
profiles are currently beta; incompatible managed policy fails closed. The app
does not change system-wide policy or use a vendor fork.

Safe snapshots/proposals and review decisions live in separate SQLite tables;
accepted financial records and existing revision/rejection behavior are untouched.
Raw rejected model output is not saved or echoed in errors. Account identities are
local only and absent from model input. Native tokens are handled solely by Codex,
not this app's database. Protect the database and its private client directory like
other local application data. No external fonts, analytics, scripts, mail sync or
background model analysis is added.

## Provider compatibility evidence (checked 24 October 2026)

**ChatGPT/Codex:** official [authentication](https://developers.openai.com/codex/auth/),
[SDK](https://developers.openai.com/codex/sdk/),
[app-server](https://developers.openai.com/codex/app-server/),
[permissions](https://developers.openai.com/codex/permissions/) and
[configuration](https://developers.openai.com/codex/config-reference/) documents
support subscription login, programmatic clients, mandatory keyring and narrow
permission profiles. Official [pricing](https://developers.openai.com/codex/pricing/)
distinguishes included subscription usage from API billing. Limits vary by
model/task/plan, share usage with other Codex surfaces and may include weekly limits.
The current page describes five-hour estimates for Plus/Standard Business, not fixed
analysis counts. Only paid plan types reported by the official client are accepted;
no purchase, credit top-up or separate API access is enabled by this app.

**Claude: unavailable.** The official [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
and [legal/authentication rules](https://code.claude.com/docs/en/legal-and-compliance)
restrict offering Claude.ai subscription login/rate limits in third-party products
without prior approval. Hosting unmodified Claude Code is not assumed to authorize
this connector. The normal supported API alternative is separately billed and is
not authorized here. Native subscription usage limits do not confer API entitlement.

**Gemini: unavailable.** Official [CLI authentication](https://geminicli.com/docs/get-started/authentication/)
and [quota/pricing](https://geminicli.com/docs/resources/quota-and-pricing/) document
Google-login headless usage with cached authentication and eligible Pro/Ultra
allowances (currently 1,500/2,000 requests/day, plus per-minute/service restrictions).
Not every Gemini/Workspace chat plan qualifies. Inspected official CLI v0.65.0 uses
fixed native OAuth service/account names and automatic encrypted-file fallback;
this app has not established an independently isolated, mandatory-native credential
path. No existing Gemini/worker credentials are inspected or reused.

## Synthetic preview and validation

```sh
uv run --locked python examples/ui_preview.py --port 8893
```

This is the actual desktop UI with a disposable database, a temporary `.env`, a fake
cached subscription, fake model and fake Gmail. **No provider/native credential
access occurs.** Port 8765 is refused. A fake transport serves synthetic DBS-origin
pages; no network is used. Choose Safe AI analysis, Connect, capture terms for
`yuu`, save a structure, inspect the input, request analysis and review/accept/reject.
Add `--ai-signed-out` to start without a cached subscription and approve a synthetic
sign-in in the sign-in window. The banner labels all screens synthetic. Ctrl-C removes
the disposable database.

```sh
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

`tests/fixtures/codex-0.164.0-chatgpt-requirements.json` records the full native
reply observed with the exact production configuration, including null fields not
in the generated stable schema. Portable tests replay it and reject malformed or
additional policy. The opt-in macOS Apple Silicon contract also runs the pinned
binary with production configuration and disposable HOME/CODEX_HOME/cwd:

```sh
NATIVE_CONTRACT_BINARY=/absolute/path/to/codex uv run --locked pytest -q tests/test_codex_native_contract.py
```

The contract requires `sandbox-exec`, denies all network and Keychain access, and
permits only initialize/config/skills protocol operations through to the native
binary. Web Connect, saved-session restore and failed-login tests use real native
pre-auth checks plus synthetic auth validated against that binary's generated
protocol schema. No authentication/account/model operation reaches the native
client, no browser follows the synthetic sign-in link, and no checksum or
production configuration override is used. These tests do not verify real-account
sign-in, Keychain persistence, callback/token exchange or subscription entitlement.

`tests/test_public_terms_tls.py` runs the real terms download over TLS to a loopback
server with synthetic certificate authorities and an emptied host trust store: the
old interpreter-default trust fails, the `certifi` bundle connects, and a wrong
hostname or unknown authority is refused. No real issuer is contacted, and a capture
from real issuer sites with this change has not been verified.

Next: qualify real public terms and sanitized bank-format evidence without
broadening the private-data or model-authority boundary.
