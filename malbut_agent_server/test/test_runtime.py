"""Tests for the bounded Mock-only runtime configuration."""

import pytest

from malbut_agent_server.cli import server_main
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator


def test_live_provider_is_rejected_until_swm25_72() -> None:
    """This stacked branch must remain network independent."""
    with pytest.raises(ValueError, match='offline mock'):
        Settings.from_env({'MALBUT_AGENT_PROVIDER': 'openai'})


def test_server_is_loopback_only() -> None:
    """Remote access belongs behind a separate authenticated proxy."""
    with pytest.raises(ValueError, match='loopback-only'):
        Settings(host='0.0.0.0').validate_for_server()


def test_auth_token_is_redacted() -> None:
    """Debug representations never reveal the local bearer token."""
    settings = Settings(auth_token='local-secret-token')
    rendered = repr(settings)
    assert 'local-secret-token' not in rendered
    assert 'auth_token=<redacted>' in rendered


def test_conversation_limits_are_bounded() -> None:
    """Unsafe context and session settings fail before startup."""
    with pytest.raises(ValueError):
        Settings.from_env(
            {'MALBUT_AGENT_CONVERSATION_HISTORY_LIMIT': '9'}
        )
    with pytest.raises(ValueError):
        Settings.from_env(
            {'MALBUT_AGENT_CONVERSATION_TTL_SECONDS': '59'}
        )
    with pytest.raises(ValueError):
        Settings.from_env(
            {'MALBUT_AGENT_CONVERSATION_SUMMARY_MAX_CHARS': '255'}
        )
    with pytest.raises(ValueError):
        Settings.from_env(
            {'MALBUT_AGENT_MAX_MODEL_INPUT_CHARS': '4095'}
        )

    settings = Settings.from_env(
        {
            'MALBUT_AGENT_MEMORY_LIMIT': '7',
            'MALBUT_AGENT_CONVERSATION_SUMMARY_MAX_CHARS': '2048',
            'MALBUT_AGENT_MAX_MODEL_INPUT_CHARS': '8192',
        }
    )
    assert settings.memory_limit == 7
    assert settings.conversation_summary_max_chars == 2048
    assert settings.max_model_input_chars == 8192


def test_factory_builds_only_mock_runtime() -> None:
    """Factory construction performs no external network request."""
    runtime = build_orchestrator(
        Settings(database_path=':memory:'),
    )
    try:
        assert runtime.provider.name == 'mock'
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


def test_cli_check_initializes_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator can validate local startup without listening."""
    monkeypatch.delenv('MALBUT_AGENT_PROVIDER', raising=False)
    assert server_main(
        ['--provider', 'mock', '--database', ':memory:', '--check']
    ) == 0
