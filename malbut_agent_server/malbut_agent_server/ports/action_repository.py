"""Repository port for exactly-once-attempt action dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from malbut_agent_server.domain.robot_action import (
    ActionState,
    DispatchAuthorization,
    RobotAction,
)


@dataclass(frozen=True)
class ActionClaim:
    """One leased action claim; the raw token is never persisted."""

    action: RobotAction
    worker_id: str
    claim_token: str = field(repr=False)
    fence: int
    lease_expires_at: float


@dataclass(frozen=True)
class DispatchIntent:
    """Durable proof that an external call may now be attempted once."""

    action: RobotAction
    intent_id: str
    worker_id: str
    claim_token: str = field(repr=False)
    fence: int


class ActionRepositoryPort(Protocol):
    """Persistence contract used by a simulation action worker."""

    def get(self, action_id: str) -> Optional[RobotAction]:
        """Return the latest action snapshot, if it exists."""

    def find_by_confirmation(
        self,
        confirmation_request_id: str,
    ) -> Optional[RobotAction]:
        """Return the only action bound to a confirmation."""

    def claim_next(
        self,
        worker_id: str,
        *,
        now: float,
        lease_for: float,
    ) -> Optional[ActionClaim]:
        """Claim one pending or safely reclaimable preflight action."""

    def record_dispatch_intent(
        self,
        claim: ActionClaim,
        authorization: DispatchAuthorization,
        *,
        now: float,
    ) -> DispatchIntent:
        """Commit intent and state atomically before external I/O."""

    def block(
        self,
        claim: ActionClaim,
        *,
        result_code: str,
        now: float,
    ) -> RobotAction:
        """Terminally block a definite pre-dispatch validation failure."""

    def mark_started(
        self,
        intent: DispatchIntent,
        *,
        now: float,
    ) -> DispatchIntent:
        """Record a known accepted external operation."""

    def finish(
        self,
        intent: DispatchIntent,
        state: ActionState,
        *,
        result_code: str,
        now: float,
    ) -> RobotAction:
        """Record one known terminal result."""

    def recover_uncertain_after_restart(self, *, now: float) -> int:
        """Move sent or started work to UNKNOWN without resending it."""
