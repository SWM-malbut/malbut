"""Read-only odometry gate for generic camera-motion events."""

from dataclasses import dataclass
import math
from typing import Optional


@dataclass
class MotionGate:
    """Allow generic motion only after navigation and odometry are stable."""

    stationary_after_sec: float = 2.0
    odom_timeout_sec: float = 2.0
    linear_threshold: float = 0.03
    angular_threshold: float = 0.05
    last_odom_at: Optional[float] = None
    stationary_since: Optional[float] = None
    navigation_active: bool = False
    last_linear_speed: Optional[float] = None
    last_angular_speed: Optional[float] = None

    def set_navigation_active(self, active: bool) -> bool:
        """Apply Nav2 state and require a new stable period after every run."""
        changed = self.navigation_active != active
        self.navigation_active = active
        if active or changed:
            self.stationary_since = None
        return changed

    def update(self, linear_speed: float, angular_speed: float, now: float) -> None:
        """Record a read-only odometry sample."""
        self.last_odom_at = now
        self.last_linear_speed = linear_speed
        self.last_angular_speed = angular_speed
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
        if self.navigation_active:
            return False
        if self.last_odom_at is None or self.stationary_since is None:
            return False
        if now - self.last_odom_at > self.odom_timeout_sec:
            return False
        return now - self.stationary_since >= self.stationary_after_sec

    def pose_motion_state(self, now: float) -> str:
        """Read-only context for pose candidates; does not disable Pose inference."""
        values = (self.last_odom_at, self.last_linear_speed, self.last_angular_speed)
        if (not math.isfinite(now)
                or any(value is None or not math.isfinite(value) for value in values)
                or not 0 <= now - self.last_odom_at <= self.odom_timeout_sec):
            return "unknown"
        if (abs(self.last_linear_speed) > self.linear_threshold
                or abs(self.last_angular_speed) > self.angular_threshold):
            return "moving"
        return "stationary" if self.generic_motion_allowed(now) else "unknown"
