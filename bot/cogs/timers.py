"""/timers: a live board of boss timers, with optional role pings.

Abyss bosses and the rift follow the game server's clock, so the bot works
their times out alone. Field bosses respawn a set time after each kill, which
only players see: members report those with the board's buttons.
"""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import i18n, theme
from ..branding import brand
from ..errors import ModalErrorMixin, ViewErrorMixin, resilient_tick
from ..utils.boss_timers import (
    DEFAULT_LEAD_MINUTES,
    DEFAULT_TIMEZONE,
    GROUPS,
    PING_LIFETIME_S,
    TIMERS,
    TIMERS_BY_KEY,
    due_pings,
    parse_countdown,
    parse_schedule,
    respawn_seconds,
    timer_state,
)
from ..utils.permissions import member_is_admin

log = logging.getLogger("kisk")

GROUP_EMOJI = {"field": "👹", "abyss": "⚔️", "rift": "🌀"}
FIELD_TIMERS = [timer for timer in TIMERS if timer.respawn_minutes is not None]
CLOCK_TIMERS = [timer for timer in TIMERS if timer.schedule is not None]
# A spawn closer than this shows its clock time without the date.
SAME_DAY_S = 18 * 3600


def board_timezone(board) -> ZoneInfo:
    """The server's game clock, falling back to the default if unset or bad."""
    name = (board["timezone"] if board is not None else None) or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def board_lead_minutes(board) -> int:
    return (board["lead_minutes"] if board is not None else None) or (
        DEFAULT_LEAD_MINUTES
    )


def local_time(ts: int, now: int) -> str:
    """A Discord timestamp, which each member sees in their own timezone: the
    clock time alone when it's soon, with the date once it's further out."""
    if ts - now < SAME_DAY_S:
        return f"<t:{ts}:t>"
    return f"<t:{ts}:d> <t:{ts}:t>"


def _timer_line(timer, state, lang: str, now: int) -> str:
    if state.live_until is not None:
        parts = [i18n.t("timers.live", lang, when=f"<t:{state.live_until}:R>")]
    elif state.up_since is not None:
        key = "timers.up_estimated" if state.up_estimated else "timers.up_since"
        parts = [i18n.t(key, lang, when=f"<t:{state.up_since}:R>")]
        if state.next_at is not None:
            when = local_time(state.next_at, now)
            parts.append(i18n.t("timers.next_estimate", lang, when=when))
    elif state.next_estimated:
        parts = [f"≈ <t:{state.next_at}:R>", local_time(state.next_at, now)]
    elif state.next_at is not None:
        parts = [f"<t:{state.next_at}:R>", local_time(state.next_at, now)]
    else:
        parts = [i18n.t("timers.unknown", lang)]
    if state.set_by is not None:
        parts.append(i18n.t("timers.set_by", lang, who=f"<@{state.set_by}>"))
    return f"**{timer.name}** · " + " · ".join(parts)


def build_timers_embed(rows: dict, tz: ZoneInfo, lang: str, now: int) -> discord.Embed:
    """The board: one section per group, one line per timer. Countdowns are
    Discord timestamps, so they tick on their own and the message only needs
    an edit when a timer rolls over."""
    embed = discord.Embed(title="⏱️ " + i18n.t("timers.title", lang), colour=theme.PINK)
    for group in GROUPS:
        lines = [
            _timer_line(
                timer, timer_state(timer, rows.get(timer.key), now, tz), lang, now
            )
            for timer in TIMERS
            if timer.group == group
        ]
        embed.add_field(
            name=f"{GROUP_EMOJI[group]} {i18n.t(f'timers.group_{group}', lang)}",
            value="\n".join(lines),
            inline=False,
        )
    brand(embed)
    clock = datetime.fromtimestamp(now, tz).strftime("%Z")
    embed.set_footer(text=i18n.t("timers.footer", lang, clock=clock))
    return embed


class SpawnModal(ModalErrorMixin, discord.ui.Modal):
    """Asks when a field boss spawns next: the in-game countdown, or nothing
    when it was just killed (the respawn time then counts from now)."""

    def __init__(self, timer, lang: str = "en"):
        super().__init__(title=i18n.t("timers.modal_title", lang, name=timer.name)[:45])
        self.timer = timer
        self.lang = lang
        self.countdown = discord.ui.TextInput(
            label=i18n.t("timers.modal_label", lang)[:45],
            placeholder=i18n.t("timers.modal_placeholder", lang)[:100],
            required=False,
            max_length=12,
        )
        self.add_item(self.countdown)

    async def on_submit(self, interaction: discord.Interaction):
        db = interaction.client.db
        try:
            seconds = parse_countdown(self.countdown.value)
        except ValueError:
            await interaction.response.send_message(
                i18n.t("timers.bad_time", self.lang, value=self.countdown.value),
                ephemeral=True,
            )
            return

        now = int(time.time())
        rows = await db.get_boss_timers(interaction.guild_id)
        respawn = respawn_seconds(self.timer, rows.get(self.timer.key))
        if seconds is None:
            seconds = respawn
        elif seconds > respawn:
            # Most likely minutes:seconds read as hours:minutes. A boss can't
            # be further away than a full respawn, so refuse rather than store
            # a timer that is days off.
            await interaction.response.send_message(
                i18n.t(
                    "timers.too_long",
                    self.lang,
                    name=self.timer.name,
                    hours=f"{respawn / 3600:g}",
                    value=self.countdown.value,
                ),
                ephemeral=True,
            )
            return
        spawn_at = now + seconds
        await db.set_boss_spawn(
            interaction.guild_id, self.timer.key, spawn_at, interaction.user.id
        )

        cog = interaction.client.get_cog("Timers")
        embed = await cog.board_embed(interaction.guild, now)
        await interaction.response.edit_message(embed=embed)
        await interaction.followup.send(
            i18n.t(
                "timers.saved",
                self.lang,
                name=self.timer.name,
                when=f"<t:{spawn_at}:R> ({local_time(spawn_at, now)})",
            ),
            ephemeral=True,
        )


class SpawnButton(discord.ui.Button):
    def __init__(self, timer):
        super().__init__(
            label=timer.name,
            emoji="⏱️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"timers:set:{timer.key}",
        )
        self.timer = timer

    async def callback(self, interaction: discord.Interaction):
        lang = await i18n.resolve_lang(interaction.client.db, interaction.guild)
        await interaction.response.send_modal(SpawnModal(self.timer, lang))


class TimersBoardView(ViewErrorMixin, discord.ui.View):
    """One button per field boss under the board. Persistent: the custom_ids
    are fixed, so the buttons keep working after a restart."""

    def __init__(self):
        super().__init__(timeout=None)
        for timer in FIELD_TIMERS:
            self.add_item(SpawnButton(timer))


async def _timer_autocomplete(interaction: discord.Interaction, current: str):
    needle = current.casefold()
    return [
        app_commands.Choice(name=timer.name, value=timer.key)
        for timer in TIMERS
        if needle in timer.name.casefold()
    ][:25]


def _find_timer(value: str):
    """A timer by key, or by name for a value typed instead of picked."""
    timer = TIMERS_BY_KEY.get(value)
    if timer is None:
        timer = next((t for t in TIMERS if t.name.casefold() == value.casefold()), None)
    return timer


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Timers(commands.GroupCog, group_name="timers"):
    """The boss timers board, its settings and its ping loop (admins)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # The board as last drawn per guild, to skip edits that change nothing.
        self._drawn: dict[int, dict] = {}

    async def cog_load(self):
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    async def board_embed(self, guild, now: int) -> discord.Embed:
        """Builds the guild's board and remembers it as the drawn state."""
        db = self.bot.db
        lang = await i18n.resolve_lang(db, guild)
        board = await db.get_timer_board(guild.id)
        rows = await db.get_boss_timers(guild.id)
        embed = build_timers_embed(rows, board_timezone(board), lang, now)
        self._drawn[guild.id] = embed.to_dict()
        return embed

    async def _refuse_non_admin(self, interaction: discord.Interaction, lang) -> bool:
        if await member_is_admin(self.bot.db, interaction.user):
            return False
        await interaction.response.send_message(
            i18n.t("timers.admin_only", lang), ephemeral=True
        )
        return True

    async def _resolve_timer(self, interaction, value: str, lang):
        timer = _find_timer(value)
        if timer is None:
            await interaction.response.send_message(
                i18n.t("timers.unknown_timer", lang, name=value), ephemeral=True
            )
        return timer

    async def _redraw(self, guild) -> None:
        """Redraws the posted board now, after a settings change."""
        self._drawn.pop(guild.id, None)
        board = await self.bot.db.get_timer_board(guild.id)
        if board is not None and board["message_id"]:
            await self._refresh_board(board, int(time.time()))

    # ----- Commands -----

    @app_commands.command(
        name="post", description="Post the boss timers board in this channel (admins)"
    )
    async def post(self, interaction: discord.Interaction):
        db = self.bot.db
        lang = await i18n.resolve_lang(db, interaction.guild)
        if await self._refuse_non_admin(interaction, lang):
            return
        previous = await db.get_timer_board(interaction.guild_id)
        embed = await self.board_embed(interaction.guild, int(time.time()))
        try:
            message = await interaction.channel.send(
                embed=embed, view=TimersBoardView()
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                i18n.t("timers.post_forbidden", lang), ephemeral=True
            )
            return
        await db.set_timer_board(
            interaction.guild_id, interaction.channel_id, message.id
        )
        await interaction.response.send_message(
            i18n.t("timers.posted", lang, link=message.jump_url), ephemeral=True
        )
        # One board per server: the one it replaces would stop updating.
        if previous is not None and previous["message_id"]:
            await self._delete_message(previous["channel_id"], previous["message_id"])

    @app_commands.command(
        name="role", description="Choose the role pinged before a boss spawns (admins)"
    )
    @app_commands.describe(
        timer="The boss or event",
        role="Role to ping. Leave empty to stop pinging for this timer",
    )
    @app_commands.autocomplete(timer=_timer_autocomplete)
    async def role(
        self,
        interaction: discord.Interaction,
        timer: str,
        role: discord.Role | None = None,
    ):
        db = self.bot.db
        lang = await i18n.resolve_lang(db, interaction.guild)
        if await self._refuse_non_admin(interaction, lang):
            return
        found = await self._resolve_timer(interaction, timer, lang)
        if found is None:
            return
        if role is not None and role.is_default():
            await interaction.response.send_message(
                i18n.t("timers.no_everyone", lang), ephemeral=True
            )
            return

        await db.set_boss_timer_config(
            interaction.guild_id, found.key, role_id=role.id if role else None
        )
        if role is None:
            message = i18n.t("timers.role_cleared", lang, name=found.name)
        else:
            board = await db.get_timer_board(interaction.guild_id)
            message = i18n.t(
                "timers.role_set",
                lang,
                role=role.mention,
                name=found.name,
                minutes=board_lead_minutes(board),
            )
            # A role nobody may mention only pings if Kisk can mention every role.
            if (
                not role.mentionable
                and not interaction.guild.me.guild_permissions.mention_everyone
            ):
                message += "\n\n" + i18n.t(
                    "eventtypes.ping_unmentionable", lang, role=role.mention
                )
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(
        name="respawn",
        description="Set how long a field boss takes to respawn after a kill (admins)",
    )
    @app_commands.describe(boss="The field boss", hours="Hours after a kill, e.g. 11")
    @app_commands.choices(
        boss=[app_commands.Choice(name=t.name, value=t.key) for t in FIELD_TIMERS]
    )
    async def respawn(
        self,
        interaction: discord.Interaction,
        boss: app_commands.Choice[str],
        hours: app_commands.Range[float, 0.5, 48.0],
    ):
        db = self.bot.db
        lang = await i18n.resolve_lang(db, interaction.guild)
        if await self._refuse_non_admin(interaction, lang):
            return
        await db.set_boss_timer_config(
            interaction.guild_id, boss.value, respawn_minutes=round(hours * 60)
        )
        await interaction.response.send_message(
            i18n.t("timers.respawn_set", lang, name=boss.name, hours=f"{hours:g}"),
            ephemeral=True,
        )

    @app_commands.command(
        name="clear",
        description="Wipe a field boss's timer, e.g. after maintenance (admins)",
    )
    @app_commands.describe(boss="The field boss to reset")
    @app_commands.choices(
        boss=[
            app_commands.Choice(name="All field bosses", value="all"),
            *[app_commands.Choice(name=t.name, value=t.key) for t in FIELD_TIMERS],
        ]
    )
    async def clear(
        self, interaction: discord.Interaction, boss: app_commands.Choice[str]
    ):
        db = self.bot.db
        lang = await i18n.resolve_lang(db, interaction.guild)
        if await self._refuse_non_admin(interaction, lang):
            return
        keys = (
            [timer.key for timer in FIELD_TIMERS]
            if boss.value == "all"
            else [boss.value]
        )
        await db.clear_boss_spawns(interaction.guild_id, keys)
        await interaction.response.send_message(
            i18n.t("timers.cleared", lang, name=boss.name), ephemeral=True
        )
        await self._redraw(interaction.guild)

    @app_commands.command(
        name="schedule",
        description="Change when a clock-based boss or event spawns (admins)",
    )
    @app_commands.describe(
        timer="The abyss boss or event",
        days="“daily”, or weekdays like “mon,thu,sat”",
        times="Server time: “21:30”, “10:30,22:30”, or “01:00/3h” for every 3 hours",
    )
    @app_commands.choices(
        timer=[app_commands.Choice(name=t.name, value=t.key) for t in CLOCK_TIMERS]
    )
    async def schedule(
        self,
        interaction: discord.Interaction,
        timer: app_commands.Choice[str],
        days: app_commands.Range[str, 1, 40],
        times: app_commands.Range[str, 1, 60],
    ):
        db = self.bot.db
        lang = await i18n.resolve_lang(db, interaction.guild)
        if await self._refuse_non_admin(interaction, lang):
            return
        spec = f"{days.replace(' ', '')} {times.replace(' ', '')}".lower()
        try:
            parse_schedule(spec)
        except ValueError:
            await interaction.response.send_message(
                i18n.t("timers.bad_schedule", lang), ephemeral=True
            )
            return
        await db.set_boss_timer_config(interaction.guild_id, timer.value, schedule=spec)
        await interaction.response.send_message(
            i18n.t("timers.schedule_set", lang, name=timer.name, schedule=spec),
            ephemeral=True,
        )
        await self._redraw(interaction.guild)

    @app_commands.command(
        name="settings",
        description="Show or change the boss timers settings (admins)",
    )
    @app_commands.describe(
        timezone="The game server's clock, e.g. America/Los_Angeles",
        ping_minutes="How many minutes before a spawn the role is pinged",
    )
    async def settings(
        self,
        interaction: discord.Interaction,
        timezone: app_commands.Range[str, 1, 60] | None = None,
        ping_minutes: app_commands.Range[int, 1, 60] | None = None,
    ):
        db = self.bot.db
        lang = await i18n.resolve_lang(db, interaction.guild)
        if await self._refuse_non_admin(interaction, lang):
            return
        if timezone is not None:
            try:
                ZoneInfo(timezone.strip())
            except (ZoneInfoNotFoundError, ValueError):
                await interaction.response.send_message(
                    i18n.t("timers.bad_timezone", lang, value=timezone), ephemeral=True
                )
                return
        if timezone is not None or ping_minutes is not None:
            await db.set_timer_settings(
                interaction.guild_id,
                timezone=timezone.strip() if timezone else None,
                lead_minutes=ping_minutes,
            )

        board = await db.get_timer_board(interaction.guild_id)
        rows = await db.get_boss_timers(interaction.guild_id)
        await interaction.response.send_message(
            self._settings_text(board, rows, lang),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if timezone is not None:
            await self._redraw(interaction.guild)

    def _settings_text(self, board, rows: dict, lang: str) -> str:
        tz = board_timezone(board)
        posted = board is not None and board["message_id"]
        where = (
            f"<#{board['channel_id']}>" if posted else i18n.t("timers.no_board", lang)
        )
        lines = [
            i18n.t(
                "timers.settings",
                lang,
                tz=tz.key,
                now=datetime.now(tz).strftime("%a %H:%M"),
                minutes=board_lead_minutes(board),
                board=where,
            )
        ]
        for timer in TIMERS:
            row = rows.get(timer.key)
            if timer.schedule is not None:
                detail = f"`{(row['schedule'] if row else None) or timer.schedule}`"
            else:
                hours = respawn_seconds(timer, row) / 3600
                detail = i18n.t("timers.respawn_label", lang, hours=f"{hours:g}")
            if row is not None and row["role_id"]:
                detail += " · " + i18n.t(
                    "eventtypes.pings", lang, role=f"<@&{row['role_id']}>"
                )
            lines.append(f"**{timer.name}** · {detail}")
        return "\n".join(lines)

    # ----- Background loop: keep boards current, ping, clean up -----

    @tasks.loop(seconds=30)
    @resilient_tick
    async def tick(self):
        now = int(time.time())
        for board in await self.bot.db.timer_boards():
            # One guild's failure must never stall every other server.
            try:
                await self._refresh_board(board, now)
                await self._send_pings(board, now)
            except Exception:
                log.exception("Boss timers failed for guild %s", board["guild_id"])
        await self._sweep_pings(now)

    @tick.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def _channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel

    async def _refresh_board(self, board, now: int) -> None:
        guild = self.bot.get_guild(board["guild_id"])
        if guild is None:
            return
        drawn = self._drawn.get(guild.id)
        embed = await self.board_embed(guild, now)
        if embed.to_dict() == drawn:
            return
        channel = await self._channel(board["channel_id"])
        if channel is None:
            return
        try:
            await channel.get_partial_message(board["message_id"]).edit(embed=embed)
        except discord.NotFound:
            # The board was deleted: forget it rather than retry every tick.
            await self.bot.db.set_timer_board(guild.id, None, None)
        except discord.HTTPException:
            self._drawn.pop(guild.id, None)  # try again on the next tick

    async def _send_pings(self, board, now: int) -> None:
        db = self.bot.db
        guild_id = board["guild_id"]
        rows = await db.get_boss_timers(guild_id)
        lead_s = board_lead_minutes(board) * 60
        by_spawn: dict[int, list] = {}
        for timer, spawn in due_pings(rows, now, board_timezone(board), lead_s):
            # Claim first: a restart or a slow send must never ping twice.
            if await db.claim_boss_ping(guild_id, timer.key, spawn):
                by_spawn.setdefault(spawn, []).append(timer)
        if not by_spawn:
            return
        channel = await self._channel(board["channel_id"])
        if channel is None:
            return
        lang = await i18n.resolve_lang(db, self.bot.get_guild(guild_id))
        for spawn, timers in by_spawn.items():
            roles = dict.fromkeys(rows[timer.key]["role_id"] for timer in timers)
            # A field boss whose time was restarted from an older report is
            # marked, so nobody takes the guess for a confirmed spawn.
            names = [
                f"≈ {timer.name}"
                if timer.respawn_minutes and rows[timer.key]["spawn_at"] != spawn
                else timer.name
                for timer in timers
            ]
            text = i18n.t(
                "timers.ping",
                lang,
                names=", ".join(names),
                when=f"<t:{spawn}:R> (<t:{spawn}:t>)",
            )
            try:
                message = await channel.send(
                    text + "\n" + " ".join(f"<@&{role_id}>" for role_id in roles),
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            except discord.HTTPException:
                continue  # no permission to post here: skip this ping
            # Tracked in the database so the clean-up survives a restart.
            await db.add_timer_ping(
                message.id, channel.id, max(spawn, now + PING_LIFETIME_S)
            )

    async def _sweep_pings(self, now: int) -> None:
        for ping in await self.bot.db.due_timer_pings(now):
            await self._delete_message(ping["channel_id"], ping["message_id"])
            await self.bot.db.remove_timer_ping(ping["message_id"])

    async def _delete_message(self, channel_id: int, message_id: int) -> None:
        channel = await self._channel(channel_id)
        if channel is None:
            return
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass  # already gone, or no permission


async def setup(bot: commands.Bot):
    await bot.add_cog(Timers(bot))
