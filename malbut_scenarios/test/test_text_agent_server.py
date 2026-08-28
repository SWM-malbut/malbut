"""Tests for the no-motion SWM25-131 scenario composition."""

from types import SimpleNamespace

import pytest

from malbut_agent_server.config import Settings
from malbut_scenarios.text_agent_server import (
    build_simulation_text_runtime,
)


class Catalog:
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
