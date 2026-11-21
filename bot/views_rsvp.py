"""The two buttons under an are-you-coming RSVP prompt."""

import time

import discord

from . import checkin, i18n
from .errors import ViewErrorMixin
from .logic import assign
from .utils.rsvp import NO, YES
from .views_common import EVENT_LOCKS, refresh_event_message


class RSVPView(ViewErrorMixin, discord.ui.View):
    """The two buttons under an 'are you coming?' prompt.

    Persistent (fixed custom_ids). The prompt is a separate message, so its
    buttons find their event through `events.rsvp_prompt_id`.

    Anyone signed up may answer, party or waitlist. "Can't make it" gives the
    seat up at once; a waitlisted "I'm coming" marks the member as ready to
    take a seat nobody confirmed (see `checkin.run_autofill`).
    """

    _LABELS = {
        "rsvp:yes": "rsvp.btn_coming",
        "rsvp:no": "rsvp.btn_not_coming",
    }

    def __init__(self, lang: str = "en"):
        super().__init__(timeout=None)
        for child in self.children:
            key = self._LABELS.get(getattr(child, "custom_id", None))
            if key is not None:
                child.label = i18n.t(key, lang)

    @discord.ui.button(
        label="I'm coming",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="rsvp:yes",
    )
    async def coming(self, interaction: discord.Interaction, _):
        await self._respond(interaction, YES)

    @discord.ui.button(
        label="Can't make it",
        emoji="❌",
        style=discord.ButtonStyle.secondary,
        custom_id="rsvp:no",
    )
    async def not_coming(self, interaction: discord.Interaction, _):
        await self._respond(interaction, NO)

    async def _respond(self, interaction: discord.Interaction, status: str):
        client = interaction.client
        db = client.db
        lang = await i18n.resolve_lang(db, interaction.guild)
        found = await db.get_event_by_rsvp_prompt(interaction.message.id)
        if found is None:
            await interaction.response.send_message(
                i18n.t("rsvp.inactive", lang), ephemeral=True
            )
            return

        message_id = found["message_id"]
        user_id = interaction.user.id
        async with EVENT_LOCKS[message_id]:
            # Re-read under the lock: the event may have been cancelled,
            # completed or given a newer prompt since the click. A reschedule
            # wipes the answers and re-arms the check-in (rsvp_sent = 0); until
            # the new prompt is posted, this one is for the old time and must
            # not take answers that could move a seat.
            event = await db.get_event(message_id)
            if (
                event is None
                or event["status"] != "open"
                or not event["rsvp_sent"]
                or event["rsvp_prompt_id"] != interaction.message.id
            ):
                await interaction.response.send_message(
                    i18n.t("rsvp.inactive", lang), ephemeral=True
                )
                return
            signups = await db.get_signups(message_id)
            if user_id not in {s["user_id"] for s in signups}:
                await interaction.response.send_message(
                    i18n.t("rsvp.sign_up_first", lang), ephemeral=True
                )
                return
            party_before, _ = assign(event["compo"], event["size"], signups)

            if status == NO:
                # Declining gives the seat up, exactly like Leave; the "no" is
                # kept so the prompt still lists who can't make it.
                await db.remove_signup(message_id, user_id)
            await db.set_rsvp(message_id, user_id, status)
            swaps = await checkin.run_autofill(client, event, time.time())

            party_after, _ = assign(
                event["compo"], event["size"], await db.get_signups(message_id)
            )
            # Answer the click first: Discord drops an interaction left
            # unanswered for three seconds, so the other edits come after.
            embed = await checkin.build_prompt_embed(client, event, lang)
            await interaction.response.edit_message(embed=embed, view=self)

            changed = [s["user_id"] for s in party_before] != [
                s["user_id"] for s in party_after
            ]
            if changed or status == NO:
                try:
                    await refresh_event_message(client, event)
                except discord.HTTPException:
                    pass  # event message gone or not editable: the answer stands
            await checkin.announce(
                interaction.followup.send,
                checkin.change_messages(
                    event, party_before, party_after, swaps, lang, skip_user_id=user_id
                ),
            )
