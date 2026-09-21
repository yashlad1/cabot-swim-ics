"""Offline parser tests. Run against a real 25Live response saved under
fixtures/, so the suite never touches the network.

Refresh the fixture with:
    curl "https://nu-universityrecreationcalendars.netlify.app/.netlify/functions/cabotpool?startdate=20260921&days=30" \
        -o fixtures/cabotpool-20260921.json

Run: python3 -m pytest test_parse_sessions.py -q
"""

import json
import os
from datetime import date, time, timedelta

import pytest

from build_feed import (
    Session,
    build_ics,
    count_events,
    describe_changes,
    parse_sessions,
    read_ics,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "cabotpool-20260921.json")


def raw():
    with open(FIXTURE, encoding="utf-8") as fh:
        return fh.read()


def mutated(**patch):
    """The fixture with `patch` applied to its first open-swim event."""
    events = json.loads(raw())
    target = next(e for e in events if e["title"] == "Open Swim")
    target.update(patch)
    return json.dumps(events)


def test_fixture_parses():
    sessions = parse_sessions(raw())
    assert len(sessions) == 44
    assert all(isinstance(s, Session) for s in sessions)


def test_returns_absolute_dates_not_recurrence():
    # Every session is pinned to one real calendar day; 26 distinct days here.
    sessions = parse_sessions(raw())
    assert all(isinstance(s.day, date) for s in sessions)
    assert len({s.day for s in sessions}) == 26
    assert all(s.start < s.end for s in sessions)


def test_known_session_present():
    sessions = parse_sessions(raw())
    assert Session(date(2026, 9, 21), time(11, 45), time(14, 15), "Open Swim") in sessions


def test_varsity_practice_excluded():
    # 107 athletics bookings share this pool; none may leak into the feed.
    titles = {s.title for s in parse_sessions(raw())}
    assert titles == {"Open Swim"}


def test_survives_dst_boundary_offsets():
    # Fixture is all EDT. Prove the offset check accepts EST too, rather than
    # only passing because every record happens to say -0400.
    payload = mutated(
        startDateTime="2026-11-02T11:45:00",
        endDateTime="2026-11-02T14:15:00",
        startTimeZoneOffset="-0500",
        endTimeZoneOffset="-0500",
    )
    assert Session(date(2026, 11, 2), time(11, 45), time(14, 15), "Open Swim") in parse_sessions(payload)


def test_canceled_session_dropped():
    assert len(parse_sessions(mutated(canceled=True))) == 43


# --- everything below must raise rather than silently drop a session ---

def test_raises_on_non_list_payload():
    with pytest.raises(ValueError, match="expected a JSON list"):
        parse_sessions('{"events": []}')


def test_raises_when_template_renamed():
    # The scenario that would silently empty the feed: title still says open
    # swim, template no longer matches, filter drops all 44.
    with pytest.raises(ValueError, match="disagree"):
        parse_sessions(mutated(template="Boston - Aquatics Open Rec"))


def test_raises_on_unknown_open_rec_booking():
    # Open recreation at the pool that isn't open swim: a real session we'd
    # otherwise never notice we were dropping.
    with pytest.raises(ValueError, match="disagree"):
        parse_sessions(mutated(title="Family Swim Hour"))


def test_raises_on_wrong_timezone_offset():
    # 25Live switching to UTC still parses cleanly and is four hours wrong.
    with pytest.raises(ValueError, match="declares offset"):
        parse_sessions(mutated(startTimeZoneOffset="+0000"))


def test_raises_on_all_day():
    with pytest.raises(ValueError, match="allDay"):
        parse_sessions(mutated(allDay=True))


def test_raises_on_backwards_times():
    with pytest.raises(ValueError, match="before it starts"):
        parse_sessions(mutated(endDateTime="2026-09-21T09:00:00"))


def test_raises_on_midnight_spanning_session():
    with pytest.raises(ValueError, match="spans midnight"):
        parse_sessions(mutated(endDateTime="2026-09-22T01:00:00", endTimeZoneOffset="-0400"))


def test_raises_on_duplicate_sessions():
    events = json.loads(raw())
    swim = next(e for e in events if e["title"] == "Open Swim")
    events.append({**swim, "eventID": swim["eventID"] + 1})
    with pytest.raises(ValueError, match="duplicate sessions"):
        parse_sessions(json.dumps(events))


def test_end_to_end_fixture_to_ics():
    ics = build_ics(parse_sessions(raw()))
    assert count_events(ics) == 44
    assert "RRULE" not in ics.split("END:VTIMEZONE")[1]  # VTIMEZONE's own rules only
    assert "DTSTART;TZID=America/New_York:20260921T114500" in ics


# --- change detection: what gets you an email, and what must not ---

def feed(sessions):
    return build_ics(sessions)


def test_read_ics_round_trips_build_ics():
    original = parse_sessions(raw())
    assert sorted(read_ics(feed(original))) == sorted(original)


def test_read_ics_unescapes_title():
    tricky = [Session(date(2026, 10, 1), time(12, 0), time(13, 0), "Open Swim; lanes 1,2")]
    assert read_ics(feed(tricky)) == tricky


def test_identical_feeds_report_nothing():
    original = parse_sessions(raw())
    assert describe_changes(feed(original), original) == ""


def test_rolling_window_reports_nothing():
    # The one that decides whether this alert is useful or ignored noise: the
    # window slides forward every day, so the oldest day leaves and a new one
    # arrives on its own. Neither is a schedule change.
    original = parse_sessions(raw())
    oldest, newest = min(s.day for s in original), max(s.day for s in original)
    rolled = [s for s in original if s.day != oldest]
    rolled += [Session(newest + timedelta(days=1), s.start, s.end, s.title)
               for s in original if s.day == oldest]
    assert describe_changes(feed(original), rolled) == ""


def test_cancelled_session_is_reported():
    original = parse_sessions(raw())
    victim = sorted(original)[10]
    report = describe_changes(feed(original), [s for s in original if s != victim])
    assert "Gone from the schedule" in report
    assert f"{victim.day:%a %Y-%m-%d} {victim.start:%H:%M}" in report
    assert "New or moved to" not in report


def test_moved_session_appears_on_both_lists():
    original = parse_sessions(raw())
    victim = sorted(original)[10]
    moved = Session(victim.day, time(20, 0), time(21, 0), victim.title)
    report = describe_changes(feed(original), [s for s in original if s != victim] + [moved])
    assert "Gone from the schedule" in report and "New or moved to" in report
    assert "20:00-21:00" in report


def test_non_overlapping_feeds_report_nothing():
    # Cron disabled for a month: the two windows share no dates, so every
    # session would look cancelled. Better to say nothing than 44 false alarms.
    original = parse_sessions(raw())
    future = [Session(s.day + timedelta(days=365), s.start, s.end, s.title) for s in original]
    assert describe_changes(feed(original), future) == ""


def test_empty_side_reports_nothing():
    # A total collapse is guard_or_die's job (it exits and GitHub mails the
    # failure); this must not also try to diff it.
    assert describe_changes(feed(parse_sessions(raw())), []) == ""
    assert describe_changes("", parse_sessions(raw())) == ""
