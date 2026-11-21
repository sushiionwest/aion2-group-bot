"""Check-in follow-up: handing unanswered seats to the waitlist.

The rules live in `utils/rsvp.py`. This module applies them to a real event:
it stores the new queue order, redraws the event and the check-in prompt, and
tells the members whose seat changed. It is used both by the buttons under the
prompt (`views_rsvp.py`) and by the timed pass in the Groups cog.
"""

import time

import discord

from . import config, i18n
from .embeds import build_rsvp_embed
from .logic import assign
from .utils.mentions import join_mentions
from .utils.rsvp import YES, autofill_swaps, checkin_deadline, ranking_priorities
from .views_common import EVENT_LOCKS, refresh_event_message

# Seat changes listed per announcement; a siege can move many at once and a
# message is capped at 2000 characters.
SWAP_LINES_PER_MESSAGE = 15


async def rsvp_channel(client, event):
    """The configured check-in channel if set, else the event's own channel."""
    settings = await client.db.get_settings(event["guild_id"])
    channel_id = settings["rsvp_channel_id"] if settings else None
    if channel_id:
        channel = client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None
        if channel is not None and hasattr(channel, "send"):
            return channel
    channel = client.get_channel(event["channel_id"])
    if channel is None:
        channel = await client.fetch_channel(event["channel_id"])
    return channel


def prompt_deadline(event, posted_at: float | None = None) -> int | None:
    """When this event's unanswered seats may start moving, or None.

    The posting time comes from the prompt message's id, so nothing extra is
    stored; pass `posted_at` for a prompt that is about to be sent.
    """
    if posted_at is None:
        if not event["rsvp_prompt_id"]:
            return None
        posted_at = discord.utils.snowflake_time(event["rsvp_prompt_id"]).timestamp()
    return checkin_deadline(event["starts_at"], posted_at, config.RSVP_DEADLINE_MINUTES)


def is_live(event, now: float) -> bool:
    """Whether the event's check-in is out and still being answered.

    False before the prompt is posted, while a rescheduled event waits for its
    new prompt, and once the event has started.
    """
    if not event["rsvp_sent"] or not event["rsvp_prompt_id"]:
        return False
    return not event["starts_at"] or now < event["starts_at"]


async def unconfirmed_ids(client, event, now: float) -> set[int]:
    """Signed-up members who still owe the live check-in an answer.

    Empty when no check-in is live, so callers can pass it straight to
    `change_messages`: a promoted member is only asked to confirm when there
    is something to confirm on.
    """
    if not is_live(event, now):
        return set()
    said_yes = {
        r["user_id"]
        for r in await client.db.get_rsvps(event["message_id"])
        if r["status"] == YES
    }
    signups = await client.db.get_signups(event["message_id"])
    return {s["user_id"] for s in signups} - said_yes


async def build_prompt_embed(client, event, lang: str, posted_at: float | None = None):
    """The check-in prompt for the event as it stands in the database."""
    signups = await client.db.get_signups(event["message_id"])
    party, waitlist = assign(event["compo"], event["size"], signups)
    rsvps = await client.db.get_rsvps(event["message_id"])
    return build_rsvp_embed(
        event,
        party,
        waitlist,
        rsvps,
        lang,
        deadline=prompt_deadline(event, posted_at),
    )


async def run_autofill(client, event, now: float) -> list[tuple[int, int]]:
    """Moves unanswered seats to ready waitlisted members, if the time is right.

    Returns the `(out_user_id, in_user_id)` swaps it stored. Does nothing
    before the deadline or once the event has started. The caller holds the
    event's lock and passes the event as read inside it.
    """
    deadline = prompt_deadline(event)
    if deadline is None or now < deadline or now >= event["starts_at"]:
        return []
    db = client.db
    signups = await db.get_signups(event["message_id"])
    responses = {
        r["user_id"]: r["status"] for r in await db.get_rsvps(event["message_id"])
    }
    swaps, ranking = autofill_swaps(event["compo"], event["size"], signups, responses)
    if swaps:
        await db.set_signup_priorities(event["message_id"], ranking_priorities(ranking))
    return swaps


def change_messages(
    event,
    party_before: list,
    party_after: list,
    swaps: list[tuple[int, int]],
    lang: str,
    skip_user_id: int | None = None,
    confirm_ids: frozenset | set = frozenset(),
) -> list[str]:
    """The public announcements for a change of party, as message bodies.

    Compares the party before and after, so only what really changed is
    announced: a member who was promoted and swapped out again in the same
    step is never pinged. `skip_user_id` is the member who caused the change
    by pressing a button, who gets their answer privately instead. Promoted
    members listed in `confirm_ids` (see `unconfirmed_ids`) are also told to
    answer the check-in, since an unconfirmed seat can still be handed on.
    """
    before = {s["user_id"] for s in party_before}
    after = {s["user_id"] for s in party_after}
    net_swaps = [
        (out_id, in_id)
        for out_id, in_id in swaps
        if out_id in before and out_id not in after and in_id in after
    ]
    swapped_in = {in_id for _out, in_id in net_swaps}
    promoted = [
        s["user_id"]
        for s in party_after
        if s["user_id"] not in before
        and s["user_id"] not in swapped_in
        and s["user_id"] != skip_user_id
    ]

    messages = []
    for key, group in (
        ("signup.promoted", [u for u in promoted if u not in confirm_ids]),
        ("rsvp.promoted_confirm", [u for u in promoted if u in confirm_ids]),
    ):
        if group:
            messages.append(
                i18n.t(key, lang, mentions=join_mentions(group), title=event["title"])
            )
    for start in range(0, len(net_swaps), SWAP_LINES_PER_MESSAGE):
        lines = [
            i18n.t("rsvp.swap_line", lang, newcomer=f"<@{in_id}>", out=f"<@{out_id}>")
            for out_id, in_id in net_swaps[start : start + SWAP_LINES_PER_MESSAGE]
        ]
        messages.append(
            i18n.t("rsvp.swapped", lang, title=event["title"])
            + "\n"
            + "\n".join(lines)
            + "\n"
            + i18n.t("rsvp.swap_footer", lang)
        )
    return messages


async def announce(send, messages: list[str]) -> None:
    """Posts each announcement with `send` (a channel's or a follow-up's)."""
    for content in messages:
        try:
            await send(content, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass  # the seats did move; a lost announcement must not undo that


async def refresh_prompt(client, event) -> None:
    """Redraws the check-in prompt from the database."""
    if not event["rsvp_prompt_id"]:
        return
    lang = await i18n.resolve_lang(client.db, client.get_guild(event["guild_id"]))
    embed = await build_prompt_embed(client, event, lang)
    channel = await rsvp_channel(client, event)
    await channel.get_partial_message(event["rsvp_prompt_id"]).edit(embed=embed)


async def sync_prompt(client, event) -> None:
    """Best-effort redraw of the check-in after the party changed elsewhere.

    Joining, leaving and admin queue changes happen on the event post; without
    this the check-in would keep listing a party that no longer exists.
    """
    if not event["rsvp_sent"] or not event["rsvp_prompt_id"]:
        return
    try:
        await refresh_prompt(client, event)
    except discord.HTTPException:
        pass  # prompt deleted or not editable: nothing to keep in sync


async def timed_pass(client, message_id: int, now: float | None = None) -> list:
    """Runs the seat hand-over for one event from the background loop.

    Returns the swaps performed. Everything Discord-side is best effort: the
    new order is already stored, so a failed redraw or announcement is skipped
    rather than retried.
    """
    now = time.time() if now is None else now
    db = client.db
    async with EVENT_LOCKS[message_id]:
        event = await db.get_event(message_id)
        if event is None or event["status"] != "open":
            return []
        party_before, _ = assign(
            event["compo"], event["size"], await db.get_signups(message_id)
        )
        swaps = await run_autofill(client, event, now)
        if not swaps:
            return []
        party_after, _ = assign(
            event["compo"], event["size"], await db.get_signups(message_id)
        )
        lang = await i18n.resolve_lang(db, client.get_guild(event["guild_id"]))
        for redraw in (refresh_event_message, refresh_prompt):
            try:
                await redraw(client, event)
            except discord.HTTPException:
                pass
        try:
            channel = await rsvp_channel(client, event)
        except discord.HTTPException:
            return swaps
        await announce(
            channel.send, change_messages(event, party_before, party_after, swaps, lang)
        )
        return swaps
