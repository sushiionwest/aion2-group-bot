"""A character's class decides which seats it may take where seats are per
role (standard and support parties). Run: pytest
"""

import pytest

from bot import config, i18n
from bot.logic import (
    CLASS_ROLES,
    COMPO_OPEN,
    COMPO_STANDARD,
    COMPO_SUPPORT,
    ROLES,
    class_fits_role,
    classes_for_role,
    roles_for_class,
)
from bot.views import CharacterPicker, SignupView
from tests.test_checkin_autofill import EVENT_ID, _run

GUILD = 1  # the guild `_World.event` creates its event in
MEMBER = 50


def test_every_class_in_the_game_has_its_seats():
    assert set(CLASS_ROLES) == set(config.CLASS_EMOJI)
    assert all(set(roles) <= set(ROLES) for roles in CLASS_ROLES.values())


def test_which_class_takes_which_seat():
    assert classes_for_role("tank") == ["Templar"]
    assert classes_for_role("heal") == ["Cleric", "Chanter"]
    assert classes_for_role("dps") == [
        "Gladiator",
        "Assassin",
        "Ranger",
        "Sorcerer",
        "Spiritmaster",
    ]


def test_a_gladiator_is_dps_and_not_a_tank():
    assert roles_for_class("Gladiator") == ("dps",)
    assert not class_fits_role("Gladiator", "tank")


def test_a_chanter_is_support():
    assert class_fits_role("Chanter", "heal")
    assert not class_fits_role("Chanter", "dps")


def test_a_class_added_later_may_take_any_seat():
    assert all(class_fits_role("Fist Fighter", role) for role in ROLES)


async def _press(w, button: str, *, compo=COMPO_STANDARD, size=5, characters=()):
    """`MEMBER` registers `characters` and presses a sign-up button.

    Returns (their sign-up row or None, the private replies, the menu shown).
    """
    await w.event([], compo=compo, size=size, starts_in=60 * 60)
    for name, char_class in characters:
        await w.db.add_character(GUILD, MEMBER, name, char_class, "dps")
    interaction = w._interaction(MEMBER, EVENT_ID)
    replies, menus = [], []

    async def send_message(content, view=None, **_):
        replies.append(content)
        menus.append(view)

    interaction.response.send_message = send_message
    await getattr(SignupView("en"), f"{button}_button").callback(interaction)
    return await w.db.get_signup(EVENT_ID, MEMBER), replies, menus[-1]


def test_a_sorcerer_cannot_take_the_tank_seat(tmp_path, monkeypatch):
    signup, replies, _menu = _run(
        tmp_path,
        monkeypatch,
        lambda w: _press(w, "tank", characters=[("Nyx", "Sorcerer")]),
    )
    assert signup is None
    assert replies == [
        "🚫 Tank seats on this event are for: Templar. "
        "**Nyx** is a Sorcerer, so sign up as DPS instead."
    ]


def test_a_gladiator_cannot_take_the_tank_seat(tmp_path, monkeypatch):
    signup, replies, _menu = _run(
        tmp_path,
        monkeypatch,
        lambda w: _press(w, "tank", characters=[("Brak", "Gladiator")]),
    )
    assert signup is None
    assert "Tank seats on this event are for: Templar." in replies[0]


def test_a_templar_takes_the_tank_seat(tmp_path, monkeypatch):
    signup, replies, _menu = _run(
        tmp_path,
        monkeypatch,
        lambda w: _press(w, "tank", characters=[("Aegis", "Templar")]),
    )
    assert signup["role"] == "tank"
    assert "You're in" in replies[0]


def test_a_chanter_takes_a_support_seat(tmp_path, monkeypatch):
    signup, replies, _menu = _run(
        tmp_path,
        monkeypatch,
        lambda w: _press(
            w, "heal", compo=COMPO_SUPPORT, size=10, characters=[("Lyra", "Chanter")]
        ),
    )
    assert signup["role"] == "heal"
    assert "**Support**" in replies[0]


def test_a_chanter_is_pointed_to_support_not_heal_in_the_support_party(
    tmp_path, monkeypatch
):
    signup, replies, _menu = _run(
        tmp_path,
        monkeypatch,
        lambda w: _press(
            w, "dps", compo=COMPO_SUPPORT, size=10, characters=[("Lyra", "Chanter")]
        ),
    )
    assert signup is None
    assert replies == [
        "🚫 DPS seats on this event are for: Gladiator, Assassin, Ranger, Sorcerer, "
        "Spiritmaster. **Lyra** is a Chanter, so sign up as Support instead."
    ]


def test_a_member_with_no_character_is_taken_at_their_word(tmp_path, monkeypatch):
    signup, _replies, _menu = _run(tmp_path, monkeypatch, lambda w: _press(w, "tank"))
    assert signup["role"] == "tank"


def test_open_events_do_not_check_the_class(tmp_path, monkeypatch):
    signup, _replies, _menu = _run(
        tmp_path,
        monkeypatch,
        lambda w: _press(
            w, "tank", compo=COMPO_OPEN, size=5, characters=[("Nyx", "Sorcerer")]
        ),
    )
    assert signup["role"] == "tank"


def test_the_one_character_that_fits_is_signed_up_without_a_menu(tmp_path, monkeypatch):
    async def scenario(w):
        signup, replies, menu = await _press(
            w, "tank", characters=[("Nyx", "Sorcerer"), ("Aegis", "Templar")]
        )
        character = await w.db.get_character(GUILD, MEMBER, signup["character_id"])
        return character["char_name"], replies, menu

    name, replies, menu = _run(tmp_path, monkeypatch, scenario)
    assert name == "Aegis"
    assert menu is None
    assert "with **Aegis**" in replies[0]


def test_the_menu_only_offers_characters_that_fit(tmp_path, monkeypatch):
    signup, _replies, menu = _run(
        tmp_path,
        monkeypatch,
        lambda w: _press(
            w,
            "dps",
            characters=[("Nyx", "Sorcerer"), ("Aegis", "Templar"), ("Kai", "Ranger")],
        ),
    )
    assert signup is None  # still choosing
    assert isinstance(menu, CharacterPicker)
    offered = [option.label for option in menu.children[0].options]
    assert sorted(offered) == ["Kai", "Nyx"]


def test_no_character_fits(tmp_path, monkeypatch):
    signup, replies, _menu = _run(
        tmp_path,
        monkeypatch,
        lambda w: _press(
            w, "heal", characters=[("Nyx", "Sorcerer"), ("Kai", "Ranger")]
        ),
    )
    assert signup is None
    assert replies == [
        "🚫 Heal seats on this event are for: Cleric, Chanter. "
        "None of your characters is one of those, so pick another role."
    ]


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_the_refusals_exist_in_both_languages(lang):
    one = i18n.t(
        "signup.class_refused",
        lang,
        label="Tank",
        classes="Templar",
        name="Nyx",
        char_class="Sorcerer",
        allowed="DPS",
    )
    many = i18n.t("signup.class_refused_many", lang, label="Tank", classes="Templar")
    for text in (one, many):
        assert "signup." not in text and "{" not in text
        assert "Templar" in text
