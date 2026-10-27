"""Explicit, bounded retrieval of public terms from a closed set of official issuer origins.

Not a crawler, search broker or monitor: it runs only inside an explicit import request,
makes at most MAX_FETCHES same-issuer HTTPS requests (redirects included) with a
shared IMPORT_SECONDS socket-I/O deadline, and sends no query anywhere. System
hostname resolution uses the OS resolver's timeout and can exceed that deadline.
"""

import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

import certifi

# Official origins checked against each issuer's own site on 25 October 2026.
ISSUERS = {
    "dbs": {
        "name": "DBS Bank Singapore",
        "hosts": ("www.dbs.com.sg", "dbs.com.sg"),
        "start": ("https://www.dbs.com.sg/personal/cards/credit-cards/dbs-yuu-cards",),
    },
    "maybank": {
        "name": "Maybank Singapore",
        "hosts": ("www.maybank2u.com.sg",),
        "start": (
            "https://www.maybank2u.com.sg/en/personal/cards/credit/index.page",
            "https://www.maybank2u.com.sg/en/personal/about_us/others/cards-terms-and-conditions.page",
        ),
    },
    "trust": {
        "name": "Trust Bank Singapore",
        "hosts": ("trustbank.sg",),
        "start": ("https://trustbank.sg/legal/general-terms-and-conditions/",),
    },
}
MAX_FETCHES = 8
MAX_REDIRECTS = 3
MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 24_000
MAX_RESULTS = 3
TIMEOUT = 15
IMPORT_SECONDS = 45
SKIP = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "table"}


class FetchProblem(ValueError):
    pass


class Budget:
    """One shared request count and monotonic deadline for a whole import."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.deadline, self.fetches = clock, clock() + IMPORT_SECONDS, MAX_FETCHES

    def exhausted(self):
        return self.fetches <= 0 or self.clock() >= self.deadline

    def remaining(self):
        left = self.deadline - self.clock()
        if left <= 0:
            raise FetchProblem("Import time budget exhausted")
        return left

    def spend(self):
        self.remaining()
        if self.fetches <= 0:
            raise FetchProblem("Import request budget exhausted")
        self.fetches -= 1


@dataclass(frozen=True)
class Document:
    issuer: str
    final_url: str
    content_type: str
    text: str
    links: tuple[tuple[str, str], ...]
    body: str


def keywords(query):
    if not isinstance(query, str) or not re.fullmatch(r"[A-Za-z][A-Za-z &'-]{1,59}", query):
        raise ValueError("Describe the public product in 2-60 letters, without numbers or IDs")
    words = [w for w in re.findall(r"[a-z]{3,}", query.lower()) if w not in {"and", "the"}]
    if not words:
        raise ValueError("Use at least one product word of three or more letters")
    return words[:6]


def official(issuer, url, base=None):
    try:
        url = url if base is None else urljoin(base, url)
        parts = urlsplit(url)
        port, host = parts.port, parts.hostname
    except ValueError:
        raise FetchProblem("Destination URL is malformed") from None
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or port not in {None, 443}
        or host not in ISSUERS[issuer]["hosts"]
    ):
        raise FetchProblem("Destination is not an official HTTPS origin for this issuer")
    return urldefrag(url)[0]


def deadline_socket(base, budget):
    """Socket class whose every blocking send/receive is limited by the import deadline."""

    class Bounded(base):
        def _limit(self):
            self.settimeout(min(TIMEOUT, budget.remaining()))

        def recv(self, *args):
            self._limit()
            return super().recv(*args)

        def recv_into(self, *args):
            self._limit()
            return super().recv_into(*args)

        def sendall(self, *args):
            self._limit()
            return super().sendall(*args)

    return Bounded


def read_body(response, budget):
    """Bounded chunks; the socket itself enforces the import deadline on each read."""
    chunks, size = [], 0
    while True:
        budget.remaining()
        chunk = response.read1(65536)
        if not chunk:
            if response.length not in {None, 0}:
                raise FetchProblem("Official document body is incomplete")
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_BYTES:
            raise FetchProblem("Official document exceeds the retrieval bound")
        chunks.append(chunk)


def https_get(url, budget, resolve=socket.getaddrinfo):
    """One GET to a resolved public address, no proxy, cookies, credentials or redirects."""
    parts = urlsplit(url)
    host = parts.hostname
    try:
        addresses = {info[4][0] for info in resolve(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        raise FetchProblem("Official host could not be resolved") from None
    if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise FetchProblem("Official host resolved to a private or local address")
    context = ssl.create_default_context(cafile=certifi.where())
    context.sslsocket_class = deadline_socket(ssl.SSLSocket, budget)

    class Pinned(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection(
                (sorted(addresses)[0], 443), min(TIMEOUT, budget.remaining())
            )
            sock.settimeout(min(TIMEOUT, budget.remaining()))
            self.sock = self._context.wrap_socket(sock, server_hostname=host)

    connection = Pinned(host, 443, context=context)
    try:
        connection.request(
            "GET",
            (parts.path or "/") + ("?" + parts.query if parts.query else ""),
            headers={
                "User-Agent": "SpendingOptimiser public-terms importer",
                "Accept": "text/html,text/plain",
            },
        )
        response = connection.getresponse()
        body = read_body(response, budget)
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    except (OSError, http.client.HTTPException):
        raise FetchProblem("Official document retrieval failed") from None
    finally:
        connection.close()


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.links, self.skip, self.href, self.anchor = [[]], [], 0, None, []
        self.body = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag in BLOCK:
            self.lines.append([])
        elif tag == "a":
            self.href, self.anchor = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK:
            self.lines.append([])
        elif tag == "a" and self.href:
            self.links.append((self.href, " ".join(self.anchor)))
            self.href = None

    def handle_data(self, data):
        if not self.skip:
            self.lines[-1].append(data)
            if self.href:
                self.anchor.append(data.strip())
            else:
                self.body.append(data)


def clean(text):
    lines = (" ".join("".join(c for c in line if ord(c) >= 32).split()) for line in text)
    return "\n".join(line for line in lines if line)


def retrieve(issuer, url, transport, budget):
    url = official(issuer, url)
    for _ in range(MAX_REDIRECTS + 1):
        budget.spend()
        status, headers, body = transport(url, budget)
        if status in {301, 302, 303, 307, 308}:
            url = official(issuer, headers.get("location", ""), base=url)
            continue
        if status != 200:
            raise FetchProblem(f"Official document returned HTTP {status}")
        if len(body) > MAX_BYTES:
            raise FetchProblem("Official document exceeds the retrieval bound")
        kind, _, params = headers.get("content-type", "").partition(";")
        kind = kind.strip().lower()
        if kind not in {"text/html", "text/plain"}:
            raise FetchProblem(f"Unsupported document type {kind or 'unknown'}")
        match = re.search(r"charset=([A-Za-z0-9_-]+)", params)
        try:
            decoded = body.decode(match[1] if match else "utf-8", errors="replace")
        except LookupError:
            raise FetchProblem("Unsupported document encoding") from None
        if kind == "text/plain":
            text = clean(decoded.splitlines())
            return Document(issuer, url, kind, text, (), text)
        page = _Page()
        try:
            page.feed(decoded)
            page.close()
        except (ValueError, AssertionError):
            raise FetchProblem("Official document HTML is malformed") from None
        links = []
        for href, label in page.links:
            try:
                links.append((official(issuer, href, base=url), label))
            except FetchProblem:
                continue
        text = clean("".join(p) for p in page.lines)
        return Document(issuer, url, kind, text, tuple(links), " ".join(page.body))
    raise FetchProblem("Too many redirects")


def discover(issuer, query, transport, clock=time.monotonic):
    """Best-first, same-issuer link following from the official start pages."""
    if issuer not in ISSUERS:
        raise ValueError("Choose an issuer from the official source collection")
    words = keywords(query)

    def score(url, label):
        haystack = (url + " " + label).lower()
        return sum(w in haystack for w in words) * 2 + any(
            t in haystack for t in ("terms", "conditions", "tnc")
        )

    queue = [(10, url) for url in ISSUERS[issuer]["start"]]
    seen, found, problems = set(), [], []
    budget = Budget(clock)
    while queue and len(found) < MAX_RESULTS and not budget.exhausted():
        queue.sort(key=lambda item: -item[0])
        _, url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            document = retrieve(issuer, url, transport, budget)
        except FetchProblem as exc:
            problems.append(f"{url}: {exc}")
            continue
        seen.add(document.final_url)
        text = document.body.lower()
        if all(w in text for w in words) and any(t in text for t in ("terms", "conditions")):
            if len(document.text) > MAX_TEXT:
                problems.append(f"{document.final_url}: too large for safe analysis")
            elif document.final_url not in {d.final_url for d in found}:
                found.append(document)
        queue += [(score(link, label), link) for link, label in document.links if link not in seen]
    complete = not queue or len(found) >= MAX_RESULTS
    return found, problems, complete
