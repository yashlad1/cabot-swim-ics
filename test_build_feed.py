"""Offline tests. No network, no fixtures needed -- these cover the parts that
are already finished, so a broken parse_sessions() can't hide a broken writer.

Run: python3 -m pytest test_build_feed.py -q
"""

from datetime import date, time

import pytest

from build_feed import Session, build_ics, count_events, esc, fold, guard_or_die


def sample():
    return [
        Session(date(2026, 9, 21), time(11, 45), time(14, 15)),
        Session(date(2026, 9, 21), time(19, 45), time(21, 0), "Open Swim (evening)"),
        Session(date(2026, 9, 26), time(10, 0), time(12, 0), "Open Swim", "Saturday; lanes 1-6"),
    ]


def test_crlf_everywhere():
    ics = build_ics(sample())
    assert "\r\n" in ics
    assert ics.endswith("\r\n")
    # No bare LF anywhere.
    assert "\n" not in ics.replace("\r\n", "")


def test_every_line_within_75_octets():
    ics = build_ics(sample())
    for line in ics.split("\r\n"):
        assert len(line.encode()) <= 75, f"unfolded: {line!r}"


def test_folded_location_round_trips():
    # LOCATION is long enough to force a fold; unfolding must restore it.
    ics = build_ics(sample())
    unfolded = ics.replace("\r\n ", "")
    assert "400 Huntington Ave" in unfolded


def test_semicolons_and_commas_escaped():
    ics = build_ics(sample())
    unfolded = ics.replace("\r\n ", "")
    assert "Saturday\\; lanes 1-6" in unfolded
    # The literal comma in the address must be escaped too.
    assert "Boston\\, MA" in unfolded


def test_uid_is_stable_and_unique():
    a = build_ics(sample())
    b = build_ics(list(reversed(sample())))
    assert a == b, "output must not depend on input ordering"

    uids = {s.uid() for s in sample()}
    assert len(uids) == 3


def test_output_is_byte_identical_across_runs():
    # Guards the no-op-commit optimisation: nothing clock-derived in the file.
    assert build_ics(sample()) == build_ics(sample())


def test_event_count():
    assert count_events(build_ics(sample())) == 3


def test_guard_rejects_thin_parse():
    with pytest.raises(SystemExit, match="min 5"):
        guard_or_die(sample(), None)


def test_guard_rejects_collapse():
    many = [Session(date(2026, 10, d), time(12, 0), time(13, 0)) for d in range(1, 21)]
    previous = build_ics(many)
    survivors = many[:6]
    with pytest.raises(SystemExit, match="fell 20"):
        guard_or_die(survivors, previous)


def test_guard_allows_normal_variation():
    many = [Session(date(2026, 10, d), time(12, 0), time(13, 0)) for d in range(1, 21)]
    guard_or_die(many[:15], build_ics(many))  # 25% drop, fine


def test_esc_and_fold_units():
    assert esc("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"
    assert fold("x" * 80).split("\r\n ")[0] == "x" * 75
    assert fold("é" * 50).count("\r\n ") == 1  # no split mid-codepoint
