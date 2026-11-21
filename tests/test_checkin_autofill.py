"""The check-in end to end: declining, the deadline pass, stale answers.

Run: pytest
"""

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from bot import checkin, views, views_rsvp
from bot.db import Database
from bot.logic import COMPO_OPEN, COMPO_STANDARD, assign
from bot.views import SignupView
from bot.views_rsvp import RSVPView

EVENT_ID = 9
PROMOTED_3 = "📣 <@3>: a spot opened up, you're in the party for **Krao run**!"
PROMOTED_4 = PROMOTED_3.replace("<@3>", "<@4>")
CONFIRM_3 = PROMOTED_3 + " Tap ✅ on the check-in to keep it."


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
        self.edits, self.private, self.deferred = [], [], False

    async def defer(self):
        self.deferred = True

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
        monkeypatch.setattr(views, "refresh_event_message", fake_refresh)

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

    def _interaction(self, user_id, message_id):
        """A click by `user_id`; `.public` / `.private` collect the follow-ups."""
        public, private = [], []

        async def followup_send(content, ephemeral=False, **_):
            (private if ephemeral else public).append(content)

        response = _Response()

        async def edit_original_response(**kwargs):
            response.edits.append(kwargs)

        return SimpleNamespace(
            client=self.client,
            guild=None,
            user=SimpleNamespace(id=user_id, display_name=f"u{user_id}"),
            message=SimpleNamespace(id=message_id),
            response=response,
            edit_original_response=edit_original_response,
            followup=SimpleNamespace(send=followup_send),
            public=public,
            private=private,
        )

    async def press(self, user_id, status, prompt=None):
        """Taps a check-in button; returns (response, public follow-ups)."""
        interaction = self._interaction(user_id, prompt or self.prompt)
        await RSVPView("en")._respond(interaction, status)
        self.private = interaction.private
        return interaction.response, interaction.public


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
        return await w.lists(), response, followups, w

    (party, waitlist), response, followups, w = _run(tmp_path, monkeypatch, scenario)
    assert party == [1, 3]
    assert waitlist == []
    # The prompt was redrawn and still lists who declined.
    fields = {f.name: f.value for f in response.edits[0]["embed"].fields}
    assert fields["❌ Can't make it (1)"] == "• <@2>"
    # 3 was never asked to confirm a seat, so the ping says to do it now.
    assert followups == [CONFIRM_3]
    # The member who declined is told privately what that did.
    assert w.private == [
        "You're out of **Krao run**, so your place goes to someone else. "
        "Changed your mind? Sign up again on the event post."
    ]
    assert w.redrawn == [EVENT_ID]


def test_declining_with_nobody_waiting_just_leaves_the_seat_open(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps")], starts_in=60 * 60)
        _response, followups = await w.press(2, "no")
        return await w.lists(), followups, w.private

    (party, waitlist), followups, private = _run(tmp_path, monkeypatch, scenario)
    assert (party, waitlist) == ([1], [])
    assert followups == []
    assert len(private) == 1 and private[0].startswith("You're out of")


def test_someone_not_signed_up_cannot_answer(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps")])
        response, _ = await w.press(77, "yes")
        return w.private, await w.db.get_rsvps(EVENT_ID)

    private, rsvps = _run(tmp_path, monkeypatch, scenario)
    assert private == ["Sign up for the event first, then RSVP here."]
    assert rsvps == []


def test_a_waitlisted_member_can_answer(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")], starts_in=60 * 60)
        response, _ = await w.press(3, "yes")
        deadline = (await w.db.get_event(EVENT_ID))["starts_at"] - 15 * 60
        return response, w.private, deadline

    response, private, deadline = _run(tmp_path, monkeypatch, scenario)
    fields = {f.name: f.value for f in response.edits[0]["embed"].fields}
    assert fields["🪑 Waitlist ready (1 of 1)"] == "• <@3>"
    # Nothing visible happens to them yet, so they are told what to expect.
    assert private == [
        "You're marked ready for **Krao run** (#1 on the waitlist). "
        f"From <t:{deadline}:t>, a seat with no reply goes to ready players "
        "first. You'll be pinged if you get one."
    ]


def test_a_party_member_who_confirms_needs_no_extra_note(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps")], starts_in=60 * 60)
        response, followups = await w.press(1, "yes")
        return response, followups, w.private

    response, followups, private = _run(tmp_path, monkeypatch, scenario)
    fields = {f.name: f.value for f in response.edits[0]["embed"].fields}
    assert fields["✅ Coming (1)"] == "• <@1>"
    assert followups == [] and private == []


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
        return await w.lists(), followups, w.private

    (party, waitlist), followups, private = _run(tmp_path, monkeypatch, scenario)
    assert (party, waitlist) == ([1, 3], [2])
    assert len(followups) == 1
    assert "<@3> takes the seat of <@2>" in followups[0]
    assert private == []  # the public line already tells them they are in


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
        await w.db.set_rsvp(EVENT_ID, 2, "yes")
        await w.db.set_rsvp(EVENT_ID, 4, "yes")
        _response, followups = await w.press(1, "no")
        return await w.lists(), followups

    (party, waitlist), followups = _run(tmp_path, monkeypatch, scenario)
    # 1's seat first falls to 3 (next in line), who is silent, so it goes on
    # to 4 in the same step. 3 ends where they started and is never pinged.
    assert (party, waitlist) == ([2, 4], [3])
    # 4 already confirmed, so the ping does not ask them to do it again.
    assert followups == [PROMOTED_4]


def test_an_old_prompt_takes_no_answers_after_a_reschedule(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")])
        await w.db.set_rsvp(EVENT_ID, 1, "yes")
        await w.db.set_rsvp(EVENT_ID, 2, "yes")
        ev = await w.db.get_event(EVENT_ID)
        # Moved a few minutes: answers are wiped and the check-in re-armed,
        # but the new prompt has not been posted yet.
        await w.db.update_event_details(
            EVENT_ID,
            title=ev["title"],
            starts_at=ev["starts_at"] + 120,
            description=None,
            rearm_notifications=True,
        )
        _response, followups = await w.press(3, "yes")
        return w.private, followups, await w.lists(), await w.db.get_rsvps(EVENT_ID)

    private, followups, lists, rsvps = _run(tmp_path, monkeypatch, scenario)
    assert private == ["This RSVP is no longer active."]
    assert followups == []
    assert lists == ([1, 2], [3])
    assert rsvps == []


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
        return response.private, await w.db.get_rsvps(EVENT_ID)

    private, rsvps = _run(tmp_path, monkeypatch, scenario)
    assert private == ["This RSVP is no longer active."]
    assert rsvps == []


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


# ----- changes made on the event post while the check-in is out -----


def test_signing_up_after_the_check_in_counts_as_coming(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps")], starts_in=60 * 60)
        interaction = w._interaction(5, EVENT_ID)
        event = await w.db.get_event(EVENT_ID)
        await SignupView("en").perform_join(interaction, event, "dps", None)
        return {r["user_id"]: r["status"] for r in await w.db.get_rsvps(EVENT_ID)}

    assert _run(tmp_path, monkeypatch, scenario) == {5: "yes"}


def test_signing_up_before_the_check_in_is_not_an_answer(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps")], starts_in=60 * 60)
        await w.db.conn.execute(
            "UPDATE events SET rsvp_sent = 0, rsvp_prompt_id = NULL"
        )
        interaction = w._interaction(5, EVENT_ID)
        event = await w.db.get_event(EVENT_ID)
        await SignupView("en").perform_join(interaction, event, "dps", None)
        return await w.db.get_rsvps(EVENT_ID)

    assert _run(tmp_path, monkeypatch, scenario) == []


def test_leaving_after_the_deadline_seats_a_confirmed_player_in_one_step(
    tmp_path, monkeypatch
):
    async def scenario(w):
        # 3 is next in line but silent; 4 is behind them and ready.
        await w.event([(1, "dps"), (2, "dps"), (3, "dps"), (4, "dps")])
        await w.db.set_rsvp(EVENT_ID, 2, "yes")
        await w.db.set_rsvp(EVENT_ID, 4, "yes")
        interaction = w._interaction(1, EVENT_ID)
        await SignupView("en").leave_button.callback(interaction)
        return await w.lists(), interaction, w

    (party, waitlist), interaction, w = _run(tmp_path, monkeypatch, scenario)
    assert (party, waitlist) == ([2, 4], [3])
    assert interaction.public == [PROMOTED_4]
    assert interaction.private == ["You've left the event. 👋"]
    # The check-in was redrawn too, so it no longer lists the member who left.
    assert [message_id for message_id, _ in w.channel.edits] == [w.prompt]


def test_leaving_before_the_deadline_asks_the_next_in_line_to_confirm(
    tmp_path, monkeypatch
):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")], starts_in=60 * 60)
        interaction = w._interaction(1, EVENT_ID)
        await SignupView("en").leave_button.callback(interaction)
        return await w.lists(), interaction.public

    (party, waitlist), public = _run(tmp_path, monkeypatch, scenario)
    assert (party, waitlist) == ([2, 3], [])
    assert public == [CONFIRM_3]


def test_promotion_without_a_check_in_keeps_the_plain_wording(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")], starts_in=60 * 60)
        await w.db.conn.execute(
            "UPDATE events SET rsvp_sent = 0, rsvp_prompt_id = NULL"
        )
        interaction = w._interaction(1, EVENT_ID)
        await SignupView("en").leave_button.callback(interaction)
        return interaction.public, w.channel.edits

    public, prompt_edits = _run(tmp_path, monkeypatch, scenario)
    assert public == [PROMOTED_3]
    assert prompt_edits == []


def test_a_tap_is_acknowledged_before_it_waits_its_turn(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps")], starts_in=60 * 60)
        interaction = w._interaction(1, w.prompt)
        seen = []

        async def defer():
            # The event's lock must not be held yet when the click is answered.
            seen.append(views_rsvp.EVENT_LOCKS[EVENT_ID].locked())

        interaction.response.defer = defer
        await RSVPView("en")._respond(interaction, "yes")
        return seen

    assert _run(tmp_path, monkeypatch, scenario) == [False]


def test_standard_events_say_that_seats_only_move_within_a_role(tmp_path, monkeypatch):
    async def scenario(w):
        members = [(1, "tank"), (2, "heal"), (3, "dps"), (4, "dps"), (5, "dps")]
        members += [(6, "dps")]
        await w.event(members, compo=COMPO_STANDARD, size=5, starts_in=60 * 60)
        response, _ = await w.press(6, "yes")
        return response.edits[0]["embed"].description, w.private[0]

    description, private = _run(tmp_path, monkeypatch, scenario)
    rule = "Seats only move within a role: a tank for a tank"
    assert rule in description
    assert rule in private


def test_open_events_do_not_mention_roles(tmp_path, monkeypatch):
    async def scenario(w):
        await w.event([(1, "dps"), (2, "dps"), (3, "dps")], starts_in=60 * 60)
        response, _ = await w.press(3, "yes")
        return response.edits[0]["embed"].description, w.private[0]

    description, private = _run(tmp_path, monkeypatch, scenario)
    assert "within a role" not in description
    assert "within a role" not in private


def test_a_player_seated_by_an_admin_is_not_swapped_back_out(tmp_path, monkeypatch):
    from bot.views import QueueManageView

    async def scenario(w):
        # 1 and 2 confirmed; 3 is ready on the waitlist; 4 is silent behind them.
        await w.event([(1, "dps"), (2, "dps"), (3, "dps"), (4, "dps")])
        for user_id in (1, 2, 3):
            await w.db.set_rsvp(EVENT_ID, user_id, "yes")
        event = await w.db.get_event(EVENT_ID)
        panel = QueueManageView(event, await w.db.get_signups(EVENT_ID), "en")
        panel.selected = 4
        # An admin moves 4 up twice: past 3, then into the party over 2.
        for _ in range(2):
            await panel.up_button.callback(w._interaction(99, EVENT_ID))
        seated = await w.lists()
        swaps = await checkin.timed_pass(w.client, EVENT_ID)
        return seated, swaps, await w.lists()

    seated, swaps, final = _run(tmp_path, monkeypatch, scenario)
    assert seated == ([1, 4], [2, 3])
    # The pass leaves the admin's choice alone.
    assert swaps == []
    assert final == seated
