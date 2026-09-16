"""Health state for a loaded detector whose inference can fail at runtime."""

from dataclasses import dataclass


@dataclass
class InferenceHealth:
    """Track recent inference success without treating a loaded model as healthy."""

    model_available: bool
    active: bool
    now: float
    stale_after_sec: float = 10.0
    failure_limit: int = 3

    def __post_init__(self) -> None:
        if self.stale_after_sec <= 0.0:
            raise ValueError("stale_after_sec must be positive")
        if self.failure_limit < 1:
            raise ValueError("failure_limit must be positive")
        self._active_since = self.now
        self._last_success = None
        self._consecutive_failures = 0

    def set_active(self, active: bool, now: float) -> None:
        """Start a fresh grace interval when inference becomes active."""
        if active and not self.active:
            self._active_since = now
            self._last_success = None
            self._consecutive_failures = 0
        self.active = active

    def record_success(self, now: float) -> None:
        """Record a completed inference, including an empty detection result."""
        self._last_success = now
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """Record an inference exception."""
        self._consecutive_failures += 1

    def healthy(self, now: float) -> bool:
        """Return model readiness while idle and runtime health while active."""
        if not self.model_available:
            return False
        if not self.active:
            return True
        if self._consecutive_failures >= self.failure_limit:
            return False
        most_recent = (
            self._last_success
            if self._last_success is not None
            else self._active_since
        )
        return now - most_recent <= self.stale_after_sec
