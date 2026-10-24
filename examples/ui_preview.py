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

from spending_optimiser.gmail import GmailRestClient
from spending_optimiser.gmail_auth import GmailProblem, GoogleConnection, configure_google
from spending_optimiser.gmail_service import GmailService
from spending_optimiser.web import create_app


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


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


def preview_app(directory, *, configured=True, populated=False):
    """All authorization, credentials and message transport are injected fakes."""
    path = Path(directory) / "synthetic.sqlite3"
    store = MemoryStore()
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
    app = create_app(path, gmail_service=service)
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

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--unconfigured", action="store_true")
    parser.add_argument("--populated", action="store_true")
    args = parser.parse_args()
    if args.port == 8765 or not 1024 <= args.port <= 65535:
        parser.error("Use a separate loopback port from 1024-65535, never 8765")
    with tempfile.TemporaryDirectory(prefix="spending-ui-preview-") as directory:
        app = preview_app(directory, configured=not args.unconfigured, populated=args.populated)
        print(f"Synthetic preview: http://127.0.0.1:{args.port}; disposable database: {directory}")
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
