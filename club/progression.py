"""Derived player progression. Attendance and completed sessions remain authoritative."""

from dataclasses import dataclass

from .models import Attendance, OpenPlay, Registration


@dataclass(frozen=True)
class Streaks:
    current: int
    best: int


@dataclass(frozen=True)
class Badge:
    name: str
    description: str


@dataclass(frozen=True)
class RewardProgress:
    pending_completion: bool
    immediate_remaining: int | None
    percent: int | None


def get_reward_progress(summary):
    """Show immediate ledger progress while keeping issuance tied to completion."""
    milestone = summary.next_milestone
    if milestone is None:
        return RewardProgress(False, None, None)
    remaining = max(milestone.threshold - summary.openplays_attended, 0)
    return RewardProgress(
        pending_completion=remaining == 0,
        immediate_remaining=remaining,
        percent=min(100, summary.openplays_attended * 100 // milestone.threshold),
    )


def get_streaks(player):
    """Count completed attended sessions; an active registration without attendance breaks a run.

    Unregistered sessions, cancelled registrations, and non-completed sessions are neutral.
    Ordering by start and id makes backdated/same-day sessions deterministic.
    """
    attended = Attendance.objects.filter(
        player=player, status=Attendance.Status.VALID,
        openplay__status=OpenPlay.Status.COMPLETED,
    ).values_list("openplay_id", "openplay__starts_at")
    registered = Registration.objects.filter(
        player=player, status=Registration.Status.ACTIVE,
        openplay__status=OpenPlay.Status.COMPLETED,
    ).values_list("openplay_id", "openplay__starts_at")
    events = {session_id: [starts_at, False] for session_id, starts_at in registered}
    for session_id, starts_at in attended:
        events[session_id] = [starts_at, True]
    current = best = 0
    for session_id in sorted(events, key=lambda pk: (events[pk][0], pk)):
        current = current + 1 if events[session_id][1] else 0
        best = max(best, current)
    return Streaks(current=current, best=best)


def get_badges(*, completed_attendances, streaks):
    """V1 badges are derived; correction can remove current qualification."""
    badges = []
    if completed_attendances >= 1:
        badges.append(Badge("First Rally", "First completed OpenPlay attended"))
    if streaks.best >= 4:
        badges.append(Badge("On a Roll", "Four consecutive OpenPlays attended"))
    return tuple(badges)
