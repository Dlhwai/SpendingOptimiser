"""Synthetic automatic processing proof: empty ledger, fictional notification transport.

All narrative/HTML fixtures below are fictional, not copied from any bank or private mail.
Only Load/Continue/Refresh/Retry creates ledger facts. No real provider/model/sign-in.
"""

import argparse
import base64
import tempfile
from urllib.parse import urlparse

import uvicorn
from ui_preview import preview_app

from spending_optimiser.gmail import GmailRestClient

BASE = (
    "Dear Fictional Reader,\nYour purchase of SGD 12.34 on 3 February 2025 "
    "using account reference wallet-blue.\nMerchant: Fictional Cafe\n"
    "Thank you from the fictional service."
)
HTML = (
    "<table><tr><td>Transaction type</td><td>Refund</td></tr>"
    "<tr><td>Amount</td><td>SGD 2.00</td></tr>"
    "<tr><td>Transaction date</td><td>2025-02-03</td></tr>"
    "<tr><td>Account reference</td><td>wallet-blue</td></tr>"
    "<tr><td>Merchant</td><td>Fictional Cafe</td></tr></table>"
)
MESSAGES = {
    "purchase": BASE,
    "reuse": BASE.replace("12.34", "5.00").replace("3 February", "4 February"),
    "refund": HTML,
    "uncertain": HTML.replace("Refund", "Purchase")
    .replace("2.00", "99.00")
    .replace("<tr><td>Account reference</td><td>wallet-blue</td></tr>", ""),
    "unsupported": "Fictional unknown bank format. SGD 500? No supported financial facts.",
}


PROFILE_CARD = (
    "Card Transaction Alert\nTransaction Ref: EXAMPLE-DEMO-A\n"
    "We refer to your card transaction request dated 03/02/25. "
    "We are pleased to confirm that the transaction was completed.\n"
    "Date & Time: 03 FEB 09:15 (SGT)\nAmount: SGD7.65\n"
    "From: DBS/POSB card ending 9999\nTo: Fictional Bookshop\n"
)
PROFILE_REUSE = (
    PROFILE_CARD.replace("EXAMPLE-DEMO-A", "EXAMPLE-DEMO-B")
    .replace("7.65", "4.32")
    .replace("09:15", "10:20")
    .replace("Fictional Bookshop", "Fictional Bakery")
)
PROFILE_WALLET = (
    "Transaction Alerts\nTransaction Ref: EXAMPLE-WALLET-DEMO\n"
    "We refer to your PayLah! Scan & Pay Transfer dated 03 Feb. "
    "We are pleased to confirm that the transaction was completed.\n"
    "Date & Time:\n03 Feb 09:15 (SGT)\nAmount:\nSGD75.00\n"
    "From:\nPayLah! Wallet (Mobile ending 8888)\nTo:\nFictional Recipient\n"
)
PROFILE_MESSAGES = {
    "card": PROFILE_CARD,
    "reuse": PROFILE_REUSE,
    "wallet-uncertain": PROFILE_WALLET,
    "bundle-uncertain": PROFILE_CARD + "\n" + PROFILE_REUSE,
}


class AutomaticTransport:
    def __init__(self, messages=MESSAGES):
        self.requests = []
        self.messages = messages

    def get_json(self, url, headers):
        path = urlparse(url).path
        self.requests.append(path)
        if path.endswith("/messages"):
            return {"messages": [{"id": key} for key in self.messages]}
        identity = path.rsplit("/", 1)[-1]
        raw = self.messages[identity].encode()
        return {
            "id": identity,
            "threadId": "synthetic-thread",
            "internalDate": "1739145600000",
            "payload": {
                "mimeType": "text/html" if identity in {"refund", "uncertain"} else "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Synthetic " + identity},
                    {"name": "From", "value": "Fictional Sender <example@example.invalid>"},
                ],
                "body": {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode()},
            },
        }


def automatic_app(directory, *, profiles=False):
    app = preview_app(directory, populated=False, ai_signed_in=False)
    service = app.state.gmail
    service.connection = service.oauth.connection()  # fixture connection, not a real sign-in
    transport = AutomaticTransport(PROFILE_MESSAGES if profiles else MESSAGES)
    service.client_factory = lambda tokens: GmailRestClient(tokens, transport)
    app.state.synthetic_transport = transport

    @app.get("/preview-template-status")
    def template_status():
        # Synthetic-only read contract for demonstrating actual learning, not a
        # production debug/export endpoint. Never opens a normal app database.
        with service.cache.ledger.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='mail_templates'"
            ).fetchone()
            templates = (
                db.execute("SELECT count(*) FROM mail_templates").fetchone()[0] if exists else 0
            )
            sources = (
                db.execute("SELECT count(*) FROM mail_template_sources").fetchone()[0]
                if exists
                else 0
            )
        document = app.state.ledger.load()
        return {
            "templates": templates,
            "learned_versions": sources,
            "events": len({t.event_id for t in document.transactions}),
            "revisions": len(document.transactions),
        }

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8897)
    parser.add_argument(
        "--profiles", action="store_true", help="Use minimal invented qualified-profile fixtures"
    )
    args = parser.parse_args()
    if args.port == 8765 or not 1024 <= args.port <= 65535:
        parser.error("Use a separate unused loopback port from 1024-65535, never 8765")
    with tempfile.TemporaryDirectory(prefix="spending-auto-synthetic-") as directory:
        app = automatic_app(directory, profiles=args.profiles)
        print(f"Synthetic only: http://127.0.0.1:{args.port}/gmail?month=2025-02", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
