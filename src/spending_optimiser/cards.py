"""Card and wallet references stated by notifications, and the product a person chose for each.

A reference is evidence only. A product is attached by an explicit, correctable choice kept
beside the ledger, never inferred from a suffix or sender and never written into source facts.
"""

import re
from datetime import UTC, datetime

from .rules import CardProduct

# Names only, as the person supplied them (see summary.md). Terms are never invented here.
CATALOGUE = (
    ("maybank-unresolved", "Maybank Rewards XL"),
    ("dbs-unresolved", "DBS yuu Credit Card Visa"),
    ("trust-unresolved", "Trust Freedom Card"),
)
# Reference prefixes assigned from an explicit "From:" line of a completed-payment notice.
SOURCES = (
    ("issuer-dbsposb-cnf-", "card", "dbsposb", "DBS/POSB card ending {}"),
    ("paylah-notice-ref-", "wallet", "paylah", "PayLah! wallet ending {}"),
)


def describe(reference):
    """The card or wallet an account reference states, or None for any other account."""
    for prefix, kind, issuer, label in SOURCES:
        found = re.fullmatch(re.escape(prefix) + r"([0-9]{4})", reference or "")
        if found:
            return {
                "kind": kind,
                "issuer": issuer,
                "suffix": found[1],
                "label": label.format(found[1]),
            }
    return None


def product_options(document):
    """Ledger products first, then supplied names the ledger does not hold yet."""
    options = {p.product_id: p.supplied_name for p in document.products}
    for product_id, name in CATALOGUE:
        options.setdefault(product_id, name)
    return options


class CardBook:
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS card_sources (namespace TEXT NOT NULL, "
                "account_id TEXT NOT NULL, kind TEXT NOT NULL, issuer TEXT NOT NULL, "
                "suffix TEXT NOT NULL, label TEXT NOT NULL, PRIMARY KEY(namespace, account_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS card_choices (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "namespace TEXT NOT NULL, account_id TEXT NOT NULL, product_id TEXT NOT NULL, "
                "chosen_at TEXT NOT NULL)"
            )

    @staticmethod
    def note(db, namespace, account_id, reference):
        """Inside the caller's transaction. Repeated sightings change nothing."""
        found = describe(reference)
        if found is not None:
            db.execute(
                "INSERT OR IGNORE INTO card_sources VALUES (?, ?, ?, ?, ?, ?)",
                (
                    namespace,
                    account_id,
                    found["kind"],
                    found["issuer"],
                    found["suffix"],
                    found["label"],
                ),
            )

    def sources(self, namespace):
        """Every stated card or wallet of one mailbox: account_id -> kind, issuer, suffix, label."""
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT account_id, kind, issuer, suffix, label FROM card_sources "
                "WHERE namespace=? ORDER BY label, account_id",
                (namespace,),
            ).fetchall()
        return {
            account_id: {"kind": kind, "issuer": issuer, "suffix": suffix, "label": label}
            for account_id, kind, issuer, suffix, label in rows
        }

    def choices(self, namespace):
        """The latest chosen product of each card of one mailbox."""
        with self.ledger.connection() as db:
            rows = db.execute(
                "SELECT account_id, product_id FROM card_choices WHERE namespace=? ORDER BY seq",
                (namespace,),
            ).fetchall()
        return dict(rows)

    def history(self, namespace, account_id):
        with self.ledger.connection() as db:
            return db.execute(
                "SELECT product_id, chosen_at FROM card_choices WHERE namespace=? "
                "AND account_id=? ORDER BY seq",
                (namespace, account_id),
            ).fetchall()

    def choose(self, namespace, account_id, product_id):
        """Save or correct the product of a card this mailbox stated. Returns False if unchanged."""
        card = self.sources(namespace).get(account_id)
        if card is None or card["kind"] != "card":
            raise ValueError("Choose a product for a card this mailbox stated")
        options = product_options(self.ledger.load())
        if product_id not in options:
            raise ValueError("Choose a card from the list")
        if self.choices(namespace).get(account_id) == product_id:
            return False

        def add(db, document, human):
            if any(p.product_id == product_id for p in document.products):
                return []
            return [
                ("products", CardProduct(product_id=product_id, supplied_name=options[product_id]))
            ]

        self.ledger.write(add, origin="manual")
        with self.ledger.connection() as db:
            db.execute(
                "INSERT INTO card_choices (namespace, account_id, product_id, chosen_at) "
                "VALUES (?, ?, ?, ?)",
                (namespace, account_id, product_id, datetime.now(UTC).isoformat()),
            )
        return True
