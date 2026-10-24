"""Deterministic calendar-month projections of canonical, saved notification facts."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .common import CURRENCY_EXPONENTS
from .ledger import CORRECTION_FIELDS, heads


@dataclass(frozen=True)
class Month:
    key: str
    start: date
    end: date

    @property
    def label(self):
        return self.start.strftime("%B %Y")

    @property
    def query(self):
        # Gmail date strings use provider timezone defaults. Epoch seconds plus a
        # local half-open UTC filter avoid that ambiguity, including midnight.
        start = int(datetime.combine(self.start, datetime.min.time(), UTC).timestamp())
        end = int(datetime.combine(self.end, datetime.min.time(), UTC).timestamp())
        return f"after:{start - 1} before:{end}"


def calendar_month(value=None):
    value = date.today().strftime("%Y-%m") if value is None else value
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise ValueError("Choose a calendar month in YYYY-MM format")
    year, month = map(int, value.split("-"))
    if not 1970 <= year <= 9998:
        raise ValueError("Choose a calendar month between 1970 and 9998")
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return Month(value, start, end)


def format_money(currency, minor_units):
    exponent = CURRENCY_EXPONENTS[currency]
    sign = "-" if minor_units < 0 else ""
    whole, fraction = divmod(abs(minor_units), 10**exponent)
    number = f"{whole:,}" + (f".{fraction:0{exponent}d}" if exponent else "")
    return f"{currency} {sign}{number}"


def grouping(document, transaction):
    """Use explicit IDs, not labels as identity or guesses from email/card products."""
    t = transaction
    accounts = {a.account_id: a for a in document.accounts}
    card = t.card_id.value
    account = t.account_id.value
    channel = t.channel.value
    label = " / ".join(
        (
            f"Card {card}" if card else "Card unassigned",
            f"{accounts[account].label} ({account})" if account else "Account unassigned",
            channel.replace("_", " ") if channel else "Avenue unknown",
        )
    )
    return (card, account, channel), label


def monthly_report(document, month):
    """Never net unsigned event kinds into cashflow. Charts show purchases only."""
    rows, undated = [], []
    counts = Counter()
    excluded = Counter()
    totals = defaultdict(int)
    groups = defaultdict(int)
    daily = defaultdict(int)
    labels = {}
    for t in heads(document):
        day = t.occurred_on.value
        if day is None:
            undated.append(t)
            continue
        if not month.start <= day < month.end:
            continue
        counts[t.review.status] += 1
        group, label = grouping(document, t)
        labels[group] = label
        reason = None
        if t.review.status != "accepted":
            reason = "Rejected" if t.review.status == "rejected" else "Pending / uncertain"
        elif t.duplicate_of:
            reason = "Explicit duplicate"
        elif any(
            getattr(t, field).status == "conflicting"
            or getattr(t, field).review.status == "rejected"
            for field in CORRECTION_FIELDS
        ):
            reason = "Unresolved / rejected fact"
        elif any(
            getattr(t, field).value is None
            for field in ("kind", "amount", "occurred_on", "account_id")
        ):
            reason = "Missing required fact"
        if reason:
            excluded[reason] += 1
        else:
            money = t.amount.value
            totals[money.currency, t.kind.value] += money.minor_units
            if t.kind.value == "purchase":
                groups[money.currency, group] += money.minor_units
                daily[money.currency, day] += money.minor_units
        rows.append({"transaction": t, "group": label, "exclusion": reason})
    currencies = sorted({currency for currency, _ in totals})
    charts = []
    for currency in currencies:
        by_group = [
            {"label": labels[group], "minor_units": amount}
            for (code, group), amount in groups.items()
            if code == currency
        ]
        by_group.sort(key=lambda item: (-item["minor_units"], item["label"]))
        timeline = [
            {"label": day.isoformat(), "minor_units": amount}
            for (code, day), amount in sorted(daily.items())
            if code == currency
        ]
        charts.append(
            {
                "currency": currency,
                "groups": by_group,
                "daily": timeline,
                "maximum_group": max((r["minor_units"] for r in by_group), default=1),
                "maximum_day": max((r["minor_units"] for r in timeline), default=1),
                "totals": {
                    kind: totals[currency, kind]
                    for kind in ("purchase", "refund", "reversal", "income", "transfer")
                },
            }
        )
    rows.sort(key=lambda row: (row["transaction"].occurred_on.value, row["transaction"].event_id))
    return {
        "rows": rows,
        "undated": undated,
        "counts": counts,
        "excluded": excluded,
        "charts": charts,
        "included_count": len(rows) - sum(excluded.values()),
    }
