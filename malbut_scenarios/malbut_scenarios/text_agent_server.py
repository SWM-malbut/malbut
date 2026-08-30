"""Run text confirmation, optionally dispatching approved Gazebo motion."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from ipaddress import ip_address
import os
from pathlib import Path
import secrets
from typing import Callable, Optional, Sequence
from urllib.parse import urlsplit

from malbut_agent_server.adapters.outbound import SQLiteActionRepository
from malbut_agent_server.application.approved_action_worker import (
    ApprovedActionWorker,
    ApprovedActionWorkerRuntime,
)
from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.http_server import make_server
from malbut_agent_server.robot_state_source import (
    StaticSimulationRobotStateSource,
)
from malbut_agent_server.schemas import RobotState
from malbut_agent_server.text_turn import TextTurnService
from malbut_gazebo.named_navigation import NamedNavigationCatalog
from malbut_gazebo.named_navigation_facade import (
    ActiveMapCatalogSource,
    NamedNavigationFacade,
    SimulationNavigationAuthority,
)
from malbut_gazebo.robot_web_navigation_client import (
    RobotWebNavigationClient,
)
from malbut_scenarios.agent_named_target import (
    CatalogNamedTargetResolver,
)
from malbut_scenarios.approved_named_navigation_executor import (
    ApprovedNamedNavigationExecutor,
    RobotWebSimulationStateSource,
)
from malbut_scenarios.concurrent_approval_resolver import (
    ConcurrentApprovalResolverGate,
    concurrent_approval_observation_path,
)
from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboFaultProfile,
    TextGazeboScenarioProfile,
    coerce_fault_profile,
    scenario_spec,
)
from malbut_scenarios.worker_competition import (
    CompetingApprovedActionWorker,
    CoordinatedActionRepository,
    WorkerCompetitionCoordinator,
)


DEFAULT_ROBOT_WEB_URL = 'http://127.0.0.1:8765'
SIMULATION_BATTERY_ASSUMPTION_PERCENT = 100.0
DISPATCH_WINDOW_SECONDS = 30.0
WORKER_LEASE_SECONDS = 240.0
STATUS_DEADLINE_SECONDS = 120.0
DISPATCHER_JOIN_TIMEOUT_SECONDS = 240.0


@dataclass(frozen=True)
class ApprovedSimulationTextRuntime:
    """Dependencies owned by the explicitly actuating simulation mode."""

    orchestrator: object
    text_turn_service: TextTurnService
    action_repository: SQLiteActionRepository
    dispatcher: ApprovedActionWorkerRuntime
    additional_action_repositories: tuple[
        SQLiteActionRepository, ...
    ] = ()
    additional_dispatchers: tuple[ApprovedActionWorkerRuntime, ...] = ()
    worker_competition: WorkerCompetitionCoordinator | None = None
    concurrent_approval_gate: ConcurrentApprovalResolverGate | None = None

    @property
    def action_repositories(self) -> tuple[SQLiteActionRepository, ...]:
        """Return every repository in deterministic ownership order."""
        return (
            self.action_repository,
            *self.additional_action_repositories,
        )

    @property
    def dispatchers(self) -> tuple[ApprovedActionWorkerRuntime, ...]:
        """Return every worker runtime in deterministic ownership order."""
        return (self.dispatcher, *self.additional_dispatchers)


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


def build_approved_simulation_text_runtime(
    settings: Settings,
    catalog_loader: Callable[[], NamedNavigationCatalog],
    *,
    robot_web_url: str,
    scenario_profile: TextGazeboScenarioProfile | str = (
        TextGazeboScenarioProfile.HAPPY_PATH
    ),
    fault_profile: TextGazeboFaultProfile | str = (
        TextGazeboFaultProfile.NONE
    ),
) -> ApprovedSimulationTextRuntime:
    """
    Compose the explicit SWM25-132 simulation execution boundary.

    Construction performs local SQLite initialization only.  Robot Web is
    contacted later for the proposal-time read and the independently fresh
    post-approval preflight; construction itself sends no HTTP request.
    """
    if settings.database_path == ':memory:':
        raise ValueError(
            'SWM25-132 execution requires a durable database path'
        )
    if settings.tool_mode != 'proposal':
        raise ValueError(
            'SWM25-132 execution requires proposal Tool mode'
        )
    if not settings.auth_token:
        raise ValueError(
            'SWM25-132 execution requires MALBUT_AGENT_AUTH_TOKEN'
        )
    if _loopback_port(robot_web_url) == settings.port:
        raise ValueError(
            'Agent HTTP port conflicts with the Robot Web port'
        )

    scenario = scenario_spec(scenario_profile)
    fault = coerce_fault_profile(fault_profile)
    catalog = catalog_loader()
    # Pin the one MVP destination before constructing SQLite or HTTP owners.
    catalog.resolve(scenario.location)
    resolver = CatalogNamedTargetResolver(catalog_loader)
    concurrent_approval_gate = None
    if fault is TextGazeboFaultProfile.CONCURRENT_APPROVAL:
        concurrent_approval_gate = ConcurrentApprovalResolverGate(
            resolver,
            observation_path=concurrent_approval_observation_path(
                settings.database_path
            ),
        )
        resolver = concurrent_approval_gate
    client = RobotWebNavigationClient(robot_web_url)
    state_source = RobotWebSimulationStateSource(
        client,
        expected_device_id=catalog.device_id,
        expected_map_id=catalog.map_id,
        expected_map_revision=catalog.map_revision,
        assumed_battery_percent=(
            SIMULATION_BATTERY_ASSUMPTION_PERCENT
        ),
    )
    facade = NamedNavigationFacade(
        catalog_loader,
        client,
        authority=(
            SimulationNavigationAuthority.explicit_test_authority()
        ),
    )
    executor = ApprovedNamedNavigationExecutor(facade)

    orchestrator = build_orchestrator(
        settings,
        robot_state_source=state_source,
    )
    action_repositories: list[SQLiteActionRepository] = []
    competition = None
    try:
        # Construct this before HTTP starts accepting approvals.  This makes
        # the action schema available to the confirmation/action transaction.
        action_repository = SQLiteActionRepository(settings.database_path)
        action_repositories.append(action_repository)
        if fault is TextGazeboFaultProfile.COMPETING_WORKERS:
            # A separate SQLite connection is essential: sharing one
            # repository object would test only its in-process RLock, not
            # the durable BEGIN IMMEDIATE/CAS boundary used across workers.
            action_repositories.append(
                SQLiteActionRepository(settings.database_path)
            )
            competition = WorkerCompetitionCoordinator(
                settings.database_path
            )
        text_turn_service = TextTurnService(
            orchestrator,
            resolver,
            create_robot_actions=True,
            action_dispatch_window_seconds=DISPATCH_WINDOW_SECONDS,
        )
        dispatchers = []
        for contender, repository in enumerate(action_repositories):
            worker_repository = repository
            worker_prefix = 'swm25-132-'
            if competition is not None:
                worker_repository = CoordinatedActionRepository(
                    repository,
                    competition,
                    contender=contender,
                )
                worker_prefix = 'swm25-136-'
            worker_arguments = {
                'worker_id': worker_prefix + secrets.token_hex(16),
                'lease_for_seconds': WORKER_LEASE_SECONDS,
                'status_deadline_seconds': STATUS_DEADLINE_SECONDS,
                'status_poll_interval_seconds': 0.25,
            }
            if competition is None:
                worker = ApprovedActionWorker(
                    worker_repository,
                    executor,
                    state_source,
                    orchestrator.safety_policy,
                    resolver,
                    **worker_arguments,
                )
            else:
                worker = CompetingApprovedActionWorker(
                    worker_repository,
                    executor,
                    state_source,
                    orchestrator.safety_policy,
                    resolver,
                    competition_coordinator=competition,
                    contender=contender,
                    **worker_arguments,
                )
            dispatchers.append(ApprovedActionWorkerRuntime(worker))
        return ApprovedSimulationTextRuntime(
            orchestrator=orchestrator,
            text_turn_service=text_turn_service,
            action_repository=action_repository,
            dispatcher=dispatchers[0],
            additional_action_repositories=tuple(
                action_repositories[1:]
            ),
            additional_dispatchers=tuple(dispatchers[1:]),
            worker_competition=competition,
            concurrent_approval_gate=concurrent_approval_gate,
        )
    except Exception:
        if competition is not None:
            competition.close()
        for repository in action_repositories:
            repository.close()
        orchestrator.conversation_store.close()
        orchestrator.memory_store.close()
        raise


def _loopback_port(robot_web_url: str) -> int:
    """Return the port of one strict literal loopback HTTP origin."""
    try:
        parsed = urlsplit(robot_web_url)
        if (
            parsed.scheme != 'http'
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {'', '/'}
            or parsed.query
            or parsed.fragment
            or '%' in parsed.hostname
            or not ip_address(parsed.hostname).is_loopback
        ):
            raise ValueError
        port = 80 if parsed.port is None else parsed.port
        if port == 0:
            raise ValueError
        return port
    except (TypeError, ValueError) as error:
        raise ValueError('Robot Web URL is invalid') from error


def _close_execution_runtime(
    runtime: ApprovedSimulationTextRuntime,
    server: object | None,
) -> None:
    """Drain request and worker threads before closing their SQLite stores."""
    first_error = None
    if runtime.worker_competition is not None:
        try:
            runtime.worker_competition.close()
        except Exception as error:
            first_error = error
    if runtime.concurrent_approval_gate is not None:
        try:
            runtime.concurrent_approval_gate.close()
        except Exception as error:
            if first_error is None:
                first_error = error
    for dispatcher in runtime.dispatchers:
        try:
            dispatcher.close()
        except Exception as error:
            if first_error is None:
                first_error = error
    if server is not None:
        try:
            server.server_close()
        except Exception as error:
            if first_error is None:
                first_error = error
    joined_all = True
    for dispatcher in runtime.dispatchers:
        try:
            joined = dispatcher.join(
                timeout=DISPATCHER_JOIN_TIMEOUT_SECONDS
            )
        except Exception as error:
            if first_error is None:
                first_error = error
            joined = False
        joined_all = joined_all and joined
    if not joined_all:
        # The worker may still own these connections.  Closing them beneath
        # it would turn an orderly shutdown into a persistence race.
        raise RuntimeError('approved action dispatcher did not stop')
    closes = [
        repository.close
        for repository in runtime.action_repositories
    ]
    closes.extend((
        runtime.orchestrator.conversation_store.close,
        runtime.orchestrator.memory_store.close,
    ))
    for close in closes:
        try:
            close()
        except Exception as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Run authenticated text proposal and confirmation.  Gazebo '
            'movement remains off unless explicitly enabled.'
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
        '--scenario-profile',
        choices=tuple(profile.value for profile in TextGazeboScenarioProfile),
        default=TextGazeboScenarioProfile.HAPPY_PATH.value,
        help='Allowlisted text/navigation scenario (default: happy_path).',
    )
    parser.add_argument(
        '--fault-profile',
        choices=tuple(profile.value for profile in TextGazeboFaultProfile),
        default=TextGazeboFaultProfile.NONE.value,
        help='Allowlisted exactly-once pressure (default: none).',
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='Validate composition and target binding, then exit.',
    )
    parser.add_argument(
        '--execute-approved-simulation',
        action='store_true',
        help=(
            'Explicitly permit approved actions to start one Gazebo-only '
            'Robot Web navigation; physical authority remains false.'
        ),
    )
    parser.add_argument(
        '--robot-web-url',
        help=(
            'Loopback Robot Web origin; defaults to MALBUT_ROBOT_WEB_URL '
            f'or {DEFAULT_ROBOT_WEB_URL}.'
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start one loopback server whose confirmation is non-actuating."""
    args = _parser().parse_args(argv)
    scenario = scenario_spec(args.scenario_profile)
    fault = coerce_fault_profile(args.fault_profile)
    if (
        fault is not TextGazeboFaultProfile.NONE
        and not args.execute_approved_simulation
    ):
        raise ValueError(
            'fault profile requires approved simulation execution'
        )
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
    source.load().resolve(scenario.location)
    runtime = None
    robot_web_url = None
    if args.execute_approved_simulation:
        robot_web_url = (
            args.robot_web_url
            or os.environ.get('MALBUT_ROBOT_WEB_URL', '')
            or DEFAULT_ROBOT_WEB_URL
        )
        if _loopback_port(robot_web_url) == settings.port:
            raise ValueError(
                'Agent HTTP port conflicts with the Robot Web port'
            )
    if args.check:
        # Construction is non-actuating: it performs no Robot Web request or
        # Nav2 command.  When execution is requested, validate that complete
        # composition (including schema and worker dependencies), then close
        # every owner without starting its dispatcher.
        if args.execute_approved_simulation:
            checked_runtime = build_approved_simulation_text_runtime(
                settings,
                source.load,
                robot_web_url=str(robot_web_url),
                scenario_profile=scenario.profile,
                fault_profile=fault,
            )
            _close_execution_runtime(checked_runtime, None)
            checked_mode = 'approved-execution'
        else:
            checked_orchestrator, _text_turn_service = (
                build_simulation_text_runtime(settings, source.load)
            )
            checked_orchestrator.conversation_store.close()
            checked_orchestrator.memory_store.close()
            checked_mode = 'baseline'
        print(
            'text confirmation composition: ok '
            '(mode=' + checked_mode + ', simulation=true, '
            'physical_authorized=false, nav2=off)'
        )
        return 0

    if args.execute_approved_simulation:
        runtime = build_approved_simulation_text_runtime(
            settings,
            source.load,
            robot_web_url=str(robot_web_url),
            scenario_profile=scenario.profile,
            fault_profile=fault,
        )
        orchestrator = runtime.orchestrator
        text_turn_service = runtime.text_turn_service
    else:
        orchestrator, text_turn_service = build_simulation_text_runtime(
            settings,
            source.load,
        )

    server = None
    try:
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
        if runtime is not None:
            # HTTP handlers must drain before the stores they use are closed.
            server.daemon_threads = False
            server.block_on_close = True
            for dispatcher in runtime.dispatchers:
                dispatcher.start()
        print(
            'Malbut text confirmation listening on '
            f'http://{settings.host}:{settings.port} '
            '(simulation=true, physical_authorized=false, '
            f"nav2={'approved-only' if runtime is not None else 'off'})"
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if runtime is None:
            if server is not None:
                server.server_close()
            orchestrator.conversation_store.close()
            orchestrator.memory_store.close()
        else:
            _close_execution_runtime(runtime, server)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
