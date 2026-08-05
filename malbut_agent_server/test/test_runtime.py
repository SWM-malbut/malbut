"""Tests for the bounded Mock-only runtime configuration."""

import pytest

from malbut_agent_server.cli import server_main
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator, build_provider
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
)
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.schemas import (
    AgentDecision,
    ProviderResult,
    ProviderUsage,
    ValidationError,
)


def test_openai_mode_requires_key_and_local_http_auth() -> None:
    """Live inference cannot start anonymously or without a key."""
    without_secrets = Settings.from_env(
        {'MALBUT_AGENT_PROVIDER': 'openai'}
    )
    with pytest.raises(ValueError, match='AUTH_TOKEN'):
        without_secrets.validate_for_server()

    without_key = Settings.from_env(
        {
            'MALBUT_AGENT_PROVIDER': 'openai',
            'MALBUT_AGENT_AUTH_TOKEN': 'local-http-token',
        }
    )
    with pytest.raises(ValueError, match='OPENAI_API_KEY'):
        without_key.validate_for_server()

    configured = Settings.from_env(
        {
            'MALBUT_AGENT_PROVIDER': 'openai',
            'MALBUT_AGENT_AUTH_TOKEN': 'local-http-token',
            'OPENAI_API_KEY': 'test-only-openai-key',
        }
    )
    configured.validate_for_server()
    assert configured.openai_model == 'gpt-5.6-terra'
    assert configured.request_timeout_seconds == 5
    assert configured.provider_total_timeout_seconds == 11
    assert configured.provider_max_retries == 0


def test_server_is_loopback_only() -> None:
    """Remote access belongs behind a separate authenticated proxy."""
    with pytest.raises(ValueError, match='loopback-only'):
        Settings(host='0.0.0.0').validate_for_server()


def test_credentials_are_redacted() -> None:
    """Debug representations never reveal either bearer credential."""
    settings = Settings(
        auth_token='local-secret-token',
        openai_api_key='sk-test-secret-openai-key',
    )
    rendered = repr(settings)
    assert 'local-secret-token' not in rendered
    assert 'sk-test-secret-openai-key' not in rendered
    assert 'auth_token=<redacted>' in rendered
    assert 'openai_api_key=<redacted>' in rendered


def test_provider_metadata_rejects_non_finite_or_negative_values() -> None:
    """Untrusted adapter metadata cannot emit non-standard JSON."""
    with pytest.raises(ValidationError):
        ProviderResult(
            decision=AgentDecision(type='message', message='ok'),
            provider='openai',
            model='test-model',
            latency_ms=float('nan'),
        ).to_dict()
    with pytest.raises(ValidationError):
        ProviderResult(
            decision=AgentDecision(type='message', message='ok'),
            provider='openai',
            model='test-model',
            latency_ms=1.0,
            usage=ProviderUsage(input_tokens=-1),
        ).to_dict()


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
    with pytest.raises(ValueError):
        Settings.from_env(
            {'MALBUT_AGENT_PROVIDER_MAX_RETRIES': '4'}
        )
    with pytest.raises(ValueError):
        Settings.from_env(
            {'OPENAI_MAX_OUTPUT_TOKENS': '63'}
        )
    with pytest.raises(ValueError, match='total timeout'):
        Settings.from_env(
            {
                'MALBUT_AGENT_TIMEOUT_SECONDS': '5',
                'MALBUT_AGENT_PROVIDER_TOTAL_TIMEOUT_SECONDS': '4',
            }
        ).validate_for_server()

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


def test_factory_builds_mock_runtime_without_wrapper() -> None:
    """Mock remains deterministic and avoids reliability/network layers."""
    runtime = build_orchestrator(
        Settings(database_path=':memory:'),
    )
    try:
        assert runtime.provider.name == 'mock'
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


def test_factory_builds_openai_primary_and_optional_model_fallback() -> None:
    """Both OpenAI models share one normalized reliability boundary."""
    provider = build_provider(
        Settings(
            provider='openai',
            openai_api_key='test-only-openai-key',
            openai_model='gpt-5.6-luna',
            openai_fallback_model='gpt-5.6-terra',
        )
    )
    assert isinstance(provider, ReliableProvider)
    assert len(provider._providers) == 2
    assert all(
        isinstance(item, OpenAIResponsesProvider)
        for item in provider._providers
    )
    assert [item.model for item in provider._providers] == [
        'gpt-5.6-luna',
        'gpt-5.6-terra',
    ]


def test_cli_check_initializes_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator can validate local startup without listening."""
    monkeypatch.delenv('MALBUT_AGENT_PROVIDER', raising=False)
    assert server_main(
        ['--provider', 'mock', '--database', ':memory:', '--check']
    ) == 0


def test_cli_openai_check_does_not_make_network_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI config can be checked safely before the first paid call."""
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-openai-key')
    monkeypatch.setenv(
        'MALBUT_AGENT_AUTH_TOKEN',
        'local-http-token',
    )
    assert server_main(
        [
            '--provider',
            'openai',
            '--model',
            'gpt-5.6-luna',
            '--database',
            ':memory:',
            '--check',
        ]
    ) == 0
