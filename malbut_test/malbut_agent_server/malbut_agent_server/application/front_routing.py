"""Application service for one fail-closed Front Router attempt."""

from __future__ import annotations

from malbut_agent_server.domain.front_route import (
    FrontRouteMatch,
    FrontRouteRequest,
)
from malbut_agent_server.ports.front_router import FrontRouterPort


class FrontRoutingError(RuntimeError):
    """Bounded public failure that does not expose adapter exceptions."""

    def __init__(self, code: str) -> None:
        """Expose only one bounded error code to the application caller."""
        self.code = code
        super().__init__(code)


class FrontRoutingService:
    """Try one fast route without retrying or invoking a fallback."""

    def __init__(self, front_router: FrontRouterPort) -> None:
        """Bind the only dependency allowed at this boundary."""
        if not callable(getattr(front_router, 'try_route', None)):
            raise TypeError('front_router must provide try_route()')
        self._front_router = front_router

    def try_route(
        self,
        request: FrontRouteRequest,
    ) -> FrontRouteMatch | None:
        """Return one match or an internal abstention after one call."""
        if type(request) is not FrontRouteRequest:
            raise TypeError('request must be a FrontRouteRequest')
        try:
            result = self._front_router.try_route(request)
        except Exception:
            raise FrontRoutingError('front_router_failed') from None
        if result is None:
            return None
        if type(result) is not FrontRouteMatch:
            raise FrontRoutingError('front_router_result_invalid')
        return result
