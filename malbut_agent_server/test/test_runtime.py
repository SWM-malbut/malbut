"""Tests for the bounded Mock-only runtime configuration."""

import pytest

from malbut_agent_server.cli import server_main
from malbut_agent_server.config import Settings
from malbut_agent_server.domain.front_route import (
    FrontRoute,
    FrontRouteMatch,
)
from malbut_agent_server.factory import (
    build_capability_registry,
    build_orchestrator,
    build_provider,
)
from malbut_agent_server.http_server import make_server
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
)
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.providers.routed import RoutedAgentProvider
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

    runtime = build_orchestrator(Settings(database_path=':memory:'))
    try:
        with pytest.raises(ValueError, match='loopback-only'):
            make_server('0.0.0.0', 0, runtime)
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


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


def test_tool_mode_is_explicit_and_independent_from_provider() -> None:
    """Mock inference alone never enables simulation adapters."""
    proposal_settings = Settings(provider='mock', tool_mode='proposal')
    proposal = build_capability_registry(proposal_settings).to_dict()
    assert all(
        item['executable'] is False
        for item in proposal['capabilities']
    )

    simulation_settings = Settings(
        provider='openai',
        tool_mode='simulation',
    )
    simulation = build_capability_registry(
        simulation_settings
    ).to_dict()
    assert simulation['runtime_mode'] == 'simulation'
    assert all(
        item['executable'] is True
        for item in simulation['capabilities']
    )

    invalid = Settings.from_env(
        {'MALBUT_AGENT_TOOL_MODE': 'physical'}
    )
    with pytest.raises(ValueError, match='TOOL_MODE'):
        invalid.validate_for_server()


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


def test_factory_wraps_only_an_explicit_front_router() -> None:
    """No calibrated Router is enabled by the default runtime settings."""
    class AbstainingFrontRouter:
        def try_route(self, request):
            del request
            return None

    runtime = build_orchestrator(
        Settings(database_path=':memory:'),
        front_router=AbstainingFrontRouter(),
    )
    try:
        assert isinstance(runtime.provider, RoutedAgentProvider)
        assert runtime.provider.general_provider is (
            runtime.provider.robot_planner_provider
        )
        assert runtime.provider.general_provider is (
            runtime.provider.fallback_provider
        )
        assert runtime.provider.fallback_provider.name == 'mock'
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


def test_openai_role_models_are_optional_and_validated() -> None:
    """Role overrides are explicit and reject malformed model IDs."""
    configured = Settings.from_env({
        'MALBUT_AGENT_PROVIDER': 'openai',
        'MALBUT_AGENT_AUTH_TOKEN': 'local-http-token',
        'OPENAI_API_KEY': 'test-only-openai-key',
        'OPENAI_GENERAL_MODEL': 'gpt-4.1-mini',
        'OPENAI_ROBOT_PLANNER_MODEL': 'gpt-5.6-terra',
    })

    configured.validate_for_server()

    assert configured.openai_general_model == 'gpt-4.1-mini'
    assert configured.openai_robot_planner_model == 'gpt-5.6-terra'
    assert Settings.from_env({}).openai_general_model == ''
    assert Settings.from_env({}).openai_robot_planner_model == ''

    invalid = Settings.from_env({
        'MALBUT_AGENT_PROVIDER': 'openai',
        'MALBUT_AGENT_AUTH_TOKEN': 'local-http-token',
        'OPENAI_API_KEY': 'test-only-openai-key',
        'OPENAI_GENERAL_MODEL': 'bad model id',
    })
    with pytest.raises(ValueError, match='OPENAI_GENERAL_MODEL'):
        invalid.validate_for_server()

    invalid_planner = Settings.from_env({
        'MALBUT_AGENT_PROVIDER': 'openai',
        'MALBUT_AGENT_AUTH_TOKEN': 'local-http-token',
        'OPENAI_API_KEY': 'test-only-openai-key',
        'OPENAI_ROBOT_PLANNER_MODEL': 'bad planner model',
    })
    with pytest.raises(
        ValueError,
        match='OPENAI_ROBOT_PLANNER_MODEL',
    ):
        invalid_planner.validate_for_server()


def test_factory_isolates_explicit_openai_role_models() -> None:
    """Chat, Planner, and abstain keep independent model circuits."""
    class GeneralRouter:
        def try_route(self, request):
            del request
            return FrontRouteMatch(
                route=FrontRoute.GENERAL_CONVERSATION,
            )

    runtime = build_orchestrator(
        Settings(
            provider='openai',
            openai_api_key='test-only-openai-key',
            openai_model='legacy-primary',
            openai_fallback_model='legacy-fallback',
            openai_general_model='gpt-4.1-mini',
            openai_robot_planner_model='gpt-5.6-terra',
            database_path=':memory:',
        ),
        front_router=GeneralRouter(),
    )
    try:
        routed = runtime.provider
        assert isinstance(routed, RoutedAgentProvider)
        assert isinstance(routed.general_provider, ReliableProvider)
        assert isinstance(
            routed.robot_planner_provider,
            ReliableProvider,
        )
        assert isinstance(routed.fallback_provider, ReliableProvider)
        assert len({
            id(routed.general_provider),
            id(routed.robot_planner_provider),
            id(routed.fallback_provider),
        }) == 3
        assert [
            item.model
            for item in routed.general_provider._providers
        ] == ['gpt-4.1-mini']
        assert all(
            item.include_reasoning is False
            for item in routed.general_provider._providers
        )
        assert [
            item.model
            for item in routed.robot_planner_provider._providers
        ] == ['gpt-5.6-terra']
        assert all(
            item.include_reasoning is False
            for item in routed.robot_planner_provider._providers
        )
        assert [
            item.model
            for item in routed.fallback_provider._providers
        ] == ['legacy-primary', 'legacy-fallback']
        assert routed.general_provider._circuits is not (
            routed.robot_planner_provider._circuits
        )
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


@pytest.mark.parametrize(
    ('general_model', 'planner_model', 'expected_general',
     'expected_planner', 'general_reasoning',
     'planner_reasoning'),
    [
        (
            'gpt-4.1-mini',
            '',
            ['gpt-4.1-mini'],
            ['legacy-primary', 'legacy-fallback'],
            [False],
            [True, True],
        ),
        (
            '',
            'gpt-4.1-mini',
            ['legacy-primary', 'legacy-fallback'],
            ['gpt-4.1-mini'],
            [True, True],
            [False],
        ),
    ],
)
def test_factory_preserves_unspecified_role_fallback_chain(
    general_model,
    planner_model,
    expected_general,
    expected_planner,
    general_reasoning,
    planner_reasoning,
) -> None:
    """One override isolates roles without weakening the other role."""
    class GeneralRouter:
        def try_route(self, request):
            del request
            return FrontRouteMatch(
                route=FrontRoute.GENERAL_CONVERSATION,
            )

    runtime = build_orchestrator(
        Settings(
            provider='openai',
            openai_api_key='test-only-openai-key',
            openai_model='legacy-primary',
            openai_fallback_model='legacy-fallback',
            openai_general_model=general_model,
            openai_robot_planner_model=planner_model,
            database_path=':memory:',
        ),
        front_router=GeneralRouter(),
    )
    try:
        routed = runtime.provider
        assert [
            item.model for item in routed.general_provider._providers
        ] == expected_general
        assert [
            item.model
            for item in routed.robot_planner_provider._providers
        ] == expected_planner
        assert [
            item.include_reasoning
            for item in routed.general_provider._providers
        ] == general_reasoning
        assert [
            item.include_reasoning
            for item in routed.robot_planner_provider._providers
        ] == planner_reasoning
        assert len({
            id(routed.general_provider),
            id(routed.robot_planner_provider),
            id(routed.fallback_provider),
        }) == 3
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


def test_factory_ignores_role_models_while_front_router_is_off() -> None:
    """Role settings alone do not activate the experimental Router."""
    runtime = build_orchestrator(
        Settings(
            provider='openai',
            openai_api_key='test-only-openai-key',
            openai_model='legacy-primary',
            openai_fallback_model='legacy-fallback',
            openai_general_model='gpt-4.1-mini',
            openai_robot_planner_model='gpt-5.6-terra',
            database_path=':memory:',
        )
    )
    try:
        provider = runtime.provider
        assert isinstance(provider, ReliableProvider)
        assert not isinstance(provider, RoutedAgentProvider)
        assert [
            item.model for item in provider._providers
        ] == ['legacy-primary', 'legacy-fallback']
        assert all(
            item.include_reasoning is True
            for item in provider._providers
        )
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


def test_factory_preserves_shared_provider_without_role_settings() -> None:
    """Existing explicit Router composition is byte-for-byte optional."""
    class AbstainingFrontRouter:
        def try_route(self, request):
            del request
            return None

    runtime = build_orchestrator(
        Settings(
            provider='openai',
            openai_api_key='test-only-openai-key',
            openai_model='cli-overridden-model',
            database_path=':memory:',
        ),
        front_router=AbstainingFrontRouter(),
    )
    try:
        routed = runtime.provider
        assert isinstance(routed, RoutedAgentProvider)
        assert routed.general_provider is routed.fallback_provider
        assert routed.robot_planner_provider is routed.fallback_provider
        assert [
            item.model
            for item in routed.fallback_provider._providers
        ] == ['cli-overridden-model']
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
