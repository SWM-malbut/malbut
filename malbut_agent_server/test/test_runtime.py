"""Tests for the bounded Mock-only runtime configuration."""

import pytest

from malbut_agent_server import factory as factory_module
from malbut_agent_server.cli import server_main
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import (
    build_capability_registry,
    build_orchestrator,
    build_provider,
    build_speech_coordinator,
    build_trusted_robot_state_source,
)
from malbut_agent_server.http_server import make_server
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


SCRIPTED_AUTH_TOKEN = 'scripted-runtime-test-token-0123456789abcdef'


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

    origin_secret = 'origin-user:origin-password'
    unsafe_origin = Settings(
        homecam_origin=(
            f'https://{origin_secret}@homecam.example.test'
        )
    )
    unsafe_rendered = repr(unsafe_origin)
    assert origin_secret not in unsafe_rendered
    assert 'homecam_origin=<configured>' in unsafe_rendered
    assert 'homecam_origin=<unconfigured>' in repr(Settings())


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


def test_scripted_speech_is_explicit_and_requires_http_auth() -> None:
    """Text-only speech ingress is opt-in and bearer authenticated."""
    assert Settings.from_env({}).enable_scripted_speech is False
    with pytest.raises(ValueError, match='SCRIPTED_SPEECH'):
        Settings.from_env(
            {'MALBUT_AGENT_ENABLE_SCRIPTED_SPEECH': 'sometimes'}
        )
    without_auth = Settings.from_env(
        {'MALBUT_AGENT_ENABLE_SCRIPTED_SPEECH': 'true'}
    )
    with pytest.raises(ValueError, match='AUTH_TOKEN'):
        without_auth.validate_for_server()
    with pytest.raises(ValueError, match='boolean'):
        Settings(
            auth_token=SCRIPTED_AUTH_TOKEN,
            enable_scripted_speech='true',
        ).validate_for_server()

    Settings(auth_token='x').validate_for_server()
    for invalid_token in (
        'a' * 31,
        'a' * 513,
        'a' * 31 + ' ',
        'a' * 31 + '\n',
        'a' * 31 + '한',
    ):
        with pytest.raises(ValueError, match='visible-ASCII'):
            Settings(
                auth_token=invalid_token,
                enable_scripted_speech=True,
            ).validate_for_server()

    enabled = Settings.from_env(
        {
            'MALBUT_AGENT_ENABLE_SCRIPTED_SPEECH': '1',
            'MALBUT_AGENT_AUTH_TOKEN': SCRIPTED_AUTH_TOKEN,
        }
    )
    enabled.validate_for_server()
    assert enabled.enable_scripted_speech is True

    whitespace_token = Settings.from_env(
        {
            'MALBUT_AGENT_ENABLE_SCRIPTED_SPEECH': 'true',
            'MALBUT_AGENT_AUTH_TOKEN': 'a' * 32 + ' ',
        }
    )
    with pytest.raises(ValueError, match='visible-ASCII'):
        whitespace_token.validate_for_server()


def test_trusted_robot_state_binding_is_explicit_and_all_or_nothing(
    tmp_path,
) -> None:
    """Production state trust needs one fixed UDS peer and device tuple."""
    defaults = Settings.from_env({})
    assert defaults.robot_state_socket_path == ''
    assert defaults.robot_state_expected_uid is None
    assert defaults.robot_state_device_id == ''
    assert defaults.monitorable_rooms == ()

    for partial in (
        {'MALBUT_ROBOT_STATE_SOCKET_PATH': str(tmp_path / 'state.sock')},
        {'MALBUT_ROBOT_STATE_EXPECTED_UID': '1000'},
        {'MALBUT_ROBOT_STATE_DEVICE_ID': 'malbut-sim-01'},
    ):
        with pytest.raises(ValueError, match='configured together'):
            Settings.from_env(partial).validate_for_server()

    with pytest.raises(ValueError, match='must be absolute'):
        Settings.from_env(
            {
                'MALBUT_ROBOT_STATE_SOCKET_PATH': 'state.sock',
                'MALBUT_ROBOT_STATE_EXPECTED_UID': '1000',
                'MALBUT_ROBOT_STATE_DEVICE_ID': 'malbut-sim-01',
            }
        ).validate_for_server()
    with pytest.raises(ValueError, match='between'):
        Settings.from_env(
            {'MALBUT_ROBOT_STATE_EXPECTED_UID': '-1'}
        )


def test_monitorable_rooms_require_both_trust_roots_and_same_device(
    tmp_path,
) -> None:
    """A room label alone never enables the monitor_room proposal path."""
    with pytest.raises(ValueError, match='HOMECAM'):
        Settings.from_env(
            {'MALBUT_AGENT_MONITORABLE_ROOMS': ' 거실 , 주방 '}
        ).validate_for_server()

    common = {
        'MALBUT_AGENT_MONITORABLE_ROOMS': ' 거실 , 주방 ',
        'MALBUT_HOMECAM_ORIGIN': 'https://homecam.example.test',
        'MALBUT_HOMECAM_AGENT_TOKEN': 'a' * 43,
        'MALBUT_HOMECAM_SIGNING_SECRET': 'b' * 43,
        'MALBUT_HOMECAM_PRINCIPAL_SUBJECT_DIGEST': 'c' * 64,
        'MALBUT_HOMECAM_DEVICE_ID': 'malbut-sim-01',
        'MALBUT_ROBOT_STATE_SOCKET_PATH': str(tmp_path / 'state.sock'),
        'MALBUT_ROBOT_STATE_EXPECTED_UID': '1000',
        'MALBUT_ROBOT_STATE_DEVICE_ID': 'malbut-sim-01',
    }
    settings = Settings.from_env(common)
    settings.validate_for_server()
    assert settings.monitorable_rooms == ('거실', '주방')

    with pytest.raises(ValueError, match='device IDs must match'):
        Settings.from_env(
            {
                **common,
                'MALBUT_ROBOT_STATE_DEVICE_ID': 'malbut-other-01',
            }
        ).validate_for_server()
    with pytest.raises(ValueError, match='duplicates'):
        Settings.from_env(
            {'MALBUT_AGENT_MONITORABLE_ROOMS': '거실, 거실'}
        )
    with pytest.raises(ValueError, match='duplicates'):
        Settings.from_env(
            {'MALBUT_AGENT_MONITORABLE_ROOMS': 'Living Room,living room'}
        )
    with pytest.raises(ValueError, match='invalid room'):
        Settings.from_env(
            {'MALBUT_AGENT_MONITORABLE_ROOMS': '거\u200b실'}
        )


def test_robot_state_timeout_matches_transport_upper_bound() -> None:
    """Settings cannot accept a timeout rejected by the UDS source."""
    with pytest.raises(ValueError, match='between'):
        Settings.from_env(
            {'MALBUT_ROBOT_STATE_TIMEOUT_SECONDS': '6'}
        )


def test_factory_builds_fixed_robot_state_source_without_connecting(
    tmp_path,
) -> None:
    """Startup binds UDS path/UID/device but performs no state read."""
    settings = Settings(
        robot_state_socket_path=str(tmp_path / 'collector.sock'),
        robot_state_expected_uid=1000,
        robot_state_device_id='malbut-sim-01',
        robot_state_timeout_seconds=5,
    )
    source = build_trusted_robot_state_source(settings)
    assert source is not None
    assert source.socket_path == str(tmp_path / 'collector.sock')
    assert source.expected_uid == 1000
    assert source.expected_device_id == 'malbut-sim-01'
    assert source.timeout_seconds == 5.0


def test_injected_state_source_skips_configured_source_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse one injected source without creating a second UDS reader."""
    class InjectedSource:
        def read(self):
            raise AssertionError('the source must not be read at startup')

    def unexpected_build(_settings):
        raise AssertionError('configured source was built twice')

    monkeypatch.setattr(
        factory_module,
        'build_trusted_robot_state_source',
        unexpected_build,
    )
    coordinator = build_speech_coordinator(
        Settings(database_path=':memory:'),
        trusted_robot_state_source=InjectedSource(),
    )
    try:
        assert coordinator.orchestrator.trusted_robot_state_source is not None
    finally:
        coordinator.orchestrator.conversation_store.close()
        coordinator.orchestrator.memory_store.close()


def test_invalid_robot_state_binding_precedes_sqlite_creation(
    tmp_path,
) -> None:
    """Malformed UDS configuration cannot mutate the configured DB."""
    database_path = tmp_path / 'must-not-be-created.sqlite3'
    settings = Settings(
        database_path=str(database_path),
        robot_state_socket_path=str(tmp_path / '..' / 'collector.sock'),
        robot_state_expected_uid=1000,
        robot_state_device_id='malbut-sim-01',
    )
    with pytest.raises(ValueError, match='socket path'):
        build_orchestrator(settings)
    assert not database_path.exists()


@pytest.mark.parametrize('value', [True, 0, 10001])
def test_failed_auth_attempt_limit_is_bounded(value) -> None:
    """Direct settings cannot disable or unbound failed-auth throttling."""
    with pytest.raises(ValueError, match='FAILED_AUTH_ATTEMPTS'):
        Settings(
            failed_auth_attempts_per_minute=value,
        ).validate_for_server()


def test_failed_auth_attempt_environment_limit_is_bounded() -> None:
    """Environment parsing rejects a disabled failed-auth limiter."""
    with pytest.raises(ValueError, match='between'):
        Settings.from_env(
            {'MALBUT_AGENT_FAILED_AUTH_ATTEMPTS_PER_MINUTE': '0'}
        )


def test_scripted_http_dependency_requires_auth_defense_in_depth() -> None:
    """Direct server construction cannot expose anonymous speech input."""
    coordinator = build_speech_coordinator(
        Settings(database_path=':memory:')
    )
    try:
        with pytest.raises(ValueError, match='bearer auth'):
            make_server(
                '127.0.0.1',
                0,
                coordinator.orchestrator,
                speech_coordinator=coordinator,
            )
        with pytest.raises(ValueError, match='visible-ASCII'):
            make_server(
                '127.0.0.1',
                0,
                coordinator.orchestrator,
                auth_token='short-token',
                speech_coordinator=coordinator,
            )
    finally:
        coordinator.orchestrator.conversation_store.close()
        coordinator.orchestrator.memory_store.close()


@pytest.mark.parametrize(
    'homecam_values',
    (
        {
            'homecam_origin': 'https://homecam.example.test',
        },
        {
            'homecam_origin': 'http://homecam.example.test',
            'homecam_agent_token': 'a' * 32,
            'homecam_signing_secret': 'b' * 32,
            'homecam_principal_subject_digest': 'c' * 64,
            'homecam_device_id': 'malbut-sim-01',
        },
    ),
)
def test_invalid_homecam_binding_precedes_sqlite_creation(
    tmp_path,
    homecam_values: dict,
) -> None:
    """A malformed remote binding has no database startup side effect."""
    database_path = tmp_path / 'must-not-be-created.sqlite3'
    settings = Settings(
        database_path=str(database_path),
        **homecam_values,
    )

    with pytest.raises(ValueError):
        build_speech_coordinator(settings)

    assert not database_path.exists()


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
    capabilities = {
        item['name']: item
        for item in simulation['capabilities']
    }
    assert capabilities['monitor_room']['executable'] is False
    assert (
        capabilities['monitor_room']['blocked_by']
        == 'confirmation_required'
    )
    assert all(
        item['executable'] is True
        for name, item in capabilities.items()
        if name != 'monitor_room'
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
    """Check OpenAI config safely before the first paid call."""
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


def test_cli_check_builds_opt_in_scripted_speech_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production entry point can explicitly assemble speech."""
    monkeypatch.setenv(
        'MALBUT_AGENT_AUTH_TOKEN',
        SCRIPTED_AUTH_TOKEN,
    )
    monkeypatch.delenv('MALBUT_AGENT_PROVIDER', raising=False)
    assert server_main(
        [
            '--provider',
            'mock',
            '--database',
            ':memory:',
            '--enable-scripted-speech',
            '--check',
        ]
    ) == 0


def test_cli_scripted_flag_rejects_untrimmed_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI opt-in validates the original bearer instead of trimming it."""
    monkeypatch.setenv(
        'MALBUT_AGENT_AUTH_TOKEN',
        'a' * 32 + ' ',
    )
    monkeypatch.delenv(
        'MALBUT_AGENT_ENABLE_SCRIPTED_SPEECH',
        raising=False,
    )
    with pytest.raises(ValueError, match='visible-ASCII'):
        server_main(
            [
                '--provider',
                'mock',
                '--database',
                ':memory:',
                '--enable-scripted-speech',
                '--check',
            ]
        )
