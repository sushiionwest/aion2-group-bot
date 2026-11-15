"""The event listing's button layout and the admin queue panel. Run: pytest"""

import asyncio
import time
from types import SimpleNamespace

import discord

from bot import i18n, views
from bot.cogs.groups import Groups
from bot.db import Database
from bot.logic import COMPO_STANDARD
from bot.views import QueueManageView, RetiredButtonsView, SignupView, _queue_panel


def _rows(view) -> dict:
    """custom_ids per rendered action row, in display order."""
    rows = {}
    for row in view.to_components():
        ids = [c["custom_id"] for c in row["components"]]
        rows[len(rows)] = ids
    return rows


def test_listing_has_two_rows_of_buttons_in_the_agreed_order():
    async def build():
        return SignupView("en")

    view = asyncio.run(build())
    assert _rows(view) == {
        0: ["aion2:tank", "aion2:heal", "aion2:dps", "aion2:leave"],
        1: ["aion2:done", "aion2:cancel", "aion2:edit", "aion2:queue"],
    }
    done = next(c for c in view.children if c.custom_id == "aion2:done")
    assert done.label == "Complete event"
    assert done.style is discord.ButtonStyle.secondary


def test_retired_buttons_are_still_answered_but_never_shown_on_new_listings():
    async def build():
        return SignupView("en"), RetiredButtonsView()

    signup, retired = asyncio.run(build())
    retired_ids = {c.custom_id for c in retired.children}
    assert retired_ids == {"aion2:ics", "aion2:lfg"}
    assert not retired_ids & {c.custom_id for c in signup.children}


def test_no_reminder_is_sent_before_an_event_starts():
    # Only the "are you coming?" prompt pings the party ahead of the start.
    assert not hasattr(Groups, "reminders")
    assert hasattr(Groups, "rsvp_prompts")


class _Response:
    def __init__(self):
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content, **_):
        self.sent.append(content)


async def _event_with(db, members):
    """A standard party of 5 with `members` = [(user_id, name, role), ...]."""
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
    )
    for offset, (user_id, name, role) in enumerate(members):
        await db.upsert_signup(9, user_id, name, role, time.time() + offset)
    return await db.get_event(9)


def _remove(tmp_path, monkeypatch, members, selected):
    """Presses Remove on `selected` and returns what happened."""

    async def go():
        db = Database(str(tmp_path / "queue.db"))
        await db.connect()
        refreshed = []

        async def fake_refresh(client, event):
            refreshed.append(event["message_id"])

        monkeypatch.setattr(views, "refresh_event_message", fake_refresh)
        try:
            event = await _event_with(db, members)
            channel = _Channel()
            interaction = SimpleNamespace(
                client=SimpleNamespace(db=db, get_channel=lambda _id: channel),
                response=_Response(),
            )
            panel = QueueManageView(event, await db.get_signups(9), "en")
            # Set after building: the panel only ever offers current sign-ups,
            # so a stale pick is one that left while the panel stayed open.
            panel.selected = selected
            await panel.remove_button.callback(interaction)
            left = [s["user_id"] for s in await db.get_signups(9)]
            return left, interaction.response.edits, refreshed, channel.sent
        finally:
            await db.close()

    return asyncio.run(go())


def test_removing_the_only_signup_leaves_a_panel_without_a_dropdown(
    tmp_path, monkeypatch
):
    left, edits, refreshed, sent = _remove(
        tmp_path, monkeypatch, [(10, "Seia", "dps")], selected=10
    )
    assert left == []
    assert refreshed == [9]
    assert sent == []
    (edit,) = edits
    assert edit["view"] is None
    assert "Seia" in edit["content"]
    assert i18n.t("signup.manage_empty", "en") in edit["content"]


def test_removing_a_party_member_promotes_and_pings_the_waitlist(tmp_path, monkeypatch):
    # One tank slot: the second tank waits until the first is removed.
    members = [(10, "Seia", "tank"), (11, "Wiz", "tank"), (12, "Sushi", "dps")]
    left, edits, refreshed, sent = _remove(tmp_path, monkeypatch, members, selected=10)
    assert left == [11, 12]
    assert refreshed == [9]
    (edit,) = edits
    assert isinstance(edit["view"], QueueManageView)
    assert edit["view"].selected == 11
    assert len(sent) == 1 and "<@11>" in sent[0] and "<@12>" not in sent[0]


def test_removing_someone_who_already_left_just_redraws(tmp_path, monkeypatch):
    left, edits, refreshed, sent = _remove(
        tmp_path, monkeypatch, [(10, "Seia", "dps")], selected=999
    )
    assert left == [10]
    assert refreshed == [] and sent == []
    assert isinstance(edits[0]["view"], QueueManageView)


def _open_queue(tmp_path, monkeypatch, members):
    """Presses Manage queue on the listing and returns the reply's kwargs."""

    async def go():
        db = Database(str(tmp_path / "open.db"))
        await db.connect()
        sent = []

        async def send_message(content, **kwargs):
            sent.append({"content": content, **kwargs})

        async def is_admin(_db, _member):
            return True

        monkeypatch.setattr(views, "member_is_admin", is_admin)
        try:
            await _event_with(db, members)
            interaction = SimpleNamespace(
                client=SimpleNamespace(db=db),
                guild=None,
                user=SimpleNamespace(id=4),
                message=SimpleNamespace(id=9),
                response=SimpleNamespace(send_message=send_message),
            )
            await SignupView("en").queue_button.callback(interaction)
            return sent
        finally:
            await db.close()

    return asyncio.run(go())


def test_manage_queue_on_an_empty_event_replies_without_a_view(tmp_path, monkeypatch):
    # discord.py rejects view=None on a new message, so the key must be absent.
    (reply,) = _open_queue(tmp_path, monkeypatch, [])
    assert reply == {
        "content": i18n.t("signup.manage_empty", "en"),
        "ephemeral": True,
    }


def test_manage_queue_opens_with_a_single_signup(tmp_path, monkeypatch):
    (reply,) = _open_queue(tmp_path, monkeypatch, [(10, "Seia", "dps")])
    assert isinstance(reply["view"], QueueManageView)
    assert [c.custom_id for c in reply["view"].children[:3]] == [
        "queue:up",
        "queue:down",
        "queue:remove",
    ]


def test_an_empty_queue_opens_as_a_single_line():
    panel = _queue_panel({"message_id": 9}, [], "en")
    assert panel == {"content": i18n.t("signup.manage_empty", "en"), "view": None}
