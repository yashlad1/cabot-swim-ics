#!/usr/bin/env python3
"""
Build a subscribable .ics feed from the Northeastern Cabot pool open-swim schedule.

The schedule lives in a standalone app embedded by iframe on
recreation.northeastern.edu, not in the WordPress site itself:
    https://nu-universityrecreationcalendars.netlify.app/cabot-pool-open-swim

Only parse_sessions() is source-specific. Everything else is format-independent
and already works -- see test_build_feed.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import requests

SOURCE_URL = "https://nu-universityrecreationcalendars.netlify.app/cabot-pool-open-swim"
# SOURCE_URL is a JS shell: it renders nothing server-side and fetches this
# Netlify function, which proxies 25Live. Parse the JSON, not the markup.
API_URL = "https://nu-universityrecreationcalendars.netlify.app/.netlify/functions/cabotpool"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "cabot-swim.ics")
# Written only when a session is cancelled or moved. Its presence is the
# signal the workflow uses to open an issue, which is what mails you.
CHANGES_PATH = os.path.join(os.path.dirname(__file__), "changes.md")

CALENDAR_NAME = "Cabot Open Swim"
LOCATION = "Barletta Natatorium, Cabot Physical Education Center, 400 Huntington Ave, Boston, MA 02115"
TZID = "America/New_York"

# 25Live categorises every booking at the pool. Open swim is the intersection
# of this template and this title; varsity practices share the pool but carry
# template 'Boston - Athletics Practice' and titles like 'WSWIM'.
OPEN_REC_TEMPLATE = "Boston - Open Recreation"
OPEN_SWIM_TITLE = "open swim"

# Fail-closed thresholds. See guard_or_die().
MIN_SESSIONS = 5
MAX_SHRINK = 0.5


@dataclass(frozen=True, order=True)
class Session:
    """One open-swim block on one calendar day, in local Boston time."""

    day: date
    start: time
    end: time
    title: str = "Open Swim"
    note: str = ""

    def uid(self) -> str:
        # Stable across runs so subscribers see updates, not delete-and-recreate.
        # If this churns, Apple Calendar drops and re-adds every event on every
        # refresh, which nukes any alerts the subscriber set and spams them.
        seed = f"{self.day}|{self.start}|{self.end}|{self.title}"
        return hashlib.sha1(seed.encode()).hexdigest()[:20] + "@cabot-swim"


# --------------------------------------------------------------------------
# Source-specific. THIS IS THE PART TO WRITE.
# --------------------------------------------------------------------------

def fetch(url: str = API_URL) -> str:
    # Same 30-day window the page's own JS asks for. A wider window is accepted
    # but returns no more than the source has published (~6 weeks), so the count
    # would swing week to week and trip guard_or_die's shrink check for nothing.
    resp = requests.get(
        url,
        params={"startdate": date.today().strftime("%Y%m%d"), "days": 30},
        timeout=30,
        headers={"User-Agent": "cabot-swim-ics/1.0"},
    )
    resp.raise_for_status()
    return resp.text


def parse_sessions(payload: str) -> list[Session]:
    """Turn the 25Live JSON from API_URL into absolute-dated Sessions.

    Raises on anything it doesn't recognise. A parser that quietly drops half
    the schedule is worse than one that fails the build, because the feed keeps
    serving and nobody finds out until they're standing at a locked pool.
    """
    events = json.loads(payload)
    if not isinstance(events, list):
        raise ValueError(f"expected a JSON list of events, got {type(events).__name__}")

    zone = ZoneInfo(TZID)
    sessions = []

    for e in events:
        eid = e.get("eventID", "?")
        is_open_rec = e["template"] == OPEN_REC_TEMPLATE
        is_open_swim = OPEN_SWIM_TITLE in e["title"].lower()

        # Two independent markers that always agree in practice. If they stop
        # agreeing, 25Live has been recategorised and our filter is now either
        # dropping real sessions or about to admit varsity practice. Either way
        # it needs a human, not a guess.
        if is_open_rec != is_open_swim:
            raise ValueError(
                f"event {eid}: template {e['template']!r} and title {e['title']!r} disagree "
                f"about whether this is open swim -- source recategorised, check the filter"
            )
        if not is_open_rec:
            continue

        if e["canceled"]:
            continue  # recognised and deliberately dropped, not a silent skip
        if e["allDay"]:
            raise ValueError(f"event {eid}: open swim marked allDay, so it carries no usable times")

        start = datetime.fromisoformat(e["startDateTime"])
        end = datetime.fromisoformat(e["endDateTime"])

        # startDateTime is naive local wall-clock, with the real offset in a
        # separate field. Emitting the wall-clock under TZID is therefore
        # correct -- but only while that offset is genuinely New York's for
        # that date. If 25Live ever switches to UTC the times still parse and
        # are silently four hours wrong, which this catches.
        for label, dt in (("start", start), ("end", end)):
            declared = e[f"{label}TimeZoneOffset"]
            expected = dt.replace(tzinfo=zone).strftime("%z")
            if declared != expected:
                raise ValueError(
                    f"event {eid}: {label} {dt} declares offset {declared}, "
                    f"but {TZID} is {expected} on that date"
                )

        if end <= start:
            raise ValueError(f"event {eid}: ends {end} at or before it starts {start}")
        if end.date() != start.date():
            raise ValueError(f"event {eid}: spans midnight ({start} -> {end}); Session is single-day")

        sessions.append(Session(start.date(), start.time(), end.time(), e["title"]))

    # Duplicate UIDs make a malformed feed that calendar clients resolve however
    # they feel like. Cheaper to fail here than to debug it in Apple Calendar.
    dupes = [s for s, n in Counter(sessions).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate sessions in source: {sorted(map(str, dupes))}")

    return sessions


# --------------------------------------------------------------------------
# Format-independent below here.
# --------------------------------------------------------------------------

def esc(text: str) -> str:
    """RFC 5545 s3.3.11 text escaping."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """RFC 5545 s3.1: fold content lines at 75 octets, continuations start with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, start = [], 0
    limit = 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        # Don't split a multi-byte character.
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(raw[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines carry a leading space
    return "\r\n ".join(chunks)


VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "BEGIN:DAYLIGHT",
    "DTSTART:19700308T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
    "TZOFFSETFROM:-0500",
    "TZOFFSETTO:-0400",
    "TZNAME:EDT",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "DTSTART:19701101T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
    "TZOFFSETFROM:-0400",
    "TZOFFSETTO:-0500",
    "TZNAME:EST",
    "END:STANDARD",
    "END:VTIMEZONE",
]


def build_ics(sessions: list[Session]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//cabot-swim-ics//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(CALENDAR_NAME)}",
        f"X-WR-TIMEZONE:{TZID}",
        "X-PUBLISHED-TTL:PT3H",  # matches the 3-hourly rebuild; a hint, not a guarantee
        *VTIMEZONE,
    ]

    for s in sorted(sessions):
        dt_start = datetime.combine(s.day, s.start).strftime("%Y%m%dT%H%M%S")
        dt_end = datetime.combine(s.day, s.end).strftime("%Y%m%dT%H%M%S")
        # Deterministic DTSTAMP: derived from the event, not from clock time, so
        # an unchanged schedule produces a byte-identical file and the workflow
        # skips the commit instead of churning one per day forever.
        stamp = s.day.strftime("%Y%m%dT000000Z")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{s.uid()}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={TZID}:{dt_start}",
            f"DTEND;TZID={TZID}:{dt_end}",
            f"SUMMARY:{esc(s.title)}",
            f"LOCATION:{esc(LOCATION)}",
        ]
        if s.note:
            lines.append(f"DESCRIPTION:{esc(s.note)}")
        lines += [
            "TRANSP:TRANSPARENT",  # don't show the subscriber as busy
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(ln) for ln in lines) + "\r\n"


# Inverse of build_ics, enough of it to diff two feeds. Field order is the one
# build_ics writes; unfolding first because LOCATION wraps past 75 octets.
_EVENT_RE = re.compile(
    r"BEGIN:VEVENT.*?"
    r"DTSTART;TZID=[^:]+:(\d{8}T\d{6}).*?"
    r"DTEND;TZID=[^:]+:(\d{8}T\d{6}).*?"
    r"SUMMARY:(.*?)\r\n.*?END:VEVENT",
    re.DOTALL,
)


def read_ics(text: str) -> list[Session]:
    out = []
    for start, end, title in _EVENT_RE.findall(text.replace("\r\n ", "")):
        s = datetime.strptime(start, "%Y%m%dT%H%M%S")
        e = datetime.strptime(end, "%Y%m%dT%H%M%S")
        title = re.sub(r"\\([\\;,n])", lambda m: "\n" if m.group(1) == "n" else m.group(1), title)
        out.append(Session(s.date(), s.time(), e.time(), title))
    return out


def describe_changes(previous: str, sessions: list[Session]) -> str:
    """Sessions cancelled or moved since the last build. "" if none.

    Only compares dates *both* feeds cover. The window rolls forward daily, so
    without that clamp every single run would report the day falling off the
    front as a cancellation and the day appearing at the back as an addition,
    and the alert would be noise inside a week.
    """
    old = read_ics(previous)
    if not old or not sessions:
        return ""

    lo = max(min(s.day for s in old), min(s.day for s in sessions))
    hi = min(max(s.day for s in old), max(s.day for s in sessions))
    if lo > hi:
        return ""  # feeds don't overlap at all; nothing to compare

    was = {s for s in old if lo <= s.day <= hi}
    now = {s for s in sessions if lo <= s.day <= hi}
    gone, added = sorted(was - now), sorted(now - was)
    if not gone and not added:
        return ""

    out = []
    for label, group in (("Gone from the schedule", gone), ("New or moved to", added)):
        if group:
            out += [f"**{label}:**", ""]
            out += [f"- {s.day:%a %Y-%m-%d} {s.start:%H:%M}-{s.end:%H:%M} {s.title}" for s in group]
            out += [""]
    out += [f"Compared {lo} to {hi}, the range both feeds cover. Source: <{SOURCE_URL}>"]
    return "\n".join(out)


def count_events(ics: str) -> int:
    return len(re.findall(r"^BEGIN:VEVENT", ics, flags=re.MULTILINE))


def guard_or_die(sessions: list[Session], previous: str | None) -> None:
    """Refuse to publish a suspicious feed.

    A silent empty publish is the worst outcome: the subscriber's calendar
    quietly empties and they show up to a locked pool. Better to fail the
    Action loudly and keep serving yesterday's file.
    """
    if len(sessions) < MIN_SESSIONS:
        sys.exit(f"REFUSING: parsed only {len(sessions)} sessions (min {MIN_SESSIONS}). Source likely changed.")

    if previous:
        before = count_events(previous)
        if before and len(sessions) < before * MAX_SHRINK:
            sys.exit(
                f"REFUSING: session count fell {before} -> {len(sessions)} "
                f"(>{int((1 - MAX_SHRINK) * 100)}% drop). Verify by hand, then rerun."
            )


def main() -> int:
    previous = None
    if os.path.exists(OUTPUT_PATH):
        # newline="" or Python folds the file's CRLFs to LF on read, and the
        # comparison below can never match what build_ics() produces.
        with open(OUTPUT_PATH, encoding="utf-8", newline="") as fh:
            previous = fh.read()

    sessions = parse_sessions(fetch())
    guard_or_die(sessions, previous)

    ics = build_ics(sessions)
    if ics == previous:
        print("No change.")
        return 0

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="") as fh:
        fh.write(ics)
    print(f"Wrote {len(sessions)} sessions to {OUTPUT_PATH}")

    # A changed file is not news by itself -- the window rolls every day. Only a
    # cancellation or a moved time is worth mailing about.
    report = describe_changes(previous, sessions) if previous else ""
    if report:
        print("\n" + report)
        with open(CHANGES_PATH, "w", encoding="utf-8") as fh:
            fh.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
