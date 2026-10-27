"""Exact boundaries of the Singapore date-range selection, with no mail, ledger or network."""

import json
import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files

import pytest

from spending_optimiser.monthly import (
    SINGAPORE,
    DateRange,
    add_months,
    calendar_month,
    date_range,
    latest_end,
    selected_range,
    shortcuts,
)


@pytest.mark.parametrize(
    ("start", "latest"),
    [
        ("2025-01-01", "2025-03-31"),  # three whole months
        ("2025-01-02", "2025-04-01"),
        ("2025-01-30", "2025-04-29"),  # 30 January + 3 months is 30 April
        ("2025-01-31", "2025-04-29"),  # clamped to 30 April, so the last day is 29 April
        ("2025-02-28", "2025-05-27"),
        ("2025-05-31", "2025-08-30"),  # 31 August exists, so no clamping
        ("2025-08-31", "2025-11-29"),  # 30 November is the clamped end
        ("2023-12-01", "2024-02-29"),  # leap February: 91 days
        ("2024-12-01", "2025-02-28"),  # ordinary February: 90 days
        ("2024-11-29", "2025-02-27"),  # 29 February 2025 does not exist
        ("2024-11-30", "2025-02-27"),
        ("2023-11-29", "2024-02-28"),  # 29 February 2024 exists, so the last day is the 28th
        ("2023-11-30", "2024-02-28"),
        ("2024-02-29", "2024-05-28"),  # starting on the leap day
        ("2024-11-15", "2025-02-14"),  # across a year end
        ("1970-01-01", "1970-03-31"),
        ("9998-12-31", "9999-03-30"),
    ],
)
def test_three_calendar_month_limit_is_exact(start, latest):
    assert latest_end(date.fromisoformat(start)).isoformat() == latest
    if latest.startswith("9999"):
        return  # years after 9998 are not selectable
    chosen = date_range(start, latest)
    assert chosen.last.isoformat() == latest
    assert chosen.end == add_months(chosen.start, 3)
    over = (date.fromisoformat(latest) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match=f"latest end date is {latest}"):
        date_range(start, over)


def test_a_month_is_never_treated_as_thirty_days():
    # 92 and 91 days are fine, while a range of only 90 days can still be too long.
    assert (date_range("2025-08-01", "2025-10-31").end - date(2025, 8, 1)).days == 92
    assert (date_range("2023-12-01", "2024-02-29").end - date(2023, 12, 1)).days == 91
    assert (date_range("2024-12-01", "2025-02-28").end - date(2024, 12, 1)).days == 90
    with pytest.raises(ValueError):
        date_range("2025-01-31", "2025-04-30")  # 90 days, but past three calendar months
    with pytest.raises(ValueError):
        date_range("2024-12-01", "2025-03-01")  # 91 days in a non-leap year


def test_half_open_interval_behind_inclusive_dates():
    single = date_range("2025-02-10", "2025-02-10")
    assert (single.start, single.end, single.last) == (
        date(2025, 2, 10),
        date(2025, 2, 11),
        single.start,
    )
    assert single.key == "2025-02-10..2025-02-10" and single.label == "10 Feb 2025"
    assert single.params == {"start": "2025-02-10", "end": "2025-02-10"}
    cross = date_range("2024-12-30", "2025-01-02")
    assert (cross.start, cross.end) == (date(2024, 12, 30), date(2025, 1, 3))
    assert cross.label == "30 Dec 2024 - 2 Jan 2025" and cross.months == ("2024-12", "2025-01")
    same_year = date_range("2025-02-10", "2025-03-14")
    assert same_year.label == "10 Feb - 14 Mar 2025"
    assert date_range("2025-01-31", "2025-04-29").months == (
        "2025-01",
        "2025-02",
        "2025-03",
        "2025-04",
    )


def test_calendar_months_keep_their_month_identity():
    for key, last in (
        ("2025-02", "2025-02-28"),
        ("2024-02", "2024-02-29"),
        ("2025-12", "2025-12-31"),
    ):
        month = calendar_month(key)
        assert month.is_month and month.key == key and month.params == {"month": key}
        assert date_range(month.start.isoformat(), last) == month
    feb = date_range("2025-02-01", "2025-02-28")
    assert feb.key == "2025-02" and feb.label == "February 2025"
    leap = date_range("2024-02-01", "2024-02-28")  # misses the 29th: not the whole month
    assert not leap.is_month and leap.key == "2024-02-01..2024-02-28"
    assert not date_range("2025-02-02", "2025-03-01").is_month
    assert calendar_month("2025-02").query == "after:1738339199 before:1740758400"


def test_range_query_and_receipt_filter_use_singapore_midnights():
    period = date_range("2025-02-10", "2025-02-10")
    after, before = (int(part.split(":")[1]) for part in period.query.split())
    assert before - (after + 1) == 86400
    midnight = datetime(2025, 2, 10, tzinfo=SINGAPORE)
    assert after + 1 == int(midnight.timestamp())
    assert before == int((midnight + timedelta(days=1)).timestamp())
    edges = {
        "before-midnight": (midnight - timedelta(microseconds=1), False),
        "midnight": (midnight, True),
        "last-instant": (midnight + timedelta(days=1, microseconds=-1), True),
        "next-midnight": (midnight + timedelta(days=1), False),
    }
    for name, (instant, inside) in edges.items():
        assert period.contains(instant) is inside, name
    # 00:30 on 1 March in Singapore is still 28 February in UTC.
    assert date_range("2025-03-01", "2025-03-05").contains(
        datetime(2025, 2, 28, 16, 30, tzinfo=UTC)
    )
    assert not date_range("2025-02-20", "2025-02-28").contains(
        datetime(2025, 2, 28, 16, 30, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2025-02-10", "2025-02-09"),  # reversed
        ("2025-2-10", "2025-02-11"),
        ("20250210", "20250211"),
        ("2025-02-10", ""),
        ("", "2025-02-10"),
        ("2025-02-30", "2025-03-01"),  # not a real date
        ("2025-13-01", "2025-13-02"),
        ("2025-02-10T00:00", "2025-02-11"),
        ("1969-12-31", "1970-01-02"),
        ("9998-12-31", "9999-01-01"),
        ("2025-01-01", "2025-04-01"),
        ("2025-01-01", "2026-01-01"),
        (" 2025-02-10", "2025-02-11"),
        ("２０２５-02-10", "2025-02-11"),
    ],
)
def test_invalid_reversed_or_over_limit_ranges_are_rejected(start, end):
    with pytest.raises(ValueError):
        date_range(start, end)


def test_selection_is_one_month_or_one_range_never_both_or_half():
    assert selected_range({"month": "2025-02"}).key == "2025-02"
    assert selected_range({"start": "2025-02-10", "end": "2025-02-12"}).label == (
        "10 Feb - 12 Feb 2025"
    )
    for params in (
        {"month": "2025-02", "start": "2025-02-01", "end": "2025-02-28"},
        {"month": "2025-02", "start": "2025-02-01"},
        {"start": "2025-02-01"},
        {"end": "2025-02-01"},
        {"month": ""},
        {"month": "all"},
        {"start": "", "end": ""},
    ):
        with pytest.raises(ValueError):
            selected_range(params)


def test_default_period_is_the_current_singapore_month(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 2, 28, 16, 30, tzinfo=UTC).astimezone(tz)  # 00:30 on 1 March

    monkeypatch.setattr("spending_optimiser.monthly.datetime", Clock)
    assert selected_range({}).key == "2025-03"
    assert [(label, period.key) for label, period in shortcuts()] == [
        ("This month", "2025-03"),
        ("Last month", "2025-02"),
        ("Last 3 months", "2025-01-01..2025-03-31"),
    ]


def test_shortcuts_follow_the_singapore_calendar_and_are_valid_ranges():
    now = datetime(2024, 3, 31, 16, 0, tzinfo=UTC)  # 00:00 on 1 April, Singapore
    chosen = dict(shortcuts(now))
    assert chosen["This month"].key == "2024-04"
    assert chosen["Last month"].key == "2024-03"
    last3 = chosen["Last 3 months"]
    assert (last3.start, last3.last) == (date(2024, 2, 1), date(2024, 4, 30))
    assert date_range(last3.start.isoformat(), last3.last.isoformat()) == last3


def test_a_range_object_cannot_exceed_the_limit_however_it_is_built():
    with pytest.raises(ValueError):
        DateRange(date(2025, 1, 1), date(2025, 4, 2))
    with pytest.raises(ValueError):
        DateRange(date(2025, 1, 2), date(2025, 1, 2))
    DateRange(date(2025, 1, 1), date(2025, 4, 1))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_browser_script_computes_the_same_latest_end_as_the_server():
    script = files("spending_optimiser").joinpath("static/period-picker.js")
    days = [date(2023, 10, 1) + timedelta(days=n) for n in range(0, 760)]
    days += [date(1970, 1, 1), date(9998, 12, 31)]
    program = (
        "const {latestEnd} = require(process.argv[1]);"
        "const days = JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(days.map(d => [d, latestEnd(d)])));"
        "console.log(JSON.stringify(latestEnd('not a date')));"
    )
    done = subprocess.run(
        [
            shutil.which("node"),
            "-e",
            program,
            str(script),
            json.dumps([day.isoformat() for day in days]),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    pairs, invalid = (json.loads(line) for line in done.stdout.splitlines())
    assert invalid == ""
    assert len(pairs) == len(days)
    for day, latest in pairs:
        assert latest == latest_end(date.fromisoformat(day)).isoformat(), day


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_browser_script_keeps_the_to_field_in_step_with_from():
    script = files("spending_optimiser").joinpath("static/period-picker.js")
    program = """
const fs = require("fs");
const vm = require("vm");
function input(name, value, min, max) {
  const listeners = {};
  return { name, value, min, max, listeners,
    addEventListener(kind, fn) { listeners[kind] = fn; },
    change(next) { this.value = next; listeners.change(); } };
}
// A page rendered for 1 February with the server's limit for that start.
const start = input("start", "2025-02-01", "1970-01-01", "9998-12-31");
const end = input("end", "2025-02-28", "2025-02-01", "2025-04-30");
const group = { querySelector: (q) => (q.includes("start") ? start : end) };
const sandbox = { document: { querySelectorAll: () => [group] } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
const seen = [];
const snap = (label) => seen.push([label, start.value, end.value, end.min, end.max]);
snap("loaded");
start.change("2025-03-15");  // later than the rendered limit allows: must widen it
snap("later");
start.change("2025-01-31");  // clamped month end
snap("clamped");
start.change("");            // cleared: nothing to enforce yet
snap("cleared");
start.change("2024-11-30");
snap("leap");
console.log(JSON.stringify(seen));
"""
    done = subprocess.run(
        [shutil.which("node"), "-e", program, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        ["loaded", "2025-02-01", "2025-02-28", "2025-02-01", "2025-04-30"],
        ["later", "2025-03-15", "2025-03-15", "2025-03-15", "2025-06-14"],
        ["clamped", "2025-01-31", "2025-03-15", "2025-01-31", "2025-04-29"],
        ["cleared", "", "2025-03-15", "2025-01-31", "2025-04-29"],
        ["leap", "2024-11-30", "2025-03-15", "2024-11-30", "2025-02-27"],
    ]
