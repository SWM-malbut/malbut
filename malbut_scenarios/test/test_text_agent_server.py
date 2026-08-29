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

    def checked_execution(_settings, _loader, *, robot_web_url):
        assert robot_web_url == 'http://127.0.0.1:8765'
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
