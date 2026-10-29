"""Synthetic Refresh, Cards and rewards walkthrough: fictional account, mail and terms.

A fake Gmail transport answers each query from its time window, newest first. The notices
below are invented: two for one fictional card, one wallet transfer and one plain notice.
The ledger starts with one fictional product and reward rule. No real provider, model,
sign-in or ledger is touched.
"""

import argparse
import base64
import json
import re
import tempfile
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import uvicorn
from ui_preview import preview_app

from spending_optimiser.document import SchemaDocument
from spending_optimiser.gmail import GmailRestClient
from spending_optimiser.monthly import SINGAPORE

CARD = (
    "Card Transaction Alert\nTransaction Ref: PREVIEW-CARD-{ref}\n"
    "We refer to your card transaction request dated 03/02/25. "
    "We are pleased to confirm that the transaction was completed.\n"
    "Date & Time: 03 FEB 09:15 (SGT)\nAmount: SGD{amount}\n"
    "From: DBS/POSB card ending 4071\nTo: {payee}\n"
)
WALLET = (
    "Transaction Alerts\nTransaction Ref: PREVIEW-WALLET-A\n"
    "We refer to your PayLah! Scan & Pay Transfer dated 03 Feb. "
    "We are pleased to confirm that the transaction was completed.\n"
    "Date & Time:\n03 Feb 2025 09:15 (SGT)\nAmount:\nSGD41.20\n"
    "From:\nPayLah! Wallet (Mobile ending 5520)\nTo:\nFictional Recipient\n"
)
MAIL = (
    ("card-1", 2, CARD.format(ref="A", amount="18.60", payee="Fictional Bookshop")),
    ("card-2", 3, CARD.format(ref="B", amount="7.40", payee="Fictional Bakery")),
    ("wallet-1", 4, WALLET),
    ("notice-1", 5, "Fictional newsletter. No financial facts are stated."),
)
TERMS = {
    "products": [{"product_id": "preview-product", "supplied_name": "Preview Rewards Card"}],
    "rules": [
        {
            "rule_id": "preview-rule",
            "revision_id": "preview-rule-v1",
            "product_id": "preview-product",
            "compiled_at": "2025-01-02T12:00:00+08:00",
            "effective": {
                "status": "proposed",
                "value": {"start": "2025-01-01", "end": "2026-01-01"},
            },
            "date_basis": {"status": "proposed", "value": "transaction_date"},
            "eligibility": {"status": "not_applicable", "note": "Invented terms state none"},
            "minimum_spend": {"status": "not_applicable", "note": "Invented terms state none"},
            "caps": {"status": "not_applicable", "note": "Invented terms state none"},
            "exclusions": {"status": "not_applicable", "note": "Invented terms state none"},
            "rounding": {"status": "not_applicable", "note": "Invented terms state none"},
            "earning": {
                "status": "proposed",
                "value": [
                    {
                        "rate_id": "base",
                        "component": "base",
                        "reward": {
                            "status": "proposed",
                            "value": {
                                "unit": {"unit_id": "preview-points", "kind": "points"},
                                "quantity": "3",
                            },
                        },
                        "per_spend": {
                            "status": "proposed",
                            "value": {"currency": "SGD", "minor_units": 100},
                        },
                        "eligibility": {"status": "not_applicable", "note": "Invented terms"},
                    }
                ],
            },
        }
    ],
}


class WindowedTransport:
    """Gmail search over the invented mail, honouring after:/before: and newest first."""

    def __init__(self, now=None):
        now = now or datetime.now(SINGAPORE)
        self.received = {
            key: int((now - timedelta(minutes=minutes)).timestamp() * 1000)
            for key, minutes, _ in MAIL
        }
        self.bodies = {key: body for key, _, body in MAIL}
        self.requests = []

    def get_json(self, url, headers):
        parsed = urlparse(url)
        self.requests.append(parsed.path)
        if parsed.path.endswith("/messages"):
            query = parse_qs(parsed.query)["q"][0]
            after, before = (int(n) for n in re.findall(r":(\d+)", query))
            found = sorted(
                (k for k, at in self.received.items() if after < at // 1000 < before),
                key=lambda k: -self.received[k],
            )
            return {"messages": [{"id": key} for key in found]}
        identity = parsed.path.rsplit("/", 1)[-1]
        raw = self.bodies[identity].encode()
        return {
            "id": identity,
            "threadId": "synthetic-thread",
            "internalDate": str(self.received[identity]),
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Synthetic " + identity},
                    {"name": "From", "value": "Fictional Sender <example@example.invalid>"},
                ],
                "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
            },
        }


def refresh_app(directory):
    app = preview_app(directory, populated=False, ai_signed_in=False)
    service = app.state.gmail
    service.connection = service.oauth.connection()  # fixture connection, not a real sign-in
    transport = WindowedTransport()
    service.client_factory = lambda tokens: GmailRestClient(tokens, transport)
    app.state.synthetic_transport = transport
    app.state.ledger.import_document(SchemaDocument.model_validate(TERMS).model_dump_json())
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    if args.port == 8765 or not 1024 <= args.port <= 65535:
        parser.error("Use a separate unused loopback port from 1024-65535, never 8765")
    with tempfile.TemporaryDirectory(prefix="spending-refresh-synthetic-") as directory:
        app = refresh_app(directory)
        print(f"Synthetic only: http://127.0.0.1:{args.port}/gmail", flush=True)
        print("State:", json.dumps({"mail": len(MAIL)}), flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
