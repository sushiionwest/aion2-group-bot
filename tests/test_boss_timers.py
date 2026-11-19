"""Boss timers: schedule maths, storage, the board and its pings. Run: pytest"""

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest
from discord.ext import commands

from bot import i18n
from bot.cogs.timers import (
    FIELD_TIMERS,
    SpawnModal,
    Timers,
    TimersBoardView,
    board_lead_minutes,
    board_timezone,
    build_timers_embed,
)
from bot.db import Database
from bot.utils.boss_timers import (
    TIMERS,
    TIMERS_BY_KEY,
    UP_WINDOW_S,
    due_pings,
    last_start,
    next_start,
    parse_countdown,
    parse_schedule,
    timer_state,
)

SGT = ZoneInfo("Asia/Singapore")  # UTC+8, no daylight saving
PACIFIC = ZoneInfo("America/Los_Angeles")


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


# Thursday 19 Nov 2026, 00:28 UTC = 08:28 on a UTC+8 server clock. At that
# moment the existing #timers embed (which runs on UTC+8) showed the countdowns
# asserted below, so this pins our maths to a real observation.
SEEN = ts("2026-11-19 00:28:00")


def _state(key, now=SEEN, tz=SGT, row=None):
    return timer_state(TIMERS_BY_KEY[key], row, now, tz)


def test_clock_timers_match_the_countdowns_seen_on_the_existing_embed():
    assert _state("watcher_kaira").next_at - SEEN == 92 * 60  # "in 2 hours"
    assert _state("executor_tamasa").next_at - SEEN == 13 * 3600 + 2 * 60
    assert _state("executor_kaira").next_at == _state("executor_argo").next_at
    assert _state("nahma").next_at - SEEN == 36 * 3600 + 32 * 60  # "in 2 days"
    rift = _state("rift")
    assert rift.live_until - SEEN == 32 * 60  # "LIVE, ends in 32 minutes"
    assert rift.next_at - SEEN == 2 * 3600 + 32 * 60


def test_the_same_schedule_follows_the_servers_own_clock():
    # 16:28 on Wednesday in Los Angeles: Thursday 21:30 is 29 hours away.
    assert _state("executor_kaira", tz=PACIFIC).next_at - SEEN == 29 * 3600 + 2 * 60
    assert _state("rift", tz=PACIFIC).live_until is None


def test_clock_times_survive_a_daylight_saving_change():
    # US clocks go back on Sunday 1 Nov 2026; 21:00 that day is still 21:00.
    saturday_noon = int(datetime(2026, 10, 31, 12, 0, tzinfo=PACIFIC).timestamp())
    spawn = datetime.fromtimestamp(_state("nahma", saturday_noon, PACIFIC).next_at)
    assert spawn.astimezone(PACIFIC).strftime("%a %H:%M") == "Sun 21:00"


@pytest.mark.parametrize(
    "spec,days,minutes",
    [
        ("mon,thu,sat 21:30", {0, 3, 5}, (1290,)),
        ("daily 10:30,22:30", set(range(7)), (630, 1350)),
        ("Daily 01:00/3h", set(range(7)), tuple(range(60, 1440, 180))),
        ("friday,sunday 21:00", {4, 6}, (1260,)),
    ],
)
def test_schedules_parse(spec, days, minutes):
    schedule = parse_schedule(spec)
    assert schedule.days == days and schedule.minutes == minutes


@pytest.mark.parametrize(
    "spec", ["", "daily", "funday 21:00", "mon 25:00", "mon 9pm", "mon 21:00/0h"]
)
def test_bad_schedules_are_refused(spec):
    with pytest.raises(ValueError):
        parse_schedule(spec)


def test_every_default_schedule_parses():
    for timer in TIMERS:
        assert (timer.schedule is None) != (timer.respawn_minutes is None)
        if timer.schedule:
            parse_schedule(timer.schedule)


def test_last_start_is_the_occurrence_at_or_before_now():
    schedule = parse_schedule("daily 02:00/3h")
    assert last_start(schedule, SEEN, SGT) == SEEN - 28 * 60
    assert next_start(schedule, SEEN - 28 * 60, SGT) == SEEN - 28 * 60 + 3 * 3600


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("", None),
        ("  ", None),
        ("5:42", 5 * 3600 + 42 * 60),
        ("5:42:10", 5 * 3600 + 42 * 60 + 10),
        ("0:07", 7 * 60),
        ("5h", 5 * 3600),
        ("5h 42m", 5 * 3600 + 42 * 60),
        ("42m", 42 * 60),
        ("90", 90 * 60),
        ("0", 0),
    ],
)
def test_countdowns_parse(text, seconds):
    assert parse_countdown(text) == seconds


@pytest.mark.parametrize("text", ["soon", "5:99", "49:00", "5h90m", "-3", "5:42pm"])
def test_bad_countdowns_are_refused(text):
    with pytest.raises(ValueError):
        parse_countdown(text)


def test_a_field_boss_goes_from_unknown_to_counting_to_up_to_unknown():
    row = {"spawn_at": SEEN + 600, "set_by": 7, "respawn_minutes": None}
    assert _state("gartua") == timer_state(TIMERS_BY_KEY["gartua"], None, SEEN, SGT)
    assert _state("gartua").next_at is None
    counting = _state("gartua", row=row)
    assert (counting.next_at, counting.set_by) == (SEEN + 600, 7)
    up = _state("gartua", now=SEEN + 900, row=row)
    assert (up.next_at, up.up_since) == (None, SEEN + 600)
    stale = _state("gartua", now=SEEN + 600 + UP_WINDOW_S + 1, row=row)
    assert stale.up_since is None and stale.next_at is None


def _row(**values):
    base = dict.fromkeys(
        ["role_id", "respawn_minutes", "schedule", "spawn_at", "set_by", "pinged_for"]
    )
    return {**base, **values}


def test_pings_are_due_only_inside_the_lead_time_and_only_once():
    kaira = TIMERS_BY_KEY["watcher_kaira"]
    spawn = _state("watcher_kaira").next_at
    rows = {
        "watcher_kaira": _row(role_id=1),
        "nahma": _row(),  # no role: never pinged
        "gartua": _row(role_id=2, spawn_at=spawn),
        "lagta": _row(role_id=3),  # no reported spawn
    }
    assert due_pings(rows, spawn - 301, SGT, 300) == []
    due = due_pings(rows, spawn - 300, SGT, 300)
    assert [(timer.key, at) for timer, at in due] == [
        ("gartua", spawn),
        ("watcher_kaira", spawn),
    ]
    rows["watcher_kaira"] = _row(role_id=1, pinged_for=spawn)
    assert [t.key for t, _ in due_pings(rows, spawn - 60, SGT, 300)] == ["gartua"]
    assert kaira.schedule == "daily 01:00/3h"


# ----- Storage -----


def _run(tmp_path, steps):
    async def go():
        db = Database(str(tmp_path / "timers.db"))
        await db.connect()
        try:
            return await steps(db)
        finally:
            await db.close()

    return asyncio.run(go())


def test_a_ping_is_claimed_once_per_spawn(tmp_path):
    async def steps(db):
        return [
            await db.claim_boss_ping(1, "gartua", 100),
            await db.claim_boss_ping(1, "gartua", 100),
            await db.claim_boss_ping(1, "gartua", 200),
            await db.claim_boss_ping(2, "gartua", 100),
        ]

    assert _run(tmp_path, steps) == [True, False, True, True]


def test_settings_reports_and_roles_are_kept_apart(tmp_path):
    async def steps(db):
        await db.set_boss_timer_config(1, "gartua", role_id=55, respawn_minutes=720)
        await db.set_boss_spawn(1, "gartua", 5000, 9)
        await db.set_boss_timer_config(1, "gartua", role_id=None)
        await db.set_timer_settings(1, timezone="Asia/Tokyo")
        await db.set_timer_settings(1, lead_minutes=10)
        await db.set_timer_board(1, 20, 30)
        with pytest.raises(ValueError):
            await db.set_boss_timer_config(1, "gartua", spawn_at=1)
        return (
            (await db.get_boss_timers(1))["gartua"],
            await db.get_timer_board(1),
            await db.get_boss_timers(2),
            len(await db.timer_boards()),
        )

    row, board, other, posted = _run(tmp_path, steps)
    assert (row["role_id"], row["respawn_minutes"]) == (None, 720)
    assert (row["spawn_at"], row["set_by"]) == (5000, 9)
    assert (board["timezone"], board["lead_minutes"]) == ("Asia/Tokyo", 10)
    assert (board["channel_id"], board["message_id"]) == (20, 30)
    assert other == {} and posted == 1
    assert board_timezone(board).key == "Asia/Tokyo" and board_lead_minutes(board) == 10


def test_defaults_apply_before_a_server_sets_anything():
    assert board_timezone(None).key == "America/Los_Angeles"
    assert board_lead_minutes(None) == 5
    assert board_timezone({"timezone": "Not/AZone"}).key == "America/Los_Angeles"


def test_ping_messages_are_swept_when_due(tmp_path):
    async def steps(db):
        await db.add_timer_ping(1, 10, 100)
        await db.add_timer_ping(2, 10, 200)
        due = [p["message_id"] for p in await db.due_timer_pings(150)]
        await db.remove_timer_ping(1)
        return due, [p["message_id"] for p in await db.due_timer_pings(300)]

    assert _run(tmp_path, steps) == ([1], [2])


# ----- The board -----


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_board_lists_every_timer_with_discord_timestamps(lang):
    rows = {"gartua": _row(spawn_at=SEEN + 3600, set_by=42)}
    embed = build_timers_embed(rows, SGT, lang, SEEN)
    assert [field.inline for field in embed.fields] == [False, False, False]
    text = "\n".join(f"{field.name}\n{field.value}" for field in embed.fields)
    for timer in TIMERS:
        assert f"**{timer.name}**" in text
    # Field bosses first, then the abyss, then the rift.
    assert text.index("Gartua") < text.index("Watcher Kaira") < text.index("Rift")
    assert f"<t:{SEEN + 3600}:R> · <t:{SEEN + 3600}:t>" in text
    assert "<@42>" in text
    assert i18n.t("timers.unknown", lang) in text  # Lagta, Dartan, Kashapa
    nahma = _state("nahma").next_at
    assert f"<t:{nahma}:R> · <t:{nahma}:d> <t:{nahma}:t>" in text
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert "{" not in embed.footer.text


def test_board_is_unchanged_until_a_timer_rolls_over():
    first = build_timers_embed({}, SGT, "en", SEEN).to_dict()
    assert build_timers_embed({}, SGT, "en", SEEN + 60).to_dict() == first
    # 33 minutes on, the rift that was live has closed.
    assert build_timers_embed({}, SGT, "en", SEEN + 33 * 60).to_dict() != first


def test_board_view_is_persistent_with_one_button_per_field_boss():
    async def build():
        return TimersBoardView()

    view = asyncio.run(build())
    assert view.is_persistent()
    assert [c.custom_id for c in view.children] == [
        f"timers:set:{timer.key}" for timer in FIELD_TIMERS
    ]
    assert [timer.name for timer in FIELD_TIMERS] == [
        "Gartua",
        "Lagta",
        "Dartan",
        "Kashapa",
    ]


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_report_form_fits_discords_limits(lang):
    async def build():
        return [SpawnModal(timer, lang) for timer in FIELD_TIMERS]

    for modal in asyncio.run(build()):
        assert len(modal.title) <= 45
        (field,) = modal.children
        assert isinstance(field, discord.ui.TextInput)
        assert len(field.label) <= 45 and len(field.placeholder) <= 100


def _submit(tmp_path, typed: str, key: str = "dartan"):
    """Submits the report form for one boss and returns what happened."""

    async def go():
        db = Database(str(tmp_path / "form.db"))
        await db.connect()
        try:
            bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
            bot.db = db
            # Added the way discord.py does, so the form finds it by its real
            # name. The background loop needs a logged-in client: not started.
            cog = Timers(bot)
            cog.cog_load = _noop
            await bot.add_cog(cog)
            calls = []

            async def edit_message(**kwargs):
                calls.append(("edit", kwargs))

            async def send_message(content, **kwargs):
                calls.append(("reply", content))

            async def followup(content, **kwargs):
                calls.append(("followup", content))

            interaction = SimpleNamespace(
                client=bot,
                guild=SimpleNamespace(id=1),
                guild_id=1,
                user=SimpleNamespace(id=77),
                response=SimpleNamespace(
                    edit_message=edit_message, send_message=send_message
                ),
                followup=SimpleNamespace(send=followup),
            )
            modal = SpawnModal(TIMERS_BY_KEY[key], "en")
            modal.countdown._value = typed
            before = int(time.time())
            await modal.on_submit(interaction)
            row = (await db.get_boss_timers(1)).get(key)
            await bot.close()
            return calls, row, before
        finally:
            await db.close()

    return asyncio.run(go())


async def _noop():
    return None


def test_reporting_a_countdown_saves_it_and_redraws_the_board(tmp_path):
    calls, row, before = _submit(tmp_path, "2:30")
    assert [kind for kind, _ in calls] == ["edit", "followup"]
    assert 0 <= row["spawn_at"] - before - 9000 <= 5
    assert row["set_by"] == 77
    field_bosses = calls[0][1]["embed"].fields[0].value
    assert f"<t:{row['spawn_at']}:R>" in field_bosses
    assert "<@77>" in field_bosses


def test_an_empty_report_means_killed_just_now(tmp_path):
    calls, row, before = _submit(tmp_path, "")
    assert [kind for kind, _ in calls] == ["edit", "followup"]
    assert 0 <= row["spawn_at"] - before - 6 * 3600 <= 5  # Dartan: 6 hours


def test_a_countdown_longer_than_the_respawn_is_refused(tmp_path):
    # 42:10 on a 6-hour boss is minutes:seconds, not 42 hours.
    calls, row, _ = _submit(tmp_path, "42:10")
    assert [kind for kind, _ in calls] == ["reply"]
    assert "42:10" in calls[0][1] and row is None


def test_an_unreadable_countdown_is_refused(tmp_path):
    calls, row, _ = _submit(tmp_path, "soon")
    assert [kind for kind, _ in calls] == ["reply"] and row is None


# ----- Pings -----


class _Channel:
    id = 10

    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=900 + len(self.sent))

    def get_partial_message(self, message_id):
        async def delete():
            self.deleted.append(message_id)

        return SimpleNamespace(delete=delete)


def test_due_pings_are_sent_once_grouped_and_cleaned_up(tmp_path):
    spawn = _state("executor_argo", tz=PACIFIC).next_at

    async def steps(db):
        channel = _Channel()
        bot = SimpleNamespace(
            db=db, get_channel=lambda _id: channel, get_guild=lambda _id: None
        )
        cog = Timers(bot)
        await db.set_timer_board(1, channel.id, 30)
        await db.set_boss_timer_config(1, "executor_argo", role_id=71)
        await db.set_boss_timer_config(1, "executor_kaira", role_id=72)
        await db.set_boss_timer_config(1, "watcher_kaira", role_id=72)
        board = await db.get_timer_board(1)

        await cog._send_pings(board, spawn - 301)  # too early
        early = len(channel.sent)
        await cog._send_pings(board, spawn - 200)
        await cog._send_pings(board, spawn - 100)  # same spawn: no second ping
        await cog._sweep_pings(spawn - 1)
        kept = list(channel.deleted)
        await cog._sweep_pings(spawn + 100)
        return early, channel.sent, kept, channel.deleted

    early, sent, kept, deleted = _run(tmp_path, steps)
    assert early == 0
    ((content, kwargs),) = sent
    assert "Executor Argo, Executor Kaira" in content
    assert f"<t:{spawn}:R>" in content
    assert content.endswith("<@&71> <@&72>")
    assert kwargs["allowed_mentions"].roles is True
    assert kept == [] and deleted == [901]


TIMER_KEYS = {
    "timers.title": {},
    "timers.group_field": {},
    "timers.group_abyss": {},
    "timers.group_rift": {},
    "timers.live": {"when": "<t:1:R>"},
    "timers.up_since": {"when": "<t:1:R>"},
    "timers.unknown": {},
    "timers.set_by": {"who": "<@1>"},
    "timers.footer": {"clock": "PST"},
    "timers.modal_title": {"name": "Gartua"},
    "timers.modal_label": {},
    "timers.modal_placeholder": {},
    "timers.bad_time": {"value": "soon"},
    "timers.too_long": {"name": "Gartua", "hours": "11", "value": "42:10"},
    "timers.saved": {"name": "Gartua", "when": "<t:1:R>"},
    "timers.ping": {"names": "Gartua", "when": "<t:1:R>"},
    "timers.admin_only": {},
    "timers.posted": {"link": "https://x/1"},
    "timers.post_forbidden": {},
    "timers.unknown_timer": {"name": "Bob"},
    "timers.role_set": {"role": "<@&1>", "name": "Gartua", "minutes": 5},
    "timers.role_cleared": {"name": "Gartua"},
    "timers.no_everyone": {},
    "timers.respawn_set": {"name": "Gartua", "hours": "11"},
    "timers.respawn_label": {"hours": "11"},
    "timers.bad_schedule": {},
    "timers.schedule_set": {"name": "Nahma", "schedule": "fri,sun 21:00"},
    "timers.bad_timezone": {"value": "Mars"},
    "timers.settings": {"tz": "UTC", "now": "Thu 08:28", "minutes": 5, "board": "#x"},
    "timers.no_board": {},
}


@pytest.mark.parametrize("lang", ["en", "fr"])
@pytest.mark.parametrize("key,params", list(TIMER_KEYS.items()))
def test_timer_key_resolves_and_formats(key, params, lang):
    out = i18n.t(key, lang, **params)
    assert out != key
    assert "{" not in out
