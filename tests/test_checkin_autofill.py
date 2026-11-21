"""The check-in end to end: declining, the deadline pass, stale answers.

Run: pytest
"""

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from bot import checkin, views_rsvp
from bot.db import Database
from bot.logic import COMPO_OPEN, COMPO_STANDARD, assign
from bot.views_rsvp import RSVPView

EVENT_ID = 9
PROMOTED_3 = "📣 <@3>: a spot opened up, you're in the party for **Krao run**!"


def _prompt_id(posted_at: float) -> int:
    """A message id whose embedded timestamp is `posted_at`."""
    return discord.utils.time_snowflake(datetime.fromtimestamp(posted_at, UTC))


class _Message:
    def __init__(self, channel, message_id):
        self.channel, self.id = channel, message_id

    async def edit(self, **kwargs):
        self.channel.edits.append((self.id, kwargs))


class _Channel:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, content, **_):
        self.sent.append(content)

    def get_partial_message(self, message_id):
        return _Message(self, message_id)


class _Response:
    def __init__(self):
        self.edits, self.private = [], []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, content, **_):
        self.private.append(content)


class _World:
    """A database, a fake Discord client and one event with a check-in posted."""

    def __init__(self, db, monkeypatch):
        self.db = db
        self.channel = _Channel()
        self.redrawn = []
        self.client = SimpleNamespace(
            db=db,
            get_channel=lambda _id: self.channel,
            get_guild=lambda _id: None,
        )

        async def fake_refresh(_client, event):
            self.redrawn.append(event["message_id"])

        monkeypatch.setattr(checkin, "refresh_event_message", fake_refresh)
        monkeypatch.setattr(views_rsvp, "refresh_event_message", fake_refresh)

    async def event(self, members, *, compo=COMPO_OPEN, size=2, starts_in=10 * 60):
        """`members` = [(user_id, role), ...] in join order.

        By default the check-in went out 50 minutes ago and the event starts in
        10 minutes, so the 15-minute deadline passed 5 minutes ago.
        """
        self.now = time.time()
        self.prompt = _prompt_id(self.now - 50 * 60)
        await self.db.create_event(
            message_id=EVENT_ID,
            channel_id=2,
            guild_id=1,
            creator_id=4,
            creator_name="Wiz",
            title="Krao run",
            activity="Krao Cave Hard",
            compo=compo,
            size=size,
            starts_at=int(self.now + starts_in),
            status="open",
        )
        for offset, (user_id, role) in enumerate(members):
            await self.db.upsert_signup(
                EVENT_ID, user_id, f"u{user_id}", role, self.now - 1000 + offset
            )
        await self.db.mark_rsvp_sent(EVENT_ID)
        await self.db.set_rsvp_prompt_id(EVENT_ID, self.prompt)

    async def lists(self):
        ev = await self.db.get_event(EVENT_ID)
        party, waitlist = assign(
            ev["compo"], ev["size"], await self.db.get_signups(EVENT_ID)
        )
        return [s["user_id"] for s in party], [s["user_id"] for s in waitlist]

    async def press(self, user_id, status, prompt=None):
        followups = []

        async def followup_send(content, **_):
            followups.append(content)

        interaction = SimpleNamespace(
            client=self.client,
            guild=None,
            user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(id=prompt or self.prompt),
            response=_Response(),
            followup=SimpleNamespace(send=followup_send),
        )
        await RSVPView("en")._respond(interaction, status)
        return interaction.response, followups


def _run(tmp_path, monkeypatch, scenario):
    async def go():
        db = Database(str(tmp_path / "checkin.db"))
        await db.connect()
        try:
            return await scenario(_World(db, monkeypatch))
        finally:
            await db.close()

    return asyncio.run(go())


# ----- "Can't make it" -----


def test_declining_frees_the_seat_and_promotes_the_next_in_line(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")], starts_in=60 * 60)
        response, followups = await w.press(2, "no")
        return await w.lists(), response, followups, w.redrawn

    (party, waitlist), response, followups, redrawn = _run(
        tmp_path, monkeypatch, scenario
    )
    assert party == [1, 3]
    assert waitlist == []
    # The prompt was redrawn and still lists who declined.
    fields = {f.name: f.value for f in response.edits[0]["embed"].fields}
    assert fields["❌ Can't make it (1)"] == "• <@2>"
    assert followups == [
        "📣 <@3>: a spot opened up, you're in the party for **Krao run**!"
    ]
    assert redrawn == [EVENT_ID]


def test_declining_with_nobody_waiting_just_leaves_the_seat_open(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps")], starts_in=60 * 60)
        _response, followups = await w.press(2, "no")
        return await w.lists(), followups

    (party, waitlist), followups = _run(tmp_path, monkeypatch, scenario)
    assert (party, waitlist) == ([1], [])
    assert followups == []


def test_someone_not_signed_up_cannot_answer(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps")])
        response, _ = await w.press(77, "yes")
        return response, await w.db.get_rsvps(EVENT_ID)

    response, rsvps = _run(tmp_path, monkeypatch, scenario)
    assert response.private == ["Sign up for the event first, then RSVP here."]
    assert rsvps == []


def test_a_waitlisted_member_can_answer(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")], starts_in=60 * 60)
        response, _ = await w.press(3, "yes")
        return response

    response = _run(tmp_path, monkeypatch, scenario)
    fields = {f.name: f.value for f in response.edits[0]["embed"].fields}
    assert fields["🪑 Waitlist ready (1 of 1)"] == "• <@3>"


# ----- the deadline -----


def test_before_the_deadline_no_seat_moves(tmp_path, monkeypatch):
    async def scenario(w):
        # Starts in an hour: the deadline is still 45 minutes away.
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")], starts_in=60 * 60)
        await w.press(3, "yes")
        swaps = await checkin.timed_pass(w.client, EVENT_ID)
        return swaps, await w.lists()

    swaps, (party, waitlist) = _run(tmp_path, monkeypatch, scenario)
    assert swaps == []
    assert (party, waitlist) == ([1, 2], [3])


def test_after_the_deadline_a_silent_seat_goes_to_a_ready_player(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")])
        await w.db.set_rsvp(EVENT_ID, 1, "yes")
        await w.db.set_rsvp(EVENT_ID, 3, "yes")
        swaps = await checkin.timed_pass(w.client, EVENT_ID)
        again = await checkin.timed_pass(w.client, EVENT_ID)
        return swaps, again, await w.lists(), w

    swaps, again, (party, waitlist), w = _run(tmp_path, monkeypatch, scenario)
    assert swaps == [(2, 3)]
    assert again == []  # nothing left to do, and nobody is pinged twice
    assert (party, waitlist) == ([1, 3], [2])
    assert w.redrawn == [EVENT_ID]
    assert [message_id for message_id, _ in w.channel.edits] == [w.prompt]
    assert len(w.channel.sent) == 1
    assert "<@3> takes the seat of <@2>" in w.channel.sent[0]


def test_silent_players_keep_their_seats_when_nobody_ready_is_waiting(
    tmp_path, monkeypatch
):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")])
        swaps = await checkin.timed_pass(w.client, EVENT_ID)
        return swaps, await w.lists(), w.channel.sent

    swaps, (party, waitlist), sent = _run(tmp_path, monkeypatch, scenario)
    assert swaps == []
    assert (party, waitlist) == ([1, 2], [3])
    assert sent == []


def test_a_waitlisted_yes_after_the_deadline_takes_a_silent_seat_at_once(
    tmp_path, monkeypatch
):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")])
        _response, followups = await w.press(3, "yes")
        return await w.lists(), followups

    (party, waitlist), followups = _run(tmp_path, monkeypatch, scenario)
    assert (party, waitlist) == ([1, 3], [2])
    assert len(followups) == 1
    assert "<@3> takes the seat of <@2>" in followups[0]


def test_standard_party_keeps_its_roles(tmp_path, monkeypatch):
    async def scenario(w):
        members = [(1, "tank"), (2, "heal"), (3, "dps"), (4, "dps"), (5, "dps")]
        members += [(6, "dps"), (7, "tank")]
        await w.event(members, compo=COMPO_STANDARD, size=5)
        await w.db.set_rsvp(EVENT_ID, 7, "yes")  # a ready tank
        swaps = await checkin.timed_pass(w.client, EVENT_ID)
        return swaps, await w.lists()

    swaps, (party, waitlist) = _run(tmp_path, monkeypatch, scenario)
    assert swaps == [(1, 7)]
    assert party == [7, 2, 3, 4, 5]
    assert waitlist == [6, 1]


def test_declining_after_the_deadline_announces_only_the_net_result(
    tmp_path, monkeypatch
):
    async def scenario(w):
        # 3 is next in line but silent; 4 is behind them and ready.
        await w.event([(1, "dps"), (2, "dps"), (3, "dps"), (4, "dps")])
        await w.db.set_rsvp(EVENT_ID, 1, "yes")
        await w.db.set_rsvp(EVENT_ID, 4, "yes")
        # The pass first gives silent 2's seat to 4.
        await checkin.timed_pass(w.client, EVENT_ID)
        before = await w.lists()
        _response, followups = await w.press(1, "no")
        return before, await w.lists(), followups

    before, (party, waitlist), followups = _run(tmp_path, monkeypatch, scenario)
    assert before == ([1, 4], [3, 2])
    # 1 left; 3 is next in line and gets the open seat.
    assert (party, waitlist) == ([4, 3], [2])
    assert followups == [
        "📣 <@3>: a spot opened up, you're in the party for **Krao run**!"
    ]


def test_no_seat_moves_once_the_event_has_started(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")])
        await w.db.set_rsvp(EVENT_ID, 3, "yes")
        started = (await w.db.get_event(EVENT_ID))["starts_at"] + 1
        return await checkin.timed_pass(w.client, EVENT_ID, now=started)

    assert _run(tmp_path, monkeypatch, scenario) == []


def test_no_seat_moves_for_a_cancelled_event(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")])
        await w.db.set_rsvp(EVENT_ID, 3, "yes")
        await w.db.set_status(EVENT_ID, "cancelled")
        return await checkin.timed_pass(w.client, EVENT_ID)

    assert _run(tmp_path, monkeypatch, scenario) == []


def test_switching_it_off_stops_every_hand_over(tmp_path, monkeypatch):
    async def scenario(w):
        monkeypatch.setattr(checkin.config, "RSVP_DEADLINE_MINUTES", 0)
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")])
        await w.db.set_rsvp(EVENT_ID, 3, "yes")
        return await checkin.timed_pass(w.client, EVENT_ID)

    assert _run(tmp_path, monkeypatch, scenario) == []


# ----- stale answers and prompts -----


def test_an_old_prompt_stops_working_once_a_new_one_is_posted(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps")])
        old = w.prompt
        await w.db.set_rsvp_prompt_id(EVENT_ID, old + 1)
        response, _ = await w.press(1, "yes", prompt=old)
        return response

    response = _run(tmp_path, monkeypatch, scenario)
    assert response.private == ["This RSVP is no longer active."]


def test_answers_do_not_survive_leaving_rejoining_or_a_reschedule(tmp_path):
    async def go():
        db = Database(str(tmp_path / "stale.db"))
        await db.connect()
        try:
            await db.create_event(
                message_id=EVENT_ID,
                channel_id=2,
                guild_id=1,
                creator_id=4,
                creator_name="Wiz",
                title="Run",
                activity="Dungeon",
                compo=COMPO_OPEN,
                size=5,
                starts_at=5000,
                status="open",
            )
            for user_id in (1, 2, 3):
                await db.upsert_signup(EVENT_ID, user_id, "x", "dps", user_id)
                await db.set_rsvp(EVENT_ID, user_id, "yes")

            await db.remove_signup(EVENT_ID, 1)  # left
            after_leave = {r["user_id"] for r in await db.get_rsvps(EVENT_ID)}
            await db.upsert_signup(EVENT_ID, 2, "x", "tank", 99)  # switched role
            after_switch = {r["user_id"] for r in await db.get_rsvps(EVENT_ID)}
            await db.update_event_details(
                EVENT_ID,
                title="Run",
                starts_at=9000,
                description=None,
                rearm_notifications=True,
            )
            return after_leave, after_switch, await db.get_rsvps(EVENT_ID)
        finally:
            await db.close()

    after_leave, after_switch, after_reschedule = asyncio.run(go())
    assert after_leave == {2, 3}
    assert after_switch == {3}
    assert after_reschedule == []


def test_only_events_inside_the_deadline_window_are_looked_at(tmp_path):
    async def go():
        db = Database(str(tmp_path / "due.db"))
        await db.connect()
        try:
            for message_id, starts_at, prompt in (
                (1, 1500, 11),  # starts in 500 s: inside a 900 s window
                (2, 5000, 12),  # too far ahead
                (3, 900, 13),  # already started
                (4, 1500, None),  # no check-in posted
            ):
                await db.create_event(
                    message_id=message_id,
                    channel_id=2,
                    guild_id=1,
                    creator_id=4,
                    creator_name="Wiz",
                    title="Run",
                    activity="Dungeon",
                    compo=COMPO_OPEN,
                    size=5,
                    starts_at=starts_at,
                    status="open",
                )
                if prompt:
                    await db.mark_rsvp_sent(message_id)
                    await db.set_rsvp_prompt_id(message_id, prompt)
            due = await db.events_in_checkin(now_ts=1000, lead_s=900)
            return [e["message_id"] for e in due]
        finally:
            await db.close()

    assert asyncio.run(go()) == [1]


def test_the_check_in_pings_the_party_and_then_the_waitlist(tmp_path, monkeypatch):
    from bot.cogs.groups import Groups

    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")], starts_in=60 * 60)
        posted = []

        async def send(**kwargs):
            posted.append(kwargs)

        w.channel.send = send
        cog = Groups(w.client)
        ev = await w.db.get_event(EVENT_ID)
        party, waitlist = assign(ev["compo"], ev["size"], await w.db.get_signups(9))
        await cog._send_rsvp(ev, party, waitlist)
        return posted[0], ev

    posted, ev = _run(tmp_path, monkeypatch, scenario)
    assert posted["content"] == (
        "<@1> <@2>\nWaitlist: <@3> — tap ✅ if you can take a seat that frees up."
    )
    # The prompt says when unanswered seats can start to move.
    assert f"<t:{ev['starts_at'] - 15 * 60}:t>" in posted["embed"].description
