"""Exercise calendar time, busy deferral, restart and duplicate prevention."""

from datetime import datetime, timezone

import pytest

from malbut_system_manager.patrol_schedule import DailyPatrolSchedule, TIMEZONE


def at(hour, minute=0, *, day=2, second=0):
    """Use deterministic Korean wall time without changing the system clock."""
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TIMEZONE)


@pytest.fixture
def schedule(tmp_path):
    """Close every test connection, including restart and concurrent instances."""
    instances = []

    def create(now):
        result = DailyPatrolSchedule(str(tmp_path / 'schedule.sqlite3'), now)
        instances.append(result)
        return result

    yield create
    for item in instances:
        item.close()


def test_default_hours_use_korean_time_even_with_utc_clock(schedule):
    """Run once at 10:00 and once at 16:00, not at the host timezone's hours."""
    clock = schedule(at(9, 59).astimezone(timezone.utc))
    assert clock.claim(at(9, 59)) is None
    assert clock.claim(at(10).astimezone(timezone.utc)) == at(10)
    assert clock.claim(at(10, second=5)) is None
    assert clock.claim(at(15, 59)) is None
    assert clock.claim(at(16)) == at(16)
    assert clock.claim(at(16, second=5)) is None
    assert clock.claim(at(10, day=3)) == at(10, day=3)


def test_busy_slot_can_start_only_within_thirty_minutes(schedule):
    """Polling while blocked retains one slot and never extends its deadline."""
    clock = schedule(at(9, 59))
    assert clock.due(at(10)) == at(10)
    assert clock.due(at(10, 20)) == at(10)
    assert clock.claim(at(10, 29, second=59)) == at(10)
    assert clock.due(at(16)) == at(16)
    assert clock.claim(at(16, 30)) is None
    assert clock.claim(at(17)) is None


@pytest.mark.parametrize('boot', [at(10, 1), at(10, 29), at(17)])
def test_boot_does_not_catch_up_offline_slots(schedule, boot):
    """Even a reboot inside the grace period must not cause surprise motion."""
    clock = schedule(boot)
    assert clock.claim(boot) is None
    assert clock.claim(at(10, day=3)) == at(10, day=3)


def test_restart_and_clock_rollback_cannot_replay_a_claimed_slot(schedule):
    """Persist before dispatch so even an unknown send result cannot repeat."""
    first = schedule(at(9, 59))
    assert first.claim(at(10)) == at(10)
    restarted = schedule(at(9, 59))
    assert restarted.claim(at(10)) is None
    assert restarted.claim(at(16)) == at(16)
    assert restarted.claim(at(9)) is None
    assert restarted.claim(at(10)) is None
    assert restarted.claim(at(16)) is None
    assert restarted.claim(at(10, day=3)) == at(10, day=3)


def test_two_managers_sharing_receipts_cannot_claim_the_same_slot(schedule):
    """Arbitrate duplicate launch attempts using one atomic database update."""
    first, second = schedule(at(9, 59)), schedule(at(9, 59))
    assert first.due(at(10)) == second.due(at(10)) == at(10)
    assert first.claim(at(10)) == at(10)
    assert second.claim(at(10)) is None


def test_long_gap_keeps_only_the_latest_unexpired_slot(schedule):
    """A delayed timer never queues a day's worth of missed patrols."""
    clock = schedule(at(9))
    assert clock.claim(at(16, 5)) == at(16)
    assert clock.claim(at(16, 5)) is None
    assert clock.claim(at(18, day=3)) is None


def test_reject_naive_clock_before_opening_database(tmp_path):
    """Require an explicit timezone so host settings cannot shift patrols."""
    with pytest.raises(ValueError, match='timezone-aware'):
        DailyPatrolSchedule(str(tmp_path / 'unused.db'), datetime(2026, 11, 2))
