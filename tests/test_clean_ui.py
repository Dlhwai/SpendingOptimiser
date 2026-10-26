"""The rendered app resolves to three steps and carries no redundant helper copy."""

import importlib.util
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from spending_optimiser.ai_inputs import digest

SPEC = importlib.util.spec_from_file_location(
    "clean_ui_preview", Path(__file__).parents[1] / "examples" / "ui_preview.py"
)
PREVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREVIEW)

STEPS = ["Connect Gmail", "Inspect saved data", "Understand this month"]
STATUS_CLASSES = {"warning", "notice", "error-note"}
# Descriptive copy the captain asked to remove; none may come back in any rendered page.
REMOVED_COPY = (
    "nothing runs in the background",
    "private by design",
    "mail loads only when you ask",
    "a simpler view of",
    "your notifications, in one place",
    "take a look around",
    "first time connecting",
    "one-time google setup",
    "returning to your workspace",
    "your month, clearly",
    "connected on your terms",
    "public data. explicit action",
    "bring your data with you",
    "a human checkpoint",
    "your local ledger",
    "reference library",
    "your judgment, recorded",
    "the source, not an assumption",
    "let's get this sorted",
    "ai only on explicit analysis",
    "no background mail sync",
    "claude: unavailable",
    "gemini: unavailable",
    "start with your mail",
    "one connection. your choice",
)


class Layout(HTMLParser):
    """Rendered structure: the steps navigation, links, forms and free-standing paragraphs."""

    def __init__(self, text):
        super().__init__()
        self.stack, self.steps, self.links, self.forms = [], [], [], []
        self.paragraphs, self.words, self._p, self._link = [], [], None, None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"br", "input", "meta", "link", "hr", "img"}:
            return
        self.stack.append((tag, attrs))
        if tag == "a":
            self._link = {"href": attrs.get("href"), "current": "aria-current" in attrs, "text": ""}
            self.links.append(self._link)
        if tag == "form":
            self.forms.append(attrs)
        if tag == "p":
            exempt = any(
                t == "pre"
                or a.get("role") in {"alert", "status"}
                or STATUS_CLASSES & set(a.get("class", "").split())
                for t, a in self.stack
            )
            self._p = [exempt, []]

    def handle_data(self, data):
        if self._p is not None:
            self._p[1].append(data)
        if self._link is not None:
            self._link["text"] += data
        self.words.append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            if self._link is not None and any(
                t == "nav" and a.get("aria-label") == "Steps" for t, a in self.stack
            ):
                self.steps.append((" ".join(self._link["text"].split()), self._link))
            self._link = None
        if tag == "p" and self._p is not None:
            self.paragraphs.append((self._p[0], " ".join("".join(self._p[1]).split())))
            self._p = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    @property
    def text(self):
        return " ".join(" ".join(self.words).split())


@pytest.fixture
def journey(tmp_path):
    """A populated synthetic app: connected Gmail, saved mail, ledger, snapshots, a proposal."""
    app = PREVIEW.preview_app(tmp_path, populated=True)
    client = TestClient(app, base_url="http://127.0.0.1:8876")
    client.get("/")

    def post(path, **data):
        return client.post(path, data={"csrf": client.cookies["ledger_csrf"], **data})

    page = post("/gmail/connect", display="window")
    link = re.search(r'href="(/preview-consent[^"]+)"', page.text)[1].replace("&amp;", "&")
    state = parse_qs(urlparse(link).query)["state"][0]
    client.get("/gmail/callback", params={"state": state, "code": "synthetic"})
    post(
        "/gmail/month",
        month="2025-02",
        pages="1",
        action="continue",
        connection_id=app.state.gmail.connection_id,
    )
    ai = app.state.ai
    ai.connect()
    terms = ai.import_terms("dbs", "yuu")[0]
    ai.save_format("Card ending <CARD_SUFFIX>: <CURRENCY> <AMOUNT> at <MERCHANT>")
    proposal = post(
        "/ai/analyse",
        snapshot_id=terms,
        fingerprint=digest(ai.preview(terms)),
        connection_id=ai.connection_id,
        consent="analyse-safe-snapshot",
    )
    pages = {
        "/": 3,
        "/?month=2025-02": 3,
        "/gmail": 1,
        "/gmail?month=2025-02": 1,
        "/saved": 2,
        "/saved?month=2025-02": 2,
        "/ledger": 2,
        "/review": 2,
        "/public": 2,
        "/imports": 2,
        "/storage": 2,
        "/transaction?event_id=test-purchase": 2,
        "/ai": 3,
        f"/ai?snapshot={terms}": 3,
        str(proposal.url).replace("http://127.0.0.1:8876", ""): 3,
        "/missing": 0,
    }
    return client, pages, app


def test_primary_navigation_is_exactly_the_three_steps_on_every_page(journey):
    client, pages, _ = journey
    for path, current in pages.items():
        layout = Layout(client.get(path).text)
        numbered = [f"{number}{label}" for number, label in enumerate(STEPS, 1)]
        assert [label for label, _ in layout.steps] == numbered, path
        marked = [index + 1 for index, (_, link) in enumerate(layout.steps) if link["current"]]
        assert marked == ([current] if current else []), path
        assert [link["href"].split("?")[0] for _, link in layout.steps] == ["/gmail", "/saved", "/"]


def test_existing_capabilities_stay_reachable_through_secondary_controls(journey):
    client, _, app = journey
    saved = {link["href"] for link in Layout(client.get("/saved").text).links}
    assert {"/ledger", "/review", "/public", "/imports", "/storage"} <= {
        href.split("?")[0] for href in saved
    }
    month = {link["href"] for link in Layout(client.get("/?month=2025-02").text).links}
    assert "/ai" in month and "/ledger" in month
    ledger = Layout(client.get("/ledger").text)
    assert any(link["href"].startswith("/transaction?event_id=") for link in ledger.links)
    detail = Layout(client.get("/transaction?event_id=test-purchase").text)
    assert {form["action"] for form in detail.forms} == {"/review"}  # review and corrections
    storage = Layout(client.get("/storage?month=2025-02").text)
    assert {"/storage/compress", "/storage/restore"} <= {form["action"] for form in storage.forms}
    ai = Layout(client.get("/ai").text)
    assert any(link["href"].startswith("/ai/proposal?id=") for link in ai.links)
    assert {"/ai/import", "/ai/save"} <= {form["action"] for form in ai.forms}
    imports = Layout(client.get("/imports").text)
    assert {"/import", "/example"} == {form["action"] for form in imports.forms}
    assert app.state.ai.proposals()  # the proposal made by the journey is still listed


def test_pages_carry_no_redundant_helper_paragraphs(journey):
    client, pages, _ = journey
    for path in pages:
        layout = Layout(client.get(path).text)
        for phrase in REMOVED_COPY:
            assert phrase not in layout.text.lower(), (path, phrase)
        for exempt, text in layout.paragraphs:
            # Alerts, warnings, notices and preformatted evidence may explain; nothing else may.
            assert exempt or len(text.split()) <= 16, (path, text)
        assert not re.search(r"\bNext:", layout.text), path


def test_empty_states_stay_meaningful_without_explanations(tmp_path):
    client = TestClient(PREVIEW.preview_app(tmp_path), base_url="http://127.0.0.1:8876")
    for path, expected in (
        ("/", "0 results"),
        ("/saved", "Connect Gmail to see saved mail"),
        ("/ledger", "No transactions yet"),
        ("/review", "Nothing waiting for review"),
        ("/ai", "0 results"),
        ("/public", "0 results"),
    ):
        text = Layout(client.get(path).text).text
        assert expected in text, path
    home = Layout(client.get("/").text)
    assert any(link["href"] == "/gmail" for link in home.links)  # the next step is one click away
