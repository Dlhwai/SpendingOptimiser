"""All messages and credentials below are synthetic. No network or credential stores."""

import base64
import json
import runpy
from copy import deepcopy
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from spending_optimiser import EvidenceRef, SchemaDocument
from spending_optimiser.gmail import (
    GMAIL_READONLY_SCOPE,
    AuthenticationFailure,
    ClientFailure,
    GmailAdapter,
    GmailRestClient,
    IngestionError,
    RefreshFailure,
)

NOW = datetime(2025, 2, 3, tzinfo=UTC)
PRIVATE = "SYNTHETIC_PRIVATE_DO_NOT_LOG"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Gmail tests must not open network connections")

    monkeypatch.setattr("socket.create_connection", forbidden)


def part(text="Synthetic notification: review required", mime="text/plain", charset="utf-8"):
    raw = text.encode(charset)
    return {
        "mimeType": mime,
        "headers": [{"name": "Content-Type", "value": f"{mime}; charset={charset}"}],
        "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode().rstrip("=")},
    }


def message(mid="m1", payload=None):
    payload = deepcopy(payload or part())
    payload.setdefault("headers", []).extend(
        [
            {"name": "Subject", "value": "=?utf-8?b?U3ludGhldGljIOKckw==?="},
            {"name": "From", "value": "Spoofable Bank <synthetic@example.invalid>"},
            {"name": "Date", "value": "Sun, 02 Feb 2025 08:00:00 +0800"},
        ]
    )
    return {"id": mid, "threadId": "t1", "internalDate": "1738540800000", "payload": payload}


class FakeClient:
    def __init__(self, pages=None, messages=None):
        self.pages = pages or {None: {}}
        self.messages = messages or {"m1": message()}
        self.calls = []

    def list_messages(self, **kwargs):
        self.calls.append(("list", kwargs))
        result = self.pages[kwargs["page_token"]]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def get_message(self, message_id):
        self.calls.append(("get", message_id))
        result = self.messages[message_id]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def adapter(client=None, namespace="mailbox-synthetic"):
    return GmailAdapter(client or FakeClient(), namespace, clock=lambda: NOW)


def candidate(payload):
    result = adapter(FakeClient(messages={"m1": message(payload=payload)})).get_candidate("m1")
    assert result.error is None
    assert result.candidate is not None
    return result.candidate


def test_pagination_and_repeated_ids():
    client = FakeClient(
        pages={
            None: {"messages": [{"id": "m1"}, {"id": "m1"}], "nextPageToken": "opaque-2"},
            "opaque-2": {"messages": [{"id": "m1"}, {"id": "m2"}]},
        },
        messages={"m1": message(), "m2": message("m2")},
    )
    ingest = adapter(client)
    first = ingest.fetch_page(query="subject:notification")
    assert first.successful and not first.exhausted
    assert first.candidate_count == 1 and first.duplicate_count == 1
    second = ingest.fetch_page(
        query="subject:notification", page_token=first.next_page_token, seen_ids={"m1"}
    )
    assert second.successful and second.exhausted
    assert second.candidate_count == 1 and second.duplicate_count == 1
    assert [call for call in client.calls if call[0] == "get"] == [("get", "m1"), ("get", "m2")]


def test_empty_page_can_have_continuation_and_empty_is_not_failure():
    ingest = adapter(FakeClient(pages={None: {"nextPageToken": "next"}, "next": {}}))
    first = ingest.fetch_page(query="synthetic")
    assert first.candidate_count == 0 and first.successful and not first.exhausted
    last = ingest.fetch_page(query="synthetic", page_token=first.next_page_token)
    assert last.candidate_count == 0 and last.exhausted
    failed = adapter(FakeClient(pages={None: RuntimeError(PRIVATE)})).fetch_page(query="synthetic")
    assert failed.candidate_count == 0 and not failed.successful and not failed.exhausted
    assert failed.error.code == "client_failed"
    assert PRIVATE not in repr(failed)


def test_partial_fetch_exposes_failures_and_allows_retry_without_duplicates():
    client = FakeClient(
        pages={None: {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "next"}},
        messages={
            "m1": message(),
            "m2": ClientFailure(IngestionError("message_not_found", False, 404)),
        },
    )
    ingest = adapter(client)
    page = ingest.fetch_page(query="synthetic")
    assert not page.successful and not page.exhausted and page.next_page_token == "next"
    assert page.candidate_count == 1
    assert page.results[1].error.code == "message_not_found"
    client.messages["m2"] = message("m2")
    retry = ingest.fetch_page(query="synthetic", seen_ids={"m1"})
    assert retry.successful and retry.candidate_count == 1 and retry.duplicate_count == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {"error": {"message": PRIVATE}},
        {"messages": None},
        {"messages": [{"threadId": "t1"}]},
        {"messages": [{"id": "../bad"}]},
        {"nextPageToken": ""},
        {"nextPageToken": 123},
    ],
)
def test_invalid_list_response_fails_before_get(response):
    client = FakeClient(pages={None: response})
    page = adapter(client).fetch_page(query="synthetic")
    assert page.error.code == "invalid_response" and not page.exhausted
    assert len(client.calls) == 1


def test_nonadvancing_page_token_and_overlong_page():
    page = adapter(FakeClient(pages={"same": {"nextPageToken": "same"}})).fetch_page(
        query="synthetic", page_token="same"
    )
    assert page.error.code == "invalid_response"
    page = adapter(FakeClient(pages={None: {"messages": [{"id": "m1"}, {"id": "m2"}]}})).fetch_page(
        query="synthetic", page_size=1
    )
    assert page.error.code == "invalid_response"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "x", "page_size": 0},
        {"query": "x", "page_size": True},
        {"query": "x", "page_size": 501},
        {"query": "x", "page_token": ""},
    ],
)
def test_bad_arguments_do_not_call_client(kwargs):
    client = FakeClient()
    with pytest.raises(ValueError):
        adapter(client).fetch_page(**kwargs)
    assert not client.calls


def test_stable_personal_provenance_and_transport_dates_are_separate():
    ingest = adapter()
    first = ingest.get_candidate("m1").candidate
    again = ingest.get_candidate("m1").candidate
    other_mailbox = adapter(namespace="other-synthetic").get_candidate("m1").candidate
    assert first.observation == again.observation
    assert first.observation.observation_id != other_mailbox.observation.observation_id
    assert first.observation.source_system == "gmail"
    assert first.observation.source_record_id == "m1"
    assert first.observation.observed_at == NOW
    assert first.gmail_internal_at == NOW
    assert first.sent_at == datetime(2025, 2, 2, tzinfo=UTC)
    assert first.subject == "Synthetic ✓"
    doc = SchemaDocument(observations=(first.observation,))
    assert SchemaDocument.model_validate_json(doc.model_dump_json()) == doc
    assert doc.transactions == () and doc.evidence == ()
    ref = EvidenceRef(
        target="observation",
        evidence_revision_id=first.observation.observation_id,
        locator=first.parts[0].locator,
    )
    assert ref.target == "observation"
    assert first.parts[0].locator == "/payload/body/data"


@pytest.mark.parametrize("date", ["not a date", "2 Feb 2025 08:00:00", None])
def test_missing_or_invalid_sent_date_never_becomes_financial_date(date):
    msg = message()
    msg["payload"]["headers"] = [h for h in msg["payload"]["headers"] if h["name"] != "Date"]
    if date is not None:
        msg["payload"]["headers"].append({"name": "Date", "value": date})
    result = adapter(FakeClient(messages={"m1": msg})).get_candidate("m1")
    assert result.error is None and result.candidate.sent_at is None
    assert bool(result.candidate.warnings) == (date is not None)


def test_multipart_variants_html_charset_and_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    part("Café SGD 12.34", charset="iso-8859-1"),
                    part(
                        "<html><head><title>ignore</title></head><body><p>Café &amp; tea</p>"
                        "<script>ignore</script><style>ignore</style><table><tr><td>SGD</td>"
                        "<td>12.34</td></tr></table></body></html>",
                        "text/html",
                    ),
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "synthetic.pdf",
                "body": {"attachmentId": "not-fetched", "size": 123},
            },
            {
                "mimeType": "text/plain",
                "filename": "synthetic.txt",
                "body": {"attachmentId": "not-fetched", "size": 123},
            },
            part("Additional inline evidence", "text/html"),
        ],
    }
    result = candidate(payload)
    assert len(result.parts) == 3
    assert result.parts[0].text == "Café SGD 12.34"
    assert "Café & tea" in result.parts[1].text and "SGD\t12.34" in result.parts[1].text
    assert "ignore" not in result.parts[1].text
    assert result.parts[0].locator == "/payload/parts/0/parts/0/body/data"
    assert "Additional inline evidence" in result.text


def test_related_multipart_empty_alternative_and_unicode():
    empty = part("")
    empty["body"].pop("data")
    payload = {
        "mimeType": "multipart/related",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [empty, part("通知 💳", "text/html")]},
            {"mimeType": "image/png", "body": {"attachmentId": "cid-image", "size": 100}},
        ],
    }
    result = candidate(payload)
    assert len(result.parts) == 2 and result.parts[0].text == ""
    assert result.text.strip() == "通知 💳"
    assert candidate(part("通知", charset="utf-16")).text == "通知"
    result = adapter(FakeClient(messages={"m1": message(payload=empty)})).get_candidate("m1")
    assert result.error.code == "invalid_message"


def test_transfer_encoding_is_not_decoded_twice_and_padded_base64_is_supported():
    payload = part("literal=3Dnot-an-escape\r\nnext")
    payload["body"]["data"] += "=" * (-len(payload["body"]["data"]) % 4)
    payload["headers"].append({"name": "Content-Transfer-Encoding", "value": "quoted-printable"})
    assert candidate(payload).text == "literal=3Dnot-an-escape\nnext"


def test_html_only_and_unsupported_named_attachment_are_safe():
    assert candidate(part("<p>Hello&nbsp;<b>world</b><br>SGD 1</p>", "text/html")).text == (
        "Hello\xa0world\nSGD 1"
    )
    payload = {"mimeType": "multipart/mixed", "parts": [part(), part(PRIVATE)]}
    payload["parts"][1]["headers"].append({"name": "Content-Disposition", "value": "attachment"})
    assert len(candidate(payload).parts) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["body"].update(data="%%%"),
        lambda p: p["body"].update(data="_w", size=1),  # invalid UTF-8
        lambda p: p["body"].update(size=999),  # truncated data
        lambda p: p["body"].pop("data"),
        lambda p: p["body"].update(attachmentId="missing-inline-body"),
        lambda p: p["body"].update(size=True),
        lambda p: p["body"].update(size=3 * 1024 * 1024),
        lambda p: p.update(
            headers=[{"name": "Content-Type", "value": "text/plain; charset=unknown"}]
        ),
        lambda p: p.update(headers=[{"name": "Content-Type", "value": "text/html"}]),
        lambda p: p.update(headers=[{"name": "Subject", "value": None}]),
        lambda p: p.update(mimeType="multipart/alternative"),
        lambda p: p.update(parts=[part()]),
        lambda p: p.update(mimeType="application/pdf"),
    ],
)
def test_invalid_or_incomplete_mime_never_yields_partial_candidate(mutate):
    payload = part(PRIVATE)
    mutate(payload)
    # Even a valid sibling must not escape as an apparently complete message.
    result = adapter(
        FakeClient(
            messages={
                "m1": message(
                    payload={
                        "mimeType": "multipart/mixed",
                        "parts": [part(), payload],
                    }
                )
            }
        )
    ).get_candidate("m1")
    assert result.error.code == "invalid_message" and result.candidate is None
    assert PRIVATE not in repr(result)


def test_malformed_html_parser_failure_is_an_explicit_invalid_message():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            part("Valid sibling"),
            part(f"<![unknown]>{PRIVATE}", "text/html"),
        ],
    }
    result = adapter(FakeClient(messages={"m1": message(payload=payload)})).get_candidate("m1")
    assert result.error.code == "invalid_message" and result.candidate is None
    assert PRIVATE not in repr(result)


def test_duplicate_headers_are_preserved_without_authenticity_claims():
    msg = message()
    msg["payload"]["headers"].extend(
        [
            {"name": "From", "value": "Other untrusted claim <nobody@example.invalid>"},
            {"name": "Date", "value": "Sun, 02 Feb 2025 08:00:00 +0800"},
            {"name": "Subject", "value": "Other subject"},
        ]
    )
    result = adapter(FakeClient(messages={"m1": msg})).get_candidate("m1").candidate
    assert len([h for h in result.headers if h.name == "from"]) == 2
    assert result.sent_at is None and result.subject is None
    assert set(result.warnings) == {"ambiguous_subject", "invalid_or_ambiguous_sent_date"}


def test_changed_content_keeps_identity_but_changes_evidence_digest():
    client = FakeClient()
    ingest = adapter(client)
    first = ingest.get_candidate("m1").candidate
    client.messages["m1"] = message(payload=part("Changed synthetic notice"))
    later = GmailAdapter(
        client, "mailbox-synthetic", clock=lambda: datetime(2025, 3, 1, tzinfo=UTC)
    )
    second = later.get_candidate("m1").candidate
    assert first.observation.observation_id == second.observation.observation_id
    assert first.observation.content_sha256 != second.observation.content_sha256
    assert first.observation.observed_at != second.observation.observed_at


def test_depth_and_part_count_limits():
    payload = part()
    for _ in range(22):
        payload = {"mimeType": "multipart/mixed", "parts": [payload]}
    for oversized in [payload, {"mimeType": "multipart/mixed", "parts": [part()] * 201}]:
        result = adapter(FakeClient(messages={"m1": message(payload=oversized)})).get_candidate(
            "m1"
        )
        assert result.error.code == "invalid_message"


@pytest.mark.parametrize(
    "change",
    [
        {"id": "wrong"},
        {"threadId": ""},
        {"internalDate": "wrong"},
        {"internalDate": "9999999999999999"},
        {"payload": None},
    ],
)
def test_invalid_message_metadata(change):
    msg = message()
    msg.update(change)
    result = adapter(FakeClient(messages={"m1": msg})).get_candidate("m1")
    assert result.candidate is None and result.error.code == "invalid_message"



def test_malformed_encoded_subject_is_invalid_message_without_aborting_page():
    bad = message("m2")
    for header in bad["payload"]["headers"]:
        if header["name"] == "Subject":
            header["value"] = "=?utf-8?b?abcde?="
    client = FakeClient(
        pages={None: {"messages": [{"id": "m1"}, {"id": "m2"}]}},
        messages={"m1": message(), "m2": bad},
    )
    page = adapter(client).fetch_page(query="synthetic")
    assert not page.successful and page.candidate_count == 1
    assert page.results[0].candidate is not None
    assert page.results[1].candidate is None
    assert page.results[1].error.code == "invalid_message"

class Tokens:
    def __init__(self, value="synthetic-token"):
        self.value = value
        self.calls = 0

    def access_token(self):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get_json(self, url, headers):
        self.calls.append((url, dict(headers)))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def test_real_rest_adapter_uses_only_get_readonly_paths_and_encoded_queries():
    tokens = Tokens()
    transport = FakeTransport(
        [
            {"messages": [{"id": "m1"}], "nextPageToken": "opaque/+ token"},
            message(),
            {},
        ]
    )
    client = GmailRestClient(tokens, transport)
    ingest = adapter(client)
    first = ingest.fetch_page(query="from:synthetic@example.invalid subject:(a & b)", page_size=2)
    second = ingest.fetch_page(query="synthetic", page_token=first.next_page_token)
    assert first.candidate_count == 1 and second.exhausted
    urls = [urlsplit(url) for url, _ in transport.calls]
    assert all(u.scheme == "https" and u.netloc == "gmail.googleapis.com" for u in urls)
    assert urls[0].path == "/gmail/v1/users/me/messages"
    assert parse_qs(urls[0].query) == {
        "q": ["from:synthetic@example.invalid subject:(a & b)"],
        "maxResults": ["2"],
        "includeSpamTrash": ["false"],
    }
    assert urls[1].path.endswith("/messages/m1")
    assert parse_qs(urls[1].query) == {"format": ["full"]}
    assert parse_qs(urls[2].query)["pageToken"] == ["opaque/+ token"]
    assert tokens.calls == 3
    assert all(h["Authorization"] == "Bearer synthetic-token" for _, h in transport.calls)
    assert GMAIL_READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"
    with pytest.raises(ValueError):
        client.get_message("../unsafe")
    assert len(transport.calls) == 3


@pytest.mark.parametrize(
    "failure,code",
    [
        (AuthenticationFailure(PRIVATE), "authentication_failed"),
        (RefreshFailure(PRIVATE), "refresh_failed"),
        (RuntimeError(PRIVATE), "authentication_failed"),
        ("", "authentication_failed"),
        ("token\r\nprivate", "authentication_failed"),
    ],
)
def test_authentication_and_refresh_failures_are_explicit_and_redacted(failure, code):
    transport = FakeTransport([])
    result = adapter(GmailRestClient(Tokens(failure), transport)).fetch_page(query="synthetic")
    assert result.error.code == code and not result.exhausted
    assert PRIVATE not in repr(result) and not transport.calls


def test_refreshing_provider_is_asked_for_new_token_on_each_request():
    class RefreshingTokens:
        def __init__(self):
            self.tokens = iter(["synthetic-one", "synthetic-refreshed"])

        def access_token(self):
            return next(self.tokens)

    transport = FakeTransport([{"messages": [{"id": "m1"}]}, message()])
    page = adapter(GmailRestClient(RefreshingTokens(), transport)).fetch_page(query="synthetic")
    assert page.successful
    assert [h["Authorization"] for _, h in transport.calls] == [
        "Bearer synthetic-one",
        "Bearer synthetic-refreshed",
    ]


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "authentication_failed", False),
        (403, "permission_denied", False),
        (404, "message_not_found", False),
        (429, "rate_limited", True),
        (500, "api_error", True),
        (503, "api_error", True),
        (302, "api_error", False),
    ],
)
def test_real_transport_http_errors_are_redacted(monkeypatch, status, code, retryable):
    class Opener:
        def open(self, request, timeout):
            assert request.get_method() == "GET" and timeout == 30
            raise HTTPError(request.full_url, status, PRIVATE, {}, BytesIO(PRIVATE.encode()))

    monkeypatch.setattr("spending_optimiser.gmail.build_opener", lambda *args: Opener())
    result = adapter(GmailRestClient(Tokens())).fetch_page(query=PRIVATE)
    assert result.error == IngestionError(code, retryable, status)
    assert PRIVATE not in repr(result)


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"\xff", b"x" * (8 * 1024 * 1024 + 1)])
def test_real_transport_invalid_responses(monkeypatch, raw):
    class Opener:
        def open(self, request, timeout):
            return BytesIO(raw)

    monkeypatch.setattr("spending_optimiser.gmail.build_opener", lambda *args: Opener())
    result = adapter(GmailRestClient(Tokens())).fetch_page(query="synthetic")
    assert result.error.code in {"invalid_response", "response_too_large"}


def test_real_transport_json_success_and_network_failure(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return BytesIO(json.dumps({"resultSizeEstimate": 0}).encode())

    monkeypatch.setattr("spending_optimiser.gmail.build_opener", lambda *args: Opener())
    assert adapter(GmailRestClient(Tokens())).fetch_page(query="synthetic").exhausted

    class BrokenOpener:
        def open(self, request, timeout):
            raise URLError(PRIVATE)

    monkeypatch.setattr("spending_optimiser.gmail.build_opener", lambda *args: BrokenOpener())
    result = adapter(GmailRestClient(Tokens())).get_candidate("m1")
    assert result.error == IngestionError("transport_failed", True)
    assert PRIVATE not in repr(result)


def test_redirects_are_refused_without_opening_destination():
    from spending_optimiser.gmail import _NoRedirect

    assert (
        _NoRedirect().redirect_request(None, None, 302, PRIVATE, {}, "https://example.invalid")
        is None
    )


def test_candidate_reprs_do_not_expose_mail_or_identity():
    msg = message(mid="private-message-id", payload=part(PRIVATE))
    msg["payload"]["headers"].append({"name": "To", "value": PRIVATE})
    result = adapter(FakeClient(messages={"private-message-id": msg})).get_candidate(
        "private-message-id"
    )
    assert result.error is None
    for value in [result, result.candidate, *result.candidate.headers, *result.candidate.parts]:
        assert PRIVATE not in repr(value)
        assert "private-message-id" not in repr(value)
    assert PRIVATE in result.candidate.text  # Explicit access remains private evidence.


def test_synthetic_executable_example_is_offline_and_reports_counts(capsys):
    example = Path(__file__).resolve().parents[1] / "examples" / "gmail_synthetic.py"
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(example), run_name="__main__")
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "status: synthetic_complete" in output
    assert "candidates: 2" in output and "duplicates: 1" in output
    assert "accepted_transactions: 0" in output


def test_ingestion_error_diagnostics_cannot_carry_arbitrary_private_text():
    for args in [(PRIVATE,), ("api_error", False, PRIVATE)]:
        with pytest.raises(ValueError, match="invalid ingestion error") as error:
            IngestionError(*args)
        assert PRIVATE not in str(error.value)


def test_injected_sdk_failures_never_leak_exception_text():
    for failure, code in [
        (RefreshFailure(PRIVATE), "refresh_failed"),
        (AuthenticationFailure(PRIVATE), "authentication_failed"),
        (RuntimeError(PRIVATE), "client_failed"),
    ]:
        result = adapter(FakeClient(messages={"m1": failure})).get_candidate("m1")
        assert result.error.code == code and PRIVATE not in repr(result)
    transport = FakeTransport([RuntimeError(PRIVATE)])
    result = adapter(GmailRestClient(Tokens(), transport)).fetch_page(query="synthetic")
    assert result.error.code == "transport_failed" and PRIVATE not in repr(result)
