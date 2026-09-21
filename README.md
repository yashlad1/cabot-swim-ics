# cabot-swim-ics

A subscribable calendar feed for open swim at the Barletta Natatorium
(Cabot Physical Education Center, Northeastern Boston).

Northeastern renders the schedule inside a standalone app embedded by iframe on
`recreation.northeastern.edu`, and publishes no iCal feed for it. This reads the
JSON that app runs on, every 3 hours, and republishes it as `.ics` on GitHub
Pages so Apple Calendar can subscribe.

Source: <https://nu-universityrecreationcalendars.netlify.app/cabot-pool-open-swim>

## State

| Piece | Status |
|---|---|
| ICS writer (`build_ics`) | done, 11 tests passing |
| Fail-closed guards | done |
| Cancellation alerts | done, emailed as an assigned issue |
| Actions workflow | done |
| `parse_sessions()` | done, 23 tests against a saved fixture |

## What the source actually is

Checked, so nobody has to check again:

- **Not a Google Calendar embed.** The only `googleapis` hits on the page are
  font preconnects. There is no `calendarId` and no public `basic.ics` to
  subscribe to instead.
- **It is a JSON endpoint.** The page is a client-rendered shell — `curl` gets
  a 25 KB skeleton whose `#scheduleContent` div just says "Loading schedule…",
  with no times in the markup at all. Its inline JS fetches
  `/.netlify/functions/cabotpool?startdate=YYYYMMDD&days=30`, a Netlify
  function proxying [25Live][25l]. `fetch()` points there, so no HTML parsing
  and no headless browser.

[25l]: https://25livepub.collegenet.com/calendars/cabot-center-swimming-pool-events

Each record is a flat object with a stable 26-key schema. Open swim is the
intersection of `template == "Boston - Open Recreation"` and a title containing
`open swim`; the same pool carries ~107 varsity bookings in any 30-day window
(`Boston - Athletics Practice`, titled `WSWIM`, `M/W Swim`, …) that must not
leak into the feed.

`startDateTime` is **naive local wall-clock**, with the real UTC offset in a
separate field — verified against a window straddling 1 November, where the
offset flips `-0400` → `-0500` while the wall-clock times stay put. So the
times are emitted as-is under `TZID`, and the parser cross-checks each declared
offset against `zoneinfo`: if 25Live ever switches to UTC, the timestamps would
still parse and be silently four hours wrong.

`fixtures/cabotpool-20260921.json` is a real unedited response. Tests parse it
offline; the suite never touches the network.

**Raises, never skips.** Unparseable times, an all-day open swim, a session
spanning midnight, an offset that isn't New York's, duplicate sessions, or a
payload that isn't a list all fail the build. So does any disagreement between
the two markers — a booking titled *Open Swim* under a renamed template, or an
unrecognised open-rec booking like *Family Swim Hour*. That pairing is the one
that matters: if 25Live recategorises, the filter silently matches nothing and
the feed empties, which is exactly the failure that gets you to a locked pool.

## Setup

```bash
pip install -r requirements.txt
python -m pytest -q          # 34 pass, all offline
python build_feed.py         # writes docs/cabot-swim.ics
```

Then:

- Push to GitHub.
- **Settings → Pages → Source: Deploy from a branch → `main` → `/docs`.**
- **Settings → Actions → General → Workflow permissions → Read and write.**
- Actions tab → *Rebuild swim feed* → Run workflow, to confirm it works before
  trusting the cron.

Feed lands at `https://<you>.github.io/cabot-swim-ics/cabot-swim.ics`.

## Subscribe

**macOS** — Calendar → File → New Calendar Subscription → paste the URL.
Set **Location: iCloud**, not *On My Mac*, or it won't reach your phone.
Auto-refresh: **Every hour**. The feed itself only rebuilds every 3 hours,
so hourly bounds your worst-case lag at ~4h without hammering GitHub Pages.

**iPhone** — Settings → Apps → Calendar → Calendar Accounts → Add Account →
Other → Add Subscribed Calendar. (Older iOS: Settings → Calendar → Accounts →
Add Account → Other.)

Only subscribe on one device. Subscribing on both gets you two copies of
every event.

## Design notes

**Stable UIDs.** `Session.uid()` is a hash of date + times + title, so an
unchanged session keeps its identity across rebuilds. If UIDs churned, every
refresh would delete and recreate all events, destroying any alerts you'd set
and spamming notifications.

**Deterministic output.** `DTSTAMP` is derived from the event date, not the
clock, so an unchanged schedule produces a byte-identical file and the workflow
skips the commit. Otherwise you'd accumulate one empty commit per day forever.

**Fail closed.** `guard_or_die()` refuses to publish if the parse yields fewer
than 5 sessions, or under half the previous count. A silent empty publish is
the worst outcome: your calendar quietly empties and you show up to a locked
pool. On failure the old feed keeps serving and GitHub emails you.

**Change alerts compare the overlap, not the file.** A changed feed is not
news: the window rolls forward every day, so the oldest day leaves and a new
one arrives on its own. `describe_changes()` clamps the comparison to the dates
*both* the old and new feed cover, and reports only what was cancelled or
moved inside it. Without that clamp the alert fires daily and you learn to
ignore it, which is worse than having no alert. If the two windows don't
overlap at all — the cron was off for a month — it stays quiet rather than
claiming 44 cancellations.

When there is something to say, `build_feed.py` writes `changes.md` and the
workflow files it as an issue **assigned to you**. Assignment is the part that
matters: GitHub does not email on pushes, and assignment notifies regardless
of your watch setting. A total collapse to zero sessions is not this path —
`guard_or_die()` exits, the feed keeps serving yesterday's file, and GitHub
emails you the failed run.

**`TRANSP:TRANSPARENT`.** Events don't mark you busy, so they don't block
meeting invites.

**VTIMEZONE is included.** Without it some clients shift events an hour when
EDT ends on 1 November.

## Known gotchas

- GitHub disables scheduled workflows in repos with ~60 days of no activity.
  It emails first; click re-enable, or push any commit.
- The workflow rebuilds every 3 hours, but that only controls how fast a new
  schedule reaches the *feed*. Apple's subscription refresh is lazy and
  `X-PUBLISHED-TTL:PT3H` is a hint, not a guarantee — iCloud-hosted
  subscriptions can still lag by hours. Set the calendar's own auto-refresh
  to its shortest option if you want it keeping up.
- GitHub throttles scheduled workflows under load; a 3-hourly cron can be
  delayed or occasionally skipped. Runs that find no change skip the commit,
  so the extra frequency costs nothing but Actions minutes.
- Rec schedules move for swim meets, lessons, lifeguard shortages, semester
  breaks, and maintenance. The aquatics area has been closed for deep cleaning
  before. Treat the calendar as a prompt to swim, not proof the pool is open.
