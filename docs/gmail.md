# Read-only Gmail candidate adapter

The read-only adapter is connected to the loopback browser application's Google
sign-in window and explicit mail-loading actions. **Implementation and synthetic
end-to-end checks are complete; live Google consent, Gmail access and actual
bank-notification compatibility have not been verified.** No personal credentials,
inbox, real `.env` or OS credential entries were used during development/tests.

```sh
uv sync --locked
uv run --locked python examples/gmail_synthetic.py
uv run --locked pytest tests/test_gmail.py
```

The example calls the real `GmailRestClient` and `GmailAdapter` methods with an
injected synthetic HTTP transport, follows two pages, and deduplicates repeated
IDs. It reports 2 candidates, 1 duplicate, and **0 accepted transactions**. It
never opens a network connection or accesses a credential store.

## Public Python entry point

Import from `spending_optimiser.gmail`:

- `GmailAdapter(client, namespace=...)`: `fetch_page(query=..., page_token=None,
  page_size=100, seen_ids=())` and `get_candidate(message_id)`.
- `GmailRestClient(tokens, transport=None)`: concrete HTTPS Gmail REST client.
  Default transport is `UrllibGetTransport`, which verifies certificate chain and
  hostname against the bundled `certifi` CA bundle rather than the Python
  installation's trust store; `JsonGetTransport.get_json(url, headers)` can be
  injected. Only list/get requests to `users/me/messages`
  exist. Retrieval requests `format=full`; spam/trash inclusion is disabled.
- `GmailClient`: injectable list/get protocol for an already authorized SDK
  wrapper. Wrap SDK exceptions in `ClientFailure(IngestionError(...))`,
  `AuthenticationFailure`, or `RefreshFailure`. Unexpected exceptions become
  redacted `client_failed` errors, not empty results.
- `AccessTokenProvider.access_token()`: narrow authorization boundary. The host
  supplies a current token on every request. Refresh, authorization and secure
  persistence belong to the host, not this adapter. No consent is initiated.

The namespace must be an opaque, stable, whitespace-free application mailbox ID,
not an email address or bank-account mapping. Keep it stable when reauthorizing
the same mailbox and distinct for different mailboxes. Observation IDs hash the
namespace and Gmail message ID, not subjects, amounts, thread IDs, or content.
Repeated retrieval retains source identity even if content changes. Dedupe is by
**message** ID, not thread ID, since threads can contain multiple notifications.

### Pagination and failure contract

`CandidatePage` provides `results`, `next_page_token`, `error`, `duplicate_ids`
(listed IDs skipped as already seen), `duplicate_count`, `candidate_count`,
`successful`, and `exhausted`. Each `MessageResult` has a
private `message_id`, either a `candidate` or an `error`. A successful empty page
means **0 candidates on that page**, not necessarily end of pagination. Only
`exhausted` indicates a successful terminal page. Gmail's result-size estimate
is not a transaction count or a completeness proof.

Follow `next_page_token` until exhausted, passing successfully handled IDs in
`seen_ids`. Within a page repeated IDs are fetched once, even if that fetch
fails. Across pages/restarts, deduplication/checkpoints belong to the caller.
The synthetic example shows the continuation loop and cycle guard. The adapter
rejects a nonadvancing token; callers must also detect longer token cycles.

On list failure, no messages are retrieved. On individual fetch/normalization
failure, successes and per-ID failures are returned together, with the server's
continuation still visible, but `successful` and `exhausted` are false. **Do not
advance blindly.** Replay the same page with successful IDs or save failed IDs
for explicit retry. Do not checkpoint failed IDs as handled. Changing mailboxes,
deleted messages and expired page tokens can require restarting a scan with
previous successful IDs. This is not a Gmail snapshot or history/watch sync.

Errors never include upstream exception strings, response bodies, request URLs,
queries or credentials. Codes: `authentication_failed`, `refresh_failed`,
`permission_denied`, `message_not_found`, `rate_limited`, `api_error`,
`transport_failed`, `response_too_large`, `invalid_response`, `invalid_message`,
`client_failed`. HTTP status and a retryable flag are explicit. 429/5xx/network
failures are retryable; other statuses need investigation. No automatic retry,
backoff, scheduler, quota policy, or silent reauthorization is implemented. A 403
may have several causes; the adapter does not guess from a discarded API body.

## Content and private provenance

Candidates are **not transactions**. A query hit can be an OTP, decline, balance,
marketing email, spoof, or unrelated message. `From`, `To`, `Date`, `Subject`, and
`Message-ID` headers are untrusted claims, not authentication evidence. Duplicate
headers are retained. This adapter performs no DKIM/SPF/DMARC verification.

- `Candidate.observation` is the existing personal `Observation` schema, with
  `source_system="gmail"`, mailbox namespace, provider ID, retrieval time, and a
  SHA-256 digest of the full payload's canonical JSON (sorted keys, compact JSON,
  Python's default ASCII escaping, UTF-8 bytes). It is not a raw RFC822 digest.
- `observed_at` is the adapter's retrieval clock. `gmail_internal_at` is Gmail's
  `internalDate` (usually SMTP receipt ordering, but imports can supply it).
  `sent_at` is the untrusted Date header with its timezone; absent, ambiguous or
  invalid values become `None`, with warnings for invalid/ambiguous values.
  **None of these timestamps is a financial event date or posting date.**
- `headers` preserve selected original values with JSON-pointer locators.
  `subject` separately decodes RFC2047 encoded words. `sender` is the decoded
  single untrusted From claim for display only. An absent From header becomes
  `None` without a warning; duplicate or undecodable From headers become `None`
  with an `ambiguous_sender` or `undecodable_sender` warning, respectively.
  These sender conditions never reject the candidate. `parts` contain every
  inline `text/plain` and `text/html` alternative, with MIME type, payload/body
  locator, normalized text and a digest of the decoded part bytes. A later
  private extractor can cite `EvidenceRef(target="observation",
  evidence_revision_id=candidate.observation.observation_id, locator=part.locator)`.
  Part locators identify the source base64 body; normalized text is derived,
  not a claim to be a byte-for-byte source quote.
- Nested mixed/alternative/related multipart structures are traversed. Charset
  decoding is strict (UTF-8 default), base64url data is validated, and declared
  decoded byte sizes must match. Gmail already decodes MIME transfer encodings;
  the adapter does not double-decode quoted-printable/base64 transfer encodings.
- HTML is converted to text with entity decoding, block/table separators, and
  omission of scripts/styles/head/template content. It is lossy, not a browser,
  HTML sanitizer or visual-layout interpreter. Never render mail as trusted HTML.
  All text remains untrusted data, never agent instructions or executable code.
- `candidate.text` concatenates **all** text parts. MIME alternatives can repeat
  the same notice. Do not treat alternatives as multiple financial events. Use
  MIME part locators and extraction/review logic rather than concatenated text
  as an event identity or proof of financial meaning.
- Named attachments and attachment-disposition parts are intentionally excluded;
  other non-text MIME bodies are not parsed. Inline text requiring a separate
  attachment fetch fails closed. No attachment endpoint is called. Notifications
  whose financial content is only in attachments are unsupported.
- Missing/truncated inline bodies, invalid metadata/encoding, empty text-only
  content, unavailable charsets and oversized/deep structures return
  `invalid_message`, not a partial candidate. Limits: 2 MiB aggregate decoded
  text bytes, 200 visited MIME parts, depth 20; default HTTP response limit 8 MiB
  and network timeout 30 seconds. Gmail's parsed full payload cannot establish
  whether the original sender's MIME wire message was complete or valid.

No amount, merchant, category/MCC, card product, account mapping, event kind,
event date, posting date or acceptance is inferred. The adapter creates no
`Transaction`, `SourceEvidence`, or public card-rule evidence. Never send raw
mail or candidate content to public research or external models. For the
application's model-input boundary, see
[safe AI privacy and enforcement](subscription-ai.md#privacy-and-enforcement).

Dataclass reprs suppress mail content, message/thread IDs, namespace and page
tokens. Error codes are allowlisted. This is a logging guard, **not an export
redactor**: explicit field access, `dataclasses.asdict`, `Observation` repr/JSON,
and caller debug/HTTP logging can still expose private data. Retention,
encryption, source archival, access control and durable evidence replay remain
host responsibilities. Do not commit messages or serialized candidates.

## User-performed Google Cloud setup

Do these steps yourself. Development did not create a Cloud project, access your
credentials, or complete consent. Never paste client JSON, secrets, tokens or mail
into chat, Git, an issue, logs, or a screenshot.

1. In [Google Cloud Console](https://console.cloud.google.com/), select/create your
   own project. Open **APIs & Services > Library > Gmail API > Enable**.
2. Open **Google Auth Platform > Branding** (Get started if needed). Supply the
   app name, user-support email and developer/contact email. Choose **External**
   audience for a personal Gmail account. This is not an organization sign-in app.
3. Under **Audience**, keep publishing status **Testing** and add your exact Gmail
   address under **Test users > Add users > Save**. Under **Data Access > Add or
   remove scopes**, select `https://www.googleapis.com/auth/gmail.readonly` and
   `openid`. Save. No modify/send/full-mail/profile/email scope is requested.
4. Under **Clients > Create client**, select **Desktop app**, name it, create it,
   and download its JSON. Do not choose Web application, mobile, service account
   or an AI-agent client. Desktop loopback redirects are supported; there is no
   Web-client Authorized JavaScript origins/redirect list to populate. The app
   sends exactly `http://127.0.0.1:<running-port>/gmail/callback` (default port 8765),
   using its existing loopback listener. The exchange uses that same redirect.
   This is not the deprecated OOB/copy-paste flow. Never expose it via a tunnel.
5. Put the client in the application's gitignored `.env`: the file in this application
   folder (next to `pyproject.toml`), wherever the app is started from.
   Copy the two values yourself (see `.env.example`):

   ```sh
   SPENDING_GOOGLE_CLIENT_ID='...apps.googleusercontent.com'
   SPENDING_GOOGLE_CLIENT_SECRET='...'
   ```

   or let the app write them from the downloaded JSON, kept temporarily outside this
   repository, and then delete that file:

   ```sh
   uv sync --locked
   uv run --locked spending-optimiser --configure-google < /absolute/private/path/client.json
   ```

   Without these values the Connect action reports the missing configuration and
   starts nothing; the app never invents or registers a client for you.

### The `.env` credential file

The app stores its Google client and the saved Gmail authorization (refresh token) in
`.env`. **This is plaintext, not encryption**, and it replaces the earlier native
OS credential store for Gmail; old Keychain entries are not read or migrated.
Google and OpenAI account passwords are never stored, and transient authorization
codes, PKCE verifiers and sessions stay in process memory.

- The file is parsed with python-dotenv's real parser: no shell sourcing, no `${}`
  interpolation, and nothing is exported to the process environment or to the GPT
  client.
- Updates are one bounded, atomic read-modify-write (256 KiB file, 16 KiB value). The
  new file is owner-only (mode 0600), a looser hand-edited file is tightened when
  read, and symlinks, non-regular files and files owned by another user are refused.
  A failed update leaves the old file untouched. Comments, other settings and layout
  are preserved.
- Connect writes `SPENDING_GMAIL_AUTHORIZATION_<ledger digest>`, so each ledger
  keeps its own authorization. Disconnect removes only that variable.
- Do not commit, paste or screenshot the file. Back it up as you would a password
  file.

### Provider testing and verification restrictions

External Testing apps only authorize added test users; Google may show an
unverified-app warning. Gmail read-only is a **restricted scope**. Testing-mode
refresh tokens normally expire after **7 days** because this app requests more
than basic identity scopes; reconnect is expected. Revocation, password/security
changes and provider policies can invalidate tokens sooner. Workspace
administrators may block access. Do not broaden scopes to work around a denial.
Use a dedicated OAuth client that has never been granted broader Gmail access;
unexpected returned scopes are rejected. Public distribution/production use can
require Google verification and, depending on data handling, a restricted-scope
security assessment. Personal/testing exceptions do not imply public approval.
Check Google's current policies before changing audience or publishing status.

Official references consulted for this implementation:
- [Installed/desktop OAuth, PKCE and loopback redirects](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Python Gmail API setup](https://developers.google.com/workspace/gmail/api/quickstart/python)
- [Consent screen and test users](https://developers.google.com/workspace/guides/configure-oauth-consent)
- [Refresh-token expiry restrictions](https://developers.google.com/identity/protocols/oauth2#expiration)
- [Gmail scopes and verification](https://developers.google.com/workspace/gmail/api/auth/scopes)

Unlike Google's quickstart example, this app does **not** write `token.json`.

## Start, connect, load, refresh, disconnect

```sh
uv run --locked spending-optimiser
```

The sidebar has three steps: **1 Connect Gmail**, **2 Inspect saved data** and
**3 Understand this month**. Nothing signs in at startup or when a page is opened.

1. Open **http://127.0.0.1:8765** and choose **Connect Gmail**. With a custom
   `--port`, use that port consistently; do not substitute `localhost` for sign-in.
2. Choose **Connect Gmail** (or **Change account**). The app opens one Google sign-in
   window. Sign in and consent only on Google's page; the app never collects a Gmail
   password. If the browser blocks the window, an alert offers **Continue in this
   window**, which does the same flow in the current tab. **Cancel sign-in** ends an
   attempt, and an attempt expires after 10 minutes. Cancelled, denied or invalid
   callbacks load nothing. Closing the window is not a connection: the app shows
   **Connected** only after the verified callback.
3. The window reports the result and closes where the browser allows it; the app
   page updates by asking its own status endpoint, with no messages between windows.
   **No mail is fetched on sign-in or startup.** Profile lookup retrieves only your
   Gmail account identity. OpenID supplies a verified stable Google `sub`, so email
   address changes do not merge identities. A namespace hashes that `sub`; message
   identities also include the Gmail message ID, never the thread or financial amount.
4. Choose a **period** with the From and To dates (inclusive, Singapore time): one day,
   a calendar month or any range up to three calendar months, or use the **This
   month**, **Last month** and **Last 3 months** links. The exact limit and the
   `?month=` and `?start=&end=` links are in [monthly.md](monthly.md#choosing-a-period).
   Then choose **Load period**. **Pages per action** is 1-5 (default 2, 20 IDs/page).
   The fixed query uses explicit epoch boundaries for the Singapore-time midnights that
   open and close the period (UTC+08:00), excluding spam/trash; a local half-open
   Singapore receipt-time filter enforces the exact period. Duplicates count toward
   the action bound. **Continue period** resumes until the query is genuinely
   exhausted, including successful empty nonterminal pages. Partial failures retain
   successes and retry the same unadvanced page with resolved IDs skipped. A proven
   per-message failure (`invalid_message` or `response_too_large`) is retained as an
   unresolved ID so later pages still load; **Retry failed messages** re-fetches only
   those IDs. Listing, network, rate-limit and auth failures never advance. Errors,
   cycles and unresolved messages stay incomplete, never empty or complete success.
   **Refresh period** restarts an expired/cycled token. No automatic retries,
   whole-mailbox scans, scheduler or background polling exist.
   Progress is stored per Google account and exact period. A scan covers only the
   period it loaded: a month loaded earlier never marks a day or range inside or
   around it as loaded. A form for another account, or a period that is invalid,
   reversed, over three months or missing, is refused before any Gmail request.
5. **Inspect saved data** lists the period's candidates with sender, subject and Gmail
   receipt time. Expand a row to view the complete subject, unverified headers and
   escaped normalized text. HTML is never executed, images never loaded, URLs never
   followed and embedded instructions never acted upon. Display is capped at 10,000
   characters per candidate with a size notice. Accepted format layouts match saved
   candidates locally and deterministically into unsaved, review-pending proposals.
   No model is called, nothing is added to the ledger and review decisions and
   financial facts remain unchanged.
6. **Refresh period** restarts this period's query with bounded pages, retaining
   saved candidates and deduplicating by stable account/message identity. Changed
   content versions are preserved. Continue until exhausted again; previous
   exhaustion is not current after refresh starts. Absent/deleted provider mail
   is not erased from saved data. This is not a snapshot or financial completeness
   guarantee. The last query-action time, completed-page/duplicate counts and
   unresolved messages are visible. Retries do not inflate these counts.
7. **Disconnect Gmail** hides saved mail, cancels pending sign-ins and removes this
   ledger's saved authorization from `.env`. It is idempotent. If `.env` cannot be
   updated, the UI is disconnected but reports the failure and the saved credential
   could remain; fix the file and retry. The client settings stay for later
   connections. To revoke the grant at Google as well, remove the app under your
   Google Account's third-party connections. Local disconnect does not make a
   provider revocation request or modify mail.

Refresh authorization is real `google-auth`; the authorization-code exchange and
S256 PKCE are handled by `google-auth-oauthlib`/OAuthlib. State, verifier, nonce,
redirect and expiry are held server-side and bound to a signed, HttpOnly,
SameSite=Lax browser-session cookie. State is single-use, checked before exchange,
and the ID token's signature, issuer, audience, expiry and nonce are verified.
Existing Strict form-CSRF, Origin, Host, CSP and loopback protections remain.
Callback codes/errors are never rendered and immediately redirect to `/gmail`
without query parameters and with no-referrer. Access logging is disabled.

Access/ID tokens live only in process memory. The refresh token and client secret
remain in `.env` (see above), keyed per ledger path digest. The sign-in window needs
one same-origin script (`script-src 'self'`, `connect-src 'self'`); the Content
Security Policy otherwise stays `default-src 'none'`, and every app response sets
`Cross-Origin-Opener-Policy: same-origin` so provider pages keep no handle to the app.
**Saved mail and checkpoints persist as readable SQLite data with no app-level
encryption**, scoped by stable Google identity, including headers, message text,
every retained content version, failed-message IDs and successful-ID/page-token
continuation. Existing ledger records stay intact. Local database format 2 supports
verified compressed payloads, not encryption; JSON schema stays 1.0. Protect
disk/backups. [Storage](storage.md) keeps the current Singapore month plus the two
before it uncompressed, whichever period you view, and compresses eligible older
detail automatically after each write once usage is above 1 decimal GB. It can
restore a selected account and period locally. Saved mail is
retained indefinitely; there is no purge or deletion action. Disconnect does not
remove the local cache or ledger history, only visible access and authorization.

Restart hides mail and clears browser sessions, without reading `.env` or the
network. Use **Restore saved connection** to read the saved authorization and view
only that identity's saved mail (no network/mail retrieval yet). Loading then
refreshes/verifies authorization. Revoked or expired refresh/API authentication
hides mail and asks you to reconnect. Other API/permission/rate-limit failures are
explicit, not empty success. Switching accounts hides the prior cache; stale
account forms cannot load into a newly selected connection. Explicit period loading
is the only Gmail retrieval path.

This remains a single-user, loopback-only local app. The OS user/other local
software are trusted; memory, browser history, OS-store access and backups still
need normal OS protection. Do not expose the app on a LAN, through a proxy/tunnel,
or enable third-party HTTP/debug logging. No private mail is sent to an external
model. The optional [safe-reference AI integration](subscription-ai.md) supports
ChatGPT only for app-captured official terms and allowlisted placeholder structures;
account-backed AI verification and real-bank format qualification remain unverified.

## Validation readiness versus live verification

`uv run --locked pytest tests/test_gmail_login.py tests/test_connect_flow.py
tests/test_dotenv_store.py` exercises production OAuth library exchange/refresh
mechanics, the existing Gmail REST adapter, the sign-in window flow (popup,
same-window fallback, cancel, denial, expiry, missing client) and the `.env` store
against fake HTTP, temporary dotenv files and disposable databases. Tests cover
state/session/host/expiry/replay, nonce/scope rejection, `.env` escaping, file
protection, atomic failure and per-ledger isolation, account switches/disconnect/
restart, pagination/duplicates/month and page limits, 0/partial/API failures,
escaped content and unchanged offline ledger data. `tests/test_date_range.py` and
`tests/test_date_range_flow.py` cover single-day, range and calendar-month periods
(boundaries, leap years, Singapore midnight edges, invalid input, period-scoped
continuation, retry, refresh and account switching, and historical month data).
`tests/test_gmail_tls.py` runs Gmail's urllib transport and the final sign-in
profile request over real TLS to a loopback server with synthetic certificate
authorities and an emptied host trust store: the `certifi` bundle connects, while a
wrong hostname, an unknown authority, a redirect and an oversized response are
refused. `tests/test_provider_script.py`
runs the real sign-in script (needs Node) against a recording browser stand-in. The
synthetic Chrome walkthrough uses a mock consent page and fake transport, not real
Google sign-in. Reproduce it with
`uv run --locked python examples/ui_preview.py --port 8876`; see the README.

**Still requires the user:** perform the Cloud/client setup above, put the client in
`.env`, complete explicit Google consent from the app's Connect action, and verify
one small period load, refresh, restart/restore and disconnect with the real account.
Also verify continuation and persisted saved mail after restore. A Google sign-in
problem reported on the installed app (the generic "Google authorization failed"
message appeared after consent) had a reproduced cause in the final Gmail profile
request: it used the Python installation's trust store, which is empty on a
python.org interpreter whose certificate step was never run. That request now
verifies against `certifi`. This is reproduced synthetically only: other stages
(token exchange, ID-token checks, a Gmail API that is not enabled) show the same
message, and a real sign-in has not been retried. No live-account smoke
verification or qualified real-bank extraction is claimed. Keep
any real mail/screenshots outside Git and chat. Missing credentials do not prevent
the implemented offline tests or offline ledger workflow.
