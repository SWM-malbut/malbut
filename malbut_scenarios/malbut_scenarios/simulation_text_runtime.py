"""Compose text proposals without importing any execution adapter."""

from typing import Callable

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.robot_state_source import (
    StaticSimulationRobotStateSource,
)
from malbut_agent_server.schemas import RobotState
from malbut_agent_server.text_turn import TextTurnService
from malbut_gazebo.named_navigation import NamedNavigationCatalog
from malbut_scenarios.agent_named_target import (
    CatalogNamedTargetResolver,
)


def build_simulation_text_runtime(
    settings: Settings,
    catalog_loader: Callable[[], NamedNavigationCatalog],
):
    """Compose Agent and catalog lookup without any navigation facade."""
    if settings.tool_mode != 'proposal':
        raise ValueError(
            'SWM25-131 text runtime requires proposal Tool mode'
        )
    if not settings.auth_token:
        raise ValueError(
            'SWM25-131 text runtime requires MALBUT_AGENT_AUTH_TOKEN'
        )
    resolver = CatalogNamedTargetResolver(catalog_loader)
    # This is explicit Gazebo evidence only.  A physical RobotStateSource is
    # owned by SWM25-123 and must replace it before real-robot authority.
    state_source = StaticSimulationRobotStateSource(RobotState(
        battery_percent=100.0,
        navigation_available=True,
        localization_ok=True,
        emergency_stop=False,
        camera_available=False,
        privacy_mode=True,
        docked=False,
    ))
    orchestrator = build_orchestrator(
        settings,
        robot_state_source=state_source,
    )
    return orchestrator, TextTurnService(orchestrator, resolver)
