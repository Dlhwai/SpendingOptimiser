"""On-device structural template discovery with a closed semantic slot vocabulary.

This runtime never sends mail, private skeletons or source links to a model. Repeated
structure is NOT financial evidence: every application rechecks explicit semantics,
all money occurrences and every inline representation. Generic forms contain only
closed grammar identifiers, MIME enums and structural indices, never source literals.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Literal

from pydantic import Field

from .common import CURRENCY_EXPONENTS, EvidenceRef, Fact, Model
from .extraction import ACCOUNT_REFERENCE, EVENT_KINDS, MAX_NOTIFICATION_CHARS, _parse_value
from .notification_profiles import PROFILE_CLAIM, PROFILE_ROLES, PROFILE_RULES, qualify

VERSION = "local-structural-v1"
LEARNING_REVISION = "local-learning-v5"
DISCOVERY_REVISION = "template-discovery-" + LEARNING_REVISION
MAX_PARTS = 8
MAX_TOTAL_CHARS = 65536
MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
# These are financial semantics, not issuer-specific patterns or learned executable regex.
KINDS = {
    "purchase": "purchase",
    "refund": "refund",
    "reversal": "reversal",
    "income": "income",
    "salary": "income",
    "transfer": "transfer",
    "decline": "decline",
    "declined": "decline",
    "otp": "otp",
    "balance": "balance",
    "marketing": "marketing",
    "card purchase": "purchase",
    "retail purchase": "purchase",
    "funds transfer": "transfer",
    "paynow transfer": "transfer",
}
NUMERIC_CONTINUATION = r"(?:[.,]?\s+(?:[+-]\s*)?(?:[0-9]|\.[0-9])|\s+[.,]\s*[0-9])"
NUMBER = (
    r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,19})(?:\.[0-9]{1,3})?"
    rf"(?!{NUMERIC_CONTINUATION})"
)
MONEY_END = rf"(?!{NUMERIC_CONTINUATION})(?=[.;:!?]?(?:\s|$))"
MONEY = re.compile(
    rf"(?<![\w.+-])(?P<currency>{'|'.join(CURRENCY_EXPONENTS)}|S\$)\s*"
    rf"(?P<number>[+-]?[0-9]\S*?(?:(?={NUMERIC_CONTINUATION})\s+\S+)*?){MONEY_END}"
)
ASSERTION_START = re.compile(
    r"(?im)^[^\S\n]*(?P<assertion>"
    r"(?:your\s+purchase|you\s+(?:have\s+)?made\s+a\s+purchase)\s+(?:of|for)|"
    r"your\s+(?:refund|(?:PayNow\s+)?transfer)\s+(?:of|for)\b)"
)
DATE = (
    r"(?:[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}|"
    r"[0-9]{1,2}[\s/-][A-Za-z]{3,9}[\s/-][0-9]{4}|[0-9]{2}/[0-9]{2}/[0-9]{4})"
)
# Every rule requires an explicit field label or financial assertion. Bare amounts,
# receipt timestamps, sender labels and a generic 'transaction' never supply a kind.
RAW_RULES = (
    (
        "kind_label",
        "kind",
        re.compile(
            r"(?im)^\s*(?:Type|Transaction type|Type of transaction|Event type)"
            r"\s*[:\t]\s*(?P<value>[A-Za-z][A-Za-z \u00a0]{0,31})\s*$"
        ),
    ),
    (
        "amount_label",
        "amount",
        re.compile(
            r"(?im)^\s*(?:Amount|Transaction amount|Purchase amount|Transfer amount|"
            r"Amount debited|Amount credited)\s*[:\t]\s*"
            rf"(?P<value>(?:[A-Z]{{3}}|S\$)\s*{NUMBER})\s*$"
        ),
    ),
    (
        "date_label",
        "occurred_on",
        re.compile(
            r"(?im)^\s*(?:Event date|Transaction date|Purchase date|Transfer date|"
            r"Date of transaction)(?:\s*(?:[/&]|and)\s*Time)?\s*[:\t]\s*"
            rf"(?P<value>{DATE})"
            r"(?:[, T]+(?:at\s+)?[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?"
            r"(?:\s*(?:AM|PM))?(?:\s*(?:SGT|UTC[+]08:00))?)?\s*$"
        ),
    ),
    (
        "posting_label",
        "posted_on",
        re.compile(rf"(?im)^\s*(?:Posting date|Posted on)\s*[:\t]\s*(?P<value>{DATE})\s*$"),
    ),
    (
        "account_label",
        "account",
        re.compile(
            r"(?im)^\s*(?:Account|Account ID|Account reference|Account number|Account No\.?)"
            r"\s*[:\t]\s*(?P<value>[^\n\t]{1,128})\s*$"
        ),
    ),
    (
        "bank_label",
        "bank",
        re.compile(r"(?im)^\s*(?:Bank|Institution)\s*[:\t]\s*(?P<value>[^\n\t]{1,64})\s*$"),
    ),
    (
        "merchant_label",
        "merchant",
        re.compile(
            r"(?im)^\s*(?:Merchant|Merchant name|Transaction description)"
            r"\s*[:\t]\s*(?P<value>[^\n\t]{1,128})\s*$"
        ),
    ),
    (
        "currency_label",
        "currency",
        re.compile(
            r"(?im)^\s*(?:Currency|Transaction currency)\s*[:\t]\s*(?P<value>[A-Z]{3}|S\$)\s*$"
        ),
    ),
    (
        "amount_number_label",
        "amount_number",
        re.compile(rf"(?im)^\s*(?:Amount|Transaction amount)\s*[:\t]\s*(?P<value>{NUMBER})\s*$"),
    ),
    (
        "amount_currency_label",
        "amount",
        re.compile(
            r"(?im)^\s*(?:Amount|Transaction amount)\s*\((?P<code>[A-Z]{3}|S\$)\)"
            rf"\s*[:\t]\s*(?P<value>{NUMBER})\s*$"
        ),
    ),
    (
        "card_reference_label",
        "card_reference",
        re.compile(
            r"(?im)^\s*(?:Card(?: number| No\.?)?|Card/Acct No\.?)\s*[:\t]\s*"
            r"(?P<value>[^\n\t]{1,128})\s*$"
        ),
    ),
    (
        "kind_heading",
        "kind",
        re.compile(
            r"(?im)^[ \t]*(?P<value>Purchase|Refund|Reversal|Funds transfer|PayNow transfer)"
            r"(?:\s+(?:notification|confirmation))?[ \t]*$"
        ),
    ),
    (
        "amount_assertion",
        "amount",
        re.compile(
            r"(?im)^[ \t]*an\s+amount\s+of\b\s*"
            r"(?P<value>(?:\S+(?:\s+\S+)*?)?)\s+was\s+(?:debited|credited)\b"
        ),
    ),
    (
        "purchase_assertion",
        "purchase",
        re.compile(
            r"(?im)^[ \t]*(?:your\s+purchase|you\s+(?:have\s+)?made\s+a\s+purchase)"
            r"\s+(?:of|for)\s*"
            rf"(?P<value>(?:[A-Z]{{3}}|S\$)\s*{NUMBER}){MONEY_END}"
        ),
    ),
    (
        "refund_assertion",
        "refund",
        re.compile(
            r"(?im)^[ \t]*your\s+refund\s+(?:of|for)\s+"
            rf"(?P<value>(?:[A-Z]{{3}}|S\$)\s*{NUMBER}){MONEY_END}"
        ),
    ),
    (
        "transfer_assertion",
        "transfer",
        re.compile(
            r"(?im)^[ \t]*your\s+(?:PayNow\s+)?transfer\s+(?:of|for)\s+"
            rf"(?P<value>(?:[A-Z]{{3}}|S\$)\s*{NUMBER}){MONEY_END}"
        ),
    ),
    ("event_on", "occurred_on", re.compile(rf"(?i)\bon\s+(?P<value>{DATE})(?![A-Za-z0-9])")),
    (
        "account_for",
        "account",
        re.compile(
            r"(?i)\b(?:from|for|using)\s+(?:your\s+)?account\s+reference\s+"
            rf"(?P<value>{ACCOUNT_REFERENCE.pattern}?)(?<![. ])"
            r"(?=[.,;!?](?:\s|$)|[^\S\n]*(?:\n|$))"
        ),
    ),
)
# Known semantic labels can be separated by inert HTML/non-breaking whitespace.
# Compile only static code vocabulary, never a regex supplied/generated by mail.
LABEL_PHRASES = (
    "Transaction type",
    "Type of transaction",
    "Event type",
    "Transaction amount",
    "Purchase amount",
    "Transfer amount",
    "Amount debited",
    "Amount credited",
    "Event date",
    "Transaction date",
    "Purchase date",
    "Transfer date",
    "Date of transaction",
    "Posting date",
    "Posted on",
    "Account ID",
    "Account reference",
    "Account number",
    "Account No",
    "Merchant name",
    "Transaction description",
    "Transaction currency",
    "Funds transfer",
    "PayNow transfer",
)


def _label_whitespace(pattern):
    text = pattern.pattern
    for label in LABEL_PHRASES:
        text = text.replace(label, label.replace(" ", r"\s+"))
    return re.compile(text, pattern.flags)


FIELD_LABEL = _label_whitespace(
    re.compile(
        r"(?im)^[^\S\n]*(?P<label>Type|Transaction type|Type of transaction|Event type|"
        r"(?:Amount|Transaction amount)(?:\s*\([^\n\t)]*\))?|Purchase amount|Transfer amount|"
        r"Amount debited|Amount credited|"
        r"(?:Event date|Transaction date|Purchase date|Transfer date|Date of transaction)"
        r"(?:\s*(?:[/&]|and)\s*Time)?|Posting date|Posted on|"
        r"Account|Account ID|Account reference|Account number|Account No\.?|Bank|Institution|"
        r"Merchant|Merchant name|Transaction description|Currency|Transaction currency|"
        r"Card(?: number| No\.?)?|Card/Acct No\.?)\s*[:\t]"
    )
)

RULES = (
    tuple((grammar, role, _label_whitespace(pattern)) for grammar, role, pattern in RAW_RULES)
    + PROFILE_RULES
    + (
        (
            "unassigned_date_token",
            "unassigned_date",
            re.compile(
                r"(?i)(?<![A-Za-z0-9])(?P<value>(?:[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}|"
                r"[0-9]{1,2}[-/.][0-9]{1,2}[-/.](?:[0-9]{4}|[0-9]{2})|"
                r"[0-9]{1,2}[\s./-]*(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
                r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
                r"dec(?:ember)?)[\s,./-]*[0-9]{2,4}))(?![A-Za-z0-9:])"
            ),
        ),
        (
            "masked_identifier_token",
            "masked_identifier",
            re.compile(
                r"(?i)(?P<value>(?:[*x•]{2,}[ -]*[0-9]{2,8}|\bending[ -]+[0-9]{2,8}))(?![0-9])"
            ),
        ),
        (
            "unassigned_time_token",
            "unassigned_time",
            re.compile(
                r"(?<![0-9])(?P<value>(?:[01]?[0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?)(?![0-9])"
            ),
        ),
        (
            "transaction_notice",
            "transaction_notice",
            re.compile(
                r"(?im)^[ \t]*(?P<value>(?:Card\s+)?Transaction"
                r"(?:\s+(?:alert|notification|details))?)[ \t]*$"
            ),
        ),
    )
)

Grammar = Literal[
    "kind_label",
    "amount_label",
    "date_label",
    "posting_label",
    "account_label",
    "bank_label",
    "merchant_label",
    "purchase_assertion",
    "refund_assertion",
    "transfer_assertion",
    "event_on",
    "account_for",
    "currency_label",
    "amount_number_label",
    "amount_currency_label",
    "card_reference_label",
    "kind_heading",
    "amount_assertion",
    "unassigned_date_token",
    "masked_identifier_token",
    "transaction_notice",
    "unassigned_time_token",
    "completed_card_dmy2",
    "completed_wallet_transfer",
    "issuer_card_source",
    "wallet_mobile_source",
    "notice_day_month_clock",
    "notice_payee",
    "notice_transaction_reference",
    "administrative_notice",
]


class GenericPart(Model):
    mime: Literal["text/plain", "text/html"]
    rules: Annotated[tuple[Grammar, ...], Field(min_length=1, max_length=32)]


class GenericForm(Model):
    version: Literal["local-structural-v1"] = VERSION
    parts: Annotated[tuple[GenericPart, ...], Field(min_length=1, max_length=MAX_PARTS)]


def validate_generic(value):
    """Never interpolate an input or a Pydantic exception into a disclosure diagnostic."""
    try:
        if isinstance(value, GenericForm):
            value = value.model_dump(mode="json", warnings=False)
        return GenericForm.model_validate(value)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid_generic_template") from None


def generic_asset(forms):
    """A reconstructed closed-schema value, not a scrubbed/copy-through private object."""
    unique = {}
    for form in forms:
        checked = validate_generic(form)
        clean = {
            "version": VERSION,
            "parts": [{"mime": part.mime, "rules": list(part.rules)} for part in checked.parts],
        }
        unique[json.dumps(clean, sort_keys=True)] = clean
    return {"version": VERSION, "forms": [unique[key] for key in sorted(unique)]}


def validate_asset(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "forms"}
        or value["version"] != VERSION
        or not isinstance(value["forms"], list)
        or len(value["forms"]) > 1000
    ):
        raise ValueError("invalid_generic_template")
    return generic_asset(value["forms"])


def bundled_forms():
    """Load shareable hints through the same closed declassification schema."""
    from importlib.resources import files

    asset = json.loads(
        files("spending_optimiser").joinpath("examples/email_templates.json").read_text()
    )
    return tuple(validate_generic(form) for form in validate_asset(asset)["forms"])


@dataclass(repr=False)
class Binding:
    grammar: str
    role: str
    value: object = field(repr=False)
    start: int
    end: int
    value_start: int
    value_end: int
    quote: str = field(repr=False)
    locator: str


@dataclass(repr=False)
class Analysis:
    bindings: list[Binding] = field(default_factory=list, repr=False)
    private_shape: str = field(default="", repr=False)
    generic: GenericForm | None = None
    issues: tuple[str, ...] = ()
    financial: bool = False
    nonfinancial_subject: bool = False


def _value(role, raw):
    raw = raw.strip()
    if role == "kind":
        key = " ".join(raw.lower().split())
        if key not in KINDS:
            raise ValueError
        return KINDS[key]
    if role == "amount":
        match = MONEY.fullmatch(raw)
        if match is None or re.fullmatch(NUMBER, match["number"]) is None:
            raise ValueError
        code = "SGD" if match["currency"] == "S$" else match["currency"]
        return _parse_value("amount", f"Amount: {code} {match['number'].replace(',', '')}")
    if role == "currency":
        code = "SGD" if raw == "S$" else raw
        if code not in CURRENCY_EXPONENTS:
            raise ValueError
        return code
    if role == "amount_number":
        if re.fullmatch(NUMBER, raw) is None:
            raise ValueError
        return raw.replace(",", "")
    if role in {"occurred_on", "posted_on"}:
        if re.fullmatch(r"[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}", raw):
            return date.fromisoformat(raw.replace("/", "-"))
        if re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", raw):
            first, second, year = map(int, raw.split("/"))
            # Without a stated convention, 03/04 is ambiguous. No receipt-date fallback.
            if first > 12 and second <= 12:
                return date(year, second, first)
            if second > 12 and first <= 12:
                return date(year, first, second)
            raise ValueError
        day, month, year = re.split(r"[\s/-]+", raw)
        # Full English month names must be exact, not arbitrary prefixes.
        names = (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        )
        if month.lower() not in {*MONTHS, *names}:
            raise ValueError
        return date(int(year), MONTHS[month.lower()[:3]], int(day))
    if not raw or len(raw) > 128:
        raise ValueError
    return raw


def analyze(candidate):
    """Discover a layout from private bytes, binding only explicit supported semantics."""
    if (
        not candidate.parts
        or len(candidate.parts) > MAX_PARTS
        or sum(len(p.text) for p in candidate.parts) > MAX_TOTAL_CHARS
        or any(
            len(p.text) > MAX_NOTIFICATION_CHARS or p.mime_type not in {"text/plain", "text/html"}
            for p in candidate.parts
        )
    ):
        return Analysis(issues=("unsupported_message_structure",))
    bindings, shapes, parts, issues = [], [], [], []
    financial = False
    profile_context = any(PROFILE_CLAIM.search(part.text) for part in candidate.parts)
    for part in candidate.parts:
        text = part.text
        if any(ord(c) < 32 and c not in "\n\t" for c in text):
            issues.append("unsupported_control_character")
            continue
        found = []
        for grammar, role, pattern in RULES:
            for match in pattern.finditer(text):
                if len(found) >= 32 or len(match[0].strip()) > 512:
                    issues.append("too_many_or_oversized_fields")
                    break
                if grammar in {"event_on", "account_for"}:
                    # These slots must belong to the same explicit financial assertion.
                    if not any(
                        b.grammar.endswith("_assertion")
                        and b.start < match.start()
                        and "\n" not in text[b.start : match.start()]
                        for b in found
                    ):
                        continue
                if grammar in {
                    "unassigned_date_token",
                    "masked_identifier_token",
                    "unassigned_time_token",
                } and any(b.start <= match.start() and match.end() <= b.end for b in found):
                    continue
                try:
                    raw_value = match["value"]
                    if grammar == "amount_currency_label":
                        raw_value = match["code"] + " " + raw_value
                    value = _value(
                        "amount" if role in {"purchase", "refund", "transfer"} else role, raw_value
                    )
                except (ValueError, TypeError, OverflowError):
                    issues.append("invalid_explicit_field")
                    continue
                found.append(
                    Binding(
                        grammar,
                        role,
                        value,
                        match.start() + len(match[0]) - len(match[0].lstrip()),
                        match.end() - len(match[0]) + len(match[0].rstrip()),
                        *match.span("value"),
                        match[0].strip(),
                        part.locator,
                    )
                )
        label_starts = {b.start for b in found if b.grammar.endswith("_label")}
        if any(m.start("label") not in label_starts for m in FIELD_LABEL.finditer(text)):
            issues.append("invalid_explicit_field")
        assertion_starts = {b.start for b in found if b.grammar.endswith("_assertion")}
        if any(
            m.start("assertion") not in assertion_starts for m in ASSERTION_START.finditer(text)
        ):
            issues.append("invalid_financial_assertion")
        found, profile_issues = qualify(
            found, text, candidate.subject, candidate.observation, profile_context
        )
        issues.extend(profile_issues)
        found.sort(key=lambda b: (b.start, b.end, b.grammar))
        if not found:
            shapes.append((part.mime_type, text))
        else:
            parts.append(GenericPart(mime=part.mime_type, rules=tuple(b.grammar for b in found)))
            # Private exact anchors plus typed slots. All matched VALUES are replaced,
            # not memorized as template constants. The rest never leaves local storage.
            replacements = sorted({(b.value_start, b.value_end, b.role) for b in found})
            cursor, chunks = 0, []
            for start, end, role in replacements:
                if start < cursor:
                    issues.append("overlapping_field_bindings")
                    continue
                chunks.extend((text[cursor:start], "{" + role + "}"))
                cursor = end
            chunks.append(text[cursor:])
            shapes.append((part.mime_type, "".join(chunks)))
        financial |= any(
            b.role in {"kind", "amount", "amount_number", "purchase", "refund", "transfer"}
            for b in found
        )
        # Every currency-bearing magnitude must have explicit financial-field authority.
        money_bindings = [
            b for b in found if b.role in {"amount", "purchase", "refund", "transfer"}
        ]
        if any(
            not any(b.value_start <= m.start() and m.end() <= b.value_end for b in money_bindings)
            for m in MONEY.finditer(text)
        ):
            issues.append("unbound_monetary_value")
        if len([b for b in found if b.role in {"kind", "purchase", "refund", "transfer"}]) > 1:
            issues.append("multiple_event_types")
        if re.search(
            r"(?i)\b(?:declined|unsuccessful|failed|pending|cancelled|rejected|not completed)\b",
            text,
        ):
            issues.append("event_not_final_or_conflicting")
        # Explicit non-financial assertions cannot be bypassed by another representation.
        if re.search(
            r"(?im)^\s*(?:one.time password|OTP\s*:|transaction declined|"
            r"payment declined|balance update|promotional offer)\b",
            text,
        ):
            issues.append("non_financial_or_conflicting_notice")
        bindings.extend(found)
    currencies = {b.value for b in bindings if b.role == "currency"}
    currencies.update(
        b.value.currency for b in bindings if b.role in {"amount", "purchase", "refund", "transfer"}
    )
    if len(currencies) > 1:
        issues.append("amount_currency_conflicting")
    financial_parts = {}
    for binding in bindings:
        financial_parts.setdefault(binding.locator, []).append(binding)
    representations = []
    for part in candidate.parts:
        selected = financial_parts.get(part.locator, [])
        if not any(
            b.role in {"kind", "amount", "amount_number", "purchase", "refund", "transfer"}
            for b in selected
        ):
            continue
        signature = []
        for role in ("kind", "amount", "currency", "occurred_on", "account"):
            values = []
            for b in selected:
                if b.role == role:
                    values.append(str(b.value))
                elif b.role in {"purchase", "refund", "transfer"} and role in {"kind", "amount"}:
                    values.append(b.role if role == "kind" else str(b.value))
            if role == "amount":
                numbers = {b.value for b in selected if b.role == "amount_number"}
                codes = {b.value for b in selected if b.role == "currency"}
                if len(numbers) == len(codes) == 1:
                    try:
                        values.append(
                            str(
                                _parse_value(
                                    "amount", f"Amount: {next(iter(codes))} {next(iter(numbers))}"
                                )
                            )
                        )
                    except ValueError:
                        pass
            if role == "currency":
                values.extend(
                    b.value.currency
                    for b in selected
                    if b.role in {"amount", "purchase", "refund", "transfer"}
                )
            signature.append(tuple(sorted(set(values))))
        representations.append((part.mime_type, tuple(signature)))
    if len(representations) > 1:
        if len({mime for mime, _ in representations}) != len(representations):
            issues.append("multiple_financial_parts")
        if any(
            signature != representations[0][1] or any(not field for field in signature)
            for _, signature in representations
        ):
            issues.append("conflicting_or_incomplete_representations")
    nonfinancial_subject = bool(
        re.search(
            r"(?i)\b(?:verification\s+code|one.time\s+password|OTP)\b", candidate.subject or ""
        )
    )
    if nonfinancial_subject and financial:
        issues.append("conflicting_event_classification")
    generic = GenericForm(parts=tuple(parts)) if parts else None
    return Analysis(
        bindings,
        json.dumps(shapes, ensure_ascii=True, separators=(",", ":")),
        generic,
        tuple(sorted(set(issues))),
        financial,
        nonfinancial_subject,
    )


def _fact(values, evidence):
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if not unique:
        return Fact(note="Not explicitly established by supported notification semantics")
    if len(unique) > 1:
        return Fact(
            status="conflicting",
            alternatives=tuple(unique),
            evidence=tuple(evidence),
            note="Saved representations contain contradictory explicit values",
        )
    return Fact(status="proposed", value=unique[0], evidence=tuple(evidence))


def facts_from(analysis, digest, observation):
    """Revalidate fields on application; self-match/repetition grants no authority."""
    fields = {
        name: ([], [])
        for name in (
            "kind",
            "direction",
            "amount",
            "occurred_on",
            "posted_on",
            "merchant",
            "account",
            "bank",
            "currency",
            "amount_number",
            "card_reference",
            "unassigned_date",
            "masked_identifier",
            "transaction_notice",
            "unassigned_time",
            *sorted(PROFILE_ROLES),
        )
    }
    for binding in analysis.bindings:
        roles = (
            ("kind", "amount")
            if binding.role in {"purchase", "refund", "transfer"}
            else (binding.role,)
        )
        for role in roles:
            value = binding.role if role == "kind" and len(roles) == 2 else binding.value
            values, refs = fields[role]
            values.append(value)
            refs.append(
                EvidenceRef(
                    target="observation",
                    evidence_revision_id=observation.observation_id,
                    locator=f"mail-version:{digest}:{binding.locator}:chars:{binding.start}:{binding.end}",
                    quote=binding.quote,
                )
            )
    facts = {name: _fact(*items) for name, items in fields.items()}
    issues = list(analysis.issues)
    if facts["occurred_on"].status == "proposed" and any(
        b.grammar == "completed_card_dmy2" and b.role == "occurred_on" for b in analysis.bindings
    ):
        facts["occurred_on"] = facts["occurred_on"].model_copy(
            update={
                "note": "Financial request and day/month agree; absent a full stated year, "
                "DD/MM/YY uses the fixed 1969-2068 convention, never a receipt-derived year"
            }
        )
    profile = {role: facts.pop(role) for role in PROFILE_ROLES}
    if (
        not analysis.financial
        and (profile["administrative_notice"].evidence or analysis.nonfinancial_subject)
        and not set(issues) - {"non_financial_or_conflicting_notice"}
    ):
        return {}, "non_transaction", ("non_financial_notification",)
    if facts["occurred_on"].status == "unknown":
        refs = tuple(
            ref
            for role in ("card_claim_date", "wallet_claim_date", "notice_clock")
            for ref in profile[role].evidence
        )
        if refs:
            facts["occurred_on"] = Fact(
                evidence=refs,
                note="Financial date is incomplete or the notice contains multiple events",
            )
    number, currency = facts.pop("amount_number"), facts.pop("currency")
    # A clock-shaped token is a structural variable, never an event date or direction.
    facts.pop("unassigned_time")
    unassigned_date = facts.pop("unassigned_date")
    masked = facts.pop("masked_identifier")
    notice = facts.pop("transaction_notice")
    if facts["occurred_on"].status == "unknown" and unassigned_date.evidence:
        facts["occurred_on"] = Fact(
            evidence=unassigned_date.evidence,
            note="Unassigned or century-ambiguous date text does not establish an event date",
        )
        issues.append("date_semantics_or_century_unresolved")
        values = ([] if unassigned_date.value is None else [unassigned_date.value]) + list(
            unassigned_date.alternatives
        )
        if any(
            not (re.match(r"[0-9]{4}[-/]", value) or re.search(r"[0-9]{4}$", value))
            for value in values
        ):
            issues.append("date_century_unresolved")
        if any(
            re.match(r"[0-9]{4}[-/]", value) or re.search(r"[0-9]{4}$", value) for value in values
        ):
            issues.append("date_scope_unresolved")
    if facts["account"].status == "unknown" and masked.evidence:
        facts["account"] = Fact(
            evidence=masked.evidence,
            note="A masked identifier does not establish an underlying account mapping",
        )
        issues.append("masked_identifier_not_account_mapping")
    if facts["kind"].status == "unknown" and notice.evidence:
        facts["kind"] = Fact(
            evidence=notice.evidence,
            note="A generic transaction notice does not determine an event kind",
        )
        issues.append("generic_transaction_kind_unresolved")
    card = facts.pop("card_reference")
    if card.value is not None and facts["account"].value is None:
        issues.append("card_reference_is_not_account_mapping")
    if number.value is not None and currency.value is not None:
        try:
            money = _parse_value("amount", f"Amount: {currency.value} {number.value}")
            values, refs = fields["amount"]
            facts["amount"] = _fact([*values, money], [*refs, *number.evidence, *currency.evidence])
        except ValueError:
            issues.append("invalid_explicit_field")
    elif number.value is not None:
        issues.append("amount_currency_unresolved")
    if number.status == "conflicting" or currency.status == "conflicting":
        issues.append("amount_currency_conflicting")
    # Explicit bank + complete account number can identify a source account. A masked
    # suffix, sender claim or card number never establishes an underlying account.
    reference = facts["account"]
    if facts["bank"].status == "conflicting":
        issues.append("bank_conflicting")
    values = (() if reference.value is None else (reference.value,)) + reference.alternatives
    resolved, refs = [], reference.evidence
    for value in values:
        numeric = re.fullmatch(r"[0-9][0-9 ._-]*", value) is not None
        symbolic = (
            not numeric
            and ACCOUNT_REFERENCE.fullmatch(value) is not None
            and not re.search(r"[0-9]{8}", value)
        )
        masked = bool(
            re.search(r"(?i)(?:[*x•]{2,}|ending|last\s*[0-9]|\b(?:card|credit|debit)\b)", value)
        )
        complete = (
            bool(re.fullmatch(r"[0-9][0-9 -]{7,33}", value)) and facts["bank"].value is not None
        )
        if masked or not (symbolic or complete):
            facts["account"] = Fact(
                evidence=reference.evidence,
                note="Identifiers do not establish complete account attribution",
            )
            issues.append("account_attribution_unresolved")
            break
        resolved.append(facts["bank"].value + ":" + value if complete else value)
        if complete:
            refs = reference.evidence + facts["bank"].evidence
    else:
        if resolved:
            facts["account"] = _fact(resolved, refs)
    kind = facts["kind"]
    if kind.value and kind.value not in EVENT_KINDS:
        return {}, "non_transaction", ("non_financial_notification",)
    if kind.alternatives and not set(kind.alternatives).issubset(EVENT_KINDS):
        facts["kind"] = Fact(
            evidence=kind.evidence, note="Conflicting financial/non-financial event kinds"
        )
        issues.append("conflicting_event_classification")
    if not analysis.financial:
        return {}, "unsupported", tuple(issues) or ("no_supported_financial_semantics",)
    facts.pop("bank")
    for name, fact in facts.items():
        if (
            fact.status == "conflicting"
            or name in {"kind", "amount", "occurred_on", "account"}
            and fact.value is None
        ):
            issues.append(f"{name}_{fact.status}")
    if (
        facts["occurred_on"].value
        and facts["posted_on"].value
        and facts["posted_on"].value < facts["occurred_on"].value
    ):
        issues.append("posting_precedes_event")
    return facts, "uncertain" if issues else "accepted", tuple(sorted(set(issues)))


def apply_generic(form, candidate, digest, observation):
    """Generic assets are reusable structure hints, never blind acceptance rules."""
    form = validate_generic(form)
    analysis = analyze(candidate)
    if analysis.generic != form:
        return {}, "unsupported", ("generic_layout_mismatch",)
    return facts_from(analysis, digest, observation)


class LocalTemplates:
    """Private append-only definitions and version-linked discovery/application receipts."""

    def __init__(self, cache):
        self.cache = cache
        self.discovery_failed = 0
        with cache.ledger.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_templates (namespace TEXT NOT NULL, "
                "identity TEXT NOT NULL, revision TEXT NOT NULL, shape TEXT NOT NULL, "
                "generic TEXT NOT NULL, PRIMARY KEY(namespace, identity, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_template_sources (namespace TEXT NOT NULL, "
                "message_id TEXT NOT NULL, digest TEXT NOT NULL, revision TEXT NOT NULL, "
                "identity TEXT, PRIMARY KEY(namespace, message_id, digest, revision))"
            )

    def learn(self, namespace, message_id, digest, candidate):
        source = candidate.observation
        if (
            source.source_system != "gmail"
            or source.source_namespace != namespace
            or source.source_record_id != message_id
        ):
            raise ValueError("template_source_mismatch")
        analysis = analyze(candidate)
        identity = (
            hashlib.sha256(analysis.private_shape.encode()).hexdigest()
            if analysis.generic
            else None
        )
        with self.cache.ledger.connection() as db:
            if not db.execute(
                "SELECT 1 FROM mail_versions WHERE namespace=? AND message_id=? AND digest=?",
                (namespace, message_id, digest),
            ).fetchone():
                raise ValueError("template_source_version_missing")
            if identity:
                db.execute(
                    "INSERT OR IGNORE INTO mail_templates VALUES (?, ?, ?, ?, ?)",
                    (
                        namespace,
                        identity,
                        LEARNING_REVISION,
                        analysis.private_shape,
                        json.dumps(analysis.generic.model_dump(mode="json")),
                    ),
                )
            db.execute(
                "INSERT INTO mail_template_sources VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(namespace, message_id, digest, revision) DO UPDATE SET "
                "identity=excluded.identity WHERE mail_template_sources.identity IS NULL",
                (namespace, message_id, digest, LEARNING_REVISION, identity),
            )
            # Clear only transient discovery failure bookkeeping, never finance heads.
            db.execute(
                "UPDATE mail_processing SET outcome='learned', reason='' "
                "WHERE namespace=? AND message_id=? AND digest=? AND extractor=? "
                "AND outcome='failed'",
                (namespace, message_id, digest, DISCOVERY_REVISION),
            )
        return analysis

    def apply(self, namespace, message_id, digest, candidate, observation):
        source = candidate.observation
        if (
            source.source_namespace != namespace
            or source.source_record_id != message_id
            or observation.source_namespace != namespace
            or observation.source_record_id != message_id
        ):
            raise ValueError("template_source_mismatch")
        analysis = analyze(candidate)
        if analysis.generic is None:
            return facts_from(analysis, digest, observation)
        identity = (
            hashlib.sha256(analysis.private_shape.encode()).hexdigest()
            if analysis.generic
            else None
        )
        with self.cache.ledger.connection() as db:
            row = db.execute(
                "SELECT shape, generic FROM mail_templates "
                "WHERE namespace=? AND identity=? AND revision=?",
                (namespace, identity, LEARNING_REVISION),
            ).fetchone()
        if row is None or row[0] != analysis.private_shape:
            return {}, "unsupported", ("layout_not_discovered",)
        # Reusing a definition requires exact private layout AND the closed role contract.
        if validate_generic(json.loads(row[1])) != analysis.generic:
            return {}, "unsupported", ("template_contract_conflict",)
        return facts_from(analysis, digest, observation)

    def discover(self, namespace, limit=100):
        from .mail_cache import decode_candidate

        with self.cache.ledger.connection() as db:
            rows = db.execute(
                "SELECT v.message_id, v.digest, v.payload FROM mail_versions v "
                "LEFT JOIN mail_template_sources s ON s.namespace=v.namespace "
                "AND s.message_id=v.message_id AND s.digest=v.digest AND s.revision=? "
                "LEFT JOIN mail_processing f ON f.namespace=v.namespace "
                "AND f.message_id=v.message_id AND f.digest=v.digest AND f.extractor=? "
                "WHERE v.namespace=? AND (s.message_id IS NULL OR f.outcome='failed') "
                "ORDER BY coalesce(f.processed_at, ''), v.rowid LIMIT ?",
                (LEARNING_REVISION, DISCOVERY_REVISION, namespace, limit),
            ).fetchall()
        self.discovery_failed = 0
        for message_id, digest, payload in rows:
            try:
                candidate = decode_candidate(namespace, message_id, payload)
                self.learn(namespace, message_id, digest, candidate)
            except (ValueError, RecursionError):
                # A failed historical version remains retryable, behind unattempted rows.
                self.discovery_failed += 1
                with self.cache.ledger.connection() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO mail_template_sources VALUES (?, ?, ?, ?, NULL)",
                        (namespace, message_id, digest, LEARNING_REVISION),
                    )
                    self.cache.record_outcome(
                        db,
                        namespace,
                        message_id,
                        digest,
                        DISCOVERY_REVISION,
                        "failed",
                        ("saved_template_source_unreadable",),
                        None,
                    )
        return len(rows)

    def pending_count(self, namespace):
        return discovery_pending(self.cache, namespace)

    def generic_forms(self):
        with self.cache.ledger.connection() as db:
            rows = db.execute(
                "SELECT generic FROM mail_templates WHERE revision=?", (LEARNING_REVISION,)
            ).fetchall()
        return generic_asset([json.loads(row[0]) for row in rows])


def discovery_pending(cache, namespace):
    """Read-only progress, including before the first explicit discovery action."""
    if namespace is None:
        return 0
    with cache.ledger.connection() as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_template_sources'"
        ).fetchone()
        if not exists:
            return db.execute(
                "SELECT count(*) FROM mail_versions WHERE namespace=?", (namespace,)
            ).fetchone()[0]
        return db.execute(
            "SELECT count(*) FROM mail_versions v LEFT JOIN mail_template_sources s "
            "ON s.namespace=v.namespace AND s.message_id=v.message_id "
            "AND s.digest=v.digest AND s.revision=? "
            "LEFT JOIN mail_processing f ON f.namespace=v.namespace "
            "AND f.message_id=v.message_id AND f.digest=v.digest AND f.extractor=? "
            "WHERE v.namespace=? AND (s.message_id IS NULL OR f.outcome='failed')",
            (LEARNING_REVISION, DISCOVERY_REVISION, namespace),
        ).fetchone()[0]
