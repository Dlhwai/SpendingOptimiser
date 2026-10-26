"""Disposable, synthetic-only browser preview. No native credentials or Google network."""

import argparse
import base64
import json
import secrets
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse
from google.oauth2.credentials import Credentials

from spending_optimiser.ai_inputs import MARKER, stated_amounts
from spending_optimiser.ai_service import AIService
from spending_optimiser.dotenv_store import DotenvFile
from spending_optimiser.gmail import GmailRestClient
from spending_optimiser.gmail_auth import EnvStore, GmailProblem, GoogleConnection, configure_google
from spending_optimiser.gmail_service import GmailService
from spending_optimiser.ledger import Ledger
from spending_optimiser.web import create_app

PUBLIC_QUOTE = (
    "Earn bonus yuu Points with your DBS yuu Card when you spend S$800 monthly, "
    "and spend at 4 different participating merchants such as SimplyGo, Cold Storage, "
    "foodpanda and Guardian."
)
PREVIEW_PAGES = {
    "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-yuu-cards": (
        "<html><body><h1>SYNTHETIC PREVIEW COPY</h1><p>DBS yuu Card</p>"
        '<a href="/personal/cards/credit-cards/dbs-yuu-terms">yuu card terms and conditions</a>'
        "</body></html>"
    ),
    "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-yuu-terms": (
        "<html><body><h1>Synthetic preview of yuu terms and conditions</h1>"
        f"<p>{PUBLIC_QUOTE}</p><script>ignored()</script></body></html>"
    ),
}


class PreviewPublicTransport:
    """Fake official-site transport. Serves synthetic pages; never opens a socket."""

    def __init__(self, pages=None):
        self.pages = dict(PREVIEW_PAGES if pages is None else pages)
        self.requests = []

    def __call__(self, url, budget):
        self.requests.append(url)
        page = self.pages.get(url)
        if page is None:
            return 404, {"content-type": "text/html"}, b""
        if isinstance(page, tuple):
            return page
        return 200, {"content-type": "text/html; charset=utf-8"}, page.encode()


PREVIEW_ROLES = (
    ("currency", "currency"),
    ("amount", "amount"),
    ("merchant", "merchant"),
    ("dmy", "date_dmy"),
    ("date", "date_iso"),
    ("card", "card_suffix"),
)


class PreviewAIClient:
    """Fake cached subscription and model transport. No native client or model calls."""

    def __init__(self, binary, home, signed_in=True):
        self.account_value = (
            {"email": "ai.preview@example.invalid", "plan": "plus"} if signed_in else None
        )
        self.inputs = []
        self.output = None
        self.started = self.closed = False
        self.login_outcome = None
        self.cancelled = False

    def start(self):
        self.started = True

    def account(self):
        return self.account_value

    def login(self):
        return "/preview-ai-consent"

    def login_finished(self):
        return self.login_outcome

    def cancel_login(self):
        self.cancelled = True

    def analyse(self, text, schema):
        self.inputs.append(text)
        if self.output is not None:
            return self.output
        snapshot = json.loads(text)["saved_snapshot"]
        content = snapshot["content"]
        if snapshot["kind"] == "terms":
            amounts = stated_amounts(content)[:1]
            if not amounts:
                return json.dumps(
                    {"kind": "terms", "currency": None, "minor_units": None, "quote": None}
                )
            start, end, amount = amounts[0]
            line_start = content.rfind("\n", 0, start) + 1
            line_end = content.find("\n", end)
            return json.dumps(
                {
                    "kind": "terms",
                    "currency": amount.currency,
                    "minor_units": amount.minor_units,
                    "quote": content[line_start : None if line_end < 0 else line_end],
                }
            )
        markers = [
            {
                "marker": marker,
                "role": next((r for word, r in PREVIEW_ROLES if word in marker.lower()), None),
            }
            for marker in MARKER.findall(content)
        ]
        if any(item["role"] is None for item in markers):
            markers = None
        return json.dumps({"kind": "format", "markers": markers})

    def logout(self):
        self.account_value = None

    def close(self):
        self.closed = True


class PreviewOAuth:
    def begin(self, config, redirect):
        state = secrets.token_urlsafe(24)
        return None, "/preview-consent?" + urlencode({"state": state}), state, "synthetic"

    def finish(self, flow, code, nonce):
        if code != "synthetic":
            raise GmailProblem("oauth_failed")
        return self.connection()

    def connection(self):
        return GoogleConnection(
            "synthetic-preview-account",
            "alex.preview@example.invalid",
            Credentials("fake-access-token", refresh_token="fake-refresh-token"),
        )

    def restore(self, raw, config):
        return self.connection()

    def refresh(self, connection):
        raise AssertionError("Synthetic credentials never need network refresh")


class PreviewTransport:
    def get_json(self, url, headers):
        parsed = urlparse(url)
        subjects = [
            "Synthetic purchase notification - Orchard Coffee",
            "Synthetic card payment received",
            "Synthetic security notice - not a transaction",
            "Synthetic monthly statement is ready: " + "Long subject for layout testing " * 8,
        ]
        if parsed.path.endswith("/messages"):
            limit = int(parse_qs(parsed.query)["maxResults"][0])
            return {"messages": [{"id": f"preview-{i}"} for i in range(min(4, limit))]}
        i = int(parsed.path.rsplit("-", 1)[-1])
        body = (
            "SYNTHETIC PREVIEW ONLY\n\n"
            "Thank you for visiting Orchard Coffee. This is a fictional notification, "
            "not a ledger transaction. No financial facts have been extracted.\n\n"
            "Claimed purchase: SGD 12.34\nCard ending: 0000 (fictional)\n\n"
            "Untrusted content stays inert: <script>alert('test')</script>\n"
            "<img src='https://tracking.invalid/pixel'>\n"
        )
        if i == 3:
            body += "Long synthetic evidence line. " * 450
        raw = body.encode()
        return {
            "id": f"preview-{i}",
            "threadId": "synthetic-thread",
            "internalDate": str(1738368000000 + i * 3600000),
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": subjects[i]},
                    {
                        "name": "From",
                        "value": "Orchard Preview <notifications@example.invalid>"
                        if i != 3
                        else "very-long-synthetic-sender-" * 8 + "@example.invalid",
                    },
                    {"name": "Date", "value": "Sat, 01 Feb 2025 08:00:00 +0800"},
                ],
                "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
            },
        }


def preview_app(directory, *, configured=True, populated=False, ai_signed_in=True):
    """All authorization and message transport are injected fakes; settings use a temp .env."""
    path = Path(directory) / "synthetic.sqlite3"
    store = EnvStore(DotenvFile(Path(directory) / ".env"))
    if configured:
        configure_google(
            store,
            json.dumps(
                {
                    "installed": {
                        "client_id": "preview.apps.googleusercontent.com",
                        "client_secret": "synthetic-only",
                    }
                }
            ),
        )
    service = GmailService(
        path,
        store=store,
        oauth=PreviewOAuth(),
        client_factory=lambda tokens: GmailRestClient(tokens, PreviewTransport()),
    )
    ai = AIService(
        Ledger(path),
        binary="synthetic-client-only",
        client_factory=lambda binary, home: PreviewAIClient(binary, home, ai_signed_in),
        transport=PreviewPublicTransport(),
    )
    app = create_app(path, gmail_service=service, ai_service=ai)
    app.state.synthetic_preview = True
    if populated:
        from importlib.resources import files

        app.state.ledger.import_document(
            files("spending_optimiser").joinpath("examples/synthetic.json").read_text()
        )

    @app.get("/preview-consent", response_class=HTMLResponse)
    def consent(request: Request, state: str):
        from html import escape

        query = urlencode({"state": state, "code": "synthetic"})
        denied = urlencode({"state": state, "error": "access_denied"})
        return HTMLResponse(
            '<!doctype html><html lang="en"><title>Synthetic consent</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="font:18px system-ui;max-width:600px;margin:60px auto;padding:24px">'
            "<h1>Synthetic consent only</h1><p>This is not Google sign-in. "
            "No network or OS credential store is used. Account and mail are fictional.</p>"
            f'<p><a href="/gmail/callback?{escape(query)}">Approve synthetic connection</a></p>'
            f'<p><a href="/gmail/callback?{escape(denied)}">Deny synthetic consent</a></p>'
            "</body></html>"
        )

    def synthetic_page(title, body):
        return HTMLResponse(
            '<!doctype html><html lang="en"><title>Synthetic ChatGPT consent</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="font:18px system-ui;max-width:600px;margin:60px auto;padding:24px">'
            f"<h1>{title}</h1>{body}</body></html>"
        )

    @app.get("/preview-ai-consent", response_class=HTMLResponse)
    def ai_consent():
        # In the real flow the official client's own callback server completes this step.
        return synthetic_page(
            "Synthetic ChatGPT consent only",
            "<p>This is not OpenAI sign-in. No network or native credential is used.</p>"
            '<p><a href="/preview-ai-consent/approve">Approve synthetic sign-in</a></p>'
            '<p><a href="/preview-ai-consent/deny">Deny synthetic sign-in</a></p>',
        )

    @app.get("/preview-ai-consent/{decision}", response_class=HTMLResponse)
    def ai_decision(decision: str):
        client = app.state.ai.client
        if client is not None and decision in {"approve", "deny"}:
            if decision == "approve":
                client.account_value = {"email": "fresh.preview@example.invalid", "plan": "plus"}
            client.login_outcome = decision == "approve"
        return synthetic_page("Synthetic sign-in finished", "<p>You can close this window.</p>")

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--unconfigured", action="store_true")
    parser.add_argument("--populated", action="store_true")
    parser.add_argument("--ai-signed-out", action="store_true")
    args = parser.parse_args()
    if args.port == 8765 or not 1024 <= args.port <= 65535:
        parser.error("Use a separate loopback port from 1024-65535, never 8765")
    with tempfile.TemporaryDirectory(prefix="spending-ui-preview-") as directory:
        app = preview_app(
            directory,
            configured=not args.unconfigured,
            populated=args.populated,
            ai_signed_in=not args.ai_signed_out,
        )
        print(f"Synthetic preview: http://127.0.0.1:{args.port}; disposable database: {directory}")
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
