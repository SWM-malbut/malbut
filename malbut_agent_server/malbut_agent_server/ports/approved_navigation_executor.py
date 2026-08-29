"""Port for one approved, non-physical named-navigation attempt."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Protocol


class ApprovedNavigationError(RuntimeError):
    """Base error carrying whether an external effect is known."""

    def __init__(self, code: str, *, outcome_known: bool) -> None:
        """Create a bounded typed error for the application worker."""
        if not isinstance(code, str) or not code:
            raise ValueError('navigation error code is required')
        self.code = code
        self.outcome_known = outcome_known
        super().__init__(code)


class ApprovedNavigationRejected(ApprovedNavigationError):
    """A definite failure occurred before an external start took effect."""

    def __init__(self, code: str) -> None:
        """Create a failure known not to require unknown reconciliation."""
        super().__init__(code, outcome_known=True)


class ApprovedNavigationOutcomeUnknown(ApprovedNavigationError):
    """A command may have taken effect and must never be resent."""

    def __init__(self, operation: str, cause_code: str) -> None:
        """Create an ambiguous command result without retaining payloads."""
        if not isinstance(operation, str) or not operation:
            raise ValueError('unknown outcome operation is required')
        if not isinstance(cause_code, str) or not cause_code:
            raise ValueError('unknown outcome cause_code is required')
        self.operation = operation
        self.cause_code = cause_code
        super().__init__('outcome_unknown', outcome_known=False)


@dataclass(frozen=True)
class ApprovedNavigationStatus:
    """Bounded status for one opaque execution handle."""

    state: str
    terminal: bool
    result_code: str | None = None
    progress_ratio: float | None = None
    simulation: bool = True
    physical_authorized: bool = False

    def __post_init__(self) -> None:
        """Reject unbounded state or authority-bearing status values."""
        if (
            not isinstance(self.state, str)
            or not self.state
            or len(self.state) > 64
        ):
            raise ValueError('approved navigation state is invalid')
        if not isinstance(self.terminal, bool):
            raise TypeError('terminal must be a boolean')
        if self.result_code is not None and (
            not isinstance(self.result_code, str)
            or not self.result_code
            or len(self.result_code) > 128
        ):
            raise ValueError('navigation result_code is invalid')
        if self.progress_ratio is not None:
            if (
                isinstance(self.progress_ratio, bool)
                or not isinstance(self.progress_ratio, (int, float))
                or not math.isfinite(float(self.progress_ratio))
                or not 0.0 <= float(self.progress_ratio) <= 1.0
            ):
                raise ValueError('navigation progress_ratio is invalid')
        if self.simulation is not True:
            raise ValueError('SWM25-132 status must be simulation-only')
        if self.physical_authorized is not False:
            raise ValueError('physical navigation authority is forbidden')


class ApprovedNavigationExecutorPort(Protocol):
    """Prepare, start once, and observe one named-navigation operation."""

    def prepare(
        self,
        location: str,
        expected_target_binding_digest: str,
    ) -> Any:
        """Return an opaque preview without causing external motion."""

    def start(self, prepared: Any, *, committed_intent_id: str) -> Any:
        """Attempt the committed operation exactly once."""

    def status(self, handle: Any) -> ApprovedNavigationStatus:
        """Read one bounded status for the opaque handle."""

    def release(self, prepared: Any) -> None:
        """Forget process-local preview/session state without external I/O."""


__all__ = [
    'ApprovedNavigationError',
    'ApprovedNavigationExecutorPort',
    'ApprovedNavigationOutcomeUnknown',
    'ApprovedNavigationRejected',
    'ApprovedNavigationStatus',
]
