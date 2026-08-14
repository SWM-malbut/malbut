"""Short-horizon target motion estimation with bounded sensor noise."""

from dataclasses import dataclass
import math

from .geometry import Point2D


@dataclass(frozen=True)
class TargetEstimate:
    """Filtered target position and planar velocity."""

    position: Point2D
    velocity: Point2D
    stamp_seconds: float


class TargetMotionEstimator:
    """Filter sensor target positions and estimate bounded velocity."""

    def __init__(
        self,
        position_alpha: float,
        velocity_alpha: float,
        maximum_speed: float,
    ) -> None:
        """Configure filter weights and the plausible person speed bound."""
        if not 0.0 < position_alpha <= 1.0:
            raise ValueError('position_alpha must be in (0, 1]')
        if not 0.0 < velocity_alpha <= 1.0:
            raise ValueError('velocity_alpha must be in (0, 1]')
        if maximum_speed <= 0.0:
            raise ValueError('maximum_speed must be positive')
        self._position_alpha = position_alpha
        self._velocity_alpha = velocity_alpha
        self._maximum_speed = maximum_speed
        self._estimate: TargetEstimate | None = None

    @property
    def estimate(self) -> TargetEstimate | None:
        """Return the latest filtered estimate."""
        return self._estimate

    def reset(self) -> None:
        """Forget all observations from the previous target action."""
        self._estimate = None

    def update(
        self,
        position: Point2D,
        stamp_seconds: float,
    ) -> TargetEstimate:
        """Add one map-frame target observation."""
        previous = self._estimate
        if previous is None:
            self._estimate = TargetEstimate(
                position,
                Point2D(0.0, 0.0),
                stamp_seconds,
            )
            return self._estimate
        if stamp_seconds <= previous.stamp_seconds:
            return previous

        alpha = self._position_alpha
        filtered = Point2D(
            alpha * position.x + (1.0 - alpha) * previous.position.x,
            alpha * position.y + (1.0 - alpha) * previous.position.y,
        )
        elapsed = stamp_seconds - previous.stamp_seconds
        raw_velocity = Point2D(
            (filtered.x - previous.position.x) / elapsed,
            (filtered.y - previous.position.y) / elapsed,
        )
        speed = math.hypot(raw_velocity.x, raw_velocity.y)
        if speed > self._maximum_speed:
            scale = self._maximum_speed / speed
            raw_velocity = Point2D(
                raw_velocity.x * scale,
                raw_velocity.y * scale,
            )
        velocity_alpha = self._velocity_alpha
        velocity = Point2D(
            velocity_alpha * raw_velocity.x
            + (1.0 - velocity_alpha) * previous.velocity.x,
            velocity_alpha * raw_velocity.y
            + (1.0 - velocity_alpha) * previous.velocity.y,
        )
        self._estimate = TargetEstimate(filtered, velocity, stamp_seconds)
        return self._estimate

    def predict(
        self,
        now_seconds: float,
        maximum_horizon: float,
    ) -> Point2D | None:
        """Predict only a short distance beyond the last sensor sample."""
        if self._estimate is None:
            return None
        horizon = min(
            max(0.0, now_seconds - self._estimate.stamp_seconds),
            max(0.0, maximum_horizon),
        )
        return Point2D(
            self._estimate.position.x + self._estimate.velocity.x * horizon,
            self._estimate.position.y + self._estimate.velocity.y * horizon,
        )
