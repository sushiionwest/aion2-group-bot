"""Storage for each server's own list of event types (the panel dropdown).

Admins edit the list with /eventtypes; each type can carry a role to ping when
a group is created, an emoji, and a staff-only flag. A server that has never
edited its list starts from DEFAULT_EVENT_TYPES.
"""

# Discord caps both a select menu and an autocomplete list at 25 entries.
MAX_EVENT_TYPES = 25

# The list a server starts with: (name, emoji, staff_only).
DEFAULT_EVENT_TYPES = (
    ("Krao Cave Hard", "🏰", False),
    ("Draupnir Hard", "🏰", False),
    ("Uruguru Hard", "🏰", False),
    ("Vakron Hard", "🏰", False),
    ("Shattered Arkanis Stage 4", "🏰", False),
    ("Dues Research Stage 4", "🏰", False),
    ("Raid", "🐉", True),
)

EVENT_TYPES_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_types (
    guild_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL COLLATE NOCASE,
    position   INTEGER NOT NULL,                  -- display order in menus
    role_id    INTEGER,                           -- role pinged on creation
    emoji      TEXT,
    staff_only INTEGER NOT NULL DEFAULT 0,        -- moderators only
    PRIMARY KEY (guild_id, name)
);
"""


class EventTypesMixin:
    """Per-guild event types. Mixed into Database (uses self.conn)."""

    async def _seed_event_types(self, guild_id: int) -> None:
        for position, (name, emoji, staff_only) in enumerate(DEFAULT_EVENT_TYPES):
            await self.conn.execute(
                """INSERT OR IGNORE INTO event_types
                   (guild_id, name, position, emoji, staff_only)
                   VALUES (?, ?, ?, ?, ?)""",
                (guild_id, name, position, emoji, int(staff_only)),
            )
        await self.conn.commit()

    async def get_event_types(self, guild_id: int):
        """The guild's event types in display order, seeded on first use."""
        query = "SELECT * FROM event_types WHERE guild_id = ? ORDER BY position"
        async with self.conn.execute(query, (guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            await self._seed_event_types(guild_id)
            async with self.conn.execute(query, (guild_id,)) as cur:
                rows = await cur.fetchall()
        return rows

    async def get_event_type(self, guild_id: int, name: str):
        """One type by name (case-insensitive), or None."""
        await self.get_event_types(guild_id)  # make sure the defaults exist
        async with self.conn.execute(
            "SELECT * FROM event_types WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        ) as cur:
            return await cur.fetchone()

    async def upsert_event_type(
        self,
        guild_id: int,
        name: str,
        *,
        role_id: int | None = None,
        clear_role: bool = False,
        emoji: str | None = None,
        staff_only: bool | None = None,
    ) -> str:
        """Adds a type at the end of the list, or edits an existing one.

        On an edit, only the fields given change. Returns "added", "updated"
        or "full" (the list already holds MAX_EVENT_TYPES).
        """
        existing = await self.get_event_type(guild_id, name)
        if existing is None:
            types = await self.get_event_types(guild_id)
            if len(types) >= MAX_EVENT_TYPES:
                return "full"
            position = max((row["position"] for row in types), default=-1) + 1
            await self.conn.execute(
                """INSERT INTO event_types
                   (guild_id, name, position, role_id, emoji, staff_only)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, name, position, role_id, emoji, int(bool(staff_only))),
            )
            await self.conn.commit()
            return "added"

        new_role = None if clear_role else (role_id or existing["role_id"])
        await self.conn.execute(
            """UPDATE event_types SET role_id = ?, emoji = ?, staff_only = ?
               WHERE guild_id = ? AND name = ?""",
            (
                new_role,
                emoji or existing["emoji"],
                existing["staff_only"] if staff_only is None else int(staff_only),
                guild_id,
                name,
            ),
        )
        await self.conn.commit()
        return "updated"

    async def remove_event_type(self, guild_id: int, name: str) -> str:
        """Removes a type. Returns "removed", "missing" or "last" (the list
        can't be emptied, or it would refill with the defaults)."""
        types = await self.get_event_types(guild_id)
        if not any(row["name"].casefold() == name.casefold() for row in types):
            return "missing"
        if len(types) == 1:
            return "last"
        await self.conn.execute(
            "DELETE FROM event_types WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self.conn.commit()
        return "removed"
