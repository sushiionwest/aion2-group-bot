"""Party composition logic, with no Discord dependency.

The idea: we never store who is "in the party" vs "waitlisted". We only
store the sign-ups (role + join order) and recompute the split on every
render with `assign()`. First come, first served; switching roles sends
you to the back of the queue.
"""

ROLES = ("tank", "heal", "dps")

# Standard composition per 5 players: 1 tank / 1 heal / 3 DPS.
STANDARD_RATIO = {"tank": 1, "heal": 1, "dps": 3}
STANDARD_SIZE = sum(STANDARD_RATIO.values())  # 5

# How many characters one member may register. Discord caps a dropdown at 25
# options; well under that keeps the character menus short enough to be useful.
MAX_CHARACTERS = 10

COMPO_STANDARD = "standard"
# Support-heavy party of 10: 2 tanks / 4 support / 4 DPS. The support seats
# are the "heal" role under another name (clerics and chanters alike), so the
# stored sign-ups, profiles and buttons stay the same three roles.
COMPO_SUPPORT = "support"
SUPPORT_RATIO = {"tank": 2, "heal": 4, "dps": 4}
# Stored value for the no-role-limits mode. Any value without per-role seats
# below behaves as open, which keeps old rows created as "libre" working.
COMPO_OPEN = "open"

# Compositions with per-role seats: stored value -> seats for one unit of the
# party. A party twice the unit's size has twice the seats.
ROLE_RATIOS = {COMPO_STANDARD: STANDARD_RATIO, COMPO_SUPPORT: SUPPORT_RATIO}


def has_role_seats(compo: str) -> bool:
    """Whether the composition limits seats per role (anything else is open)."""
    return compo in ROLE_RATIOS


def role_slots(compo: str, size: int) -> dict | None:
    """Per-role seats for a party, or None when the composition is open.

    Standard: 5 -> 1/1/3, 10 (raid/BG) -> 2/2/6. Support: 10 -> 2/4/4.
    """
    ratio = ROLE_RATIOS.get(compo)
    if ratio is None:
        return None
    groups = max(1, size // sum(ratio.values()))
    return {role: n * groups for role, n in ratio.items()}


def standard_slots(size: int) -> dict:
    """Per-role slots in standard mode: 5 -> 1/1/3, 10 (raid/BG) -> 2/2/6."""
    return role_slots(COMPO_STANDARD, size)


MOVE_UP = "up"
MOVE_DOWN = "down"


def reorder_priorities(ordered_ids: list, user_id, direction: str) -> dict:
    """Swap a sign-up with its neighbour and return the new priority map.

    `ordered_ids`: the sign-ups' user ids in their current ranking (highest
    first). Returns `{user_id: priority}` with dense, strictly descending
    priorities so the swapped ranking is unambiguous. Moving the top entry up
    or the bottom entry down leaves the order untouched.
    """
    ranking = list(ordered_ids)
    index = ranking.index(user_id)
    neighbour = index - 1 if direction == MOVE_UP else index + 1
    if 0 <= neighbour < len(ranking):
        ranking[index], ranking[neighbour] = ranking[neighbour], ranking[index]
    return {uid: len(ranking) - position for position, uid in enumerate(ranking)}


def signup_priority(signup) -> int:
    """Manual ordering override for a sign-up, defaulting to 0.

    Admins can boost a waitlisted player so they jump the queue. Rows from
    before the column existed (and plain dicts without the key) rank as 0,
    which reproduces the first-come-first-served behaviour.
    """
    try:
        value = signup["priority"]
    except (KeyError, IndexError):
        return 0
    return value if value is not None else 0


def assign(compo: str, size: int, signups: list) -> tuple[list, list]:
    """Splits the sign-ups between the party and the waitlist.

    `signups`: list of dicts (or SQLite rows) with at least a "role" key,
    sorted by join order. A higher "priority" floats a sign-up towards the
    front (admin reordering); equal priority keeps join order. Returns
    (party, waitlist).
    """
    # Stable sort keeps join order within an equal priority.
    signups = sorted(signups, key=signup_priority, reverse=True)

    party: list = []
    waitlist: list = []

    slots = role_slots(compo, size)
    if slots is not None:
        taken = {role: 0 for role in ROLES}
        for s in signups:
            role = s["role"]
            if taken[role] < slots[role]:
                taken[role] += 1
                party.append(s)
            else:
                waitlist.append(s)
    else:  # open composition: only the total size matters
        for s in signups:
            if len(party) < size:
                party.append(s)
            else:
                waitlist.append(s)

    return party, waitlist


def missing_slots(compo: str, size: int, signups: list) -> dict:
    """Open seats per role that still need filling, for display.

    Compositions with per-role seats only: returns `{role: remaining}` for each
    role not yet full (filled roles are omitted). Open mode has no per-role
    slots, so it returns an empty dict; the party's own count already shows how
    many seats are left.
    """
    slots = role_slots(compo, size)
    if slots is None:
        return {}
    party, _waitlist = assign(compo, size, signups)
    filled = {role: 0 for role in ROLES}
    for s in party:
        filled[s["role"]] += 1
    return {
        role: slots[role] - filled[role]
        for role in ROLES
        if slots[role] - filled[role] > 0
    }


def split_groups(members: list, groups: int, group_size: int) -> list:
    """Split a flat roster into `groups` display groups of `group_size` each.

    Members fill the groups in order; the last group may be partial and empty
    groups are still returned, so a multi-group (siege) event always shows every
    group.
    """
    return [members[i * group_size : (i + 1) * group_size] for i in range(groups)]
