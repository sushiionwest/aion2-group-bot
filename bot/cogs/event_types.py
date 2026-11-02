"""/eventtypes: lets a server's admins edit the event types themselves.

The list feeds the panel's "Create an event" dropdown and /event's type
option. Each type can ping a role when a group is created, carry an emoji,
and be reserved to staff (moderators).
"""

import discord
from discord import app_commands
from discord.ext import commands

from .. import i18n
from ..db_event_types import MAX_EVENT_TYPES
from ..utils.emoji import is_valid as emoji_is_valid
from ..utils.permissions import member_is_admin


def describe_event_type(row, lang: str) -> str:
    """One line of /eventtypes list: emoji, name, ping role, staff flag."""
    parts = [f"{row['emoji'] or '▫️'} **{row['name']}**"]
    if row["role_id"]:
        parts.append(i18n.t("eventtypes.pings", lang, role=f"<@&{row['role_id']}>"))
    if row["staff_only"]:
        parts.append(i18n.t("eventtypes.staff_only_label", lang))
    return " · ".join(parts)


async def _type_name_autocomplete(interaction: discord.Interaction, current: str):
    needle = current.casefold()
    return [
        app_commands.Choice(name=row["name"], value=row["name"])
        for row in await interaction.client.db.get_event_types(interaction.guild_id)
        if needle in row["name"].casefold()
    ][:25]


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class EventTypes(commands.GroupCog, name="eventtypes"):
    """Edit the event types offered when creating a group (admins)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _refuse_non_admin(self, interaction: discord.Interaction, lang) -> bool:
        """Answers and returns True when the member isn't a Kisk admin."""
        if await member_is_admin(self.bot.db, interaction.user):
            return False
        await interaction.response.send_message(
            i18n.t("eventtypes.admin_only", lang), ephemeral=True
        )
        return True

    @app_commands.command(name="list", description="Show the event types (admins)")
    async def list_types(self, interaction: discord.Interaction):
        lang = await i18n.resolve_lang(self.bot.db, interaction.guild)
        if await self._refuse_non_admin(interaction, lang):
            return
        types = await self.bot.db.get_event_types(interaction.guild_id)
        lines = [describe_event_type(row, lang) for row in types]
        await interaction.response.send_message(
            i18n.t("eventtypes.list_title", lang)
            + "\n"
            + "\n".join(lines)
            + "\n\n"
            + i18n.t("eventtypes.list_hint", lang),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="add", description="Add an event type, or edit one that exists (admins)"
    )
    @app_commands.describe(
        name="Name shown in the menu, e.g. “Krao Cave Hard”",
        ping="Role to ping when a group of this type is created",
        staff_only="Only moderators can create this type",
        emoji="Emoji shown next to it (a normal emoji or a server emoji)",
        remove_ping="Stop pinging a role for this type",
    )
    @app_commands.autocomplete(name=_type_name_autocomplete)
    async def add(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 50],
        ping: discord.Role | None = None,
        staff_only: bool | None = None,
        emoji: str | None = None,
        remove_ping: bool = False,
    ):
        lang = await i18n.resolve_lang(self.bot.db, interaction.guild)
        if await self._refuse_non_admin(interaction, lang):
            return
        name = name.strip()
        emoji = emoji.strip() if emoji else None
        if emoji and not emoji_is_valid(emoji):
            await interaction.response.send_message(
                i18n.t("eventtypes.bad_emoji", lang, value=emoji), ephemeral=True
            )
            return
        if ping is not None and ping.is_default():
            await interaction.response.send_message(
                i18n.t("eventtypes.no_everyone", lang), ephemeral=True
            )
            return

        result = await self.bot.db.upsert_event_type(
            interaction.guild_id,
            name,
            role_id=ping.id if ping else None,
            clear_role=remove_ping,
            emoji=emoji,
            staff_only=staff_only,
        )
        if result == "full":
            await interaction.response.send_message(
                i18n.t("eventtypes.full", lang, max=MAX_EVENT_TYPES), ephemeral=True
            )
            return

        row = await self.bot.db.get_event_type(interaction.guild_id, name)
        message = (
            i18n.t(f"eventtypes.{result}", lang) + "\n" + describe_event_type(row, lang)
        )
        # A role nobody may mention only pings if Kisk can mention every role.
        if (
            ping is not None
            and not ping.mentionable
            and not interaction.guild.me.guild_permissions.mention_everyone
        ):
            message += "\n\n" + i18n.t(
                "eventtypes.ping_unmentionable", lang, role=ping.mention
            )
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="remove", description="Remove an event type (admins)")
    @app_commands.describe(name="The event type to remove")
    @app_commands.autocomplete(name=_type_name_autocomplete)
    async def remove(self, interaction: discord.Interaction, name: str):
        lang = await i18n.resolve_lang(self.bot.db, interaction.guild)
        if await self._refuse_non_admin(interaction, lang):
            return
        result = await self.bot.db.remove_event_type(interaction.guild_id, name)
        await interaction.response.send_message(
            i18n.t(f"eventtypes.{result}", lang, name=name), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EventTypes(bot))
