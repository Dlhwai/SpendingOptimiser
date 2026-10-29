"""Estimated card rewards from the product a person chose and the terms the ledger holds.

Only complete, unambiguous terms produce a figure. Anything missing, conflicting or
unsupported ends in a stated reason, never in an invented rate or a misleading zero. The
result is a calculation from saved facts, not an earned amount confirmed by a bank.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

from .common import CURRENCY_EXPONENTS
from .ledger import heads
from .monthly import exclusion

KNOWN = {"proposed", "verified"}
MODES = {
    "floor": ROUND_FLOOR,
    "ceiling": ROUND_CEILING,
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
}


class Unsupported(Exception):
    """The saved terms or transaction facts cannot decide this amount."""


@dataclass
class CardRewards:
    account_id: str
    label: str
    product: str | None
    status: str  # pending | no_terms | unsupported | idle | estimated
    reason: str | None = None
    spend: dict = field(default_factory=dict)  # currency -> minor units, in the period
    earned: dict = field(default_factory=dict)  # unit label -> Decimal, in the period


def known(fact, name):
    """A fact's value(s) as a tuple. Unknown or conflicting terms stop the calculation."""
    if fact.status == "not_applicable":
        return ()
    if fact.status not in KNOWN or fact.value is None:
        raise Unsupported(f"{name} not established")
    return fact.value if isinstance(fact.value, tuple) else (fact.value,)


def holds(condition, t):
    """True or False when the transaction facts decide it; Unsupported when they cannot."""
    subject = {
        "merchant": t.merchant.value,
        "mcc": t.mcc.value,
        "channel": t.channel.value,
        "currency": t.amount.value.currency,
    }.get(condition.dimension)
    if subject is None:
        raise Unsupported(f"{condition.dimension.replace('_', ' ')} not known for a purchase")
    listed = subject.casefold() in {operand.casefold() for operand in condition.operands}
    return not listed if condition.operator == "not_in" else listed


def rounded(value, rules, stage):
    chosen = [r for r in rules if r.stage == stage]
    if len(chosen) > 1:
        raise Unsupported("several rounding terms")
    if not chosen or chosen[0].mode == "none":
        return value
    rule = chosen[0]
    if rule.scope != "transaction":
        raise Unsupported("rounding over a period")
    return (value / rule.increment).quantize(Decimal(1), MODES[rule.mode]) * rule.increment


def latest_rules(document, product_id):
    superseded = {r.supersedes for r in document.rules}
    return [
        r
        for r in document.rules
        if r.product_id == product_id and r.revision_id not in superseded and r.scope == "standard"
    ]


def basis_day(t, basis):
    day = t.occurred_on.value if basis == "transaction_date" else t.posted_on.value
    if day is None:
        raise Unsupported("posting date not known")
    return day


def month_awards(rule, purchases, basis):
    """Reward of each purchase of one card in one calendar month, in order.

    Returns {event_id: (unit, Decimal)}; an entry of None means the terms say it earns nothing.
    """
    rates = known(rule.earning, "earning rates")
    requirements = known(rule.minimum_spend, "minimum spend")
    caps = known(rule.caps, "caps")
    rounding = known(rule.rounding, "rounding")
    exclusions = known(rule.exclusions, "exclusions")
    conditions = known(rule.eligibility, "eligibility")
    if not rates:
        raise Unsupported("no earning rate")
    qualifying, awards = [], {}
    for t in purchases:
        money = t.amount.value
        if not all(holds(c, t) for c in conditions) or any(
            e.affects in {"earning", "both"} and holds(e.condition, t) for e in exclusions
        ):
            awards[t.event_id] = None
            continue
        matches = []
        for rate in rates:
            reward, per_spend = rate.reward, rate.per_spend
            if reward.status not in KNOWN or per_spend.status not in KNOWN:
                raise Unsupported("rate not established")
            if per_spend.value.currency != money.currency:
                continue
            if rate.date_basis.status in KNOWN and rate.date_basis.value != basis:
                raise Unsupported("conflicting date basis")
            if all(holds(c, t) for c in known(rate.eligibility, "rate eligibility")):
                matches.append(rate)
        if len(matches) > 1:
            raise Unsupported("several rates apply")
        if not matches:
            awards[t.event_id] = None
            continue
        qualifying.append((t, matches[0]))
    # Minimum spend counts the card's whole month, not only the days being viewed.
    unmet = set()
    for requirement in requirements:
        threshold = requirement.threshold
        period = requirement.period
        if threshold.status not in KNOWN or period.status not in KNOWN:
            raise Unsupported("minimum spend not established")
        if period.value.kind != "calendar_month" or period.value.timezone != "Asia/Singapore":
            raise Unsupported("minimum spend period")
        if requirement.date_basis.status in KNOWN and requirement.date_basis.value != basis:
            raise Unsupported("conflicting date basis")
        scope = known(requirement.applies_to, "minimum spend scope")
        counted = known(requirement.qualifying_spend, "qualifying spend")
        total = sum(
            t.amount.value.minor_units
            for t in purchases
            if t.amount.value.currency == threshold.value.currency
            and all(holds(c, t) for c in counted)
        )
        if total < threshold.value.minor_units:
            unmet.update(scope)
    capped = defaultdict(Decimal)
    for t, rate in qualifying:
        if rate.rate_id in unmet:
            awards[t.event_id] = None
            continue
        money = t.amount.value
        exponent = CURRENCY_EXPONENTS[money.currency]
        spend = Decimal(money.minor_units) / 10**exponent
        unit_spend = Decimal(rate.per_spend.value.minor_units) / 10**exponent
        quantity = rate.reward.value.quantity
        reward_unit = rate.reward.value.unit
        for cap in caps:
            if cap.period.status not in KNOWN or cap.limit.status not in KNOWN:
                raise Unsupported("cap not established")
            if cap.period.value.kind != "calendar_month":
                raise Unsupported("cap period")
            if rate.rate_id not in known(cap.applies_to, "cap scope"):
                continue
            if cap.basis == "eligible_spend":
                if cap.limit.value.currency != money.currency:
                    raise Unsupported("cap currency")
                room = Decimal(cap.limit.value.minor_units) / 10**exponent - capped[cap.cap_id]
                spend = max(Decimal(0), min(spend, room))
                capped[cap.cap_id] += spend
        reward = rounded(
            rounded(spend, rounding, "spend") / unit_spend * quantity, rounding, "reward"
        )
        for cap in caps:
            if cap.basis == "reward" and rate.rate_id in known(cap.applies_to, "cap scope"):
                if cap.limit.value.unit.unit_id != reward_unit.unit_id:
                    raise Unsupported("cap unit")
                room = cap.limit.value.quantity - capped[cap.cap_id]
                reward = max(Decimal(0), min(reward, room))
                capped[cap.cap_id] += reward
        awards[t.event_id] = (reward_unit, reward)
    return awards


def unit_label(unit):
    if unit.kind == "cashback":
        return f"{unit.currency} cashback"
    return unit.unit_id.replace("-", " ")


def card_rewards(document, period, cards, choices, hidden=frozenset()):
    """One CardRewards per stated card, for the purchases inside the period."""
    names = {p.product_id: p.supplied_name for p in document.products}
    transactions = [t for t in heads(document) if t.event_id not in hidden]
    results = []
    for account_id, card in cards.items():
        product_id = choices.get(account_id)
        result = CardRewards(account_id, card["label"], names.get(product_id), "pending")
        results.append(result)
        if product_id is None:
            result.reason = "Choose card"
            continue
        owned = [
            t
            for t in transactions
            if t.account_id.value == account_id and t.amount.value and exclusion(t) is None
        ]
        viewed = [t for t in owned if period.start <= t.occurred_on.value < period.end]
        for t in viewed:
            if t.kind.value == "purchase":
                money = t.amount.value
                result.spend[money.currency] = (
                    result.spend.get(money.currency, 0) + money.minor_units
                )
        rules = latest_rules(document, product_id)
        result.status = "no_terms"
        if not rules:
            result.reason = "No reward terms for this card"
            continue
        result.status = "unsupported"
        if not result.spend:
            result.status, result.reason = "idle", "No purchases"
            continue
        try:
            if len(rules) > 1:
                raise Unsupported("several reward rules")
            rule = rules[0]
            if rule.review.status == "rejected":
                raise Unsupported("rule rejected")
            if any(t.kind.value in {"refund", "reversal"} for t in viewed):
                raise Unsupported("refunds or reversals")
            effective = rule.effective
            if effective.status not in KNOWN or None in (
                effective.value.start,
                effective.value.end,
            ):
                raise Unsupported("effective dates not established")
            if rule.date_basis.status not in KNOWN:
                raise Unsupported("date basis not established")
            basis = rule.date_basis.value
            # A month's minimum spend and caps need all of that month's purchases on the card.
            wanted = {
                (day.year, day.month)
                for day in (basis_day(t, basis) for t in viewed if t.kind.value == "purchase")
            }
            months = defaultdict(list)
            for t in owned:
                day = basis_day(t, basis) if t.kind.value == "purchase" else None
                if day is not None and (day.year, day.month) in wanted:
                    if not effective.value.start <= day < effective.value.end:
                        raise Unsupported("purchase outside the rule's dates")
                    months[day.year, day.month].append(t)
            earned = defaultdict(Decimal)
            for purchases in months.values():
                purchases.sort(key=lambda t: (t.occurred_on.value, t.event_id))
                awards = month_awards(rule, purchases, basis)
                for t in purchases:
                    award = awards[t.event_id]
                    if award is not None and period.start <= t.occurred_on.value < period.end:
                        earned[unit_label(award[0])] += award[1]
            result.earned = dict(earned)
            result.status = "estimated"
            if not earned:
                result.reason = "Terms give nothing for these purchases"
        except Unsupported as exc:
            result.status, result.reason = "unsupported", f"Terms incomplete: {exc}"
        except ArithmeticError:
            result.status, result.reason = "unsupported", "Terms incomplete: amounts out of range"
    return results
