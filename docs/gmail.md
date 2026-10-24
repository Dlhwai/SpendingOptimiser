# Read-only Gmail candidate adapter

Implemented independently of the ledger, UI and extractor, using only existing
runtime dependencies. **Live Gmail and actual bank-notification compatibility
have not been verified.** No personal credentials or inbox were used. All tests
and the executable example are synthetic and offline.

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
  `subject` separately decodes RFC2047 encoded words. `parts` contain every
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

## Future user-authorized OAuth setup

These are setup instructions, **not a flow run during development**:

1. After explicit authorization for the selected mailbox, create/select a Google
   Cloud project, enable Gmail API, configure the OAuth consent screen and allowed
   test users, and create an installed/desktop application OAuth client.
2. A separate local host authorization command should use the installed-app
   authorization-code flow with PKCE and a loopback redirect. Request exactly
   `https://www.googleapis.com/auth/gmail.readonly` (`GMAIL_READONLY_SCOPE`), not
   modify/send/full-mail scopes. Do not request broader scopes to resolve an
   error. Do not reuse previously granted broader credentials as proof of a
   read-only grant. Google consent/verification and test-mode token expiry rules
   still apply.
3. Offline access is needed only if the user authorizes refreshable unattended
   access. Keep access tokens in memory. Persist refresh tokens and any sensitive
   OAuth client material only through an explicitly selected native OS credential
   backend: macOS Keychain, Windows Credential Manager, or a verified Linux Secret
   Service backend. A host may use `keyring` with native-backend allowlisting,
   but must fail closed if the backend is absent, locked or plaintext. Do not
   install a plaintext fallback, write `token.json`/`.env` credentials, or store
   tokens in SQLite/project files. OS credential storage is proposed, not
   implemented or touched by this milestone.
4. Implement `AccessTokenProvider` in that authorized host, backed by an OAuth
   library such as `google-auth`. `access_token()` must return an unexpired
   in-memory token, refreshing previously granted credentials when necessary.
   Map revoked/failed refresh to `RefreshFailure`; unavailable authorization to
   `AuthenticationFailure`. Never log token/library exception details or launch
   a browser automatically on refresh failure. Persist rotated refresh credentials
   only to the selected OS store. Do not share a mailbox namespace across users.
5. Wire the provider explicitly, without embedding a token in source code:

   ```python
   from spending_optimiser.gmail import GmailAdapter, GmailRestClient

   # authorized_provider is supplied by the future user-authorized host.
   adapter = GmailAdapter(GmailRestClient(authorized_provider), "opaque-mailbox-id")
   # This next call is LIVE. Run only after explicit mailbox authorization.
   page = adapter.fetch_page(query="your deliberately scoped notification query")
   ```

No Google OAuth SDK, keyring backend, client secrets, consent command, refresh
implementation or credential persistence ships here. An already authorized host
can supply the provider or an SDK client immediately; the bundled example needs
neither. Google scope grants are enforced at authorization, not retroactively by
this client. This client's operation surface is read-only regardless.

Next: obtain explicit live-access authorization and authorized sanitized bank
samples, verify real MIME/API compatibility, and implement secure host OAuth and
private extraction/review separately. No live or actual-bank support is claimed.
