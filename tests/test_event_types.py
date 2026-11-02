"""A server's own event types (/eventtypes) and how events use them. Run: pytest"""

import asyncio

import pytest

from bot import i18n
from bot.cogs.event_types import describe_event_type
from bot.cogs.panel import visible_event_types
from bot.db import Database
from bot.db_event_types import DEFAULT_EVENT_TYPES, MAX_EVENT_TYPES
from bot.embeds import build_event_embed
from bot.logic import COMPO_STANDARD


def _run(tmp_path, steps):
    """Runs `steps(db)` against a fresh database and returns its result."""

    async def go():
        db = Database(str(tmp_path / "types.db"))
        await db.connect()
        try:
            return await steps(db)
        finally:
            await db.close()

    return asyncio.run(go())


def test_a_new_server_starts_with_the_default_list_in_order(tmp_path):
    async def steps(db):
        return await db.get_event_types(1)

    rows = _run(tmp_path, steps)
    assert [row["name"] for row in rows] == [name for name, _, _ in DEFAULT_EVENT_TYPES]
    raid = rows[-1]
    assert raid["name"] == "Raid" and raid["staff_only"] == 1
    assert all(row["role_id"] is None for row in rows)


def test_adding_appends_and_editing_keeps_position_and_untouched_fields(tmp_path):
    async def steps(db):
        added = await db.upsert_event_type(1, "Fire Temple Hard", role_id=55)
        # Edit by a differently-cased name: only the emoji changes.
        updated = await db.upsert_event_type(1, "fire temple hard", emoji="🔥")
        return added, updated, await db.get_event_types(1)

    added, updated, rows = _run(tmp_path, steps)
    assert (added, updated) == ("added", "updated")
    last = rows[-1]
    assert last["name"] == "Fire Temple Hard"
    assert (last["role_id"], last["emoji"], last["staff_only"]) == (55, "🔥", 0)
    assert len(rows) == len(DEFAULT_EVENT_TYPES) + 1


def test_a_ping_role_can_be_set_and_cleared(tmp_path):
    async def steps(db):
        await db.upsert_event_type(1, "Krao Cave Hard", role_id=77)
        with_role = await db.get_event_type(1, "Krao Cave Hard")
        await db.upsert_event_type(1, "Krao Cave Hard", clear_role=True)
        cleared = await db.get_event_type(1, "Krao Cave Hard")
        return with_role, cleared

    with_role, cleared = _run(tmp_path, steps)
    assert with_role["role_id"] == 77
    assert cleared["role_id"] is None
    assert cleared["emoji"] == "🏰"  # untouched by the edit


def test_removing_a_type_and_the_last_one_is_kept(tmp_path):
    async def steps(db):
        missing = await db.remove_event_type(1, "Abyss")
        results = [
            await db.remove_event_type(1, name) for name, _, _ in DEFAULT_EVENT_TYPES
        ]
        return missing, results, await db.get_event_types(1)

    missing, results, rows = _run(tmp_path, steps)
    assert missing == "missing"
    assert results == ["removed"] * (len(DEFAULT_EVENT_TYPES) - 1) + ["last"]
    assert [row["name"] for row in rows] == ["Raid"]


def test_the_list_is_capped_at_discords_menu_limit(tmp_path):
    async def steps(db):
        await db.get_event_types(1)
        extra = MAX_EVENT_TYPES - len(DEFAULT_EVENT_TYPES)
        for n in range(extra):
            assert await db.upsert_event_type(1, f"Dungeon {n}") == "added"
        return await db.upsert_event_type(1, "One too many")

    assert _run(tmp_path, steps) == "full"


def test_each_server_has_its_own_list(tmp_path):
    async def steps(db):
        await db.upsert_event_type(1, "Only Here")
        return await db.get_event_type(1, "Only Here"), await db.get_event_type(
            2, "Only Here"
        )

    here, elsewhere = _run(tmp_path, steps)
    assert here is not None and elsewhere is None


def test_an_events_saved_emoji_is_stored(tmp_path):
    async def steps(db):
        await db.create_event(
            message_id=9,
            channel_id=2,
            guild_id=1,
            creator_id=4,
            creator_name="Wiz",
            title="Krao run",
            activity="Krao Cave Hard",
            compo=COMPO_STANDARD,
            size=5,
            status="open",
            emoji="🏰",
        )
        return await db.get_event(9)

    assert _run(tmp_path, steps)["emoji"] == "🏰"


EVENT = {
    "message_id": 1,
    "channel_id": 2,
    "guild_id": 3,
    "creator_id": 4,
    "creator_name": "Wiz",
    "title": "Krao run",
    "activity": "Krao Cave Hard",
    "description": None,
    "compo": COMPO_STANDARD,
    "size": 5,
    "starts_at": None,
    "status": "open",
    "emoji": "🐲",
}


def test_event_posts_have_no_banner_image():
    assert build_event_embed(EVENT, []).image.url is None


def test_event_title_uses_the_types_saved_emoji():
    assert build_event_embed(EVENT, []).title == "🐲 Krao run"


def test_staff_only_types_are_hidden_from_members():
    rows = [
        {"name": "Krao Cave Hard", "staff_only": 0},
        {"name": "Raid", "staff_only": 1},
    ]
    assert [row["name"] for row in visible_event_types(rows, False)] == [
        "Krao Cave Hard"
    ]
    assert len(visible_event_types(rows, True)) == 2


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_list_line_shows_ping_and_staff_flag(lang):
    row = {"name": "Raid", "emoji": "🐉", "role_id": 42, "staff_only": 1}
    line = describe_event_type(row, lang)
    assert line.startswith("🐉 **Raid**")
    assert "<@&42>" in line
    assert i18n.t("eventtypes.staff_only_label", lang) in line


EVENT_TYPE_KEYS = {
    "eventtypes.admin_only": {},
    "eventtypes.added": {},
    "eventtypes.updated": {},
    "eventtypes.full": {"max": 25},
    "eventtypes.removed": {"name": "Raid"},
    "eventtypes.missing": {"name": "Raid"},
    "eventtypes.last": {"name": "Raid"},
    "eventtypes.bad_emoji": {"value": "x"},
    "eventtypes.no_everyone": {},
    "eventtypes.ping_unmentionable": {"role": "<@&1>"},
    "eventtypes.list_title": {},
    "eventtypes.list_hint": {},
    "eventtypes.pings": {"role": "<@&1>"},
    "eventtypes.staff_only_label": {},
    "eventtypes.staff_only": {"name": "Raid"},
    "eventtypes.unknown": {"name": "PvP"},
}


@pytest.mark.parametrize("lang", ["en", "fr"])
@pytest.mark.parametrize("key,params", list(EVENT_TYPE_KEYS.items()))
def test_event_type_key_resolves_and_formats(key, params, lang):
    out = i18n.t(key, lang, **params)
    assert out != key
    assert "{" not in out


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_staff_only_label_fits_a_select_option_description(lang):
    # Select option descriptions are capped at 100 characters by Discord.
    assert len(i18n.t("eventtypes.staff_only_label", lang)) <= 100
