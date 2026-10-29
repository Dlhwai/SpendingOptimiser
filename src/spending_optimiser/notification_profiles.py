"""Finite reusable notification contracts, never sender/account inference.

Only explicit completed-payment structure establishes a source notification
reference. That reference is NOT an underlying bank account or card-product link.
Templates contain no customer values, source identifiers or learned executable code.
"""

import re
from dataclasses import replace
from datetime import date, timedelta, timezone

COMPLETE = r"\.\s*We\s+are\s+pleased\s+to\s+confirm\s+that\s+the\s+transaction\s+was\s+completed\."
PROFILE_CLAIM = re.compile(
    r"(?i)We\s+refer\s+to\s+your\s+(?:card\s+transaction\s+request|"
    r"PayLah!\s+Scan\s*&\s*Pay\s+Transfer)\s+dated\b"
)
PROFILE_FIELD = re.compile(
    r"(?im)^[^\S\n]*(?P<field>From|Date\s*&\s*Time|To|Transaction\s+Ref)\s*:"
)
PROFILE_RULES = (
    (
        "completed_card_dmy2",
        "card_claim_date",
        re.compile(
            r"(?i)We\s+refer\s+to\s+your\s+card\s+transaction\s+request\s+dated\s+"
            r"(?P<value>[0-9]{2}/[0-9]{2}/[0-9]{2})" + COMPLETE
        ),
    ),
    (
        "completed_wallet_transfer",
        "wallet_claim_date",
        re.compile(
            r"(?i)We\s+refer\s+to\s+your\s+PayLah!\s+Scan\s*&\s*Pay\s+Transfer\s+dated\s+"
            r"(?P<value>[0-9]{1,2}\s+[A-Za-z]{3})" + COMPLETE
        ),
    ),
    (
        "issuer_card_source",
        "card_from",
        re.compile(r"(?im)^\s*From\s*:\s*(?P<value>DBS/POSB\s+card\s+ending\s+[0-9]{4})\s*$"),
    ),
    (
        "wallet_mobile_source",
        "wallet_from",
        re.compile(
            r"(?im)^\s*From\s*:\s*(?P<value>PayLah!\s+Wallet\s*\(Mobile\s+ending\s+[0-9]{4}\))\s*$"
        ),
    ),
    (
        "notice_day_month_clock",
        "notice_clock",
        re.compile(r"(?im)^\s*Date\s*&\s*Time\s*:\s*(?P<value>[^\n\r\t]{1,128})\s*$"),
    ),
    (
        "notice_payee",
        "payee",
        re.compile(r"(?im)^\s*To\s*:\s*(?P<value>[^\n\r\t]{1,128})\s*$"),
    ),
    (
        "notice_transaction_reference",
        "notice_reference",
        re.compile(r"(?im)^\s*Transaction\s+Ref\s*:\s*(?P<value>[A-Za-z0-9_-]{1,128})\s*$"),
    ),
    (
        "administrative_notice",
        "administrative_notice",
        re.compile(
            r"(?i)(?P<value>"
            r"request\b[^\n]{0,120}\bto\s+update\s+your\s+(?:email\s+address|contact\s+details)|"
            r"your\s+email\s+address\s+has\s+been\s+updated|"
            r"your\s+latest\s+eStatement\s+is\s+ready\s+for\s+viewing|"
            r"you['’]re\s+almost\s+done\s+setting\s+Google\s+up|"
            r"recovery\s+contact\s+request|"
            r"(?:Google\s+)?verification\s+code)"
        ),
    ),
)
PROFILE_ROLES = frozenset(role for _, role, _ in PROFILE_RULES)
MONTHS = {
    name: i
    for i, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
CLOCK = re.compile(
    r"(?i)([0-9]{1,2})\s+([A-Za-z]{3})\s+(?:([0-9]{4})\s+)?"
    r"(?:[01]?[0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?\s*\(SGT\)"
)
NON_PURCHASE = re.compile(
    r"(?i)\b(?:refund|reversal|reversed|withdrawal|cash\s+advance|chargeback|"
    r"declined|failed|repayment|top.?up|loan|fee)\b"
)


def qualify(bindings, text, subject, observation, profile_context):
    """Recheck the complete contract within EACH inline representation."""

    def one(role):
        selected = [b for b in bindings if b.role == role]
        return selected[0] if len(selected) == 1 else None

    if profile_context:
        starts = {b.start for b in bindings if b.role in PROFILE_ROLES}
        if any(m.start("field") not in starts for m in PROFILE_FIELD.finditer(text)) or any(
            m.start() not in starts for m in PROFILE_CLAIM.finditer(text)
        ):
            return bindings, ("invalid_notification_contract",)
    claims = [b for b in bindings if b.role in {"card_claim_date", "wallet_claim_date"}]
    if not claims:
        return bindings, ()
    if len(claims) != 1 or re.search(
        r"(?im)^\s*(?:Fwd?:|Forwarded\s+Conversation|Begin\s+forwarded|[- ]*Forwarded\s+message)",
        (subject or "") + "\n" + text,
    ):
        return bindings, ("forwarded_or_multiple_financial_events",)
    claim = claims[0]
    card = claim.role == "card_claim_date"
    source = one("card_from" if card else "wallet_from")
    clock, payee, reference = one("notice_clock"), one("payee"), one("notice_reference")
    if (
        not all((source, clock, payee, reference))
        or len([b for b in bindings if b.role in {"card_from", "wallet_from"}]) != 1
    ):
        return bindings, ("incomplete_notification_contract",)
    if card and (
        NON_PURCHASE.search((subject or "") + "\n" + text)
        or re.search(r"(?i)(?:@|\b(?:account|wallet|transfer|ATM)\b)|^[0-9 -]+$", payee.value)
    ):
        return bindings, ("unsupported_card_movement_semantics",)
    parsed = CLOCK.fullmatch(clock.value)
    if not parsed or parsed[2].lower() not in MONTHS:
        return bindings, ("unsupported_financial_datetime",)
    day, month = int(parsed[1]), MONTHS[parsed[2].lower()]
    stated_year = int(parsed[3]) if parsed[3] else None
    updates = {}
    issues = []

    def assign(binding, role, value):
        updates[id(binding)] = replace(binding, role=role, value=value)

    suffix = re.search(r"[0-9]{4}", source.value)[0]
    # These identify only the explicit notification reference. No real account,
    # credit/debit type, bank-account ownership or CardInstance is inferred.
    assign(source, "account", ("issuer-dbsposb-cnf-" if card else "paylah-notice-ref-") + suffix)
    if card:
        assign(payee, "merchant", payee.value)
    try:
        if card:
            request_day, request_month, short_year = map(int, claim.value.split("/"))
            # Fixed, documented POSIX %y convention, NEVER a receipt-derived year.
            year = 2000 + short_year if short_year <= 68 else 1900 + short_year
            if stated_year is not None and stated_year % 100 == short_year:
                year = stated_year
            request_date = date(year, request_month, request_day)
            clock_date = date(stated_year or year, month, day)
        else:
            requested = re.fullmatch(r"([0-9]{1,2})\s+([A-Za-z]{3})", claim.value)
            if stated_year is None:
                raise LookupError
            request_date = date(stated_year, MONTHS[requested[2].lower()], int(requested[1]))
            clock_date = date(stated_year, month, day)
        assign(claim, "occurred_on", request_date)
        assign(clock, "occurred_on", clock_date)
        # This is only a contradiction guard, never a date/century substitution.
        observed_day = observation.observed_at.astimezone(timezone(timedelta(hours=8))).date()
        if max(request_date, clock_date) > observed_day:
            issues.append("completed_event_after_observation")
    except LookupError:
        issues.append("financial_event_year_not_stated")
    except (ValueError, TypeError, KeyError):
        issues.append("invalid_financial_datetime")
    # Kind cites the completed claim; direction cites the explicit From field.
    # Zero-width structural slots do not overlap independently quoted values.
    additions = [
        replace(base, role=role, value=value, value_start=base.start, value_end=base.start)
        for base, role, value in (
            (claim, "kind", "purchase" if card else "transfer"),
            (source, "direction", "outflow"),
        )
    ]
    return [updates.get(id(b), b) for b in bindings] + additions, tuple(issues)
