"""Read-only odometry gate for generic camera-motion events."""

from dataclasses import dataclass
import math
from typing import Optional


@dataclass
class MotionGate:
    """Allow generic motion only after fresh odometry reports a stable stop."""

    stationary_after_sec: float = 1.0
    odom_timeout_sec: float = 2.0
    linear_threshold: float = 0.03
    angular_threshold: float = 0.05
    last_odom_at: Optional[float] = None
    stationary_since: Optional[float] = None

    def update(self, linear_speed: float, angular_speed: float, now: float) -> None:
        """Record a read-only odometry sample."""
        self.last_odom_at = now
        if not math.isfinite(linear_speed) or not math.isfinite(angular_speed):
            self.stationary_since = None
            return
        moving = (
            abs(linear_speed) > self.linear_threshold
            or abs(angular_speed) > self.angular_threshold
        )
        if moving:
            self.stationary_since = None
        elif self.stationary_since is None:
            self.stationary_since = now

    def generic_motion_allowed(self, now: float) -> bool:
        """Return false for absent, stale, moving, or not-yet-stable odometry."""
        if self.last_odom_at is None or self.stationary_since is None:
            return False
        if now - self.last_odom_at > self.odom_timeout_sec:
            return False
        return now - self.stationary_since >= self.stationary_after_sec
