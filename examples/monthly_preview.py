"""Synthetic period desktop preview. Disposable SQLite, fake Gmail, no AI or keychain."""

import argparse
import tempfile
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import uvicorn
from monthly_fixture import monthly_document
from ui_preview import PreviewTransport, preview_app

from spending_optimiser.gmail import GmailRestClient
from spending_optimiser.monthly import SINGAPORE, calendar_month

# Fictional mail around February 2025, served only when a requested window contains it.
OTHER_MAIL = {
    "range-jan-31": datetime(2025, 1, 31, 23, 30, tzinfo=SINGAPORE),
    "range-mar-01": datetime(2025, 3, 1, 0, 30, tzinfo=SINGAPORE),
    "range-mar-15": datetime(2025, 3, 15, 12, tzinfo=SINGAPORE),
    "range-apr-10": datetime(2025, 4, 10, 12, tzinfo=SINGAPORE),
}
FEBRUARY = datetime(2025, 2, 1, 8, tzinfo=SINGAPORE)
RECEIVED = {
    **{f"preview-{i}": FEBRUARY + timedelta(hours=i) for i in range(5)},
    **OTHER_MAIL,
}


def window(query):
    after, before = (int(part.split(":")[1]) for part in query.split())
    return after + 1, before


class MonthlyTransport(PreviewTransport):
    def get_json(self, url, headers):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        identity = parsed.path.rsplit("/", 1)[-1]
        if parsed.path.endswith("/messages"):
            token = query.get("pageToken", [None])[0]
            if calendar_month("2025-02").query == query.get("q", [""])[0]:
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
            first, last = window(query.get("q", ["after:0 before:0"])[0])
            found = sorted(
                (when, name) for name, when in RECEIVED.items() if first <= when.timestamp() < last
            )
            page = int(token or 0)
            result = {"messages": [{"id": name} for _, name in found[page : page + 2]]}
            if page + 2 < len(found):
                result["nextPageToken"] = str(page + 2)
            return result if result["messages"] else {}
        reused = identity in OTHER_MAIL or identity == "preview-4"
        message = super().get_json(url.replace(identity, "preview-0") if reused else url, headers)
        message["id"] = identity
        if identity in OTHER_MAIL:
            message["internalDate"] = str(int(OTHER_MAIL[identity].timestamp() * 1000))
        return message


def monthly_app(directory, state="partial"):
    app = preview_app(directory)
    if state != "empty":
        app.state.ledger.import_document(monthly_document().model_dump_json())
    service = app.state.gmail
    service.connection = service.oauth.connection()
    service.notice = "Synthetic connection. No mail fetched. Choose a month or a date range."
    service.client_factory = lambda tokens: GmailRestClient(tokens, MonthlyTransport())
    if state != "empty":
        service.load_range(
            calendar_month("2025-02"),
            1 if state == "partial" else 3,
            service.connection_id,
            "continue",
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
