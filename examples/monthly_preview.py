"""Synthetic monthly desktop preview. Disposable SQLite, fake Gmail, no AI or keychain."""

import argparse
import tempfile
from urllib.parse import parse_qs, urlparse

import uvicorn
from monthly_fixture import monthly_document
from ui_preview import PreviewTransport, preview_app

from spending_optimiser.gmail import GmailRestClient


class MonthlyTransport(PreviewTransport):
    def get_json(self, url, headers):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/messages"):
            if "after:1738367999 before:1740787200" != query.get("q", [""])[0]:
                return {}
            token = query.get("pageToken", [None])[0]
            return {
                None: {
                    "messages": [{"id": "preview-0"}, {"id": "preview-1"}],
                    "nextPageToken": "second",
                },
                "second": {
                    "messages": [{"id": "preview-2"}, {"id": "preview-3"}],
                    "nextPageToken": "last",
                },
                "last": {"messages": [{"id": "preview-1"}, {"id": "preview-4"}]},
            }[token]
        message = super().get_json(url.replace("preview-4", "preview-0"), headers)
        message["id"] = parsed.path.rsplit("/", 1)[-1]
        return message


def monthly_app(directory, state="partial"):
    app = preview_app(directory)
    if state != "empty":
        app.state.ledger.import_document(monthly_document().model_dump_json())
    service = app.state.gmail
    service.connection = service.oauth.connection()
    service.notice = "Synthetic connection. No mail fetched. Choose a calendar month."
    service.client_factory = lambda tokens: GmailRestClient(tokens, MonthlyTransport())
    if state != "empty":
        service.load_month(
            "2025-02", 1 if state == "partial" else 3, service.connection_id, "continue"
        )
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--state", choices=("empty", "partial", "full"), default="partial")
    args = parser.parse_args()
    if args.port == 8765 or not 1024 <= args.port <= 65535:
        parser.error("Use a separate loopback port from 1024-65535, never 8765")
    with tempfile.TemporaryDirectory(prefix="spending-monthly-preview-") as directory:
        print(f"SYNTHETIC ONLY: http://127.0.0.1:{args.port}/?month=2025-02")
        print("Fictional saved ledger, not AI-extracted mail. Database deleted on exit.")
        uvicorn.run(
            monthly_app(directory, args.state),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            proxy_headers=False,
        )


if __name__ == "__main__":
    main()
