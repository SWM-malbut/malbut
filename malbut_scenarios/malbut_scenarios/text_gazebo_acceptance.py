"""Run one installed text-to-Gazebo acceptance flow with exact evidence."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Mapping, Optional, Sequence

from malbut_scenarios.counting_robot_web_proxy import (
    CountingRobotWebProxy,
    RobotWebProxyCounts,
    request_body_digest,
)
from malbut_scenarios.nav2_goal_status_observer import (
    Nav2GoalStatusEvidence,
    Nav2GoalStatusObserver,
)
from malbut_scenarios.owned_process import (
    OwnedProcess,
    OwnedProcessError,
    ProcessCleanupEvidence,
)
from malbut_scenarios.source_install_attestation import (
    SourceInstallAttestation,
    SourceInstallAttestationError,
    attest_source_install,
)
from malbut_scenarios.concurrent_approval_resolver import (
    ConcurrentApprovalGateError,
    ConcurrentApprovalGateObservation,
    concurrent_approval_observation_path,
    read_concurrent_approval_observation,
)
from malbut_scenarios.dispatch_safety_fault import (
    DispatchSafetyFaultError,
    dispatch_safety_observation_path,
    read_dispatch_safety_observation,
)
from malbut_scenarios.text_gazebo_evidence import (
    CleanupEvidence,
    ConfirmationState,
    DispatchState,
    EvidenceCounts,
    EvidenceDurations,
    ExecutionFaultObservation,
    NavigationState,
    PressureEvidence,
    ProductOutcome,
    ReadinessState,
    RobotActionState,
    SafetyFaultObservation,
    StableStates,
    TestStatus,
    TextGazeboEvidenceManifest,
    TextGazeboEvidenceReceipt,
    pressure_evidence_for,
    execution_fault_observation_for,
    safety_fault_observation_for,
    write_evidence_manifest,
)
from malbut_scenarios.text_gazebo_runtime import (
    ConcurrentApprovalResult,
    DuplicateRequestResult,
    LedgerSnapshot,
    LoopbackPortReservation,
    SQLiteAcceptanceObserver,
    TextAgentHTTPClient,
    TextGazeboRuntimeError,
    installed_artifact_digest,
    loopback_listener_present,
    runtime_binding_digest,
    sanitized_ros_environment,
)
from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboExecutionProfile,
    TextGazeboFaultProfile,
    TextGazeboSafetyProfile,
    TextGazeboScenarioProfile,
    coerce_fault_profile,
    coerce_execution_profile,
    coerce_safety_profile,
    coerce_scenario_profile,
    safety_contract,
    execution_contract,
    scenario_spec,
)
from malbut_scenarios.worker_competition import (
    WorkerCompetitionError,
    WorkerCompetitionObservation,
    read_worker_competition_observation,
    worker_competition_observation_path,
)


_FULL_COMMIT = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
_READINESS_TIMEOUT_SECONDS = 180.0
_EXECUTION_TIMEOUT_SECONDS = 180.0
_NODE_CLEANUP_TIMEOUT_SECONDS = 20.0
_REPLAY_STABILITY_SAMPLES = 8
_REPLAY_SAMPLE_SECONDS = 0.25


class TextGazeboAcceptanceError(RuntimeError):
    """Expose one bounded acceptance code without private runtime data."""

    _CODES = frozenset({
        'acceptance_arguments_invalid',
        'installed_layout_invalid',
        'source_attestation_failed',
        'ros_domain_not_empty',
        'fixture_preparation_failed',
        'gazebo_readiness_timeout',
        'gazebo_readiness_invalid',
        'preapproval_effect_detected',
        'terminal_evidence_invalid',
        'replay_effect_detected',
        'pressure_evidence_invalid',
        'safety_evidence_invalid',
        'cleanup_incomplete',
        'evidence_publish_failed',
        'unexpected_failure',
    })

    def __init__(self, code: str) -> None:
        """Normalize failures to one public-safe code."""
        normalized = (
            code if code in self._CODES else 'unexpected_failure'
        )
        super().__init__(normalized)
        self.code = normalized


class _SafeArgumentParser(argparse.ArgumentParser):
    """Turn invalid CLI input into one bounded machine-readable error."""

    def error(self, message: str) -> None:
        """Reject arguments without printing usage or exiting the process."""
        del message
        raise TextGazeboAcceptanceError('acceptance_arguments_invalid')


@dataclass(frozen=True, repr=False, slots=True)
class InstalledLayout:
    """Program-selected installed files, never rendered with their paths."""

    ros2_executable: Path
    agent_executable: Path
    gazebo_launch: Path
    installed_digest: str
    source_bindings: tuple[tuple[str, Path], ...] = ()

    def __repr__(self) -> str:
        """Render the content digest without installed host paths."""
        return (
            'InstalledLayout('
            f'installed_digest={self.installed_digest!r})'
        )


@dataclass(frozen=True, slots=True)
class _SuccessfulRun:
    readiness_seconds: float
    execution_seconds: float
    preapproval_goal_count: int
    final_ledger: object
    proxy_counts: RobotWebProxyCounts
    pressure: PressureEvidence = field(
        default_factory=lambda: pressure_evidence_for(
            TextGazeboFaultProfile.NONE
        )
    )
    product_outcome: ProductOutcome = ProductOutcome.SUCCEEDED
    block_result_code: str | None = None
    unknown_result_code: str | None = None
    fault_observation: SafetyFaultObservation = field(
        default_factory=lambda: safety_fault_observation_for(
            TextGazeboSafetyProfile.NONE
        )
    )
    execution_fault_observation: ExecutionFaultObservation = field(
        default_factory=lambda: execution_fault_observation_for(
            TextGazeboExecutionProfile.NONE
        )
    )


@dataclass(frozen=True, slots=True)
class _CleanupResult:
    duration_seconds: float
    evidence: CleanupEvidence
    nav2: Nav2GoalStatusEvidence


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description=(
            'Validate or run one installed text-to-Gazebo acceptance flow. '
            'Execution is default-off and simulation-only.'
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        '--check',
        action='store_true',
        help='Validate installed artifacts without starting any runtime.',
    )
    mode.add_argument(
        '--run',
        action='store_true',
        help='Run exactly one authenticated text-to-Gazebo flow.',
    )
    parser.add_argument(
        '--execute-approved-simulation',
        action='store_true',
        help='Explicitly enable Gazebo-only approved execution.',
    )
    parser.add_argument(
        '--evidence',
        type=Path,
        help='New absolute owner-private evidence manifest path.',
    )
    parser.add_argument(
        '--source-commit',
        required=True,
        help='Full lowercase Git object ID used to build this overlay.',
    )
    parser.add_argument(
        '--source-tree',
        type=Path,
        required=True,
        help='Absolute clean Git worktree used to build the overlay.',
    )
    parser.add_argument(
        '--gui',
        action='store_true',
        help='Show Gazebo/RViz while preserving the same acceptance checks.',
    )
    parser.add_argument(
        '--ros-domain-id',
        type=int,
        help='Optional isolated ROS domain in [1, 100].',
    )
    parser.add_argument(
        '--scenario-profile',
        choices=tuple(
            profile.value for profile in TextGazeboScenarioProfile
        ),
        default=TextGazeboScenarioProfile.HAPPY_PATH.value,
        help='Select one server-owned named-location acceptance scenario.',
    )
    parser.add_argument(
        '--fault-profile',
        choices=tuple(
            profile.value for profile in TextGazeboFaultProfile
        ),
        default=TextGazeboFaultProfile.NONE.value,
        help='Select one bounded exactly-once pressure profile.',
    )
    parser.add_argument(
        '--safety-profile',
        choices=tuple(
            profile.value for profile in TextGazeboSafetyProfile
        ),
        default=TextGazeboSafetyProfile.NONE.value,
        help='Select one dispatch-time Safety condition.',
    )
    parser.add_argument(
        '--execution-profile',
        choices=tuple(
            profile.value for profile in TextGazeboExecutionProfile
        ),
        default=TextGazeboExecutionProfile.NONE.value,
        help='Select one bounded default-off execution ambiguity profile.',
    )
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    try:
        coerce_scenario_profile(args.scenario_profile)
        coerce_fault_profile(args.fault_profile)
        coerce_safety_profile(args.safety_profile)
        coerce_execution_profile(args.execution_profile)
    except (TypeError, ValueError):
        raise TextGazeboAcceptanceError(
            'acceptance_arguments_invalid'
        ) from None
    if _FULL_COMMIT.fullmatch(args.source_commit) is None:
        raise TextGazeboAcceptanceError('acceptance_arguments_invalid')
    active_fault_axes = sum((
        coerce_fault_profile(args.fault_profile)
        is not TextGazeboFaultProfile.NONE,
        coerce_safety_profile(args.safety_profile)
        is not TextGazeboSafetyProfile.NONE,
        coerce_execution_profile(args.execution_profile)
        is not TextGazeboExecutionProfile.NONE,
    ))
    if active_fault_axes > 1:
        raise TextGazeboAcceptanceError('acceptance_arguments_invalid')
    if (
        not isinstance(args.source_tree, Path)
        or not args.source_tree.is_absolute()
    ):
        raise TextGazeboAcceptanceError('acceptance_arguments_invalid')
    if args.check:
        if (
            args.execute_approved_simulation
            or args.evidence is not None
            or args.gui
            or args.ros_domain_id is not None
        ):
            raise TextGazeboAcceptanceError(
                'acceptance_arguments_invalid'
            )
        return
    if not args.execute_approved_simulation:
        raise TextGazeboAcceptanceError('acceptance_arguments_invalid')
    if (
        not isinstance(args.evidence, Path)
        or not args.evidence.is_absolute()
    ):
        raise TextGazeboAcceptanceError('acceptance_arguments_invalid')
    if args.ros_domain_id is not None and (
        type(args.ros_domain_id) is not int
        or not 1 <= args.ros_domain_id <= 100
    ):
        raise TextGazeboAcceptanceError('acceptance_arguments_invalid')
    if args.ros_domain_id is None:
        raise TextGazeboAcceptanceError('acceptance_arguments_invalid')


def _artifact_label(logical_name: str) -> str:
    digest = hashlib.sha256(logical_name.encode('utf-8')).hexdigest()[:48]
    return 'artifact-' + digest


def _installed_files(
    package_prefixes: Mapping[str, Path],
) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    observed_paths: set[Path] = set()
    for package_name, prefix in sorted(package_prefixes.items()):
        for candidate in sorted(prefix.rglob('*')):
            if candidate.is_symlink():
                raise TextGazeboAcceptanceError(
                    'installed_layout_invalid'
                )
            if not candidate.is_file():
                continue
            if '__pycache__' in candidate.parts or candidate.suffix == '.pyc':
                continue
            resolved = candidate.resolve(strict=True)
            if resolved in observed_paths or resolved.stat().st_size == 0:
                continue
            observed_paths.add(resolved)
            relative = resolved.relative_to(prefix).as_posix()
            logical = package_name + '/' + relative
            artifacts[_artifact_label(logical)] = resolved
            if len(artifacts) > 4096:
                raise TextGazeboAcceptanceError(
                    'installed_layout_invalid'
                )
    return artifacts


def _package_directory(module, prefix: Path) -> Path:
    try:
        package_file = Path(module.__file__).resolve(strict=True)
        package_file.relative_to(prefix)
        roots = tuple(
            Path(value).resolve(strict=True)
            for value in module.__path__
        )
    except (
        AttributeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        raise TextGazeboAcceptanceError(
            'installed_layout_invalid'
        ) from error
    if roots != (package_file.parent,):
        raise TextGazeboAcceptanceError('installed_layout_invalid')
    return package_file.parent


def _bind_tree(
    bindings: dict[str, Path],
    *,
    installed_root: Path,
    source_root: str,
    suffixes: Optional[frozenset[str]] = None,
) -> None:
    for candidate in sorted(installed_root.rglob('*')):
        if candidate.is_symlink() or not candidate.is_file():
            if candidate.is_symlink():
                raise TextGazeboAcceptanceError(
                    'installed_layout_invalid'
                )
            continue
        if '__pycache__' in candidate.parts or candidate.suffix == '.pyc':
            continue
        if suffixes is not None and candidate.suffix not in suffixes:
            continue
        relative = candidate.relative_to(installed_root).as_posix()
        source_relative = source_root + '/' + relative
        if source_relative in bindings:
            raise TextGazeboAcceptanceError('installed_layout_invalid')
        bindings[source_relative] = candidate.resolve(strict=True)


def _source_bindings(
    *,
    agent_package: Path,
    gazebo_package: Path,
    scenarios_package: Path,
    gazebo_share: Path,
    scenarios_share: Path,
) -> tuple[tuple[str, Path], ...]:
    bindings: dict[str, Path] = {}
    for installed_root, source_root in (
        (
            agent_package,
            'malbut_agent_server/malbut_agent_server',
        ),
        (gazebo_package, 'malbut_gazebo/malbut_gazebo'),
        (scenarios_package, 'malbut_scenarios/malbut_scenarios'),
    ):
        _bind_tree(
            bindings,
            installed_root=installed_root,
            source_root=source_root,
            suffixes=frozenset({'.py'}),
        )
    for share, source_package, names in (
        (
            gazebo_share,
            'malbut_gazebo',
            (
                'LICENSE',
                'README.md',
                'THIRD_PARTY_NOTICES.md',
                'config',
                'launch',
                'maps',
                'models',
                'package.xml',
                'rviz',
                'urdf',
                'worlds',
            ),
        ),
        (
            scenarios_share,
            'malbut_scenarios',
            ('README.md', 'config', 'launch', 'maps', 'package.xml'),
        ),
    ):
        for name in names:
            candidate = share / name
            if candidate.is_dir():
                _bind_tree(
                    bindings,
                    installed_root=candidate,
                    source_root=source_package + '/' + name,
                )
            elif candidate.is_file() and not candidate.is_symlink():
                source_relative = source_package + '/' + name
                bindings[source_relative] = candidate.resolve(strict=True)
            else:
                raise TextGazeboAcceptanceError(
                    'installed_layout_invalid'
                )
    return tuple(sorted(bindings.items()))


def _installed_layout() -> InstalledLayout:
    try:
        from ament_index_python.packages import (
            get_package_prefix,
            get_package_share_directory,
        )
        import malbut_agent_server
        import malbut_gazebo
        import malbut_interfaces
        import malbut_scenarios
        from malbut_interfaces.action import FollowPerson
        from malbut_interfaces.msg import (
            LidarCluster,
            LidarClusterArray,
        )

        # A partial or interrupted colcon build can leave the generated
        # Python modules present while their native type-support library is
        # missing or corrupt.  Force the exact interfaces used by this
        # testbed to load before any process is started.
        for interface in (
            FollowPerson,
            LidarCluster,
            LidarClusterArray,
        ):
            interface.__class__.__import_type_support__()

        package_names = (
            'malbut_agent_server',
            'malbut_description',
            'malbut_gazebo',
            'malbut_gazebo_plugins',
            'malbut_interfaces',
            'malbut_lidar_preprocessor',
            'malbut_patrol',
            'malbut_perception',
            'malbut_roaming',
            'malbut_scenarios',
            'malbut_tracking',
        )
        package_prefixes = {
            name: Path(get_package_prefix(name)).resolve(strict=True)
            for name in package_names
        }
        scenarios_prefix = package_prefixes['malbut_scenarios']
        agent_prefix = package_prefixes['malbut_agent_server']
        gazebo_prefix = package_prefixes['malbut_gazebo']
        interfaces_prefix = package_prefixes['malbut_interfaces']
        gazebo_share = Path(
            get_package_share_directory('malbut_gazebo')
        ).resolve(strict=True)
        scenarios_share = Path(
            get_package_share_directory('malbut_scenarios')
        ).resolve(strict=True)
        agent_package = _package_directory(
            malbut_agent_server, agent_prefix
        )
        gazebo_package = _package_directory(malbut_gazebo, gazebo_prefix)
        scenarios_package = _package_directory(
            malbut_scenarios, scenarios_prefix
        )
        _package_directory(malbut_interfaces, interfaces_prefix)
        module = Path(__file__).resolve(strict=True)
        module.relative_to(scenarios_prefix)
        agent = (
            scenarios_prefix
            / 'lib'
            / 'malbut_scenarios'
            / 'malbut_text_agent_server'
        ).resolve(strict=True)
        launch = (
            gazebo_share
            / 'launch'
            / 'small_house_nav2_testbed.launch.py'
        ).resolve(strict=True)
        ros2_candidate = Path('/opt/ros/humble/bin/ros2')
        if not ros2_candidate.is_file():
            selected = shutil.which('ros2')
            if selected is None:
                raise FileNotFoundError
            ros2_candidate = Path(selected)
        ros2 = ros2_candidate.resolve(strict=True)
        for executable in (agent, ros2):
            metadata = executable.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not os.access(executable, os.X_OK)
            ):
                raise OSError
        artifacts = _installed_files(package_prefixes)
        artifacts['ros2-executable'] = ros2
        digest = installed_artifact_digest(artifacts)
        bindings = _source_bindings(
            agent_package=agent_package,
            gazebo_package=gazebo_package,
            scenarios_package=scenarios_package,
            gazebo_share=gazebo_share,
            scenarios_share=scenarios_share,
        )
        return InstalledLayout(ros2, agent, launch, digest, bindings)
    except (
        AttributeError,
        ImportError,
        KeyError,
        OSError,
        RuntimeError,
        ValueError,
        TextGazeboRuntimeError,
    ) as error:
        raise TextGazeboAcceptanceError(
            'installed_layout_invalid'
        ) from error


def _goal_set_digest(evidence: Nav2GoalStatusEvidence) -> str:
    payload = json.dumps(
        sorted(goal.goal_uuid_sha256 for goal in evidence.goals),
        ensure_ascii=True,
        allow_nan=False,
        separators=(',', ':'),
    ).encode('ascii')
    return hashlib.sha256(payload).hexdigest()


def _source_attestation(
    args: argparse.Namespace,
    layout: InstalledLayout,
) -> SourceInstallAttestation:
    try:
        return attest_source_install(
            args.source_tree,
            args.source_commit,
            dict(layout.source_bindings),
        )
    except (
        OSError,
        RuntimeError,
        SourceInstallAttestationError,
        TypeError,
        ValueError,
    ) as error:
        raise TextGazeboAcceptanceError(
            'source_attestation_failed'
        ) from error


def _ros_node_count(
    ros2_executable: Path,
    environment: Mapping[str, str],
) -> int:
    process = None
    try:
        process = subprocess.Popen(
            [
                str(ros2_executable),
                'node',
                'list',
                '--no-daemon',
                '--spin-time',
                '1.0',
                '--all',
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(environment),
            shell=False,
        )
        stdout, _stderr = process.communicate(timeout=10.0)
    except subprocess.TimeoutExpired as error:
        if process is not None:
            process.kill()
            process.communicate()
        raise TextGazeboAcceptanceError(
            'cleanup_incomplete'
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise TextGazeboAcceptanceError(
            'cleanup_incomplete'
        ) from error
    if process.returncode != 0 or len(stdout) > 1_000_000:
        raise TextGazeboAcceptanceError('cleanup_incomplete')
    try:
        lines = stdout.decode('utf-8', errors='strict').splitlines()
    except UnicodeDecodeError as error:
        raise TextGazeboAcceptanceError(
            'cleanup_incomplete'
        ) from error
    node_names = [
        line.strip()
        for line in lines
        if line.strip().startswith('/')
    ]
    own_cli_node = f'/_ros2cli_{process.pid}'
    # ``ros2 node list --all`` creates one hidden query node in its own
    # process.  Remove that exact PID-derived observer only; an unrelated
    # CLI-shaped node remains residue and therefore fails cleanup.
    return len(node_names) - min(1, node_names.count(own_cli_node))


def _await_zero_ros_nodes(
    ros2_executable: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while True:
        count = _ros_node_count(ros2_executable, environment)
        if count == 0:
            return 0
        if time.monotonic() >= deadline:
            return count
        time.sleep(0.25)


def _await_robot_web_readiness(
    port: int,
    *,
    device_id: str,
    map_id: str,
    map_revision: str,
    process: OwnedProcess,
) -> float:
    from malbut_gazebo.robot_web_navigation_client import (
        RobotWebNavigationClient,
        RobotWebNavigationClientError,
    )

    started = time.monotonic()
    deadline = started + _READINESS_TIMEOUT_SECONDS
    consecutive = 0
    client = RobotWebNavigationClient(f'http://127.0.0.1:{port}')
    while time.monotonic() < deadline:
        process.require_running()
        try:
            readiness = client.readiness()
            acceptable = bool(
                readiness.simulation
                and readiness.ready_for_navigation
                and readiness.matches_runtime(
                    device_id=device_id,
                    map_id=map_id,
                    map_revision=map_revision,
                )
            )
        except RobotWebNavigationClientError:
            acceptable = False
        consecutive = consecutive + 1 if acceptable else 0
        if consecutive >= 2:
            return time.monotonic() - started
        time.sleep(0.25)
    raise TextGazeboAcceptanceError('gazebo_readiness_timeout')


class _AcceptanceSupervisor:
    """Own every process, thread, listener, and private runtime artifact."""

    def __init__(
        self,
        *,
        layout: InstalledLayout,
        run_root: Path,
        domain_id: int,
        gui: bool,
        nonce: str,
        scenario_profile: TextGazeboScenarioProfile = (
            TextGazeboScenarioProfile.HAPPY_PATH
        ),
        fault_profile: TextGazeboFaultProfile = (
            TextGazeboFaultProfile.NONE
        ),
        safety_profile: TextGazeboSafetyProfile = (
            TextGazeboSafetyProfile.NONE
        ),
        execution_profile: TextGazeboExecutionProfile = (
            TextGazeboExecutionProfile.NONE
        ),
    ) -> None:
        self._layout = layout
        self._run_root = run_root
        self._domain_id = domain_id
        self._gui = gui
        self._nonce = nonce
        self._scenario = scenario_spec(scenario_profile)
        self._fault_profile = coerce_fault_profile(fault_profile)
        self._safety_profile = coerce_safety_profile(safety_profile)
        self._execution_profile = coerce_execution_profile(
            execution_profile
        )
        self._private_runtime = run_root / 'runtime-home'
        self._private_runtime.mkdir(mode=0o700)
        for name in ('ros', 'cache', 'config'):
            (self._private_runtime / name).mkdir(mode=0o700)
        self._ros_environment = sanitized_ros_environment(
            os.environ,
            private_home=self._private_runtime,
            domain_id=domain_id,
            gui=gui,
        )
        self._gazebo: Optional[OwnedProcess] = None
        self._agent: Optional[OwnedProcess] = None
        self._proxy: Optional[CountingRobotWebProxy] = None
        self._observer: Optional[Nav2GoalStatusObserver] = None
        self._window_open = False
        self._agent_port: Optional[int] = None
        self._robot_web_port: Optional[int] = None
        self._proxy_port: Optional[int] = None
        self._ledger: Optional[SQLiteAcceptanceObserver] = None
        self._confirmation_request_id: Optional[str] = None
        self._cleaned: Optional[_CleanupResult] = None
        self._empty_domain_verified = False

    def run(self) -> tuple[_SuccessfulRun, dict[str, str]]:
        """Run the public flow and return content-free/private bindings."""
        if _ros_node_count(
            self._layout.ros2_executable,
            self._ros_environment,
        ) != 0:
            raise TextGazeboAcceptanceError('ros_domain_not_empty')
        self._empty_domain_verified = True
        fixture = self._prepare_fixture()
        robot_web = LoopbackPortReservation()
        agent_port = LoopbackPortReservation()
        proxy_port = LoopbackPortReservation()
        self._robot_web_port = robot_web.port
        self._agent_port = agent_port.port
        self._proxy_port = proxy_port.port
        try:
            self._start_observer()
            robot_web.release()
            self._start_gazebo(fixture)
            readiness_seconds = _await_robot_web_readiness(
                self._robot_web_port,
                device_id=fixture['device_id'],
                map_id=fixture['map_id'],
                map_revision=fixture['map_revision'],
                process=self._gazebo,
            )
            proxy_port.release()
            self._start_proxy(fixture)
            agent_port.release()
            client = self._start_agent(fixture)
            execution_started = time.monotonic()
            client.create_conversation()
            proposal = client.request_navigation()
            duplicate_result = None
            if (
                self._fault_profile
                is TextGazeboFaultProfile.DUPLICATE_REQUEST
            ):
                duplicate_result = client.replay_navigation_request(
                    proposal
                )
            self._confirmation_request_id = (
                proposal.confirmation_request_id
            )
            self._ledger = SQLiteAcceptanceObserver(
                (self._run_root / 'agent.sqlite3').resolve()
            )
            preapproval = self._ledger.snapshot(
                proposal.confirmation_request_id
            )
            pre_goal_count = self._observer.snapshot().distinct_goal_count
            pre_proxy = self._proxy.snapshot()
            if not (
                preapproval.is_preapproval()
                and pre_goal_count == 0
                and pre_proxy.start_count == 0
                and pre_proxy.cancel_count == 0
            ):
                raise TextGazeboAcceptanceError(
                    'preapproval_effect_detected'
                )
            concurrent_result = None
            if (
                self._fault_profile
                is TextGazeboFaultProfile.CONCURRENT_APPROVAL
            ):
                concurrent_result = (
                    client.approve_navigation_concurrently()
                )
            else:
                client.approve_navigation()
            safety_contract_value = safety_contract(
                self._safety_profile
            )
            execution_contract_value = execution_contract(
                self._execution_profile
            )
            unknown_result_code = None
            execution_fault_observation = (
                execution_fault_observation_for(
                    TextGazeboExecutionProfile.NONE
                )
            )
            if (
                self._execution_profile
                is not TextGazeboExecutionProfile.NONE
            ):
                product_outcome = ProductOutcome.UNKNOWN
                unknown_result_code = execution_contract_value.result_code
                if unknown_result_code is None:
                    raise TextGazeboAcceptanceError(
                        'terminal_evidence_invalid'
                    )
                final = self._ledger.await_expected_unknown(
                    proposal.confirmation_request_id,
                    result_code=unknown_result_code,
                    timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                )
                self._await_execution_navigation_observation(
                    execution_contract_value
                )
                fault_observation = safety_fault_observation_for(
                    TextGazeboSafetyProfile.NONE
                )
                execution_fault_observation = (
                    self._execution_fault_observation()
                )
            elif self._safety_profile is TextGazeboSafetyProfile.NONE:
                product_outcome = ProductOutcome.SUCCEEDED
                final = self._ledger.await_known_success(
                    proposal.confirmation_request_id,
                    timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                )
                self._await_nav2_success()
                fault_observation = safety_fault_observation_for(
                    TextGazeboSafetyProfile.NONE
                )
            else:
                product_outcome = ProductOutcome.BLOCKED
                if safety_contract_value.result_code is None:
                    raise TextGazeboAcceptanceError(
                        'safety_evidence_invalid'
                    )
                final = self._ledger.await_expected_blocked(
                    proposal.confirmation_request_id,
                    result_code=safety_contract_value.result_code,
                    timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                )
                fault_observation = self._safety_fault_observation()
            approval_observation = (
                self._concurrent_approval_pressure_observation()
            )
            worker_observation = self._worker_pressure_observation()
            before_replay = self._effect_counts()
            if concurrent_result is None:
                client.replay_approval()
            else:
                client.replay_winning_approval(concurrent_result)
            client.send_late_approval()
            for _sample in range(_REPLAY_STABILITY_SAMPLES):
                time.sleep(_REPLAY_SAMPLE_SECONDS)
                if before_replay != self._effect_counts():
                    raise TextGazeboAcceptanceError(
                        'replay_effect_detected'
                    )
            final_after = self._ledger.snapshot(
                proposal.confirmation_request_id
            )
            proxy_counts = self._proxy.snapshot()
            terminal_valid = self._terminal_evidence_valid(
                final=final,
                final_after=final_after,
                proxy_counts=proxy_counts,
                product_outcome=product_outcome,
                block_result_code=safety_contract_value.result_code,
                unknown_result_code=unknown_result_code,
            )
            if not terminal_valid:
                raise TextGazeboAcceptanceError(
                    'terminal_evidence_invalid'
                )
            pressure = self._pressure_evidence(
                duplicate_result=duplicate_result,
                concurrent_result=concurrent_result,
                approval_observation=approval_observation,
                worker_observation=worker_observation,
            )
            return (
                _SuccessfulRun(
                    readiness_seconds=readiness_seconds,
                    execution_seconds=(
                        time.monotonic() - execution_started
                    ),
                    preapproval_goal_count=pre_goal_count,
                    final_ledger=final_after,
                    proxy_counts=proxy_counts,
                    pressure=pressure,
                    product_outcome=product_outcome,
                    block_result_code=(
                        safety_contract_value.result_code
                    ),
                    unknown_result_code=unknown_result_code,
                    fault_observation=fault_observation,
                    execution_fault_observation=(
                        execution_fault_observation
                    ),
                ),
                {
                    'device_id': fixture['device_id'],
                    'map_id': fixture['map_id'],
                    'map_revision': fixture['map_revision'],
                    'target_binding_digest': (
                        fixture['target_binding_digest']
                    ),
                },
            )
        finally:
            robot_web.release()
            agent_port.release()
            proxy_port.release()

    def _terminal_evidence_valid(
        self,
        *,
        final: LedgerSnapshot,
        final_after: LedgerSnapshot,
        proxy_counts: RobotWebProxyCounts,
        product_outcome: ProductOutcome,
        block_result_code: str | None,
        unknown_result_code: str | None,
    ) -> bool:
        """Validate exact success or exact pre-dispatch zero effects."""
        if final != final_after:
            return False
        if product_outcome is ProductOutcome.SUCCEEDED:
            return bool(
                final_after.is_known_success()
                and proxy_counts.preview_count == 1
                and proxy_counts.verified_preview_count == 1
                and proxy_counts.start_count == 1
                and proxy_counts.cancel_count == 0
            )
        nav2 = self._observer.snapshot()
        if product_outcome is ProductOutcome.UNKNOWN:
            contract = execution_contract(self._execution_profile)
            goal_shape_valid = bool(
                nav2.distinct_goal_count
                == contract.expected_nav2_goal_count
                and nav2.terminal_goal_count
                == contract.expected_nav2_terminal_count
                and nav2.rejected_status_entry_count == 0
            )
            if contract.expected_nav2_goal_count == 0:
                goal_shape_valid = goal_shape_valid and not nav2.goals
            else:
                goal_shape_valid = bool(
                    goal_shape_valid
                    and len(nav2.goals) == 1
                    and nav2.goals[0].latest_status == 'succeeded'
                )
            return bool(
                unknown_result_code == contract.result_code
                and unknown_result_code is not None
                and final_after.is_expected_unknown(unknown_result_code)
                and proxy_counts.preview_count == 1
                and proxy_counts.verified_preview_count == 1
                and proxy_counts.start_count == 1
                and proxy_counts.start_forward_count
                == contract.start_forward_count
                and proxy_counts.start_response_drop_count
                == contract.start_response_drop_count
                and proxy_counts.terminal_status_response_drop_count
                == contract.terminal_status_response_drop_count
                and proxy_counts.unavailable_endpoint_count
                == contract.unavailable_endpoint_count
                and proxy_counts.cancel_count == 0
                and goal_shape_valid
            )
        return bool(
            block_result_code is not None
            and final_after.is_expected_blocked(block_result_code)
            and proxy_counts.preview_count == 1
            and proxy_counts.verified_preview_count == 1
            and proxy_counts.start_count == 0
            and proxy_counts.cancel_count == 0
            and nav2.distinct_goal_count == 0
            and nav2.terminal_goal_count == 0
            and nav2.rejected_status_entry_count == 0
            and not nav2.goals
        )

    def _worker_pressure_observation(
        self,
    ) -> WorkerCompetitionObservation | None:
        """Read the private worker-race proof only for its exact profile."""
        database = str((self._run_root / 'agent.sqlite3').resolve())
        try:
            path = worker_competition_observation_path(database)
            if (
                self._fault_profile
                is TextGazeboFaultProfile.COMPETING_WORKERS
            ):
                return read_worker_competition_observation(path)
            if path.exists() or path.is_symlink():
                raise WorkerCompetitionError(
                    'worker_competition_observation_invalid'
                )
            return None
        except (OSError, ValueError, WorkerCompetitionError):
            raise TextGazeboAcceptanceError(
                'pressure_evidence_invalid'
            ) from None

    def _safety_fault_observation(self) -> SafetyFaultObservation:
        """Project one strict private post-claim fault observation."""
        database = str((self._run_root / 'agent.sqlite3').resolve())
        contract = safety_contract(self._safety_profile)
        try:
            observation = read_dispatch_safety_observation(
                dispatch_safety_observation_path(database)
            )
        except (OSError, ValueError, DispatchSafetyFaultError):
            raise TextGazeboAcceptanceError(
                'safety_evidence_invalid'
            ) from None

        if (
            observation.safety_profile is not self._safety_profile
            or observation.result_code != contract.result_code
            or observation.claim_arm_count != 1
            or observation.preclaim_read_count != 1
            or observation.postclaim_read_count != 1
            or observation.fault_application_count
            != contract.fault_application_count
            or observation.map_switch_count != contract.map_switch_count
        ):
            raise TextGazeboAcceptanceError('safety_evidence_invalid')
        return SafetyFaultObservation(
            observed=True,
            fault_application_count=(
                observation.fault_application_count
            ),
            map_switch_count=observation.map_switch_count,
        )

    def _execution_fault_observation(self) -> ExecutionFaultObservation:
        """Project exact profile application and proxy boundary counts."""
        profile = self._execution_profile
        counts = self._proxy.snapshot()
        fault_application_count = 0
        if profile is not TextGazeboExecutionProfile.NONE:
            fault_application_count = sum((
                counts.start_response_drop_count,
                counts.terminal_status_response_drop_count,
                counts.unavailable_endpoint_count,
            ))
        return ExecutionFaultObservation(
            observed=fault_application_count > 0,
            fault_application_count=fault_application_count,
            start_forward_count=counts.start_forward_count,
            start_response_drop_count=counts.start_response_drop_count,
            terminal_status_response_drop_count=(
                counts.terminal_status_response_drop_count
            ),
            unavailable_endpoint_count=counts.unavailable_endpoint_count,
        )

    def _concurrent_approval_pressure_observation(
        self,
    ) -> ConcurrentApprovalGateObservation | None:
        """Read server-owned proof only for concurrent approval pressure."""
        database = str((self._run_root / 'agent.sqlite3').resolve())
        try:
            path = concurrent_approval_observation_path(database)
            if (
                self._fault_profile
                is TextGazeboFaultProfile.CONCURRENT_APPROVAL
            ):
                return read_concurrent_approval_observation(path)
            if path.exists() or path.is_symlink():
                raise ConcurrentApprovalGateError(
                    'concurrent approval observation is invalid'
                )
            return None
        except (OSError, ValueError, ConcurrentApprovalGateError):
            raise TextGazeboAcceptanceError(
                'pressure_evidence_invalid'
            ) from None

    def _pressure_evidence(
        self,
        *,
        duplicate_result: DuplicateRequestResult | None,
        concurrent_result: ConcurrentApprovalResult | None,
        approval_observation: (
            ConcurrentApprovalGateObservation | None
        ),
        worker_observation: WorkerCompetitionObservation | None,
    ) -> PressureEvidence:
        """Derive exact public counters from the pressure actually observed."""
        profile = self._fault_profile
        if profile is TextGazeboFaultProfile.NONE:
            valid = bool(
                duplicate_result is None
                and concurrent_result is None
                and approval_observation is None
                and worker_observation is None
            )
            pressure = PressureEvidence(1, 3, 1, 1, 1, 0)
        elif profile is TextGazeboFaultProfile.DUPLICATE_REQUEST:
            valid = bool(
                duplicate_result == DuplicateRequestResult(2, 2, 0)
                and concurrent_result is None
                and approval_observation is None
                and worker_observation is None
            )
            pressure = PressureEvidence(2, 3, 1, 2, 1, 1)
        elif profile is TextGazeboFaultProfile.CONCURRENT_APPROVAL:
            valid = bool(
                duplicate_result is None
                and concurrent_result
                == ConcurrentApprovalResult(2, 1, 1)
                and approval_observation
                == ConcurrentApprovalGateObservation()
                and worker_observation is None
            )
            pressure = PressureEvidence(1, 4, 1, 2, 1, 1)
        elif profile is TextGazeboFaultProfile.COMPETING_WORKERS:
            valid = bool(
                duplicate_result is None
                and concurrent_result is None
                and approval_observation is None
                and worker_observation == WorkerCompetitionObservation()
            )
            pressure = PressureEvidence(1, 3, 2, 2, 1, 1)
        else:  # pragma: no cover - enum exhaustiveness guard
            valid = False
            pressure = PressureEvidence(1, 3, 1, 1, 1, 0)
        if not valid:
            raise TextGazeboAcceptanceError(
                'pressure_evidence_invalid'
            )
        return pressure

    def cleanup(self) -> _CleanupResult:
        """Stop all exact owners and verify zero residue."""
        if self._cleaned is not None:
            return self._cleaned
        started = time.monotonic()
        cleanups: list[ProcessCleanupEvidence] = []
        cleanup_ok = True
        nav2 = Nav2GoalStatusEvidence(
            status_topic='/navigate_to_pose/_action/status',
            distinct_goal_count=0,
            status_message_count=0,
            rejected_status_entry_count=0,
            goals=(),
        )
        if self._agent is not None:
            try:
                result = self._agent.stop(
                    interrupt_seconds=30.0,
                    terminate_seconds=10.0,
                    kill_seconds=5.0,
                )
                cleanups.append(result)
                cleanup_ok = cleanup_ok and result.cleanup_complete
            except Exception:
                cleanup_ok = False
        if self._proxy is not None:
            try:
                cleanup_ok = self._proxy.close(10.0) and cleanup_ok
            except Exception:
                cleanup_ok = False
        if self._observer is not None:
            try:
                if self._window_open:
                    nav2 = self._observer.end_window()
                    self._window_open = False
                self._observer.close()
                cleanup_ok = self._observer.join(10.0) and cleanup_ok
                self._observer.raise_if_failed()
            except Exception:
                cleanup_ok = False
        if self._gazebo is not None:
            try:
                result = self._gazebo.stop(
                    interrupt_seconds=45.0,
                    terminate_seconds=10.0,
                    kill_seconds=5.0,
                )
                cleanups.append(result)
                cleanup_ok = cleanup_ok and result.cleanup_complete
            except Exception:
                cleanup_ok = False
        if self._ledger is not None:
            try:
                cleanup_ok = self._ledger.quick_check() and cleanup_ok
            except TextGazeboRuntimeError:
                cleanup_ok = False
        remaining_nodes = 0
        if self._empty_domain_verified:
            try:
                remaining_nodes = _await_zero_ros_nodes(
                    self._layout.ros2_executable,
                    self._ros_environment,
                    _NODE_CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception:
                cleanup_ok = False
                remaining_nodes = 1
        ports = tuple(
            port
            for port in (
                self._agent_port,
                self._robot_web_port,
                self._proxy_port,
            )
            if port is not None
        )
        try:
            remaining_sockets = sum(
                loopback_listener_present(port) for port in ports
            )
        except Exception:
            cleanup_ok = False
            remaining_sockets = 1
        remaining_processes = sum(
            item.remaining_process_count for item in cleanups
        )
        forced = sum(
            item.forced_termination_count for item in cleanups
        )
        complete = bool(
            cleanup_ok
            and remaining_processes == 0
            and remaining_nodes == 0
            and remaining_sockets == 0
            and forced == 0
        )
        evidence = CleanupEvidence(
            completed=complete,
            owned_processes_remaining=remaining_processes,
            ros_nodes_remaining=remaining_nodes,
            owned_sockets_remaining=remaining_sockets,
            forced_termination_count=forced,
        )
        self._cleaned = _CleanupResult(
            duration_seconds=time.monotonic() - started,
            evidence=evidence,
            nav2=nav2,
        )
        if not complete:
            raise TextGazeboAcceptanceError('cleanup_incomplete')
        return self._cleaned

    def _prepare_fixture(self) -> dict[str, str]:
        try:
            from malbut_gazebo.named_navigation_facade import (
                ActiveMapCatalogSource,
            )
            from malbut_scenarios.named_navigation_fixture import (
                _package_sources,
                prepare_small_house_named_navigation_fixture,
            )

            map_yaml, user_map, zones = _package_sources()
            result = prepare_small_house_named_navigation_fixture(
                self._run_root / 'map-store',
                map_yaml=map_yaml,
                user_map=user_map,
                zones=zones,
            )
            selected = {
                key: result[key]
                for key in (
                    'device_id',
                    'map_id',
                    'map_revision',
                    'store',
                    'user_map_path',
                )
            }
            if any(type(value) is not str or not value
                   for value in selected.values()):
                raise ValueError
            target = ActiveMapCatalogSource(
                Path(selected['store']),
                selected['device_id'],
            ).load().resolve(self._scenario.location)
            preview_body = json.dumps(
                {
                    'map_id': target.map_id,
                    'map_revision': target.map_revision,
                    'user_map_digest': target.source_digest,
                    'x': target.x,
                    'y': target.y,
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(',', ':'),
            ).encode('utf-8')
            selected['expected_preview_digest'] = request_body_digest(
                preview_body
            )
            selected['target_binding_digest'] = target.binding_digest
            return selected
        except Exception as error:
            raise TextGazeboAcceptanceError(
                'fixture_preparation_failed'
            ) from error

    def _start_observer(self) -> None:
        os.environ['ROS_DOMAIN_ID'] = str(self._domain_id)
        os.environ['ROS_LOCALHOST_ONLY'] = '1'
        os.environ['ROS2CLI_NO_DAEMON'] = '1'
        observer = Nav2GoalStatusObserver(self._domain_id)
        self._observer = observer
        observer.start()
        observer.begin_window()
        self._window_open = True

    def _start_gazebo(self, fixture: Mapping[str, str]) -> None:
        headless = 'false' if self._gui else 'true'
        process = OwnedProcess(
            'gazebo-testbed',
            (
                str(self._layout.ros2_executable),
                'launch',
                'malbut_gazebo',
                self._layout.gazebo_launch.name,
                'enable_named_navigation:=true',
                'named_navigation_user_map:='
                + fixture['user_map_path'],
                'named_navigation_map_store:=' + fixture['store'],
                'named_navigation_port:=' + str(self._robot_web_port),
                'named_navigation_test_unavailable_action:=' + (
                    'true'
                    if self._execution_profile
                    is TextGazeboExecutionProfile.NAV2_UNAVAILABLE
                    else 'false'
                ),
                'gui:=' + ('true' if self._gui else 'false'),
                'headless:=' + headless,
                'rviz:=' + ('true' if self._gui else 'false'),
            ),
            cwd=self._run_root,
            environment=self._ros_environment,
            maximum_output_bytes=16 * 1024 * 1024,
        )
        self._gazebo = process
        process.start()

    def _start_proxy(self, fixture: Mapping[str, str]) -> None:
        proxy = CountingRobotWebProxy(
            self._proxy_port,
            self._robot_web_port,
            timeout_seconds=30.0,
            expected_preview_digest=fixture['expected_preview_digest'],
            execution_profile=self._execution_profile,
        )
        self._proxy = proxy
        proxy.start()

    def _start_agent(
        self,
        fixture: Mapping[str, str],
    ) -> TextAgentHTTPClient:
        token = secrets.token_urlsafe(32)
        user_id = 'swm25-133-user-' + self._nonce
        environment = dict(self._ros_environment)
        environment.update({
            'MALBUT_AGENT_PROVIDER': 'mock',
            'MALBUT_AGENT_TOOL_MODE': 'proposal',
            'MALBUT_AGENT_AUTH_TOKEN': token,
            'MALBUT_AGENT_USER_ID': user_id,
            'MALBUT_AGENT_HOST': '127.0.0.1',
            'MALBUT_AGENT_PORT': str(self._agent_port),
            'MALBUT_AGENT_DB': str(self._run_root / 'agent.sqlite3'),
        })
        empty_env = self._run_root / 'empty.env'
        empty_env.write_text('', encoding='utf-8')
        empty_env.chmod(0o600)
        process = OwnedProcess(
            'text-agent',
            (
                str(self._layout.agent_executable),
                '--env-file',
                str(empty_env),
                '--map-store',
                fixture['store'],
                '--device-id',
                fixture['device_id'],
                '--execute-approved-simulation',
                '--scenario-profile',
                self._scenario.profile.value,
                '--fault-profile',
                self._fault_profile.value,
                '--safety-profile',
                self._safety_profile.value,
                '--robot-web-url',
                self._proxy.origin,
            ),
            cwd=self._run_root,
            environment=environment,
            maximum_output_bytes=4 * 1024 * 1024,
        )
        self._agent = process
        process.start()
        client = TextAgentHTTPClient(
            self._agent_port,
            token=token,
            user_id=user_id,
            run_nonce=self._nonce,
            scenario_profile=self._scenario.profile,
        )
        client.await_health(30.0)
        process.require_running()
        return client

    def _await_nav2_success(self) -> None:
        deadline = time.monotonic() + 10.0
        while True:
            self._observer.raise_if_failed()
            evidence = self._observer.snapshot()
            valid = bool(
                evidence.distinct_goal_count == 1
                and evidence.terminal_goal_count == 1
                and evidence.rejected_status_entry_count == 0
                and len(evidence.goals) == 1
                and evidence.goals[0].latest_status == 'succeeded'
            )
            if valid:
                return
            if evidence.distinct_goal_count > 1:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        raise TextGazeboAcceptanceError('terminal_evidence_invalid')

    def _await_execution_navigation_observation(self, contract) -> None:
        """Wait for the exact independent Nav2 shape of one ambiguity."""
        deadline = time.monotonic() + 30.0
        while True:
            self._observer.raise_if_failed()
            evidence = self._observer.snapshot()
            valid = bool(
                evidence.distinct_goal_count
                == contract.expected_nav2_goal_count
                and evidence.terminal_goal_count
                == contract.expected_nav2_terminal_count
                and evidence.rejected_status_entry_count == 0
            )
            if contract.expected_nav2_goal_count == 0:
                valid = valid and not evidence.goals
            else:
                valid = bool(
                    valid
                    and len(evidence.goals) == 1
                    and evidence.goals[0].latest_status == 'succeeded'
                )
            if valid:
                return
            if (
                evidence.distinct_goal_count
                > contract.expected_nav2_goal_count
                or evidence.terminal_goal_count
                > contract.expected_nav2_terminal_count
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.1)
        raise TextGazeboAcceptanceError('terminal_evidence_invalid')

    def _effect_counts(self) -> tuple[int, ...]:
        ledger = self._ledger.snapshot(self._confirmation_request_id)
        proxy = self._proxy.snapshot()
        nav2 = self._observer.snapshot()
        return (
            ledger.durable_agent_turn_count,
            ledger.confirmation_count,
            ledger.approved_confirmation_count,
            ledger.robot_action_count,
            ledger.dispatch_intent_count,
            proxy.preview_count,
            proxy.start_count,
            proxy.start_forward_count,
            proxy.start_response_drop_count,
            proxy.terminal_status_response_drop_count,
            proxy.unavailable_endpoint_count,
            proxy.cancel_count,
            nav2.distinct_goal_count,
            nav2.terminal_goal_count,
        )


def _build_receipt(
    *,
    args: argparse.Namespace,
    layout: InstalledLayout,
    attestation: SourceInstallAttestation,
    run_id: str,
    total_seconds: float,
    successful: _SuccessfulRun,
    binding: Mapping[str, str],
    cleanup: _CleanupResult,
) -> TextGazeboEvidenceReceipt:
    nav2 = cleanup.nav2
    if successful.product_outcome is ProductOutcome.SUCCEEDED:
        nav2_valid = bool(
            nav2.distinct_goal_count == 1
            and nav2.terminal_goal_count == 1
            and len(nav2.goals) == 1
            and nav2.goals[0].latest_status == 'succeeded'
            and nav2.rejected_status_entry_count == 0
        )
        states = StableStates(
            readiness=ReadinessState.READY,
            confirmation=ConfirmationState.APPROVED,
            robot_action=RobotActionState.SUCCEEDED,
            dispatch=DispatchState.TERMINAL,
            navigation=NavigationState.SUCCEEDED,
        )
    elif successful.product_outcome is ProductOutcome.BLOCKED:
        nav2_valid = bool(
            nav2.distinct_goal_count == 0
            and nav2.terminal_goal_count == 0
            and not nav2.goals
            and nav2.rejected_status_entry_count == 0
        )
        states = StableStates(
            readiness=ReadinessState.READY,
            confirmation=ConfirmationState.APPROVED,
            robot_action=RobotActionState.BLOCKED,
            dispatch=DispatchState.NOT_CREATED,
            navigation=NavigationState.NOT_STARTED,
        )
    elif successful.product_outcome is ProductOutcome.UNKNOWN:
        contract = execution_contract(
            coerce_execution_profile(args.execution_profile)
        )
        nav2_valid = bool(
            nav2.distinct_goal_count == contract.expected_nav2_goal_count
            and nav2.terminal_goal_count
            == contract.expected_nav2_terminal_count
            and nav2.rejected_status_entry_count == 0
        )
        if contract.expected_nav2_goal_count == 0:
            nav2_valid = nav2_valid and not nav2.goals
            navigation_state = NavigationState.NOT_STARTED
        else:
            nav2_valid = bool(
                nav2_valid
                and len(nav2.goals) == 1
                and nav2.goals[0].latest_status == 'succeeded'
            )
            navigation_state = NavigationState.SUCCEEDED
        states = StableStates(
            readiness=ReadinessState.READY,
            confirmation=ConfirmationState.APPROVED,
            robot_action=RobotActionState.UNKNOWN,
            dispatch=DispatchState.UNKNOWN,
            navigation=navigation_state,
        )
    else:
        raise TextGazeboAcceptanceError('terminal_evidence_invalid')
    if not nav2_valid:
        raise TextGazeboAcceptanceError('terminal_evidence_invalid')
    final = successful.final_ledger
    return TextGazeboEvidenceReceipt(
        run_id=run_id,
        commit=args.source_commit,
        source_tree_digest=attestation.tree_digest,
        installed_digest=layout.installed_digest,
        goal_set_digest=_goal_set_digest(nav2),
        runtime_binding_digest=runtime_binding_digest(
            device_id=binding['device_id'],
            map_id=binding['map_id'],
            map_revision=binding['map_revision'],
        ),
        target_binding_digest=binding['target_binding_digest'],
        scenario_profile=coerce_scenario_profile(
            args.scenario_profile
        ),
        fault_profile=coerce_fault_profile(args.fault_profile),
        pressure=successful.pressure,
        safety_profile=coerce_safety_profile(args.safety_profile),
        execution_profile=coerce_execution_profile(
            args.execution_profile
        ),
        product_outcome=successful.product_outcome,
        test_status=TestStatus.PASSED,
        block_result_code=successful.block_result_code,
        unknown_result_code=successful.unknown_result_code,
        fault_observation=successful.fault_observation,
        execution_fault_observation=(
            successful.execution_fault_observation
        ),
        states=states,
        counts=EvidenceCounts(
            agent_proposal_count=final.durable_agent_turn_count,
            confirmation_count=final.confirmation_count,
            approved_confirmation_count=(
                final.approved_confirmation_count
            ),
            robot_action_count=final.robot_action_count,
            dispatch_intent_count=final.dispatch_intent_count,
            robot_web_start_count=(
                successful.proxy_counts.start_count
            ),
            robot_web_verified_target_count=(
                successful.proxy_counts.verified_preview_count
            ),
            nav2_goal_count=nav2.distinct_goal_count,
            preapproval_nav2_goal_count=(
                successful.preapproval_goal_count
            ),
            terminal_result_count=nav2.terminal_goal_count,
            replay_additional_effect_count=0,
        ),
        durations=EvidenceDurations(
            readiness_seconds=successful.readiness_seconds,
            execution_seconds=successful.execution_seconds,
            cleanup_seconds=cleanup.duration_seconds,
            total_seconds=total_seconds,
        ),
        cleanup=cleanup.evidence,
    )


def _run_acceptance(
    args: argparse.Namespace,
    layout: InstalledLayout,
    attestation: SourceInstallAttestation,
) -> TextGazeboEvidenceManifest:
    started = time.monotonic()
    nonce = secrets.token_hex(16)
    domain_id = args.ros_domain_id
    run_id = 'run-' + nonce
    with tempfile.TemporaryDirectory(
        prefix='malbut-swm25-133-',
    ) as temporary:
        run_root = Path(temporary).resolve()
        run_root.chmod(0o700)
        supervisor = _AcceptanceSupervisor(
            layout=layout,
            run_root=run_root,
            domain_id=domain_id,
            gui=args.gui,
            nonce=nonce,
            scenario_profile=coerce_scenario_profile(
                args.scenario_profile
            ),
            fault_profile=coerce_fault_profile(args.fault_profile),
            safety_profile=coerce_safety_profile(args.safety_profile),
            execution_profile=coerce_execution_profile(
                args.execution_profile
            ),
        )
        successful = None
        binding = None
        try:
            successful, binding = supervisor.run()
        finally:
            cleanup = supervisor.cleanup()
        receipt = _build_receipt(
            args=args,
            layout=layout,
            attestation=attestation,
            run_id=run_id,
            total_seconds=time.monotonic() - started,
            successful=successful,
            binding=binding,
            cleanup=cleanup,
        )
        manifest = TextGazeboEvidenceManifest(receipt)
        try:
            write_evidence_manifest(args.evidence, manifest)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise TextGazeboAcceptanceError(
                'evidence_publish_failed'
            ) from error
        return manifest


def _safe_code(error: BaseException) -> str:
    if isinstance(error, TextGazeboAcceptanceError):
        return error.code
    if isinstance(error, TextGazeboRuntimeError):
        return error.code
    if isinstance(error, OwnedProcessError):
        return error.code
    if isinstance(error, KeyboardInterrupt):
        return 'interrupted'
    return 'unexpected_failure'


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Validate installed code or execute one explicit Gazebo acceptance."""
    try:
        args = _parser().parse_args(argv)
        _validate_arguments(args)
        layout = _installed_layout()
        attestation = _source_attestation(args, layout)
        if args.check:
            print(json.dumps({
                'installed_digest': layout.installed_digest,
                'mode': 'check',
                'nav2_start_count': 0,
                'physical_authorized': False,
                'fault_profile': coerce_fault_profile(
                    args.fault_profile
                ).value,
                'execution_profile': coerce_execution_profile(
                    args.execution_profile
                ).value,
                'scenario_profile': coerce_scenario_profile(
                    args.scenario_profile
                ).value,
                'safety_profile': coerce_safety_profile(
                    args.safety_profile
                ).value,
                'simulation': True,
                'source_tree_digest': attestation.tree_digest,
                'status': 'ok',
            }, ensure_ascii=True, sort_keys=True))
            return 0
        manifest = _run_acceptance(args, layout, attestation)
        print(json.dumps({
            'manifest_digest': manifest.digest(),
            'mode': 'run',
            'physical_authorized': False,
            'fault_profile': coerce_fault_profile(
                args.fault_profile
            ).value,
            'execution_profile': coerce_execution_profile(
                args.execution_profile
            ).value,
            'scenario_profile': coerce_scenario_profile(
                args.scenario_profile
            ).value,
            'safety_profile': coerce_safety_profile(
                args.safety_profile
            ).value,
            'simulation': True,
            'product_outcome': (
                manifest.receipt.product_outcome.value
            ),
            'test_status': manifest.receipt.test_status.value,
            'status': 'succeeded',
        }, ensure_ascii=True, sort_keys=True))
        return 0
    except BaseException as error:  # CLI security boundary
        print(json.dumps({
            'error_code': _safe_code(error),
            'status': 'failed',
        }, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
