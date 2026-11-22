"""Pure helpers for the check-in ("are you coming?"), with no Discord dependency.

The check-in asks everyone signed up, party and waitlist, whether they are
coming. Two things follow from the answers:

* a "no" frees the seat at once (handled where the button is pressed);
* after a deadline, a party seat whose holder never answered goes to a
  waitlisted member who said yes. That exchange is worked out here.

A silent player is never replaced by another silent one: the swap only happens
when it puts a confirmed player in the seat.
"""

from ..logic import assign, has_role_seats, signup_priority

YES = "yes"
NO = "no"

# A check-in posted late (with `/rsvp`, or for an event created at the last
# minute) still leaves members this long to answer before seats can move.
MIN_ANSWER_WINDOW_S = 10 * 60


def checkin_tally(
    party_ids: list[int], waitlist_ids: list[int], responses: dict[int, str]
) -> tuple[list[int], list[int], list[int], list[int]]:
    """Splits the answers into (confirmed, declined, awaiting, ready).

    `confirmed` and `awaiting` are party members, in party order. `ready` are
    waitlisted members who said yes, in waitlist order. `declined` is everyone
    who said no: declining gives the seat up, so those members are usually no
    longer signed up at all.
    """
    confirmed = [u for u in party_ids if responses.get(u) == YES]
    awaiting = [u for u in party_ids if u not in responses]
    ready = [u for u in waitlist_ids if responses.get(u) == YES]
    declined = [u for u, status in responses.items() if status == NO]
    return confirmed, declined, awaiting, ready


def checkin_deadline(
    starts_at: int | None,
    posted_at: float,
    deadline_minutes: int,
    min_window_s: int = MIN_ANSWER_WINDOW_S,
) -> int | None:
    """When unanswered seats may start going to the waitlist, or None.

    Normally `deadline_minutes` before the start. A check-in posted late pushes
    it back so members always get `min_window_s` to answer. None means no seat
    ever moves on its own: the feature is off (0 minutes), the event has no
    start time, or the answer window would run past the start.
    """
    if not starts_at or deadline_minutes <= 0:
        return None
    deadline = max(starts_at - deadline_minutes * 60, int(posted_at) + min_window_s)
    return deadline if deadline < starts_at else None


def autofill_swaps(
    compo: str, size: int, signups: list, responses: dict[int, str]
) -> tuple[list[tuple[int, int]], list[int]]:
    """Gives unanswered party seats to waitlisted members who said yes.

    `signups` is every sign-up (join order); `responses` maps a user id to
    'yes' or 'no'. Returns `(swaps, ranking)`: `swaps` is a list of
    `(out_user_id, in_user_id)` and `ranking` is every user id in the new
    queue order, ready to be stored as priorities.

    Each waitlisted "yes", in waitlist order, takes the seat of a party member
    who has not said yes; where the composition has per-role seats only one
    with the same role, so the party keeps its tanks and healers. The two trade
    places in the queue, so nobody else moves and the player who lost the seat
    stands where the newcomer was. Among several unanswered seats the most
    recent sign-up gives way first.
    """
    ranked = sorted(signups, key=signup_priority, reverse=True)
    ranking = [s["user_id"] for s in ranked]
    party, waitlist = assign(compo, size, ranked)

    # Most recent sign-up first: they give way before earlier ones. A member
    # who said no but is still signed up (an answer from before declining freed
    # the seat) gives way before anyone who is merely silent.
    replaceable = [s for s in reversed(party) if responses.get(s["user_id"]) != YES]
    replaceable.sort(key=lambda s: responses.get(s["user_id"]) != NO)
    ready = [s for s in waitlist if responses.get(s["user_id"]) == YES]

    same_role_only = has_role_seats(compo)
    swaps: list[tuple[int, int]] = []
    for newcomer in ready:
        for seat in replaceable:
            if not same_role_only or seat["role"] == newcomer["role"]:
                break
        else:
            continue  # no unanswered seat this player can fill
        replaceable.remove(seat)
        out_id, in_id = seat["user_id"], newcomer["user_id"]
        a, b = ranking.index(out_id), ranking.index(in_id)
        ranking[a], ranking[b] = ranking[b], ranking[a]
        swaps.append((out_id, in_id))
    return swaps, ranking


def ranking_priorities(ranking: list[int]) -> dict[int, int]:
    """Dense, strictly descending priorities that store `ranking` as is."""
    return {uid: len(ranking) - position for position, uid in enumerate(ranking)}
