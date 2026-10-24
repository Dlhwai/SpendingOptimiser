# Read-only Gmail candidate adapter

The read-only adapter is now connected to the loopback browser application's
Google sign-in and explicit mail-loading actions. **Implementation and synthetic
end-to-end checks are complete; live Google consent, Gmail access and actual
bank-notification compatibility have not been verified.** No personal credentials,
inbox or OS credential entries were used during development/tests.

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
  Default transport is `UrllibGetTransport`; `JsonGetTransport.get_json(url,
  headers)` can be injected. Only list/get requests to `users/me/messages`
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

`CandidatePage` provides `results`, `next_page_token`, `error`, `duplicate_count`,
`candidate_count`, `successful`, and `exhausted`. Each `MessageResult` has a
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
  single untrusted From claim for display only; absent, duplicate or undecodable
  From headers become `None` with an `ambiguous_sender`/`undecodable_sender`
  warning and never reject the candidate. `parts` contain every
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
mail or candidate content to public research or external models. Model use
requires a separate explicit privacy opt-in.

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
5. Keep the downloaded JSON temporarily in an owner-only location **outside this
   repository** (for example, a private temporary folder with mode 0700 and file
   mode 0600). Import it directly into the native OS credential store:

   ```sh
   uv sync --locked
   uv run --locked spending-optimiser --configure-google < /absolute/private/path/client.json
   ```

   This reads bounded JSON from stdin, stores only the Desktop client settings in
   the native store, and exits without starting the server. It never creates a
   plaintext token/config file. Delete the downloaded file after successful import
   and protect any retained backup. Avoid shell tracing and debug/HTTP logging.
   No secrets belong in environment variables, command-line arguments or `.env`.

The `keyring` integration selects only **macOS Keychain**, **Windows Credential
Manager**, or **Linux Secret Service** for the running platform. Unlock the native
store first; on Linux run inside a session with a working Secret Service/D-Bus
provider. A headless or locked/unavailable backend returns
`secure_store_unavailable` and exit code 2 for configuration. There is no
plaintext/chainer/fallback backend, and tests never touch native entries. Keychain
may ask the OS user to approve access. Do not install a plaintext fallback to
bypass an error.

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

1. Open **http://127.0.0.1:8765**, then **Mail inbox**. The overview also has a
   **Set up Gmail** shortcut. Expand **One-time Google setup** for the concise
   local configuration checklist; a missing-client response opens it automatically.
   With a custom `--port`, use that port consistently; do not substitute `localhost`
   for sign-in.
2. Choose **Connect Gmail**, then **Continue to Google**. Sign in and consent only
   on Google's page in the same browser profile. The app never collects a Gmail
   password. The attempt expires after 10 minutes; a new attempt replaces the old
   one for that browser. Cancelled/denied/invalid callbacks load nothing.
3. After the clean redirect back, check the displayed account address. **No mail
   is fetched on sign-in or startup**. Profile lookup retrieves only your Gmail
   account identity. OpenID supplies a verified stable Google `sub`, so email
   address changes do not merge identities. No extra profile/email scopes are
   needed. A namespace hashes that `sub`; message identities also include the
   Gmail message ID, never the thread or financial amount.
4. Choose last **1, 7 or 30 days** and a limit of **1-100 messages**, then **Load
   mail**. The query is fixed to `newer_than:Nd`, excluding spam/trash. At most five
   pages of up to 20 IDs are examined, counting duplicate IDs against the limit.
   The result reports candidates, refreshes, duplicates, exhaustion or a loading
   limit, and explicit partial/API failures. **0 candidates** after success is
   distinct from failed/incomplete loading. No whole-mailbox scan, scheduler,
   background polling or automatic retry exists.
5. Candidate rows show sender, subject and Gmail receipt time. Expand a row to
   view the complete subject, unverified headers and escaped
   normalized text. HTML is never executed, images never loaded, URLs never
   followed and embedded instructions never acted upon. Display is capped at
   10,000 characters per candidate with a size notice. Candidates are not parsed
   or added to the ledger and do not change review decisions or financial facts.
6. **Refresh mail** repeats the bounded query, replaces the temporary batch and
   deduplicates by account/message identity. On partial failure, successful
   candidates remain visible and errors are explicit; retry starts a fresh scan,
   not a checkpoint that silently skips failed IDs. This is not a snapshot sync.
7. Under **Connection settings & privacy**, **Disconnect Gmail / forget saved
   authorization** clears in-memory mail,
   cancels pending sign-ins and deletes this database's refresh authorization from
   the OS store. It is idempotent. If deletion fails, the UI is disconnected but
   explicitly reports that the saved credential could remain; unlock and retry.
   Client configuration stays for later connections. To revoke the grant at
   Google as well, remove it under your Google Account's third-party connections.
   Local disconnect does not make a provider revocation request or modify mail.

Refresh authorization is real `google-auth`; the authorization-code exchange and
S256 PKCE are handled by `google-auth-oauthlib`/OAuthlib. State, verifier, nonce,
redirect and expiry are held server-side and bound to a signed, HttpOnly,
SameSite=Lax browser-session cookie. State is single-use, checked before exchange,
and the ID token's signature, issuer, audience, expiry and nonce are verified.
Existing Strict form-CSRF, Origin, Host, CSP and loopback protections remain.
Callback codes/errors are never rendered and immediately redirect to `/gmail`
without query parameters and with no-referrer. Access logging is disabled.

Access/ID tokens and the latest bounded candidate batch live only in process
memory, never SQLite or files. Refresh tokens and client secrets live only in the
native store under service `spending-optimiser.gmail`. The client entry is shared
by this OS app; saved authorization is keyed by the absolute ledger-path digest.
There is no ledger schema/storage migration and offline imports/reviews survive
upgrade unchanged. **No persistent mail cache is implemented.** Restart clears
mail and browser sessions, and does not access the credential store or network.
Use **Restore saved connection** after restarting to read the native store (no
network/mail yet); loading then refreshes and verifies authorization. Revoked or
expired refresh/API authentication clears visible mail and asks you to reconnect.
Other API/permission/rate-limit failures are explicit, not silent empty success.
Switching accounts immediately hides the prior batch; stale account forms cannot
load into a newly selected connection.

This remains a single-user, loopback-only local app. The OS user/other local
software are trusted; memory, browser history, OS-store access and backups still
need normal OS protection. Do not expose the app on a LAN, through a proxy/tunnel,
or enable third-party HTTP/debug logging. No private mail is sent to an external
model, and there is no live AI integration.

## Validation readiness versus live verification

`uv run --locked pytest tests/test_gmail_login.py` exercises production OAuth
library exchange/refresh mechanics, the existing Gmail REST adapter and browser
endpoints against fake HTTP and stores. Tests cover cancellation/failure,
state/session/host/expiry/replay, nonce/scope rejection, secure-store errors,
account switches/disconnect/restart, pagination/duplicates/range limits,
0/partial/API failures, escaped content and unchanged offline ledger data.
The synthetic Chrome walkthrough uses an injected mock consent page and fake
transport/store, not real Google sign-in or OS entries. Reproduce it with
`uv run --locked python examples/ui_preview.py --port 8876`; see the README.
The UI additionally tests navigation, configuration-required guidance, candidate
headers/truncation, selected range/limit, offline imports and the review journey.

**Still requires the user:** perform Cloud/client setup above, approve native
credential-store access, complete explicit Google browser consent, verify one
small recent-message load, refresh, restart/restore and disconnect with the real
account. No live-account smoke verification or qualified real-bank extraction is
claimed. Keep any real mail/screenshots outside Git and chat. Missing credentials
do not prevent the implemented offline tests or offline ledger workflow.
