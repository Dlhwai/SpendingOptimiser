"""Deterministic Singapore-date projections of canonical, saved notification facts."""

import calendar
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .common import CURRENCY_EXPONENTS
from .ledger import CORRECTION_FIELDS, heads

# Asia/Singapore has used a fixed UTC+08:00 offset since 1982, before Gmail existed.
SINGAPORE = timezone(timedelta(hours=8), "SGT")
FIRST_YEAR, LAST_YEAR = 1970, 9998
MAX_MONTHS = 3
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def add_months(day, months):
    """Same day number `months` calendar months later, clamped to that month's last day."""
    year, index = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(year, index + 1, min(day.day, calendar.monthrange(year, index + 1)[1]))


@dataclass(frozen=True)
class DateRange:
    """Whole Singapore calendar days as the half-open interval [start, end).

    A calendar month is the range from its first day to the first day of the next
    month, and keeps its `YYYY-MM` key. Any other selection is keyed by its inclusive
    first and last day. The key identifies saved retrieval progress.
    """

    start: date
    end: date

    def __post_init__(self):
        # Every instance, however built, is bounded: one day up to three calendar months.
        if not (
            FIRST_YEAR <= self.start.year
            and self.start < self.end <= add_months(self.start, MAX_MONTHS)
            and self.end.year <= LAST_YEAR + 1
        ):
            raise ValueError("A period is from one day up to three calendar months")

    @property
    def last(self):
        return self.end - timedelta(days=1)

    @property
    def is_month(self):
        return self.start.day == 1 and self.end == add_months(self.start, 1)

    @property
    def key(self):
        if self.is_month:
            return self.start.strftime("%Y-%m")
        return f"{self.start.isoformat()}..{self.last.isoformat()}"

    @property
    def params(self):
        if self.is_month:
            return {"month": self.key}
        return {"start": self.start.isoformat(), "end": self.last.isoformat()}

    @property
    def months(self):
        """Every `YYYY-MM` that holds at least one selected day."""
        first = self.start.year * 12 + self.start.month - 1
        final = self.last.year * 12 + self.last.month - 1
        return tuple(f"{i // 12:04}-{i % 12 + 1:02}" for i in range(first, final + 1))

    @property
    def label(self):
        if self.is_month:
            return self.start.strftime("%B %Y")

        def short(day, year):
            return f"{day.day} {day:%b}" + (f" {day.year}" if year else "")

        if self.last == self.start:
            return short(self.start, True)
        return f"{short(self.start, self.start.year != self.last.year)} - {short(self.last, True)}"

    @property
    def query(self):
        # Gmail date strings use provider timezone defaults. Epoch seconds plus a
        # local half-open Singapore filter avoid that ambiguity, including midnight.
        start = int(datetime.combine(self.start, datetime.min.time(), SINGAPORE).timestamp())
        end = int(datetime.combine(self.end, datetime.min.time(), SINGAPORE).timestamp())
        return f"after:{start - 1} before:{end}"

    def contains(self, instant):
        return self.start <= instant.astimezone(SINGAPORE).date() < self.end


def calendar_month(value=None):
    value = datetime.now(SINGAPORE).strftime("%Y-%m") if value is None else value
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise ValueError("Choose a calendar month in YYYY-MM format")
    year, month = map(int, value.split("-"))
    if not FIRST_YEAR <= year <= LAST_YEAR:
        raise ValueError(f"Choose a calendar month between {FIRST_YEAR} and {LAST_YEAR}")
    start = date(year, month, 1)
    return DateRange(start, add_months(start, 1))


def latest_end(start):
    """Last inclusive day of the longest allowed range beginning on `start`."""
    return add_months(start, MAX_MONTHS) - timedelta(days=1)


def calendar_day(value, name):
    if not DAY.fullmatch(value):
        raise ValueError(f"Choose the {name} date in YYYY-MM-DD format")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"The {name} date is not a real calendar date") from None
    if not FIRST_YEAR <= day.year <= LAST_YEAR:
        raise ValueError(f"Choose a {name} date between {FIRST_YEAR} and {LAST_YEAR}")
    return day


def date_range(start, end):
    """Inclusive first and last day, from one day up to three calendar months.

    Three months are counted on the calendar from the first day, clamped to the end
    of a shorter month, so 31 January allows up to 29 April and 1 December allows up
    to 28 or 29 February. A month is never treated as 30 days.
    """
    first, last = calendar_day(start, "start"), calendar_day(end, "end")
    if last < first:
        raise ValueError("The end date cannot be before the start date")
    if last > latest_end(first):
        raise ValueError(
            f"Choose at most three calendar months: from {first.isoformat()} "
            f"the latest end date is {latest_end(first).isoformat()}"
        )
    return DateRange(first, last + timedelta(days=1))


def selected_range(params):
    """The period named by `?month=YYYY-MM` or `?start=...&end=...`, else this Singapore month."""
    month, start, end = (params.get(name) for name in ("month", "start", "end"))
    if month is not None and (start is not None or end is not None):
        raise ValueError("Choose a calendar month or a date range, not both")
    if start is None and end is None:
        return calendar_month(month)
    if start is None or end is None:
        raise ValueError("Choose both a start and an end date (the same date for one day)")
    return date_range(start, end)


def shortcuts(now=None):
    """Quick selections: this month, last month, and this month with the two before it."""
    today = (now or datetime.now(SINGAPORE)).astimezone(SINGAPORE).date().replace(day=1)
    return [
        ("This month", DateRange(today, add_months(today, 1))),
        ("Last month", DateRange(add_months(today, -1), today)),
        ("Last 3 months", DateRange(add_months(today, -2), add_months(today, 1))),
    ]


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


def range_report(document, period):
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
        if not period.start <= day < period.end:
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
                "maximum_group": max((r["minor_units"] for r in by_group), default=0) or 1,
                "maximum_day": max((r["minor_units"] for r in timeline), default=0) or 1,
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
