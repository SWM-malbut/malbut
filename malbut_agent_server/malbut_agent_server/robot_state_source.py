"""Server-owned robot-state evidence used after model inference."""

import math
import time
from dataclasses import dataclass
from typing import Protocol

from malbut_agent_server.schemas import RobotState


@dataclass(frozen=True)
class RobotStateEvidence:
    """One bounded snapshot whose trust is decided outside the model."""

    state: RobotState
    observed_at: float
    evidence_id: str
    trusted: bool

    def __post_init__(self) -> None:
        """Reject malformed provenance before Safety can consume it."""
        if not isinstance(self.state, RobotState):
            raise TypeError('robot state evidence requires RobotState')
        if (
            isinstance(self.observed_at, bool)
            or not isinstance(self.observed_at, (int, float))
            or not math.isfinite(float(self.observed_at))
            or self.observed_at < 0
        ):
            raise ValueError('robot state observed_at is invalid')
        if (
            not isinstance(self.evidence_id, str)
            or not self.evidence_id
            or len(self.evidence_id) > 128
            or any(ord(character) < 32 for character in self.evidence_id)
        ):
            raise ValueError('robot state evidence_id is invalid')
        if not isinstance(self.trusted, bool):
            raise TypeError('robot state trusted must be a boolean')


class RobotStateSource(Protocol):
    """Read one server-owned snapshot without accepting model authority."""

    def read(self) -> RobotStateEvidence:
        """Return the latest independently collected state evidence."""


class StaticSimulationRobotStateSource:
    """Explicit test-only source for the SWM25-131 no-motion harness."""

    def __init__(
        self,
        state: RobotState,
        *,
        evidence_id: str = 'swm25-131-static-simulation-state',
        clock=time.time,
    ) -> None:
        """Keep a fixed simulation state behind a server-owned adapter."""
        if not isinstance(state, RobotState):
            raise TypeError('simulation source requires RobotState')
        self._state = state
        self._evidence_id = evidence_id
        self._clock = clock

    def read(self) -> RobotStateEvidence:
        """Return a fresh sample each time the server asks for one."""
        return RobotStateEvidence(
            state=self._state,
            observed_at=float(self._clock()),
            evidence_id=self._evidence_id,
            trusted=True,
        )
