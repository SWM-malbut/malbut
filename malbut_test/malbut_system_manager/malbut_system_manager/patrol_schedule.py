"""Calendar slots and durable at-most-once admission for daily patrols."""

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo('Asia/Seoul')
HOURS = (10, 16)
MAX_DELAY = timedelta(minutes=30)


def _local(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError('patrol schedule requires a timezone-aware clock')
    return now.astimezone(TIMEZONE)


def _next_slot(now: datetime) -> datetime:
    for hour in HOURS:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate >= now:
            return candidate
    return (now + timedelta(days=1)).replace(
        hour=HOURS[0], minute=0, second=0, microsecond=0)


class DailyPatrolSchedule:
    """Skip offline slots and claim each due slot before dispatching motion."""

    def __init__(self, path: str, now: datetime) -> None:
        now = _local(now)
        if path != ':memory:':
            target = Path(path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            path = str(target)
        self._db = sqlite3.connect(path, timeout=1.0, check_same_thread=False)
        with self._db:
            self._db.execute('CREATE TABLE IF NOT EXISTS patrol_schedule ('
                             'singleton INTEGER PRIMARY KEY CHECK(singleton = 1), '
                             'last_claimed REAL NOT NULL)')
            self._db.execute('INSERT OR IGNORE INTO patrol_schedule VALUES (1, 0)')
        self._last_tick = now
        self._next = _next_slot(now)
        self._pending = None

    def due(self, now: datetime) -> datetime | None:
        """Return a live slot, without replaying slots missed while offline."""
        now = _local(now)
        if now < self._last_tick:
            # NTP/time corrections must not retain an old pending deadline.
            self._pending = None
            self._next = _next_slot(now)
        self._last_tick = now
        if now >= self._next:
            # Select only the latest crossed slot, even after a long timer gap.
            candidates = [now.replace(hour=h, minute=0, second=0, microsecond=0)
                          for h in HOURS]
            self._pending = max((slot for slot in candidates if slot <= now),
                                default=(now - timedelta(days=1)).replace(
                                    hour=HOURS[-1], minute=0, second=0, microsecond=0))
            self._next = _next_slot(now + timedelta(microseconds=1))
        if self._pending is not None and now >= self._pending + MAX_DELAY:
            self._pending = None
        return self._pending

    def claim(self, now: datetime) -> datetime | None:
        """Commit a slot before sending; uncertain sends are never retried."""
        slot = self.due(now)
        if slot is None:
            return None
        with self._db:
            claimed = self._db.execute(
                'UPDATE patrol_schedule SET last_claimed = ? '
                'WHERE singleton = 1 AND last_claimed < ?',
                (slot.timestamp(), slot.timestamp()),
            ).rowcount == 1
        self._pending = None
        return slot if claimed else None

    def close(self) -> None:
        """Release the schedule receipt database."""
        self._db.close()
