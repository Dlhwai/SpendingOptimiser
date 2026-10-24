"""Read-only Gmail candidate ingestion. No OAuth consent, persistence or transactions.

Public entry points: GmailAdapter, GmailRestClient, and their injected protocols.
All content, sender claims, message IDs and pagination tokens are private data.
"""

import hashlib
import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import TypeAdapter

from ._gmail_mime import Header, InvalidMessage, TextPart, decoded_header, headers, normalize
from .common import Identifier
from .transactions import Observation

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class IngestionError:
    """Allowlisted diagnostics only, never API bodies, tokens, URLs or exception text."""

    code: str
    retryable: bool = False
    http_status: int | None = None

    def __post_init__(self):
        if (
            self.code
            not in {
                "response_too_large",
                "authentication_failed",
                "refresh_failed",
                "permission_denied",
                "message_not_found",
                "rate_limited",
                "api_error",
                "transport_failed",
                "invalid_response",
                "client_failed",
                "invalid_message",
            }
            or type(self.retryable) is not bool
            or (
                self.http_status is not None
                and (type(self.http_status) is not int or not 100 <= self.http_status <= 599)
            )
        ):
            raise ValueError("invalid ingestion error")


class AuthenticationFailure(Exception):
    """The token provider could not supply authorized credentials."""


class RefreshFailure(AuthenticationFailure):
    """Refresh was revoked, expired or otherwise failed; authorization may be needed."""


class ClientFailure(Exception):
    def __init__(self, error: IngestionError):
        self.error = error
        super().__init__(error.code)


class AccessTokenProvider(Protocol):
    def access_token(self) -> str:
        """Return a valid bearer token, refreshing outside this adapter if necessary.

        Raise RefreshFailure or AuthenticationFailure without secret details.
        Never start consent implicitly. Implementations own secure storage.
        """
        ...


class GmailClient(Protocol):
    def list_messages(self, *, query: str, page_token: str | None, page_size: int) -> dict: ...

    def get_message(self, message_id: str) -> dict: ...


class JsonGetTransport(Protocol):
    def get_json(self, url: str, headers: Mapping[str, str]) -> dict: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward an Authorization header to any redirect destination.
        return None


class UrllibGetTransport:
    """Bounded HTTPS GETs, no redirects/retries/logging, 30-second network timeout."""

    def get_json(self, url: str, headers: Mapping[str, str]) -> dict:
        try:
            with build_opener(_NoRedirect()).open(
                Request(url, headers=dict(headers)), timeout=30
            ) as r:
                raw = r.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ClientFailure(IngestionError("response_too_large"))
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except HTTPError as exc:
            status = exc.code
            exc.close()
            code = {
                401: "authentication_failed",
                403: "permission_denied",
                404: "message_not_found",
                429: "rate_limited",
            }.get(status, "api_error")
            raise ClientFailure(
                IngestionError(code, status == 429 or status >= 500, status)
            ) from None
        except (URLError, TimeoutError, OSError):
            raise ClientFailure(IngestionError("transport_failed", True)) from None
        except (ValueError, UnicodeError):
            raise ClientFailure(IngestionError("invalid_response")) from None


class GmailRestClient:
    """Concrete Gmail REST client with only list/get operations on users/me.

    Token retrieval occurs on every request. No token is cached or persisted here.
    A provider may refresh previously authorized credentials, but never consent.
    """

    def __init__(self, tokens: AccessTokenProvider, transport: JsonGetTransport | None = None):
        self._tokens = tokens
        self._transport = transport if transport is not None else UrllibGetTransport()

    def _get(self, path: str, params: dict) -> dict:
        try:
            token = self._tokens.access_token()
        except RefreshFailure:
            raise ClientFailure(IngestionError("refresh_failed")) from None
        except Exception:
            raise ClientFailure(IngestionError("authentication_failed")) from None
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token) is None:
            raise ClientFailure(IngestionError("authentication_failed"))
        try:
            return self._transport.get_json(
                f"{_API_ROOT}{path}?{urlencode(params)}",
                {"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except ClientFailure:
            raise
        except Exception:
            # A third-party transport/SDK exception may contain credentials or raw mail.
            raise ClientFailure(IngestionError("transport_failed", True)) from None

    def list_messages(self, *, query: str, page_token: str | None, page_size: int) -> dict:
        params = {"q": query, "maxResults": page_size, "includeSpamTrash": "false"}
        if page_token is not None:
            params["pageToken"] = page_token
        return self._get("", params)

    def get_message(self, message_id: str) -> dict:
        if not _valid_id(message_id):
            raise ValueError("invalid Gmail message id")
        return self._get(f"/{message_id}", {"format": "full"})


@dataclass(frozen=True)
class Candidate:
    observation: Observation = field(repr=False)
    thread_id: str = field(repr=False)
    gmail_internal_at: datetime  # Gmail internalDate, NOT a transaction/posting date
    sent_at: datetime | None  # untrusted Date header, None if absent/invalid/ambiguous
    headers: tuple[Header, ...] = field(repr=False)
    parts: tuple[TextPart, ...] = field(repr=False)
    subject: str | None = field(repr=False)
    sender: str | None = field(repr=False)  # untrusted From claim, None if absent/ambiguous
    warnings: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """All inline text, including possibly repeating MIME alternatives.

        This view deliberately omits no inline text siblings. Extractors must not
        treat each alternative as a separate event; use parts and their locators.
        """
        return "\n\n".join(part.text for part in self.parts)


@dataclass(frozen=True)
class MessageResult:
    message_id: str = field(repr=False)
    candidate: Candidate | None = field(default=None, repr=False)
    error: IngestionError | None = None


@dataclass(frozen=True)
class CandidatePage:
    results: tuple[MessageResult, ...] = field(default=(), repr=False)
    next_page_token: str | None = field(default=None, repr=False)
    error: IngestionError | None = None
    duplicate_count: int = 0

    @property
    def successful(self) -> bool:
        return self.error is None and all(result.error is None for result in self.results)

    @property
    def exhausted(self) -> bool:
        return self.successful and self.next_page_token is None

    @property
    def candidate_count(self) -> int:
        return sum(result.candidate is not None for result in self.results)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _MESSAGE_ID.fullmatch(value) is not None


def _failure(exc: Exception) -> IngestionError:
    if isinstance(exc, ClientFailure):
        return exc.error
    if isinstance(exc, RefreshFailure):
        return IngestionError("refresh_failed")
    if isinstance(exc, AuthenticationFailure):
        return IngestionError("authentication_failed")
    return IngestionError("client_failed", True)


class GmailAdapter:
    """Retrieve private notification candidates, never accepted financial events.

    namespace is an opaque stable mailbox identity supplied by the application,
    not an email address or account mapping. Keep it stable across authorization.
    """

    def __init__(
        self,
        client: GmailClient,
        namespace: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._namespace = TypeAdapter(Identifier).validate_python(namespace)
        self._client = client
        self._clock = clock

    def get_candidate(self, message_id: str) -> MessageResult:
        if not _valid_id(message_id):
            raise ValueError("invalid Gmail message id")
        try:
            message = self._client.get_message(message_id)
        except Exception as exc:
            return MessageResult(message_id, error=_failure(exc))
        try:
            if not isinstance(message, dict) or message.get("id") != message_id:
                raise InvalidMessage()
            thread_id = message.get("threadId")
            internal = message.get("internalDate")
            payload = message.get("payload")
            if (
                not _valid_id(thread_id)
                or not isinstance(internal, str)
                or not re.fullmatch(r"[0-9]{1,16}", internal)
                or not isinstance(payload, dict)
            ):
                raise InvalidMessage()
            received = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(internal))
            normalized_headers = headers(payload, "/payload")
            parts = normalize(payload)
            subjects = [h.value for h in normalized_headers if h.name == "subject"]
            subject = decoded_header(subjects[0]) if len(subjects) == 1 else None
            warnings = []
            if len(subjects) > 1:
                warnings.append("ambiguous_subject")
            senders = [h.value for h in normalized_headers if h.name == "from"]
            sender = decoded_header(senders[0]) if len(senders) == 1 else None
            if len(senders) > 1:
                warnings.append("ambiguous_sender")
            dates = [h.value for h in normalized_headers if h.name == "date"]
            sent_at = None
            if dates:
                try:
                    if len(dates) != 1:
                        raise ValueError()
                    sent_at = parsedate_to_datetime(dates[0])
                    if sent_at.tzinfo is None or sent_at.utcoffset() is None:
                        raise ValueError()
                except (ValueError, TypeError, OverflowError):
                    sent_at = None
                    warnings.append("invalid_or_ambiguous_sent_date")
            identity = hashlib.sha256(
                json.dumps([self._namespace, message_id], separators=(",", ":")).encode()
            ).hexdigest()
            observation = Observation(
                observation_id=f"gmail-{identity}",
                source_system="gmail",
                source_namespace=self._namespace,
                source_record_id=message_id,
                observed_at=self._clock(),
                content_sha256=hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            )
            return MessageResult(
                message_id,
                Candidate(
                    observation,
                    thread_id,
                    received,
                    sent_at,
                    normalized_headers,
                    parts,
                    subject,
                    sender,
                    tuple(warnings),
                ),
            )
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return MessageResult(message_id, error=IngestionError("invalid_message"))

    def fetch_page(
        self,
        *,
        query: str,
        page_token: str | None = None,
        page_size: int = 100,
        seen_ids: Collection[str] = (),
    ) -> CandidatePage:
        """List then retrieve one page. Caller follows next_page_token explicitly.

        Pass IDs already successfully processed to deduplicate across pages/runs.
        On any failure, do not advance blindly: retry the current page (passing
        successful IDs) or retain failed IDs for explicit later retry. No automatic
        retries, durable checkpoint, or Gmail snapshot/completeness guarantee.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("a nonempty candidate query is required")
        if type(page_size) is not int or not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        if page_token is not None and (not isinstance(page_token, str) or not page_token):
            raise ValueError("invalid page token")
        try:
            response = self._client.list_messages(
                query=query, page_token=page_token, page_size=page_size
            )
        except Exception as exc:
            return CandidatePage(error=_failure(exc))
        if not isinstance(response, dict) or "error" in response:
            return CandidatePage(error=IngestionError("invalid_response"))
        messages = response.get("messages", [])
        token = response.get("nextPageToken")
        if (
            not isinstance(messages, list)
            or len(messages) > page_size
            or any(not isinstance(m, dict) or not _valid_id(m.get("id")) for m in messages)
            or (
                token is not None
                and (not isinstance(token, str) or not token or token == page_token)
            )
        ):
            return CandidatePage(error=IngestionError("invalid_response"))
        seen = set(seen_ids)
        results = []
        duplicates = 0
        for item in messages:
            message_id = item["id"]
            if message_id in seen:
                duplicates += 1
                continue
            seen.add(message_id)
            results.append(self.get_candidate(message_id))
        return CandidatePage(tuple(results), token, duplicate_count=duplicates)
