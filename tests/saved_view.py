"""What a person can still learn about saved mail, rebuilt from stored data for assertions.

The Inspect saved data page is gone from the app. These helpers read the same facts directly:
each message's processing outcome, reasons, associated events and source text.
"""

from html import escape

from spending_optimiser.ledger import heads
from spending_optimiser.monthly import SINGAPORE, calendar_month, exclusion, foreign_events
from spending_optimiser.processing import REVISION


def outcomes(service, period=None):
    period = period or calendar_month("2025-02")
    candidates, _ = service.saved_range(period)
    document = service.cache.ledger.load()
    namespace = service.namespace
    sources = {
        o.observation_id: o
        for o in document.observations
        if o.source_system == "gmail" and o.source_namespace == namespace
    }
    hidden = foreign_events(document, namespace)
    latest = {}
    for transaction in heads(document):
        if transaction.event_id in hidden:
            continue
        for origin in transaction.origins:
            source = sources.get(origin.observation_id)
            if source is not None:
                latest.setdefault(source.source_record_id, {})[transaction.event_id] = transaction
    saved = service.cache.outcomes(namespace, REVISION)
    result = {}
    for message_id, candidate in candidates.items():
        processed = saved.get(message_id, {"outcome": "awaiting processing", "reasons": ()})
        outcome = {
            "outcome": "unreadable" if candidate is None else processed["outcome"],
            "reasons": ("saved_payload_unreadable_retry_load",)
            if candidate is None
            else processed["reasons"],
            "events": [
                {
                    "event_id": t.event_id,
                    "status": t.review.status,
                    "included": exclusion(t) is None,
                }
                for t in latest.get(message_id, {}).values()
            ],
        }
        outcome["included"] = any(event["included"] for event in outcome["events"])
        outcome["prior_version"] = outcome["outcome"] in {
            "failed",
            "awaiting processing",
            "unreadable",
        }
        result[message_id] = outcome
    return candidates, result


def saved_text(service, period=None):
    """Escaped text covering outcomes, reasons, headers and message text of the period's mail."""
    candidates, results = outcomes(service, period)
    lines = [f"Saved candidates: {len(candidates)}"]
    included_events = {
        event["event_id"]
        for outcome in results.values()
        for event in outcome["events"]
        if event["included"]
    }
    previous = {
        event["event_id"]
        for outcome in results.values()
        if outcome["prior_version"]
        for event in outcome["events"]
        if event["included"]
    }
    lines.append(
        f"{len(included_events)} accepted transactions from this saved mail · "
        f"{sum(not o['included'] for o in results.values())} other / uncertain messages."
    )
    if previous:
        lines.append(
            f"{len(previous)} previous canonical revisions remain included while their current "
            "source versions await processing."
        )
    unreadable = sum(candidate is None for candidate in candidates.values())
    if unreadable:
        lines.append(f"{unreadable} saved messages unreadable or identity-invalid")
    for message_id, candidate in candidates.items():
        outcome = results[message_id]
        lines.append(
            f"Current source: {outcome['outcome'].capitalize()}"
            + (
                " · " + ", ".join(outcome["reasons"]).replace("_", " ")
                if outcome["reasons"]
                else ""
            )
        )
        for event in outcome["events"]:
            label = (
                "Previous canonical revision" if outcome["prior_version"] else "Canonical revision"
            )
            lines.append(
                f"{label}: {event['status']} · "
                + ("Included" if event["included"] else "Excluded from totals")
                + f" · /transaction?event_id={event['event_id']}"
            )
        if not outcome["events"]:
            lines.append("Excluded from totals: no associated canonical events.")
        if candidate is None:
            lines.append("Unreadable saved message")
        else:
            received = candidate.gmail_internal_at.astimezone(SINGAPORE)
            lines.append(f"{candidate.sender} {candidate.subject} {received:%d %b %Y}")
            lines.extend(header.value for header in candidate.headers)
            lines.append(candidate.text)
            lines.append(f"Observation: {candidate.observation.observation_id}")
            lines.append("Warnings: " + (", ".join(candidate.warnings) or "0 results"))
    return escape("\n".join(lines))
