"""Tests for the default-off Gazebo monitor-room runtime composition."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
from threading import Event, Thread as _TestThread
from types import SimpleNamespace

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.utilities import perform_substitutions
from launch_ros.actions import Node as LaunchNode
import pytest

import malbut_agent_server.homecam_semantic as homecam_semantic_module
from malbut_agent_server.homecam_semantic import (
    VerifiedSemanticSnapshotEvidence,
)
from malbut_agent_server.monitor_room_target import TrustedSemanticSnapshot
import malbut_gazebo.gazebo_monitor_room_runtime as runtime_module
from malbut_gazebo.gazebo_monitor_room_runtime import (
    GazeboMonitorRoomRuntimeError,
    build_gazebo_monitor_room_runtime,
    load_gazebo_monitor_room_runtime_config,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text(
        encoding='ascii'
    ).strip().lower()


def _enabled_value(root: Path) -> dict:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    private = root / 'private'
    private.mkdir(mode=0o700, parents=True)
    map_store = private / 'maps'
    map_store.mkdir(mode=0o700)
    return {
        'schema_version': 1,
        'enabled': True,
        'robot_id': 'gazebo-robot-1',
        'worker_id': 'monitor-room-worker-1',
        'expected_agent_uid': os.geteuid(),
        'host_boot_id': _boot_id(),
        'map_store_path': str(map_store),
        'map_store_owner_uid': os.geteuid(),
        'expected_map_id': 'home-map',
        'expected_map_revision': 'map-revision-1',
        'core_database_path': str(private / 'core.sqlite3'),
        'gateway_replay_database_path': str(private / 'replay.sqlite3'),
        'prepare_socket_path': str(private / 'prepare.sock'),
        'gateway_socket_path': str(private / 'command.sock'),
        'homecam_origin': 'https://homecam.example.test',
        'homecam_service_token': 'a' * 43,
        'homecam_envelope_signing_secret': 'b' * 43,
        'homecam_agent_user_id': 'user-1',
        'homecam_principal_subject_digest': 'c' * 64,
        'homecam_device_id': 'gazebo-robot-1',
        'homecam_timeout_seconds': 2,
        'lease_seconds': 5.0,
        'socket_timeout_seconds': 2.0,
        'nav2_response_timeout_seconds': 2.0,
        'nav2_cancel_timeout_seconds': 2.0,
    }


def _write_config(path: Path, value: dict, *, mode: int = 0o600) -> Path:
    path.write_text(
        json.dumps(value, sort_keys=True),
        encoding='utf-8',
    )
    path.chmod(mode)
    return path


def _enabled_config(tmp_path: Path):
    value = _enabled_value(tmp_path)
    path = _write_config(tmp_path / 'runtime.json', value)
    return load_gazebo_monitor_room_runtime_config(path), path, value


def _verified_semantic_evidence():
    snapshot = TrustedSemanticSnapshot(
        device_id='gazebo-robot-1',
        device_binding_revision='d' * 64,
        source_revision='srv-1-0123456789abcdef',
        map_id='home-map',
        map_revision='map-revision-1',
        semantic_revision='e' * 64,
        frame_id='map',
        zones_digest=hashlib.sha256(b'null').hexdigest(),
        rooms=(),
    )
    return VerifiedSemanticSnapshotEvidence(
        snapshot=snapshot,
        content_sha256='f' * 64,
        map_generation=1,
        authorization_generation=1,
        expires_at_ms=1_000_000,
        _zones=None,
        _construction_token=(
            homecam_semantic_module._EVIDENCE_CONSTRUCTION_TOKEN
        ),
    )


class _FakeServer:
    def __init__(self, socket_path, trace, name):
        self.socket_path = socket_path
        self.trace = trace
        self.name = name
        self.started = 0
        self.served = 0
        self.closed = 0
        self.entered = Event()
        self.release = Event()
        self.block_start = Event()
        self.start_entered = Event()
        self.start_release = Event()
        self.block_close = Event()
        self.close_entered = Event()
        self.close_release = Event()
        self.second_close_entered = Event()
        self.close_entries = []

    def start(self):
        if self.block_start.is_set():
            self.start_entered.set()
            self.start_release.wait(2.0)
        self.started += 1
        self.trace.append(f'{self.name}.start')

    def serve_forever(self):
        self.served += 1
        self.trace.append(f'{self.name}.serve')
        self.entered.set()
        self.release.wait(2.0)

    def close(self):
        self.close_entries.append(object())
        self.close_entered.set()
        if len(self.close_entries) > 1:
            self.second_close_entered.set()
        if self.block_close.is_set():
            self.start_release.set()
            self.close_release.wait(2.0)
        self.closed += 1
        self.trace.append(f'{self.name}.close')
        self.release.set()


def _install_fake_composition(monkeypatch, trace):
    state = SimpleNamespace(
        semantic_fetch_calls=0,
        semantic_evidence=None,
        prepare_calls=0,
        drive_calls=0,
        goal_calls=0,
        cancel_calls=0,
        homecam_start_calls=0,
        homecam_stop_calls=0,
        servers=[],
        resources=[],
    )

    class FakeStore:
        def __init__(self, path, *, boot_id_reader):
            trace.append('store.construct')
            assert path.endswith('core.sqlite3')
            assert boot_id_reader() == _boot_id()
            self.store_namespace = '1' * 32
            self.closed = 0
            state.resources.append(self)

        def close(self):
            self.closed += 1
            trace.append('store.close')

    class FakePrepareProcessor:
        def __init__(
            self, store, *, expected_robot_id, local_boot_id
        ):
            trace.append('prepare_processor.construct')
            self.store = store
            self.expected_robot_id = expected_robot_id
            self.local_boot_id = local_boot_id

        def prepare(self, _request):
            state.prepare_calls += 1
            raise AssertionError

    class FakePrepareServer(_FakeServer):
        def __init__(
            self,
            processor,
            socket_path,
            *,
            expected_agent_uid,
            timeout_seconds,
        ):
            trace.append('prepare_server.construct')
            assert processor.expected_robot_id == 'gazebo-robot-1'
            assert expected_agent_uid == os.geteuid()
            assert timeout_seconds == 2.0
            super().__init__(socket_path, trace, 'prepare')
            state.servers.append(self)

    class FakeActiveMapConfig:
        def __init__(self, *, map_store_path, owner_uid):
            trace.append('active_map_config.construct')
            self.map_store_path = map_store_path
            self.owner_uid = owner_uid

    class FakeProjection:
        def __init__(self):
            self.active_map_evidence = SimpleNamespace(
                map_id='home-map',
                map_revision='map-revision-1',
            )

    class FakeActiveMapResolver:
        def __init__(self, config):
            trace.append('active_map.construct')
            self.config = config

        def resolve_static_navigation_projection(self):
            trace.append('active_map.project')
            return FakeProjection()

    class FakeHomecamConfig:
        def __init__(
            self,
            *,
            origin,
            service_token,
            envelope_signing_secret,
            agent_user_id,
            principal_subject_digest,
            device_id,
            timeout_seconds,
        ):
            trace.append('homecam_config.construct')
            self.origin = origin
            self.service_token = service_token
            self.envelope_signing_secret = envelope_signing_secret
            self.agent_user_id = agent_user_id
            self.principal_subject_digest = principal_subject_digest
            self.device_id = device_id
            self.timeout_seconds = timeout_seconds

    class FakeSemanticResolver:
        def __init__(self, config):
            trace.append('semantic_resolver.construct')
            self.config = config

        def fetch_snapshot_evidence(self):
            state.semantic_fetch_calls += 1
            if state.semantic_evidence is not None:
                return state.semantic_evidence
            raise AssertionError('semantic fetch is request-driven only')

        def start_stream(self):
            state.homecam_start_calls += 1
            raise AssertionError

        def stop_stream(self):
            state.homecam_stop_calls += 1
            raise AssertionError

    class FakeLiveFacade:
        def __init__(self, node):
            trace.append('live_facade.construct')
            self.node = node

    class FakeLiveSource:
        def __init__(self, facade):
            trace.append('live_source.construct')
            self.facade = facade

    class FakeValidator:
        def __init__(self, store, semantic, active_map, live_source):
            trace.append('validator.construct')
            self.store = store
            self.semantic = semantic
            self.active_map = active_map
            self.live_source = live_source

    class FakeNav2Port:
        def __init__(
            self,
            node,
            *,
            validator,
            response_timeout_seconds,
            cancel_timeout_seconds,
        ):
            trace.append('nav2_port.construct')
            self.node = node
            self.validator = validator
            self.closed = 0
            assert response_timeout_seconds == 2.0
            assert cancel_timeout_seconds == 2.0
            state.resources.append(self)

        def ensure_started(self, _request):
            state.goal_calls += 1
            raise AssertionError

        def cancel_goal(self, _request):
            state.cancel_calls += 1
            raise AssertionError

        def close(self):
            self.closed += 1
            trace.append('nav2_port.close')

    class FakeController:
        def __init__(
            self,
            store,
            port,
            *,
            worker_id,
            lease_seconds,
        ):
            trace.append('controller.construct')
            self.store = store
            self.port = port
            self.worker_id = worker_id
            assert lease_seconds == 5.0

        def drive_once(self, _operation_id):
            state.drive_calls += 1
            raise AssertionError

    class FakeReplayStore:
        def __init__(self, path, *, core_store_namespace):
            trace.append('replay.construct')
            assert path.endswith('replay.sqlite3')
            assert core_store_namespace == '1' * 32
            self.closed = 0
            state.resources.append(self)

        def close(self):
            self.closed += 1
            trace.append('replay.close')

    class FakeGatewayProcessor:
        def __init__(self, store, controller, replay):
            trace.append('gateway_processor.construct')
            self.store = store
            self.controller = controller
            self.replay = replay

    class FakeGatewayServer(_FakeServer):
        def __init__(
            self,
            processor,
            socket_path,
            *,
            expected_agent_uid,
            timeout_seconds,
        ):
            trace.append('gateway_server.construct')
            assert expected_agent_uid == os.geteuid()
            assert timeout_seconds == 2.0
            self.processor = processor
            super().__init__(socket_path, trace, 'gateway')
            state.servers.append(self)

    replacements = {
        'GazeboMonitorRoomStore': FakeStore,
        'GazeboMonitorRoomPrepareProcessor': FakePrepareProcessor,
        'GazeboMonitorRoomPrepareServer': FakePrepareServer,
        'ActiveMapResolverConfig': FakeActiveMapConfig,
        'ActiveMapStaticNavigationProjection': FakeProjection,
        'ActiveMapEvidenceResolver': FakeActiveMapResolver,
        'HomecamSemanticConfig': FakeHomecamConfig,
        'AuthenticatedHomecamSemanticResolver': FakeSemanticResolver,
        'GazeboMonitorRoomRclpyLiveRosFacade': FakeLiveFacade,
        'GazeboMonitorRoomLiveRosSource': FakeLiveSource,
        'GazeboMonitorRoomLiveValidator': FakeValidator,
        'GazeboMonitorRoomNav2RosPort': FakeNav2Port,
        'GazeboMonitorRoomNav2Controller': FakeController,
        'GazeboMonitorRoomGatewayReplayStore': FakeReplayStore,
        'GazeboMonitorRoomGatewayProcessor': FakeGatewayProcessor,
        'GazeboMonitorRoomGatewayServer': FakeGatewayServer,
    }
    for name, replacement in replacements.items():
        monkeypatch.setattr(runtime_module, name, replacement)
    return state


def test_disabled_config_is_exact_and_builds_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """The shipped safe state cannot instantiate any runtime collaborator."""
    path = _write_config(
        tmp_path / 'disabled.json',
        {'schema_version': 1, 'enabled': False},
    )
    config = load_gazebo_monitor_room_runtime_config(path)
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append('constructed')
        raise AssertionError

    monkeypatch.setattr(runtime_module, 'GazeboMonitorRoomStore', forbidden)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        build_gazebo_monitor_room_runtime(config, object())

    assert captured.value.code == 'runtime_disabled'
    assert calls == []


def test_enabled_config_requires_private_exact_current_file(
    tmp_path: Path,
) -> None:
    """Permissions, duplicate/extra keys, and post-load drift fail closed."""
    value = _enabled_value(tmp_path)
    public_path = _write_config(
        tmp_path / 'public.json', value, mode=0o644
    )
    with pytest.raises(GazeboMonitorRoomRuntimeError):
        load_gazebo_monitor_room_runtime_config(public_path)

    path = _write_config(tmp_path / 'runtime.json', value)
    config = load_gazebo_monitor_room_runtime_config(path)
    assert config.enabled is True
    assert config.robot_id == config.homecam_device_id
    path.chmod(0o640)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        config.assert_current()
    assert captured.value.code == 'runtime_config_changed'


@pytest.mark.parametrize(
    'change',
    [
        lambda value: value.update({'unexpected': True}),
        lambda value: value.update({'robot_id': 'other-device'}),
        lambda value: value.update({'host_boot_id': '0' * 36}),
        lambda value: value.update({
            'gateway_socket_path': value['prepare_socket_path']
        }),
    ],
)
def test_enabled_config_rejects_untrusted_binding_variants(
    tmp_path: Path, change
) -> None:
    """Robot/device, boot, path, and exact-schema bindings are mandatory."""
    value = _enabled_value(tmp_path)
    change(value)
    path = _write_config(tmp_path / 'runtime.json', value)
    with pytest.raises(GazeboMonitorRoomRuntimeError):
        load_gazebo_monitor_room_runtime_config(path)


def test_construction_and_startup_have_zero_actuation_or_stream_calls(
    tmp_path: Path, monkeypatch
) -> None:
    """Only explicit later gateway commands may reach controller methods."""
    config, _path, _value = _enabled_config(tmp_path)
    trace = []
    state = _install_fake_composition(monkeypatch, trace)

    runtime = build_gazebo_monitor_room_runtime(config, object())

    assert state.semantic_fetch_calls == 0
    assert state.prepare_calls == 0
    assert state.drive_calls == 0
    assert state.goal_calls == 0
    assert state.cancel_calls == 0
    assert state.homecam_start_calls == 0
    assert state.homecam_stop_calls == 0
    assert 'active_map.project' in trace
    assert not any(item.endswith('.start') for item in trace)

    runtime.start()
    assert all(server.entered.wait(1.0) for server in state.servers)
    runtime.assert_healthy()

    assert [server.started for server in state.servers] == [1, 1]
    assert [server.served for server in state.servers] == [1, 1]
    assert state.semantic_fetch_calls == 0
    assert state.prepare_calls == 0
    assert state.drive_calls == 0
    assert state.goal_calls == 0
    assert state.cancel_calls == 0
    assert state.homecam_start_calls == 0
    assert state.homecam_stop_calls == 0

    runtime.close()
    assert all(server.closed == 1 for server in state.servers)
    assert all(resource.closed == 1 for resource in state.resources)
    assert trace.index('gateway.close') < trace.index('nav2_port.close')
    assert trace.index('prepare.close') < trace.index('nav2_port.close')


def test_config_drift_prevents_socket_start_and_every_command(
    tmp_path: Path, monkeypatch
) -> None:
    """A changed protected file is rejected before either listener starts."""
    config, path, value = _enabled_config(tmp_path)
    trace = []
    state = _install_fake_composition(monkeypatch, trace)
    runtime = build_gazebo_monitor_room_runtime(config, object())

    value['worker_id'] = 'changed-worker'
    _write_config(path, value)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime.start()

    assert captured.value.code == 'runtime_config_changed'
    assert all(server.started == 0 for server in state.servers)
    assert state.drive_calls == 0
    assert state.goal_calls == 0
    assert state.cancel_calls == 0
    runtime.close()


def test_object_level_config_or_component_replacement_cannot_start(
    tmp_path: Path, monkeypatch
) -> None:
    """Low-level attribute replacement is fenced before listener startup."""
    config, _path, _value = _enabled_config(tmp_path)
    object.__setattr__(config, 'worker_id', 'replacement-worker')
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        config.assert_current()
    assert captured.value.code == 'runtime_config_changed'

    config, _path, _value = _enabled_config(tmp_path / 'second')
    trace = []
    state = _install_fake_composition(monkeypatch, trace)
    runtime = build_gazebo_monitor_room_runtime(config, object())
    semantic_source = runtime._components.semantic_evidence_source
    object.__setattr__(semantic_source._resolver, 'replacement', object())
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime.start()
    assert captured.value.code == 'runtime_binding_invalid'
    assert all(server.started == 0 for server in state.servers)
    del semantic_source._resolver.__dict__['replacement']
    replacement = _FakeServer('/private/replacement.sock', trace, 'bad')
    object.__setattr__(runtime._components, 'gateway_server', replacement)

    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime.start()
    assert captured.value.code == 'runtime_binding_invalid'
    assert replacement.started == 0
    assert all(server.started == 0 for server in state.servers)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    assert captured.value.code == 'runtime_binding_invalid'
    object.__setattr__(
        runtime._components, 'gateway_server', state.servers[1]
    )
    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    assert replacement.closed == 0
    assert all(server.closed == 1 for server in state.servers)


def test_trusted_method_shadows_never_execute_before_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    """Every runtime-owned call bypasses and detects instance shadows."""
    config, _path, _value = _enabled_config(tmp_path)
    poison_calls = []

    def poison(*_args, **_keywords):
        poison_calls.append('called')
        raise AssertionError

    object.__setattr__(config, 'assert_current', poison)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        config.assert_current()
    assert captured.value.code == 'runtime_config_changed'
    assert poison_calls == []
    del config.__dict__['assert_current']

    trace = []
    state = _install_fake_composition(monkeypatch, trace)
    runtime = build_gazebo_monitor_room_runtime(config, object())
    object.__setattr__(runtime, 'start', poison)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime.start()
    assert captured.value.code == 'runtime_binding_invalid'
    assert poison_calls == []
    del runtime.__dict__['start']

    prepare = state.servers[0]
    object.__setattr__(prepare, 'start', poison)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime_module._RUNTIME_START_UNBOUND(runtime)
    assert captured.value.code == 'runtime_worker_failed'
    assert poison_calls == []
    del prepare.__dict__['start']
    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    assert poison_calls == []
    assert all(server.closed == 1 for server in state.servers)
    assert all(resource.closed == 1 for resource in state.resources)


def test_semantic_seal_shadow_is_rejected_without_execution(
    tmp_path: Path, monkeypatch
) -> None:
    """Semantic attestation uses its externally captured seal function."""
    config, _path, _value = _enabled_config(tmp_path)
    trace = []
    state = _install_fake_composition(monkeypatch, trace)
    runtime = build_gazebo_monitor_room_runtime(config, object())
    source = runtime._components.semantic_evidence_source
    poison_calls = []

    def poison(*_arguments, **_keywords):
        poison_calls.append('called')
        raise AssertionError('private poison detail')

    object.__setattr__(source, '_seal_value', poison)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime_module._RUNTIME_START_UNBOUND(runtime)

    assert captured.value.code == 'runtime_binding_invalid'
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert poison_calls == []
    assert all(server.started == 0 for server in state.servers)
    del source.__dict__['_seal_value']
    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)


def test_semantic_canonical_copy_shadow_is_bypassed_and_errors_are_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    """Evidence canonicalization never dispatches through its instance."""
    config, _path, _value = _enabled_config(tmp_path)
    trace = []
    state = _install_fake_composition(monkeypatch, trace)
    runtime = build_gazebo_monitor_room_runtime(config, object())
    source = runtime._components.semantic_evidence_source
    evidence = _verified_semantic_evidence()
    state.semantic_evidence = evidence
    poison_calls = []

    def poison(*_arguments, **_keywords):
        poison_calls.append('called')
        raise AssertionError('private poison detail')

    object.__setattr__(evidence, 'canonical_copy', poison)
    canonical = source.fetch_snapshot_evidence()

    assert type(canonical) is VerifiedSemanticSnapshotEvidence
    assert canonical is not evidence
    assert poison_calls == []
    object.__setattr__(evidence, 'content_sha256', 'private-invalid')
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        source.fetch_snapshot_evidence()
    assert captured.value.code == 'runtime_binding_invalid'
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert str(captured.value) == 'runtime_binding_invalid'
    assert poison_calls == []
    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)


def test_close_and_resource_method_shadows_retain_dependencies(
    tmp_path: Path, monkeypatch
) -> None:
    """Poisoned close attributes cannot run or race worker dependencies."""
    config, _path, _value = _enabled_config(tmp_path)
    trace = []
    state = _install_fake_composition(monkeypatch, trace)
    runtime = build_gazebo_monitor_room_runtime(config, object())
    runtime_module._RUNTIME_START_UNBOUND(runtime)
    assert all(server.entered.wait(1.0) for server in state.servers)
    poison_calls = []

    def poison(*_args, **_keywords):
        poison_calls.append('called')
        raise AssertionError

    gateway = state.servers[1]
    object.__setattr__(gateway, 'close', poison)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    assert captured.value.code == 'runtime_worker_failed'
    assert poison_calls == []
    assert all(resource.closed == 0 for resource in state.resources)
    del gateway.__dict__['close']

    nav2_port = state.resources[1]
    object.__setattr__(nav2_port, 'close', poison)
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    assert captured.value.code == 'runtime_worker_failed'
    assert poison_calls == []
    assert all(server.closed == 1 for server in state.servers)
    assert all(resource.closed == 0 for resource in state.resources)
    del nav2_port.__dict__['close']

    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    assert poison_calls == []
    assert all(resource.closed == 1 for resource in state.resources)


def test_second_thread_start_failure_rolls_back_once(
    tmp_path: Path, monkeypatch
) -> None:
    """A partial worker barrier closes listeners and every dependency once."""
    config, _path, _value = _enabled_config(tmp_path)
    trace = []
    state = _install_fake_composition(monkeypatch, trace)

    class BarrierThread:
        created = 0

        def __init__(self, *, target, args, name, daemon):
            self.target = target
            self.args = args
            self.name = name
            self.daemon = daemon
            self.index = BarrierThread.created
            BarrierThread.created += 1
            self.alive = False
            self.start_calls = 0
            self.join_calls = 0

        def start(self):
            self.start_calls += 1
            if self.index == 1:
                raise RuntimeError('private thread failure')
            self.alive = True

        def join(self, timeout=None):
            assert timeout == 2.0
            self.join_calls += 1
            if self.args[1].release.is_set():
                self.alive = False

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(runtime_module, 'Thread', BarrierThread)
    runtime = build_gazebo_monitor_room_runtime(config, object())

    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime_module._RUNTIME_START_UNBOUND(runtime)

    assert captured.value.code == 'runtime_worker_failed'
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert [server.started for server in state.servers] == [1, 1]
    assert [server.closed for server in state.servers] == [1, 1]
    assert all(resource.closed == 1 for resource in state.resources)
    workers = [handle.thread for handle in runtime._threads]
    assert [worker.start_calls for worker in workers] == [1, 1]
    assert [worker.join_calls for worker in workers] == [1, 0]
    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    assert [server.closed for server in state.servers] == [1, 1]
    assert all(resource.closed == 1 for resource in state.resources)


def test_live_workers_block_dependency_close_until_retry(
    tmp_path: Path, monkeypatch
) -> None:
    """A bounded close retains Nav2 and stores until every worker exits."""
    config, _path, _value = _enabled_config(tmp_path)
    trace = []
    state = _install_fake_composition(monkeypatch, trace)

    class StubbornThread:
        allow_exit = False

        def __init__(self, *, target, args, name, daemon):
            self.target = target
            self.args = args
            self.name = name
            self.daemon = daemon
            self.alive = False
            self.start_calls = 0
            self.join_calls = 0

        def start(self):
            self.start_calls += 1
            self.alive = True

        def join(self, timeout=None):
            assert timeout == 2.0
            self.join_calls += 1
            if StubbornThread.allow_exit:
                self.alive = False

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(runtime_module, 'Thread', StubbornThread)
    runtime = build_gazebo_monitor_room_runtime(config, object())
    runtime_module._RUNTIME_START_UNBOUND(runtime)

    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)

    assert captured.value.code == 'runtime_worker_failed'
    assert [server.closed for server in state.servers] == [1, 1]
    assert all(resource.closed == 0 for resource in state.resources)

    StubbornThread.allow_exit = True
    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)

    assert [server.closed for server in state.servers] == [1, 1]
    assert all(resource.closed == 1 for resource in state.resources)


def test_start_and_close_are_one_linearized_lifecycle(
    tmp_path: Path, monkeypatch
) -> None:
    """A close racing startup cannot be followed by a started commit."""
    config, _path, _value = _enabled_config(tmp_path)
    trace = []
    state = _install_fake_composition(monkeypatch, trace)
    runtime = build_gazebo_monitor_room_runtime(config, object())
    prepare = state.servers[0]
    prepare.block_start.set()
    start_errors = []
    close_errors = []
    close_called = Event()
    close_done = Event()

    def run_start():
        try:
            runtime_module._RUNTIME_START_UNBOUND(runtime)
        except BaseException as error:
            start_errors.append(error)

    def run_close():
        close_called.set()
        try:
            runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_done.set()

    starter = _TestThread(target=run_start)
    starter.start()
    assert prepare.start_entered.wait(1.0)
    closer = _TestThread(target=run_close)
    closer.start()
    assert close_called.wait(1.0)
    assert not close_done.wait(0.1)

    prepare.start_release.set()
    starter.join(3.0)
    closer.join(3.0)

    assert not starter.is_alive()
    assert not closer.is_alive()
    assert start_errors == []
    assert close_errors == []
    assert runtime._closed is True
    assert runtime._started is False
    assert [server.started for server in state.servers] == [1, 1]
    assert [server.closed for server in state.servers] == [1, 1]
    assert all(resource.closed == 1 for resource in state.resources)
    assert state.drive_calls == 0
    assert state.goal_calls == 0
    assert state.cancel_calls == 0
    assert state.homecam_start_calls == 0
    assert state.homecam_stop_calls == 0
    with pytest.raises(GazeboMonitorRoomRuntimeError) as captured:
        runtime_module._RUNTIME_HEALTH_UNBOUND(runtime)
    assert captured.value.code == 'runtime_closed'


def test_concurrent_close_calls_are_once_only(
    tmp_path: Path, monkeypatch
) -> None:
    """Concurrent closers serialize before reading lifecycle indexes."""
    config, _path, _value = _enabled_config(tmp_path)
    trace = []
    state = _install_fake_composition(monkeypatch, trace)
    runtime = build_gazebo_monitor_room_runtime(config, object())
    runtime_module._RUNTIME_START_UNBOUND(runtime)
    assert all(server.entered.wait(1.0) for server in state.servers)
    gateway = state.servers[1]
    gateway.block_close.set()
    errors = []
    second_called = Event()

    def run_close(*, second=False):
        if second:
            second_called.set()
        try:
            runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
        except BaseException as error:
            errors.append(error)

    first = _TestThread(target=run_close)
    first.start()
    assert gateway.close_entered.wait(1.0)
    second = _TestThread(target=run_close, kwargs={'second': True})
    second.start()
    assert second_called.wait(1.0)
    assert not gateway.second_close_entered.wait(0.1)

    gateway.close_release.set()
    first.join(3.0)
    second.join(3.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert [server.closed for server in state.servers] == [1, 1]
    assert [len(server.close_entries) for server in state.servers] == [1, 1]
    assert all(resource.closed == 1 for resource in state.resources)
    assert state.drive_calls == 0
    assert state.goal_calls == 0
    assert state.cancel_calls == 0
    assert state.homecam_start_calls == 0
    assert state.homecam_stop_calls == 0
    runtime_module._RUNTIME_CLOSE_UNBOUND(runtime)
    assert [server.closed for server in state.servers] == [1, 1]
    assert all(resource.closed == 1 for resource in state.resources)


def test_launch_and_installed_template_are_default_off() -> None:
    """The launch graph contains one conditionally absent runtime node."""
    launch_path = (
        PACKAGE_ROOT / 'launch' / 'gazebo_monitor_room_runtime.launch.py'
    )
    spec = importlib.util.spec_from_file_location(
        'malbut_gazebo_monitor_room_runtime_launch', launch_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()
    context = LaunchContext()
    defaults = {
        entity.name: perform_substitutions(context, entity.default_value)
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    assert defaults == {'enabled': 'false', 'config_file': ''}
    nodes = [
        entity for entity in description.entities
        if isinstance(entity, LaunchNode)
    ]
    assert len(nodes) == 1
    node = nodes[0]
    assert node.node_executable == 'gazebo_monitor_room_runtime'
    assert isinstance(node.condition, IfCondition)
    assert node._ExecuteLocal__respawn is False
    disabled = json.loads(
        (
            PACKAGE_ROOT
            / 'config'
            / 'gazebo_monitor_room_runtime.disabled.json'
        ).read_text(encoding='utf-8')
    )
    assert disabled == {'schema_version': 1, 'enabled': False}


def test_setup_installs_runtime_entry_point_and_launch_config_assets() -> None:
    """The ament package ships the executable, launch, and safe template."""
    setup_source = (PACKAGE_ROOT / 'setup.py').read_text(encoding='utf-8')
    assert (
        'gazebo_monitor_room_runtime = '
        in setup_source
    )
    assert "'launch'" in setup_source
    assert "'config'" in setup_source


def test_disabled_main_returns_without_initializing_ros(
    tmp_path: Path, monkeypatch
) -> None:
    """Even direct invocation with a disabled file performs no ROS setup."""
    path = _write_config(
        tmp_path / 'disabled.json',
        {'schema_version': 1, 'enabled': False},
    )
    calls = []
    monkeypatch.setattr(
        runtime_module.rclpy,
        'init',
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert runtime_module._main(['--config', str(path)]) == 0
    assert calls == []
