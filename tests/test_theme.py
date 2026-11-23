"""The bot's look: one palette, one author line, artwork from this repo.
Run: pytest
"""

import json
import re
from pathlib import Path

import discord

from bot import config, theme
from bot.branding import LOGO_URL, brand
from bot.embeds import (
    COLOUR_CANCELLED,
    COLOUR_FULL,
    COLOUR_OPEN,
    build_event_embed,
    build_rsvp_embed,
)
from tests.test_event_embed import EVENT, signup

BOT = Path(__file__).resolve().parent.parent / "bot"
LOCALES = BOT / "locales"


def test_every_embed_takes_its_colour_from_the_theme():
    # A stray discord.Colour.blurple() would put one off-theme bar back.
    offenders = [
        path.name
        for path in BOT.rglob("*.py")
        if path.name != "theme.py" and "discord.Colour" in path.read_text()
    ]
    assert offenders == []


def test_the_states_of_an_event_stay_easy_to_tell_apart():
    shades = [theme.PINK, theme.MINT, theme.BERRY, theme.BUTTER, theme.PEACH]
    shades.append(theme.MAUVE)
    assert len({shade.value for shade in shades}) == len(shades)
    assert (COLOUR_OPEN, COLOUR_FULL, COLOUR_CANCELLED) == (
        theme.PINK,
        theme.MINT,
        theme.BERRY,
    )


def test_an_open_event_is_pink_and_a_full_one_is_mint():
    assert build_event_embed(EVENT, [signup(10, "tank")]).colour == theme.PINK
    full = [signup(1, "tank"), signup(2, "heal")]
    full += [signup(u, "dps") for u in (3, 4, 5)]
    assert build_event_embed(EVENT, full).colour == theme.MINT
    assert build_event_embed({**EVENT, "status": "cancelled"}, []).colour == theme.BERRY
    assert build_event_embed({**EVENT, "status": "done"}, []).colour == theme.BUTTER


def test_the_check_in_is_pink():
    assert build_rsvp_embed(EVENT, [], [], []).colour == theme.PINK


def test_embeds_are_signed_kawaii_kisk():
    embed = brand(discord.Embed(title="x"))
    assert embed.author.name == "Kawaii Kisk ♡"
    assert embed.author.icon_url == LOGO_URL


def test_the_artwork_is_served_from_this_repository():
    # The pink art lives in this fork; the upstream URL would show the old one.
    assert "/sushiionwest/aion2-group-bot/" in config.ASSET_BASE_URL
    assert LOGO_URL.startswith(config.ASSET_BASE_URL + "/avatar.png?v=")


def test_the_sparkle_only_ever_ends_a_title():
    english = json.loads((LOCALES / "en.json").read_text())
    for key, text in english.items():
        if theme.SPARKLE in text and not key.endswith(("thread_intro", "panel.title")):
            assert text.endswith(theme.SPARKLE), key


def test_the_restyled_english_keeps_every_placeholder_the_french_has():
    english = json.loads((LOCALES / "en.json").read_text())
    french = json.loads((LOCALES / "fr.json").read_text())

    def names(text):
        return sorted(set(re.findall(r"\{(\w+)", text)))

    assert {k for k in english if names(english[k]) != names(french[k])} == set()


def test_titles_and_buttons_still_fit_discord():
    english = json.loads((LOCALES / "en.json").read_text())
    for key, text in english.items():
        if ".btn_" in key:
            assert len(text) <= 80, key
        if key.endswith(("title", "_title")) or ".title" in key:
            assert len(text) <= 200, key
