"""Tests for the no-motion SWM25-131 scenario composition."""

from types import SimpleNamespace

import pytest

from malbut_agent_server.config import Settings
from malbut_scenarios import text_agent_server
from malbut_scenarios.text_agent_server import (
    ApprovedSimulationTextRuntime,
    _close_execution_runtime,
    _parser,
    build_approved_simulation_text_runtime,
    build_simulation_text_runtime,
)


class Catalog:
    device_id = 'malbut-sim-01'
    map_id = 'map-small-house'
    map_revision = 'revision-1'

    def __init__(self) -> None:
        self.resolve_calls = 0

    def resolve(self, location: str):
        self.resolve_calls += 1
        if location != '거실':
            raise ValueError('unknown target')
        return SimpleNamespace(
            room_name='거실',
            room_category='living_room',
            binding_digest='a' * 64,
        )


def test_scenario_composes_text_confirmation_without_nav2() -> None:
    catalog = Catalog()
    settings = Settings(
        provider='mock',
        auth_token='local-test-token',
        database_path=':memory:',
        tool_mode='proposal',
    )
    orchestrator, service = build_simulation_text_runtime(
        settings,
        lambda: catalog,
    )
    try:
        orchestrator.conversation_store.create(
            'local-user',
            'conversation-1',
        )
        proposal = service.handle(
            user_id='local-user',
            value={
                'request_id': 'request-1',
                'conversation_id': 'conversation-1',
                'turn_id': 'turn-1',
                'text': '거실로 가줘',
            },
        )
        approved = service.handle(
            user_id='local-user',
            value={
                'request_id': 'response-1',
                'conversation_id': 'conversation-1',
                'turn_id': 'turn-2',
                'text': '네',
            },
        )

        assert proposal['status'] == 'awaiting_confirmation'
        assert approved['status'] == 'approved'
        assert approved['execution']['nav2_start_count'] == 0
        assert approved['execution']['nav2_cancel_count'] == 0
        assert approved['execution']['physical_authorized'] is False
        assert service.create_robot_actions is False
        assert catalog.resolve_calls == 2
    finally:
        orchestrator.conversation_store.close()
        orchestrator.memory_store.close()


def test_scenario_rejects_unauthenticated_or_executable_tool_mode() -> None:
    catalog = Catalog()
    with pytest.raises(ValueError, match='AUTH_TOKEN'):
        build_simulation_text_runtime(
            Settings(auth_token='', tool_mode='proposal'),
            lambda: catalog,
        )
    with pytest.raises(ValueError, match='proposal Tool mode'):
        build_simulation_text_runtime(
            Settings(auth_token='token', tool_mode='simulation'),
            lambda: catalog,
        )


def test_execution_flags_are_explicit_and_default_off() -> None:
    parsed = _parser().parse_args([])

    assert parsed.execute_approved_simulation is False
    assert parsed.robot_web_url is None
    assert parsed.fault_profile == 'none'
    assert parsed.safety_profile == 'none'


def test_explicit_execution_composes_without_robot_web_io(
    monkeypatch,
    tmp_path,
) -> None:
    """Building dependencies must not bootstrap Robot Web or start Nav2."""
    def forbidden_request(*_args, **_kwargs):
        raise AssertionError('composition performed Robot Web I/O')

    monkeypatch.setattr(
        text_agent_server.RobotWebNavigationClient,
        '_request',
        forbidden_request,
    )
    catalog = Catalog()
    settings = Settings(
        provider='mock',
        auth_token='local-test-token',
        database_path=str(tmp_path / 'runtime.sqlite3'),
        tool_mode='proposal',
        port=8877,
    )

    runtime = build_approved_simulation_text_runtime(
        settings,
        lambda: catalog,
        robot_web_url='http://127.0.0.1:8765',
    )
    try:
        assert runtime.text_turn_service.create_robot_actions is True
        assert runtime.dispatcher.is_alive is False
        assert runtime.action_repository.find_by_confirmation(
            'never-approved'
        ) is None
    finally:
        runtime.action_repository.close()
        runtime.orchestrator.conversation_store.close()
        runtime.orchestrator.memory_store.close()


def test_execution_rejects_agent_robot_web_port_collision(tmp_path) -> None:
    settings = Settings(
        provider='mock',
        auth_token='local-test-token',
        database_path=str(tmp_path / 'runtime.sqlite3'),
        tool_mode='proposal',
        port=8765,
    )

    with pytest.raises(ValueError, match='port conflicts'):
        build_approved_simulation_text_runtime(
            settings,
            lambda: Catalog(),
            robot_web_url='http://127.0.0.1:8765',
        )


def test_execution_shutdown_order_drains_before_sqlite_close() -> None:
    events = []

    class Dispatcher:
        def close(self):
            events.append('dispatcher.close')

        def join(self, *, timeout):
            assert timeout > 120.0
            events.append('dispatcher.join')
            return True

    def owned(name):
        return SimpleNamespace(
            close=lambda: events.append(name + '.close')
        )

    runtime = ApprovedSimulationTextRuntime(
        orchestrator=SimpleNamespace(
            conversation_store=owned('conversation'),
            memory_store=owned('memory'),
        ),
        text_turn_service=object(),
        action_repository=owned('action_repository'),
        dispatcher=Dispatcher(),
    )
    server = SimpleNamespace(
        server_close=lambda: events.append('server.close')
    )

    _close_execution_runtime(runtime, server)

    assert events == [
        'dispatcher.close',
        'server.close',
        'dispatcher.join',
        'action_repository.close',
        'conversation.close',
        'memory.close',
    ]


def test_execution_check_validates_full_composition_without_starting_it(
    monkeypatch,
    tmp_path,
) -> None:
    events = []
    stores = []

    class RuntimeSettings:
        database_path = str(tmp_path / 'runtime.sqlite3')
        port = 8877

        @staticmethod
        def from_env(_source):
            return RuntimeSettings()

        def validate_for_server(self):
            return None

    class Source:
        def __init__(self, _path, _device_id):
            pass

        def load(self):
            events.append('catalog.load')
            return Catalog()

    class Dispatcher:
        def close(self):
            events.append('dispatcher.close')

        def join(self, *, timeout):
            assert timeout > 120.0
            events.append('dispatcher.join')
            return True

    def checked_execution(
        _settings,
        _loader,
        *,
        robot_web_url,
        scenario_profile,
        fault_profile,
        safety_profile,
        map_switch_callback,
    ):
        assert robot_web_url == 'http://127.0.0.1:8765'
        assert scenario_profile.value == 'happy_path'
        assert fault_profile.value == 'none'
        assert safety_profile.value == 'none'
        assert map_switch_callback is None
        events.append('approved_execution')
        return ApprovedSimulationTextRuntime(
            orchestrator=SimpleNamespace(
                conversation_store=SimpleNamespace(
                    close=lambda: stores.append('conversation')
                ),
                memory_store=SimpleNamespace(
                    close=lambda: stores.append('memory')
                ),
            ),
            text_turn_service=object(),
            action_repository=SimpleNamespace(
                close=lambda: stores.append('action_repository')
            ),
            dispatcher=Dispatcher(),
        )

    monkeypatch.setattr(text_agent_server, 'Settings', RuntimeSettings)
    monkeypatch.setattr(text_agent_server, 'load_env_file', lambda _p: None)
    monkeypatch.setattr(
        text_agent_server,
        'ActiveMapCatalogSource',
        Source,
    )
    monkeypatch.setattr(
        text_agent_server,
        'build_simulation_text_runtime',
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError('baseline composition must not be selected')
        ),
    )
    monkeypatch.setattr(
        text_agent_server,
        'build_approved_simulation_text_runtime',
        checked_execution,
    )

    result = text_agent_server.main([
        '--map-store', str(tmp_path),
        '--device-id', 'malbut-sim-01',
        '--execute-approved-simulation',
        '--check',
    ])

    assert result == 0
    assert events == [
        'catalog.load',
        'approved_execution',
        'dispatcher.close',
        'dispatcher.join',
    ]
    assert stores == ['action_repository', 'conversation', 'memory']


def test_parser_defaults_to_legacy_living_room_profile() -> None:
    assert _parser().parse_args([]).scenario_profile == 'happy_path'


def test_unknown_profile_is_rejected_before_environment_io(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        text_agent_server,
        'load_env_file',
        lambda _path: (_ for _ in ()).throw(
            AssertionError('invalid profile reached environment I/O')
        ),
    )

    with pytest.raises(SystemExit):
        text_agent_server.main(['--scenario-profile', '거실', '--check'])


def test_approved_runtime_pins_selected_location_before_robot_web_io(
    monkeypatch,
    tmp_path,
) -> None:
    locations = []

    class MultiRoomCatalog(Catalog):
        def resolve(self, location: str):
            locations.append(location)
            return SimpleNamespace(
                room_name=location,
                room_category='room',
                binding_digest='a' * 64,
            )

    monkeypatch.setattr(
        text_agent_server.RobotWebNavigationClient,
        '_request',
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError('composition performed Robot Web I/O')
        ),
    )
    settings = Settings(
        provider='mock',
        auth_token='local-test-token',
        database_path=str(tmp_path / 'runtime.sqlite3'),
        tool_mode='proposal',
        port=8877,
    )

    runtime = build_approved_simulation_text_runtime(
        settings,
        MultiRoomCatalog,
        robot_web_url='http://127.0.0.1:8765',
        scenario_profile='happy_kitchen',
    )
    try:
        assert locations == ['주방']
    finally:
        runtime.action_repository.close()
        runtime.orchestrator.conversation_store.close()
        runtime.orchestrator.memory_store.close()


def test_competing_worker_profile_owns_two_independent_connections(
    monkeypatch,
    tmp_path,
) -> None:
    """The race must cross SQLite connections, not one Python RLock."""
    monkeypatch.setattr(
        text_agent_server.RobotWebNavigationClient,
        '_request',
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError('composition performed Robot Web I/O')
        ),
    )
    settings = Settings(
        provider='mock',
        auth_token='local-test-token',
        database_path=str(tmp_path / 'runtime.sqlite3'),
        tool_mode='proposal',
        port=8877,
    )

    runtime = build_approved_simulation_text_runtime(
        settings,
        lambda: Catalog(),
        robot_web_url='http://127.0.0.1:8765',
        fault_profile='competing_workers',
    )
    try:
        assert len(runtime.action_repositories) == 2
        assert len(runtime.dispatchers) == 2
        assert (
            runtime.action_repositories[0]
            is not runtime.action_repositories[1]
        )
        assert runtime.worker_competition is not None
        assert all(
            dispatcher.is_alive is False
            for dispatcher in runtime.dispatchers
        )
    finally:
        _close_execution_runtime(runtime, None)


def test_dispatch_safety_profile_is_armed_without_composition_io(
    monkeypatch,
    tmp_path,
) -> None:
    """Keep the real state read lazy while composing the Safety wrappers."""
    monkeypatch.setattr(
        text_agent_server.RobotWebNavigationClient,
        '_request',
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError('composition performed Robot Web I/O')
        ),
    )
    settings = Settings(
        provider='mock',
        auth_token='local-test-token',
        database_path=str(tmp_path / 'runtime.sqlite3'),
        tool_mode='proposal',
        port=8877,
    )

    runtime = build_approved_simulation_text_runtime(
        settings,
        lambda: Catalog(),
        robot_web_url='http://127.0.0.1:8765',
        safety_profile='emergency_stop',
    )
    try:
        assert runtime.dispatch_safety_fault is not None
        assert runtime.dispatch_safety_fault.safety_profile.value == (
            'emergency_stop'
        )
        assert len(runtime.action_repositories) == 1
        assert len(runtime.dispatchers) == 1
        assert runtime.dispatcher.is_alive is False
    finally:
        _close_execution_runtime(runtime, None)


def test_map_revision_safety_requires_an_explicit_server_callback(
    tmp_path,
) -> None:
    settings = Settings(
        provider='mock',
        auth_token='local-test-token',
        database_path=str(tmp_path / 'runtime.sqlite3'),
        tool_mode='proposal',
        port=8877,
    )

    with pytest.raises(TypeError, match='switch callback'):
        build_approved_simulation_text_runtime(
            settings,
            lambda: Catalog(),
            robot_web_url='http://127.0.0.1:8765',
            safety_profile='map_revision_changed',
        )


def test_pressure_and_dispatch_safety_profiles_cannot_be_combined(
    tmp_path,
) -> None:
    settings = Settings(
        provider='mock',
        auth_token='local-test-token',
        database_path=str(tmp_path / 'runtime.sqlite3'),
        tool_mode='proposal',
        port=8877,
    )

    with pytest.raises(ValueError, match='cannot be combined'):
        build_approved_simulation_text_runtime(
            settings,
            lambda: Catalog(),
            robot_web_url='http://127.0.0.1:8765',
            fault_profile='duplicate_request',
            safety_profile='stale_state',
        )


def test_concurrent_approval_profile_wraps_only_the_target_resolver(
    monkeypatch,
    tmp_path,
) -> None:
    """Approval pressure keeps one worker and the production DB CAS."""
    monkeypatch.setattr(
        text_agent_server.RobotWebNavigationClient,
        '_request',
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError('composition performed Robot Web I/O')
        ),
    )
    settings = Settings(
        provider='mock',
        auth_token='local-test-token',
        database_path=str(tmp_path / 'runtime.sqlite3'),
        tool_mode='proposal',
        port=8877,
    )

    runtime = build_approved_simulation_text_runtime(
        settings,
        lambda: Catalog(),
        robot_web_url='http://127.0.0.1:8765',
        fault_profile='concurrent_approval',
    )
    try:
        assert len(runtime.action_repositories) == 1
        assert len(runtime.dispatchers) == 1
        assert runtime.worker_competition is None
        assert runtime.concurrent_approval_gate is not None
        assert runtime.concurrent_approval_gate.snapshot().contender_count == 0
    finally:
        _close_execution_runtime(runtime, None)


def test_two_worker_shutdown_joins_every_runtime_before_repositories() -> None:
    events = []

    class Competition:
        def close(self):
            events.append('competition.close')

    class Dispatcher:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(self.name + '.close')

        def join(self, *, timeout):
            assert timeout > 120.0
            events.append(self.name + '.join')
            return True

    def owned(name):
        return SimpleNamespace(
            close=lambda: events.append(name + '.close')
        )

    runtime = ApprovedSimulationTextRuntime(
        orchestrator=SimpleNamespace(
            conversation_store=owned('conversation'),
            memory_store=owned('memory'),
        ),
        text_turn_service=object(),
        action_repository=owned('repository-a'),
        dispatcher=Dispatcher('dispatcher-a'),
        additional_action_repositories=(owned('repository-b'),),
        additional_dispatchers=(Dispatcher('dispatcher-b'),),
        worker_competition=Competition(),
    )

    _close_execution_runtime(runtime, None)

    assert events == [
        'competition.close',
        'dispatcher-a.close',
        'dispatcher-b.close',
        'dispatcher-a.join',
        'dispatcher-b.join',
        'repository-a.close',
        'repository-b.close',
        'conversation.close',
        'memory.close',
    ]
