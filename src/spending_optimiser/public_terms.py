"""Explicit, bounded retrieval of public terms from a closed set of official issuer origins.

Not a crawler, search broker or monitor: it runs only inside an explicit import request,
fetches at most MAX_FETCHES same-issuer HTTPS documents, and sends no query anywhere.
"""

import http.client
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

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
SKIP = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "table"}


class FetchProblem(ValueError):
    pass


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


def official(issuer, url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
        or parts.hostname not in ISSUERS[issuer]["hosts"]
    ):
        raise FetchProblem("Destination is not an official HTTPS origin for this issuer")
    return urldefrag(url)[0]


def https_get(url, resolve=socket.getaddrinfo):
    """One GET to a resolved public address, no proxy, cookies, credentials or redirects."""
    parts = urlsplit(url)
    host = parts.hostname
    try:
        addresses = {info[4][0] for info in resolve(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        raise FetchProblem("Official host could not be resolved") from None
    if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise FetchProblem("Official host resolved to a private or local address")

    class Pinned(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((sorted(addresses)[0], 443), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=host)

    connection = Pinned(host, 443, timeout=TIMEOUT, context=ssl.create_default_context())
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
        body = response.read(MAX_BYTES + 1)
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


def retrieve(issuer, url, transport):
    url = official(issuer, url)
    for _ in range(MAX_REDIRECTS + 1):
        status, headers, body = transport(url)
        if status in {301, 302, 303, 307, 308}:
            url = official(issuer, urljoin(url, headers.get("location", "")))
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
        page.feed(decoded)
        links = []
        for href, label in page.links:
            try:
                links.append((official(issuer, urljoin(url, href)), label))
            except (FetchProblem, ValueError):
                continue
        text = clean("".join(p) for p in page.lines)
        return Document(issuer, url, kind, text, tuple(links), " ".join(page.body))
    raise FetchProblem("Too many redirects")


def discover(issuer, query, transport):
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
    fetches = 0
    while queue and fetches < MAX_FETCHES and len(found) < MAX_RESULTS:
        queue.sort(key=lambda item: -item[0])
        _, url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        fetches += 1
        try:
            document = retrieve(issuer, url, transport)
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
