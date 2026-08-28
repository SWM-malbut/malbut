"""Run the SWM25-131 text-confirmation server with simulation authority."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable, Optional, Sequence

from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.http_server import make_server
from malbut_agent_server.robot_state_source import (
    StaticSimulationRobotStateSource,
)
from malbut_agent_server.schemas import RobotState
from malbut_agent_server.text_turn import TextTurnService
from malbut_gazebo.named_navigation import NamedNavigationCatalog
from malbut_gazebo.named_navigation_facade import ActiveMapCatalogSource
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Run authenticated text proposal and confirmation only; '
            'no Robot Web, ROS, or Nav2 command is sent.'
        ),
    )
    parser.add_argument('--env-file', default='.env.local')
    parser.add_argument(
        '--map-store',
        help=(
            'Active SWM25-130 map store; defaults to '
            'MALBUT_NAMED_NAVIGATION_MAP_STORE.'
        ),
    )
    parser.add_argument(
        '--device-id',
        help=(
            'Server-owned simulation device; defaults to '
            'MALBUT_ROBOT_DEVICE_ID.'
        ),
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='Validate composition and target binding, then exit.',
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start one loopback server whose confirmation is non-actuating."""
    args = _parser().parse_args(argv)
    load_env_file(Path(args.env_file).expanduser())
    settings = Settings.from_env(os.environ)
    settings.validate_for_server()
    if settings.database_path == ':memory:':
        raise ValueError(
            'SWM25-131 live runtime requires a durable database path'
        )
    map_store = (
        args.map_store
        or os.environ.get('MALBUT_NAMED_NAVIGATION_MAP_STORE', '')
    )
    device_id = (
        args.device_id
        or os.environ.get('MALBUT_ROBOT_DEVICE_ID', '')
    )
    if not map_store:
        raise ValueError(
            'MALBUT_NAMED_NAVIGATION_MAP_STORE is required'
        )
    if not device_id:
        raise ValueError('MALBUT_ROBOT_DEVICE_ID is required')
    source = ActiveMapCatalogSource(
        Path(map_store),
        device_id,
    )
    # Fail before binding HTTP if the exact MVP target is unavailable.
    source.load().resolve('거실')
    orchestrator, text_turn_service = build_simulation_text_runtime(
        settings,
        source.load,
    )
    if args.check:
        orchestrator.conversation_store.close()
        orchestrator.memory_store.close()
        print(
            'text confirmation composition: ok '
            '(simulation=true, physical_authorized=false, nav2=off)'
        )
        return 0

    server = make_server(
        settings.host,
        settings.port,
        orchestrator,
        max_request_bytes=settings.max_request_bytes,
        auth_token=settings.auth_token,
        allowed_user_id=settings.user_id,
        max_concurrent_requests=settings.max_concurrent_requests,
        requests_per_minute=settings.requests_per_minute,
        socket_timeout_seconds=settings.socket_timeout_seconds,
        text_turn_service=text_turn_service,
    )
    print(
        'Malbut text confirmation listening on '
        f'http://{settings.host}:{settings.port} '
        '(simulation=true, physical_authorized=false, nav2=off)'
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        orchestrator.conversation_store.close()
        orchestrator.memory_store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
