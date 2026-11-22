"""The support party: 2 tanks / 4 support / 4 DPS, where the heal seats are
called "Support". Run: pytest
"""

import asyncio

import pytest

from bot import checkin, i18n
from bot.cogs.groups import FIXED_SETUPS, Groups, party_setup
from bot.cogs.panel import SETUPS
from bot.db import Database
from bot.embeds import build_event_embed, same_role_note, seat_label
from bot.logic import (
    COMPO_OPEN,
    COMPO_STANDARD,
    COMPO_SUPPORT,
    assign,
    has_role_seats,
    missing_slots,
    role_slots,
)
from bot.views import QueueSelect, SignupView
from tests.test_checkin_autofill import EVENT_ID, _run
from tests.test_db_recurring import rec
from tests.test_event_embed import EVENT, signup

SUPPORT_EVENT = {**EVENT, "compo": COMPO_SUPPORT, "size": 10}

# A full support party in join order: tanks 1-2, supports 3-6, DPS 7-10.
FULL = (
    [(1, "tank"), (2, "tank")]
    + [(u, "heal") for u in (3, 4, 5, 6)]
    + [(u, "dps") for u in (7, 8, 9, 10)]
)


def s(user_id, role):
    return {"user_id": user_id, "role": role}


def ids(items):
    return [x["user_id"] for x in items]


def _heal_label(view) -> str:
    return next(c.label for c in view.children if c.custom_id == "aion2:heal")


# ----- Seats -----


def test_support_party_has_two_tanks_four_supports_four_dps():
    assert role_slots(COMPO_SUPPORT, 10) == {"tank": 2, "heal": 4, "dps": 4}
    assert has_role_seats(COMPO_SUPPORT)


def test_open_and_legacy_values_have_no_role_seats():
    for compo in (COMPO_OPEN, "libre", "anything-else"):
        assert role_slots(compo, 10) is None
        assert not has_role_seats(compo)


def test_the_fifth_support_and_the_third_tank_wait():
    signups = (
        [s(u, "tank") for u in (1, 2, 3)]
        + [s(u, "heal") for u in (4, 5, 6, 7, 8)]
        + [s(u, "dps") for u in (9, 10, 11, 12, 13)]
    )
    party, waitlist = assign(COMPO_SUPPORT, 10, signups)
    assert ids(party) == [1, 2, 4, 5, 6, 7, 9, 10, 11, 12]
    assert ids(waitlist) == [3, 8, 13]


def test_missing_seats_are_counted_per_role():
    signups = [s(1, "tank"), s(2, "heal"), s(3, "dps")]
    assert missing_slots(COMPO_SUPPORT, 10, signups) == {
        "tank": 1,
        "heal": 3,
        "dps": 3,
    }


# ----- The name of the seat -----


def test_the_heal_seat_is_called_support_only_in_the_support_party():
    assert seat_label("heal", COMPO_SUPPORT) == "Support"
    assert seat_label("heal", COMPO_STANDARD) == "Heal"
    assert seat_label("heal", COMPO_OPEN) == "Heal"
    assert seat_label("heal") == "Heal"
    assert seat_label("tank", COMPO_SUPPORT) == "Tank"
    assert seat_label("dps", COMPO_SUPPORT) == "DPS"


def test_the_event_post_says_support():
    embed = build_event_embed(SUPPORT_EVENT, [signup(10, "heal")])
    names = [field.name for field in embed.fields]
    assert any("Support (1/4)" in name for name in names)
    assert not any("Heal" in name for name in names)
    assert "Support" in embed.description and "Heal" not in embed.description


def test_a_standard_event_still_says_heal():
    embed = build_event_embed({**EVENT, "size": 10}, [signup(10, "heal")])
    assert any("Heal (1/2)" in field.name for field in embed.fields)


def test_the_sign_up_button_says_support():
    async def build():
        return (
            SignupView("en", COMPO_SUPPORT),
            SignupView("en", COMPO_STANDARD),
            SignupView("en"),
        )

    support, standard, default = asyncio.run(build())
    assert _heal_label(support) == "Support"
    assert _heal_label(standard) == "Heal"
    assert _heal_label(default) == "Heal"


def test_the_button_still_says_support_after_someone_leaves(tmp_path, monkeypatch):
    # The click is served by the view registered at startup, whose labels are
    # the defaults; the redraw must not send that one back.
    async def scenario(w):
        await w.event(FULL, compo=COMPO_SUPPORT, size=10, starts_in=60 * 60)
        interaction = w._interaction(3, EVENT_ID)
        await SignupView().leave_button.callback(interaction)
        return interaction.response.edits

    edits = _run(tmp_path, monkeypatch, scenario)
    assert len(edits) == 1
    assert _heal_label(edits[0]["view"]) == "Support"


def test_the_admin_queue_names_the_seat_support():
    async def build():
        ranked = [{"user_id": 1, "role": "heal", "display_name": "u1", "char_name": ""}]
        return (
            QueueSelect(ranked, {1}, 1, "en", COMPO_SUPPORT),
            QueueSelect(ranked, {1}, 1, "en", COMPO_STANDARD),
        )

    support, standard = asyncio.run(build())
    assert "Support" in support.options[0].description
    assert "Heal" in standard.options[0].description


# ----- A freed or unanswered seat only goes to the same role -----


def test_a_ready_dps_does_not_take_a_silent_support_seat(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event(FULL + [(11, "dps")], compo=COMPO_SUPPORT, size=10)
        await w.db.set_rsvp(EVENT_ID, 11, "yes")
        for user_id in (7, 8, 9, 10):  # every DPS confirmed; supports are silent
            await w.db.set_rsvp(EVENT_ID, user_id, "yes")
        swaps = await checkin.timed_pass(w.client, EVENT_ID)
        return swaps, await w.lists()

    swaps, (party, waitlist) = _run(tmp_path, monkeypatch, scenario)
    assert swaps == []
    assert waitlist == [11]


def test_a_ready_support_takes_a_silent_support_seat(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event(FULL + [(11, "dps"), (12, "heal")], compo=COMPO_SUPPORT, size=10)
        await w.db.set_rsvp(EVENT_ID, 12, "yes")
        swaps = await checkin.timed_pass(w.client, EVENT_ID)
        return swaps, await w.lists()

    swaps, (party, waitlist) = _run(tmp_path, monkeypatch, scenario)
    assert swaps == [(6, 12)]  # the most recent silent support gives way
    assert 12 in party and 11 not in party
    assert waitlist == [11, 6]


def test_a_support_who_declines_is_replaced_by_a_support(tmp_path, monkeypatch):
    async def scenario(w):
        members = FULL + [(11, "dps"), (12, "heal")]
        await w.event(members, compo=COMPO_SUPPORT, size=10, starts_in=60 * 60)
        _response, followups = await w.press(3, "no")
        return await w.lists(), followups

    (party, waitlist), followups = _run(tmp_path, monkeypatch, scenario)
    assert 12 in party and 3 not in party
    assert waitlist == [11]  # the DPS ahead in the queue stays waiting
    assert len(followups) == 1 and "<@12>" in followups[0]


def test_a_support_seat_stays_open_when_no_support_is_waiting(tmp_path, monkeypatch):
    async def scenario(w):
        members = FULL + [(11, "dps"), (12, "tank")]
        await w.event(members, compo=COMPO_SUPPORT, size=10, starts_in=60 * 60)
        _response, followups = await w.press(3, "no")
        return await w.lists(), followups

    (party, waitlist), followups = _run(tmp_path, monkeypatch, scenario)
    assert len(party) == 9
    assert waitlist == [11, 12]
    assert followups == []


def test_the_check_in_states_the_role_rule_in_support_words(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event(FULL + [(12, "heal")], compo=COMPO_SUPPORT, size=10)
        response, _followups = await w.press(3, "yes")
        return response.edits[-1]["embed"].description

    description = _run(tmp_path, monkeypatch, scenario)
    assert "a support for a support" in description
    assert "healer" not in description


def test_the_role_rule_follows_the_party_setup():
    assert "a support for a support" in same_role_note(SUPPORT_EVENT)
    assert "a healer for a healer" in same_role_note(EVENT)
    assert same_role_note({**EVENT, "compo": COMPO_OPEN}) == ""
    assert same_role_note({}) == ""


def test_a_waitlisted_support_is_told_the_rule_in_support_words(tmp_path, monkeypatch):
    async def scenario(w):
        members = FULL + [(12, "heal")]
        await w.event(members, compo=COMPO_SUPPORT, size=10, starts_in=60 * 60)
        for user_id in (3, 4, 5, 6):  # no silent support seat to take
            await w.db.set_rsvp(EVENT_ID, user_id, "yes")
        await w.press(12, "yes")
        return w.private

    private = _run(tmp_path, monkeypatch, scenario)
    assert len(private) == 1
    assert "a support for a support" in private[0]
    assert "healer" not in private[0]


# ----- Creating one -----


def test_the_panel_offers_the_support_party():
    assert SETUPS["support10"] == (COMPO_SUPPORT, 10)


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_the_panel_entry_fits_a_discord_menu_and_names_the_classes(lang):
    label = i18n.t("panel.setup_label_support10", lang)
    description = i18n.t("panel.setup_desc_support10", lang)
    assert label != "panel.setup_label_support10" and len(label) <= 100
    assert description != "panel.setup_desc_support10" and len(description) <= 100
    assert "2 tanks / 4 support" in description and "4 DPS" in description


def test_the_event_command_offers_the_support_party():
    choices = {
        choice.value: choice.name
        for choice in Groups.event.parameters[2].choices  # comp
    }
    assert set(choices) == {*FIXED_SETUPS, COMPO_OPEN}
    assert "2 tanks / 4 support / 4 DPS" in choices["support10"]
    assert all(len(name) <= 100 for name in choices.values())


def test_each_event_command_choice_gets_its_own_setup():
    assert party_setup("support10", None) == (COMPO_SUPPORT, 10)
    assert party_setup("support10", 25) == (COMPO_SUPPORT, 10)  # size is fixed
    assert party_setup("standard5", None) == (COMPO_STANDARD, 5)
    assert party_setup("standard10", None) == (COMPO_STANDARD, 10)
    assert party_setup(COMPO_OPEN, None) == (COMPO_OPEN, 5)
    assert party_setup(COMPO_OPEN, 12) == (COMPO_OPEN, 12)


def test_a_weekly_support_party_keeps_its_setup(tmp_path):
    async def go():
        db = Database(str(tmp_path / "r.db"))
        await db.connect()
        await db.create_recurrence(**rec(compo=COMPO_SUPPORT, size=10, next_at=1000))
        due = await db.recurrences_due(now_ts=900, lead_s=200)
        await db.close()
        return [(r["compo"], r["size"]) for r in due]

    assert asyncio.run(go()) == [(COMPO_SUPPORT, 10)]
