"""Port for one bounded, non-authorizing Front Router decision."""

from __future__ import annotations

from typing import Protocol

from malbut_agent_server.domain.front_route import (
    FrontRouteMatch,
    FrontRouteRequest,
)


class FrontRouterPort(Protocol):
    """Select a high-confidence route or abstain without side effects."""

    def try_route(
        self,
        request: FrontRouteRequest,
    ) -> FrontRouteMatch | None:
        """Return one route, or ``None`` for server-owned abstention."""
        ...
