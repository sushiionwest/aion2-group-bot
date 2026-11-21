"""Tests for the pure check-in helpers. Run: pytest"""

from bot.logic import COMPO_OPEN, COMPO_STANDARD, assign
from bot.utils.rsvp import (
    autofill_swaps,
    checkin_deadline,
    checkin_tally,
    ranking_priorities,
)


def _signup(user_id, role="dps", priority=0):
    return {"user_id": user_id, "role": role, "priority": priority}


def _apply(compo, size, signups, ranking):
    """The party and waitlist once `ranking` is stored as priorities."""
    priorities = ranking_priorities(ranking)
    stored = [{**s, "priority": priorities[s["user_id"]]} for s in signups]
    party, waitlist = assign(compo, size, stored)
    return [s["user_id"] for s in party], [s["user_id"] for s in waitlist]


# ----- tally -----


def test_tally_partitions_the_party():
    confirmed, declined, awaiting, ready = checkin_tally(
        [1, 2, 3, 4], [], {1: "yes", 2: "no"}
    )
    assert confirmed == [1]
    assert declined == [2]
    assert awaiting == [3, 4]
    assert ready == []


def test_tally_lists_waitlisted_yes_as_ready_not_as_coming():
    confirmed, _declined, awaiting, ready = checkin_tally(
        [1, 2], [8, 9], {1: "yes", 9: "yes"}
    )
    assert confirmed == [1]
    assert awaiting == [2]
    assert ready == [9]


def test_tally_keeps_a_decline_from_someone_no_longer_signed_up():
    # Declining frees the seat, so the member is in neither list any more.
    _confirmed, declined, _awaiting, _ready = checkin_tally([1], [], {7: "no"})
    assert declined == [7]


def test_tally_ignores_a_yes_from_someone_not_signed_up():
    confirmed, declined, awaiting, ready = checkin_tally([1], [], {99: "yes"})
    assert (confirmed, declined, awaiting, ready) == ([], [], [1], [])


# ----- deadline -----


def test_deadline_is_a_set_time_before_the_start():
    assert checkin_deadline(10_000, posted_at=1_000, deadline_minutes=15) == 9_100


def test_late_checkin_still_leaves_time_to_answer():
    # Posted 12 minutes before the start: the usual deadline (15 minutes
    # before) has already gone, so members get the minimum window instead.
    start = 10_000
    posted = start - 12 * 60
    assert checkin_deadline(start, posted, 15, min_window_s=600) == posted + 600


def test_no_deadline_when_the_window_would_pass_the_start():
    start = 10_000
    assert checkin_deadline(start, start - 5 * 60, 15, min_window_s=600) is None


def test_no_deadline_when_switched_off_or_unscheduled():
    assert checkin_deadline(10_000, 1_000, 0) is None
    assert checkin_deadline(None, 1_000, 15) is None


# ----- seat hand-over -----


def test_silent_seat_goes_to_a_waitlisted_player_who_said_yes():
    signups = [_signup(u) for u in (1, 2, 3, 4)]
    swaps, ranking = autofill_swaps(COMPO_OPEN, 3, signups, {1: "yes", 4: "yes"})
    # 2 and 3 are silent; the later sign-up (3) gives way first.
    assert swaps == [(3, 4)]
    party, waitlist = _apply(COMPO_OPEN, 3, signups, ranking)
    assert party == [1, 2, 4]
    assert waitlist == [3]


def test_a_silent_player_is_never_replaced_by_another_silent_one():
    signups = [_signup(u) for u in (1, 2, 3)]
    swaps, ranking = autofill_swaps(COMPO_OPEN, 2, signups, {})
    assert swaps == []
    assert ranking == [1, 2, 3]


def test_confirmed_players_keep_their_seat():
    signups = [_signup(u) for u in (1, 2, 3)]
    swaps, _ranking = autofill_swaps(
        COMPO_OPEN, 2, signups, {1: "yes", 2: "yes", 3: "yes"}
    )
    assert swaps == []


def test_standard_party_only_swaps_within_the_same_role():
    signups = [
        _signup(1, "tank"),
        _signup(2, "heal"),
        _signup(3, "dps"),
        _signup(4, "dps"),
        _signup(5, "dps"),
        _signup(6, "tank"),  # waitlisted, ready
        _signup(7, "dps"),  # waitlisted, silent
    ]
    # Every DPS is silent but the only ready player is a tank: only the silent
    # tank can give way.
    swaps, ranking = autofill_swaps(COMPO_STANDARD, 5, signups, {6: "yes"})
    assert swaps == [(1, 6)]
    party, waitlist = _apply(COMPO_STANDARD, 5, signups, ranking)
    assert party == [6, 2, 3, 4, 5]
    assert waitlist == [1, 7]


def test_standard_party_keeps_a_confirmed_tank_when_a_tank_is_ready():
    signups = [_signup(1, "tank"), _signup(2, "dps"), _signup(3, "tank")]
    swaps, _ranking = autofill_swaps(COMPO_STANDARD, 5, signups, {1: "yes", 3: "yes"})
    assert swaps == []


def test_ready_players_are_served_in_waitlist_order():
    signups = [_signup(u) for u in (1, 2, 3, 4, 5)]
    # One silent seat (2), two ready players: the first in line gets it.
    swaps, ranking = autofill_swaps(
        COMPO_OPEN, 2, signups, {1: "yes", 4: "yes", 5: "yes"}
    )
    assert swaps == [(2, 4)]
    party, waitlist = _apply(COMPO_OPEN, 2, signups, ranking)
    assert party == [1, 4]
    # 2 stands where 4 was; the silent 3 ahead of them did not move.
    assert waitlist == [3, 2, 5]


def test_several_seats_can_move_in_one_pass():
    signups = [_signup(u) for u in (1, 2, 3, 4, 5)]
    swaps, ranking = autofill_swaps(COMPO_OPEN, 3, signups, {4: "yes", 5: "yes"})
    assert swaps == [(3, 4), (2, 5)]
    party, waitlist = _apply(COMPO_OPEN, 3, signups, ranking)
    assert party == [1, 5, 4]
    assert waitlist == [3, 2]


def test_an_admin_boosted_order_is_respected():
    # 3 was moved to the front by an admin, so 2 is the last one in the party.
    signups = [_signup(1), _signup(2), _signup(3, priority=5), _signup(4)]
    swaps, ranking = autofill_swaps(COMPO_OPEN, 3, signups, {4: "yes"})
    assert swaps == [(2, 4)]
    assert ranking == [3, 1, 4, 2]


def test_a_no_from_someone_still_in_the_party_gives_way_before_silence():
    signups = [_signup(u) for u in (1, 2, 3, 4)]
    swaps, _ranking = autofill_swaps(COMPO_OPEN, 3, signups, {1: "no", 4: "yes"})
    assert swaps == [(1, 4)]


def test_ranking_priorities_are_dense_and_descending():
    assert ranking_priorities([7, 8, 9]) == {7: 3, 8: 2, 9: 1}
