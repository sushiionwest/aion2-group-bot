"""Storage for the boss timers board (/timers).

One board per server, a row per timer the server has touched (a ping role, a
custom respawn time or schedule, a reported spawn), and the short-lived ping
messages the bot still has to delete.
"""

TIMERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS timer_boards (
    guild_id     INTEGER PRIMARY KEY,
    channel_id   INTEGER,
    message_id   INTEGER,                  -- the board; edited in place
    timezone     TEXT,                     -- game server clock (IANA name)
    lead_minutes INTEGER                   -- how long before a spawn to ping
);

CREATE TABLE IF NOT EXISTS boss_timers (
    guild_id        INTEGER NOT NULL,
    key             TEXT    NOT NULL,      -- see utils.boss_timers.TIMERS
    role_id         INTEGER,               -- role pinged before a spawn
    respawn_minutes INTEGER,               -- field boss: custom respawn time
    schedule        TEXT,                  -- clock timer: custom schedule
    spawn_at        INTEGER,               -- field boss: reported next spawn
    set_by          INTEGER,               -- member who reported it
    pinged_for      INTEGER,               -- spawn time the last ping was for
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS timer_pings (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    delete_at  INTEGER NOT NULL
);
"""

_CONFIG_COLUMNS = ("role_id", "respawn_minutes", "schedule")


class TimersMixin:
    """Boss timers. Mixed into Database (uses self.conn)."""

    # ----- The board and its settings -----

    async def get_timer_board(self, guild_id: int):
        async with self.conn.execute(
            "SELECT * FROM timer_boards WHERE guild_id = ?", (guild_id,)
        ) as cur:
            return await cur.fetchone()

    async def set_timer_board(
        self, guild_id: int, channel_id: int | None, message_id: int | None
    ) -> None:
        await self.conn.execute(
            """INSERT INTO timer_boards (guild_id, channel_id, message_id)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   channel_id = excluded.channel_id,
                   message_id = excluded.message_id""",
            (guild_id, channel_id, message_id),
        )
        await self.conn.commit()

    async def set_timer_settings(
        self,
        guild_id: int,
        *,
        timezone: str | None = None,
        lead_minutes: int | None = None,
    ) -> None:
        """Changes the settings given; the others keep their value."""
        await self.conn.execute(
            "INSERT OR IGNORE INTO timer_boards (guild_id) VALUES (?)", (guild_id,)
        )
        await self.conn.execute(
            """UPDATE timer_boards SET
                   timezone = COALESCE(?, timezone),
                   lead_minutes = COALESCE(?, lead_minutes)
               WHERE guild_id = ?""",
            (timezone, lead_minutes, guild_id),
        )
        await self.conn.commit()

    async def timer_boards(self):
        """Every posted board (for the refresh and ping loop)."""
        async with self.conn.execute(
            "SELECT * FROM timer_boards WHERE message_id IS NOT NULL"
        ) as cur:
            return await cur.fetchall()

    # ----- Timers -----

    async def get_boss_timers(self, guild_id: int) -> dict:
        """The server's stored timer rows, by key. Untouched timers have none."""
        async with self.conn.execute(
            "SELECT * FROM boss_timers WHERE guild_id = ?", (guild_id,)
        ) as cur:
            return {row["key"]: row for row in await cur.fetchall()}

    async def _ensure_boss_timer(self, guild_id: int, key: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO boss_timers (guild_id, key) VALUES (?, ?)",
            (guild_id, key),
        )

    async def set_boss_spawn(
        self, guild_id: int, key: str, spawn_at: int, user_id: int
    ) -> None:
        """Records a member's report of when a field boss spawns next."""
        await self._ensure_boss_timer(guild_id, key)
        await self.conn.execute(
            "UPDATE boss_timers SET spawn_at = ?, set_by = ? "
            "WHERE guild_id = ? AND key = ?",
            (spawn_at, user_id, guild_id, key),
        )
        await self.conn.commit()

    async def set_boss_timer_config(self, guild_id: int, key: str, **changes) -> None:
        """Sets role_id, respawn_minutes and/or schedule (None clears one)."""
        unknown = set(changes) - set(_CONFIG_COLUMNS)
        if unknown:
            raise ValueError(f"not a timer setting: {sorted(unknown)}")
        await self._ensure_boss_timer(guild_id, key)
        for column, value in changes.items():
            await self.conn.execute(
                f"UPDATE boss_timers SET {column} = ? WHERE guild_id = ? AND key = ?",
                (value, guild_id, key),
            )
        await self.conn.commit()

    async def claim_boss_ping(self, guild_id: int, key: str, spawn_at: int) -> bool:
        """Atomically claims the ping for one spawn. False if already sent, so
        a restart or an overlapping tick never pings twice for the same spawn."""
        cur = await self.conn.execute(
            """INSERT INTO boss_timers (guild_id, key, pinged_for) VALUES (?, ?, ?)
               ON CONFLICT(guild_id, key) DO UPDATE SET
                   pinged_for = excluded.pinged_for
               WHERE boss_timers.pinged_for IS NOT excluded.pinged_for""",
            (guild_id, key, spawn_at),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # ----- Ping messages awaiting deletion -----

    async def add_timer_ping(
        self, message_id: int, channel_id: int, delete_at: int
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO timer_pings (message_id, channel_id, delete_at) "
            "VALUES (?, ?, ?)",
            (message_id, channel_id, delete_at),
        )
        await self.conn.commit()

    async def due_timer_pings(self, now_ts: int):
        async with self.conn.execute(
            "SELECT * FROM timer_pings WHERE delete_at <= ?", (now_ts,)
        ) as cur:
            return await cur.fetchall()

    async def remove_timer_ping(self, message_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM timer_pings WHERE message_id = ?", (message_id,)
        )
        await self.conn.commit()
