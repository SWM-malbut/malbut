"""Time-based metrics for one person-tracking simulation lap."""

from dataclasses import dataclass


@dataclass
class TrackingDurationMetrics:
    """Accumulate tracking and recovery durations without pass thresholds."""

    started_s: float | None = None
    last_sample_s: float | None = None
    tracking_duration_s: float = 0.0
    recovery_duration_s: float = 0.0
    longest_continuous_tracking_s: float = 0.0
    current_continuous_tracking_s: float = 0.0
    reacquisition_count: int = 0
    _tracking_active: bool = False
    _recovery_active: bool = False
    _tracking_was_lost: bool = False

    def start(self, now_s: float) -> None:
        """Start a lap when the person is first visibly tracked."""
        self.started_s = now_s
        self.last_sample_s = now_s
        self._tracking_active = True
        self._recovery_active = False

    def sample(
        self,
        now_s: float,
        tracking_active: bool,
        recovery_active: bool,
    ) -> None:
        """Integrate the previous state until ``now_s`` and set a new state."""
        if self.started_s is None or self.last_sample_s is None:
            raise RuntimeError('tracking metrics must be started first')
        if now_s < self.last_sample_s:
            raise ValueError('sample time must be monotonic')
        elapsed_s = now_s - self.last_sample_s
        if self._tracking_active:
            self.tracking_duration_s += elapsed_s
            self.current_continuous_tracking_s += elapsed_s
            self.longest_continuous_tracking_s = max(
                self.longest_continuous_tracking_s,
                self.current_continuous_tracking_s,
            )
        else:
            self.current_continuous_tracking_s = 0.0
        if self._recovery_active:
            self.recovery_duration_s += elapsed_s
        if self._tracking_active and not tracking_active:
            self._tracking_was_lost = True
        elif (
            not self._tracking_active
            and tracking_active
            and self._tracking_was_lost
        ):
            self.reacquisition_count += 1
        self._tracking_active = tracking_active
        self._recovery_active = recovery_active
        self.last_sample_s = now_s

    def elapsed(self, now_s: float) -> float:
        """Return lap time elapsed since the first visible observation."""
        if self.started_s is None:
            return 0.0
        return max(0.0, now_s - self.started_s)

    def report(self, now_s: float) -> dict[str, float | int]:
        """Return stable numeric values for logs and JSON output."""
        elapsed_s = self.elapsed(now_s)
        ratio = (
            self.tracking_duration_s / elapsed_s
            if elapsed_s > 0.0
            else 0.0
        )
        return {
            'elapsed_s': elapsed_s,
            'tracking_duration_s': self.tracking_duration_s,
            'tracking_ratio': ratio,
            'longest_continuous_tracking_s': (
                self.longest_continuous_tracking_s
            ),
            'recovery_duration_s': self.recovery_duration_s,
            'reacquisition_count': self.reacquisition_count,
        }
