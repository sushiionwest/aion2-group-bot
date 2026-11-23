"""Kisk's look: one pastel palette and the flourish around titles.

Every embed takes its colour from here, so the bot reads as one thing and a
server can be rethemed in one place. The pinks are sampled from the Kawaii
Community server's banner and icon; the other shades are pastels chosen to sit
next to them while staying easy to tell apart, because an event's colour says
what state it is in.
"""

import discord

PINK = discord.Colour(0xE898B8)  # the default: open events, boards, prompts
MINT = discord.Colour(0x9FE0C3)  # good news: a full party, an accepted recruit
BERRY = discord.Colour(0xC2456F)  # cancelled
BUTTER = discord.Colour(0xF7DE9C)  # completed events, announcements
PEACH = discord.Colour(0xF5B59A)  # absences
MAUVE = discord.Colour(0x9A8793)  # closed and over

# The server decorates its category names with these glyphs, so they are known
# to render for its members. Kept to the end of a title: a screen reader then
# reads the words first, and the title still sorts and searches as plain text.
SPARKLE = "₊˚⊹♡"

# What the author line of every embed says, next to the logo.
NAME = "Kawaii Kisk ♡"


def cute(title: str) -> str:
    """A title with the server's sparkle after it."""
    return f"{title} {SPARKLE}"
