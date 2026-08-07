"""Sensor-agnostic moving-target interest logic."""

from collections import deque
from dataclasses import dataclass
import math

from malbut_roaming.geometry import Point2D, distance


@dataclass(frozen=True)
class TargetObservation:
    """A map-frame target observation and its local receipt time."""

    point: Point2D
    time_seconds: float


class TargetInterest:
    """Detect useful target motion without depending on simulator truth."""

    def __init__(
        self,
        timeout_seconds: float,
        minimum_speed: float,
        history_size: int = 5,
    ) -> None:
        """Configure staleness and motion thresholds."""
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise ValueError('timeout_seconds must be positive')
        if not math.isfinite(minimum_speed) or minimum_speed < 0.0:
            raise ValueError('minimum_speed must be non-negative')
        if history_size < 2:
            raise ValueError('history_size must be at least two')
        self.timeout_seconds = timeout_seconds
        self.minimum_speed = minimum_speed
        self._observations = deque(maxlen=history_size)

    def observe(self, point: Point2D, now_seconds: float) -> None:
        """Record one externally localized target observation."""
        if not all(math.isfinite(value) for value in (
            point.x,
            point.y,
            now_seconds,
        )):
            raise ValueError('target observation must be finite')
        if (
            self._observations
            and now_seconds <= self._observations[-1].time_seconds
        ):
            return
        while (
            self._observations
            and now_seconds - self._observations[0].time_seconds
            > self.timeout_seconds
        ):
            self._observations.popleft()
        self._observations.append(TargetObservation(point, now_seconds))

    def latest(self, now_seconds: float) -> Point2D | None:
        """Return the latest point while it remains fresh."""
        if not math.isfinite(now_seconds):
            raise ValueError('now_seconds must be finite')
        if not self._observations:
            return None
        latest = self._observations[-1]
        if now_seconds - latest.time_seconds > self.timeout_seconds:
            return None
        return latest.point

    def speed(self, now_seconds: float) -> float:
        """Estimate speed over the available fresh observation history."""
        latest = self.latest(now_seconds)
        fresh = [
            observation
            for observation in self._observations
            if now_seconds - observation.time_seconds <= self.timeout_seconds
        ]
        if latest is None or len(fresh) < 2:
            return 0.0
        first = fresh[0]
        last = fresh[-1]
        elapsed = last.time_seconds - first.time_seconds
        if elapsed <= 0.0:
            return 0.0
        return distance(first.point, last.point) / elapsed

    def is_moving(self, now_seconds: float) -> bool:
        """Return whether a fresh target exceeds the configured speed."""
        return self.speed(now_seconds) >= self.minimum_speed
