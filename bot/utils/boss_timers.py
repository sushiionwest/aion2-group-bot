"""Pure timer maths for the boss timers board (no Discord dependency).

Two kinds of timer share the board:

* clock timers (rift, abyss bosses) follow a weekly schedule on the game
  server's clock, so the bot can work out every start on its own;
* respawn timers (field bosses) count from each kill, which only players
  see, so members report them and the bot stores the next spawn.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# The game server's clock when a server hasn't set one (NA West).
DEFAULT_TIMEZONE = "America/Los_Angeles"
# How long before a spawn its role is pinged, and how long a ping stays up.
DEFAULT_LEAD_MINUTES = 5
PING_LIFETIME_S = 5 * 60
# A reported field boss counts as "up" this long after its spawn time; past
# that nobody reported the kill and the timer is unknown again.
UP_WINDOW_S = 2 * 3600
# The longest countdown a member can enter.
MAX_COUNTDOWN_S = 48 * 3600


@dataclass(frozen=True)
class TimerDef:
    key: str
    name: str
    group: str  # "field", "abyss" or "rift"
    respawn_minutes: int | None = None  # respawn timers: minutes after a kill
    schedule: str | None = None  # clock timers: see parse_schedule
    duration_minutes: int = 0  # clock timers that stay "live" for a while


_EXECUTORS = "mon,thu,sat 21:30"
_NAHMA = "fri,sun 21:00"

TIMERS = (
    TimerDef("gartua", "Gartua", "field", respawn_minutes=11 * 60),
    TimerDef("lagta", "Lagta", "field", respawn_minutes=11 * 60),
    TimerDef("dartan", "Dartan", "field", respawn_minutes=6 * 60),
    TimerDef("kashapa", "Kashapa", "field", respawn_minutes=6 * 60),
    TimerDef("watcher_kaira", "Watcher Kaira", "abyss", schedule="daily 01:00/3h"),
    TimerDef("executor_tamasa", "Executor Tamasa", "abyss", schedule=_EXECUTORS),
    TimerDef("executor_argo", "Executor Argo", "abyss", schedule=_EXECUTORS),
    TimerDef("executor_kaira", "Executor Kaira", "abyss", schedule=_EXECUTORS),
    TimerDef("dramos", "Executioner Dramos", "abyss", schedule=_EXECUTORS),
    TimerDef("ducal", "Turncoat Ducal", "abyss", schedule=_EXECUTORS),
    TimerDef("marakha", "Ravager Marakha", "abyss", schedule=_EXECUTORS),
    TimerDef("nahma", "Guardian Lord Nahma", "abyss", schedule=_NAHMA),
    TimerDef("enraged_nahma", "Enraged Guardian Lord Nahma", "abyss", schedule=_NAHMA),
    TimerDef(
        "rift", "Spacetime Rift", "rift", schedule="daily 02:00/3h", duration_minutes=60
    ),
)
TIMERS_BY_KEY = {timer.key: timer for timer in TIMERS}
GROUPS = ("field", "abyss", "rift")  # display order on the board


@dataclass(frozen=True)
class Schedule:
    days: frozenset  # weekdays, 0 = Monday
    minutes: tuple  # minutes after local midnight, ascending


_TIME = re.compile(r"(\d{1,2}):(\d{2})(?:/(\d{1,2})h)?")


def parse_schedule(spec: str) -> Schedule:
    """Reads "<days> <times>", e.g. "mon,thu,sat 21:30" or "daily 01:00/3h".

    Days are "daily" or a comma list of weekday names. Times are a comma list
    of HH:MM; "HH:MM/Nh" repeats every N hours from HH:MM until midnight.
    Raises ValueError on anything else.
    """
    parts = spec.strip().lower().split(None, 1)
    if len(parts) != 2:
        raise ValueError(spec)
    days_part, times_part = parts
    if days_part == "daily":
        days = frozenset(range(7))
    else:
        # ValueError from .index() covers an unknown day name.
        days = frozenset(DAYS.index(day.strip()[:3]) for day in days_part.split(","))
    minutes = set()
    for chunk in times_part.replace(" ", "").split(","):
        match = _TIME.fullmatch(chunk)
        if match is None:
            raise ValueError(spec)
        hour, minute = int(match[1]), int(match[2])
        every = int(match[3]) if match[3] else None
        if hour > 23 or minute > 59 or (every is not None and not 1 <= every <= 24):
            raise ValueError(spec)
        start = hour * 60 + minute
        minutes.update(range(start, 24 * 60, every * 60) if every else [start])
    return Schedule(days, tuple(sorted(minutes)))


def _starts(schedule: Schedule, day, tz: ZoneInfo, *, reverse: bool = False):
    if day.weekday() not in schedule.days:
        return
    for minute in reversed(schedule.minutes) if reverse else schedule.minutes:
        local = datetime.combine(day, clock_time(minute // 60, minute % 60), tz)
        yield int(local.timestamp())


def next_start(schedule: Schedule, now_ts: int, tz: ZoneInfo) -> int:
    """The first scheduled start strictly after now."""
    today = datetime.fromtimestamp(now_ts, tz).date()
    for offset in range(9):
        for ts in _starts(schedule, today + timedelta(days=offset), tz):
            if ts > now_ts:
                return ts
    raise ValueError("schedule has no starts")


def last_start(schedule: Schedule, now_ts: int, tz: ZoneInfo) -> int | None:
    """The latest scheduled start at or before now, within the past week."""
    today = datetime.fromtimestamp(now_ts, tz).date()
    for offset in range(9):
        day = today - timedelta(days=offset)
        for ts in _starts(schedule, day, tz, reverse=True):
            if ts <= now_ts:
                return ts
    return None


_COLON = re.compile(r"(\d{1,3}):(\d{2})(?::(\d{2}))?")
_UNITS = re.compile(r"(?:(\d{1,3})h)?(?:(\d{1,4})m?)?")


def parse_countdown(text: str) -> int | None:
    """Seconds until a spawn, as typed by a member; None when left empty.

    Accepts the in-game countdown ("5:42" or "5:42:10" = hours:minutes), "5h",
    "5h42m", "42m", or a bare number of minutes. Raises ValueError otherwise.
    """
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    match = _COLON.fullmatch(text)
    if match:
        hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    else:
        match = _UNITS.fullmatch(text)
        if match is None:
            raise ValueError(text)
        hours, minutes = (int(g) if g else 0 for g in match.groups())
        seconds = 0
    total = hours * 3600 + minutes * 60 + seconds
    if minutes > 59 and hours or seconds > 59 or total > MAX_COUNTDOWN_S:
        raise ValueError(text)
    return total


@dataclass(frozen=True)
class TimerState:
    next_at: int | None = None  # the upcoming spawn / start
    live_until: int | None = None  # clock timer currently running
    up_since: int | None = None  # field boss whose spawn time has passed
    set_by: int | None = None  # member who reported a field boss


def _value(row, column):
    return row[column] if row is not None else None


def timer_schedule(timer: TimerDef, row) -> Schedule:
    """A clock timer's schedule: the server's own override, else the default."""
    override = _value(row, "schedule")
    if override:
        try:
            return parse_schedule(override)
        except ValueError:
            pass  # a stored override is validated on write; never trust blindly
    return parse_schedule(timer.schedule)


def respawn_seconds(timer: TimerDef, row) -> int:
    return (_value(row, "respawn_minutes") or timer.respawn_minutes) * 60


def timer_state(timer: TimerDef, row, now_ts: int, tz: ZoneInfo) -> TimerState:
    """Where one timer stands right now. `row` is its stored row, or None."""
    if timer.schedule is not None:
        schedule = timer_schedule(timer, row)
        live_until = None
        if timer.duration_minutes:
            started = last_start(schedule, now_ts, tz)
            if started is not None and started + timer.duration_minutes * 60 > now_ts:
                live_until = started + timer.duration_minutes * 60
        return TimerState(next_start(schedule, now_ts, tz), live_until)

    spawn_at = _value(row, "spawn_at")
    set_by = _value(row, "set_by")
    if spawn_at is None or now_ts - spawn_at > UP_WINDOW_S:
        return TimerState()
    if spawn_at > now_ts:
        return TimerState(next_at=spawn_at, set_by=set_by)
    return TimerState(up_since=spawn_at, set_by=set_by)


def due_pings(rows: dict, now_ts: int, tz: ZoneInfo, lead_s: int) -> list:
    """(timer, spawn_ts) for every timer with a role whose spawn is within the
    lead time and hasn't been pinged for yet."""
    due = []
    for timer in TIMERS:
        row = rows.get(timer.key)
        if not _value(row, "role_id"):
            continue
        spawn = timer_state(timer, row, now_ts, tz).next_at
        if spawn is None or spawn - now_ts > lead_s:
            continue
        if row["pinged_for"] != spawn:
            due.append((timer, spawn))
    return due
