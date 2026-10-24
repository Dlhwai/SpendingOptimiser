"""Strict normalization of Gmail format=full payloads, not RFC822 wire messages."""

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from email.errors import MessageError
from email.header import decode_header
from email.message import Message
from html.parser import HTMLParser

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PARTS = 200
MAX_DEPTH = 20


class InvalidMessage(ValueError):
    """A candidate must not be handed downstream with silently missing content."""


@dataclass(frozen=True)
class Header:
    name: str
    value: str = field(repr=False)
    locator: str


@dataclass(frozen=True)
class TextPart:
    mime_type: str
    locator: str
    text: str = field(repr=False)
    content_sha256: str


def headers(payload: dict, locator: str) -> tuple[Header, ...]:
    items = payload.get("headers", [])
    if not isinstance(items, list):
        raise InvalidMessage()
    result = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise InvalidMessage()
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise InvalidMessage()
        # Preserve duplicates and original values. These are untrusted claims.
        if name.lower() in {"from", "to", "subject", "date", "message-id", "content-type"}:
            result.append(Header(name.lower(), value, f"{locator}/headers/{i}/value"))
    return tuple(result)


def decoded_header(value: str) -> str:
    try:
        return "".join(
            part.decode(charset or "ascii", errors="strict") if isinstance(part, bytes) else part
            for part, charset in decode_header(value)
        )
    except (LookupError, UnicodeError, ValueError, MessageError) as exc:
        raise InvalidMessage() from exc


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "template"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "tr", "li", "table", "hr"}:
            self.chunks.append("\n")
        if not self.hidden and tag in {"td", "th"}:
            self.chunks.append("\t")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head", "template"} and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in {"p", "div", "tr", "li", "table"}:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.chunks.append(data)


def normalize(payload: dict) -> tuple[TextPart, ...]:
    """Keep every inline text alternative with JSON-pointer provenance.

    Gmail has already decoded MIME Content-Transfer-Encoding; body.data adds
    base64url encoding. Do not apply quoted-printable/base64 decoding twice.
    Attachments are not fetched, including text attachments. Unavailable inline
    text fails closed rather than returning a partial candidate.
    """
    count = 0
    total = 0
    result: list[TextPart] = []

    def visit(part: dict, pointer: str, depth: int):
        nonlocal count, total
        count += 1
        if count > MAX_PARTS or depth > MAX_DEPTH or not isinstance(part, dict):
            raise InvalidMessage()
        mime = part.get("mimeType")
        if not isinstance(mime, str):
            raise InvalidMessage()
        mime = mime.lower()
        part_headers = headers(part, pointer)
        filename = part.get("filename", "")
        if not isinstance(filename, str):
            raise InvalidMessage()
        # Inspect disposition even though it is not exported as extraction evidence.
        raw_headers = part.get("headers", [])
        attachment = any(
            h["name"].lower() == "content-disposition"
            and h["value"].split(";", 1)[0].strip().lower() == "attachment"
            for h in raw_headers
        )
        if filename or attachment:
            return
        content_types = [h.value for h in part_headers if h.name == "content-type"]
        if len(content_types) > 1:
            raise InvalidMessage()
        content_type = Message()
        content_type["Content-Type"] = content_types[0] if content_types else mime
        if content_type.get_content_type() != mime:
            raise InvalidMessage()
        if mime.startswith("multipart/"):
            children = part.get("parts")
            if not isinstance(children, list) or not children:
                raise InvalidMessage()
            for i, child in enumerate(children):
                visit(child, f"{pointer}/parts/{i}", depth + 1)
            return
        if mime not in {"text/plain", "text/html"}:
            return
        if part.get("parts"):
            raise InvalidMessage()
        body = part.get("body")
        if not isinstance(body, dict) or "attachmentId" in body:
            raise InvalidMessage()
        # Gmail can omit data for a genuinely empty part. A nonzero size still fails below.
        data, size = body.get("data", ""), body.get("size")
        if (
            not isinstance(data, str)
            or type(size) is not int
            or size < 0
            or size > MAX_BODY_BYTES
            or len(data) > (MAX_BODY_BYTES * 4 // 3 + 4)
        ):
            raise InvalidMessage()
        try:
            # b64decode's altchars also accepts '+'/'/'; Gmail's contract is URL-safe only.
            if any(
                c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_="
                for c in data
            ):
                raise InvalidMessage()
            raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise InvalidMessage() from exc
        total += len(raw)
        if len(raw) != size or total > MAX_BODY_BYTES:
            raise InvalidMessage()
        try:
            text = raw.decode(content_type.get_content_charset() or "utf-8", errors="strict")
        except (LookupError, UnicodeError) as exc:
            raise InvalidMessage() from exc
        if mime == "text/html":
            parser = _HTMLText()
            try:
                parser.feed(text)
                parser.close()
            except (ValueError, AssertionError) as exc:
                raise InvalidMessage() from exc
            text = "".join(parser.chunks)
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        result.append(TextPart(mime, f"{pointer}/body/data", text, hashlib.sha256(raw).hexdigest()))

    visit(payload, "/payload", 0)
    if not result or not any(part.text for part in result):
        raise InvalidMessage()
    return tuple(result)
