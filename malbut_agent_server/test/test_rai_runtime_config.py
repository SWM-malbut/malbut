"""Configuration tests for the optional, isolated RAI sidecar."""

from dataclasses import fields, replace
from pathlib import Path
import subprocess
import sys

import pytest

from malbut_agent_server.cli import server_main
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import (
    RAI_SIDECAR_MODULE,
    build_provider,
)
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.rai_sidecar_client import RaiSidecarProvider
from malbut_agent_server.rai_sidecar_protocol import MAX_MODEL_INPUT_LENGTH


def _isolated_rai_python(tmp_path: Path) -> Path:
    """Create the filesystem shape of a dedicated test virtualenv."""
    venv_root = tmp_path / 'rai-venv'
    executable_directory = venv_root / 'bin'
    executable_directory.mkdir(parents=True, exist_ok=True)
    (venv_root / 'pyvenv.cfg').write_text(
        'home = /test-only\n',
        encoding='utf-8',
    )
    executable = executable_directory / 'python'
    if not executable.exists():
        executable.symlink_to(Path(sys.executable).resolve())
    return executable


def _rai_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        'provider': 'rai-sidecar',
        'auth_token': 'local-http-token',
        'rai_sidecar_python': str(_isolated_rai_python(tmp_path)),
        'rai_sidecar_working_directory': str(tmp_path.resolve()),
        'rai_sidecar_timeout_seconds': 7,
        'openai_api_key': 'test-only-sidecar-key',
        'rai_model': 'test-rai-model',
        'database_path': ':memory:',
    }
    values.update(overrides)
    return Settings(**values)


def test_rai_sidecar_is_optional_and_default_provider_stays_mock() -> None:
    """Existing startup never imports or launches RAI unless selected."""
    settings = Settings.from_env({})
    settings.validate_for_server()

    assert settings.provider == 'mock'
    assert settings.rai_sidecar_python == ''
    assert settings.rai_sidecar_working_directory == ''
    assert isinstance(build_provider(settings), MockProvider)


def test_rai_fields_append_after_legacy_positional_contract() -> None:
    """Adding the optional sidecar does not shift any existing argument."""
    legacy_fields = (
        'provider',
        'host',
        'port',
        'database_path',
        'user_id',
        'memory_limit',
        'conversation_ttl_seconds',
        'conversation_history_limit',
        'conversation_summary_max_chars',
        'max_model_input_chars',
        'max_conversation_sessions',
        'max_conversation_turns',
        'max_request_bytes',
        'max_concurrent_requests',
        'requests_per_minute',
        'socket_timeout_seconds',
        'request_timeout_seconds',
        'provider_total_timeout_seconds',
        'provider_max_retries',
        'provider_retry_base_delay_ms',
        'provider_retry_max_delay_ms',
        'provider_failure_threshold',
        'provider_recovery_timeout_seconds',
        'openai_api_key',
        'openai_model',
        'openai_fallback_model',
        'openai_base_url',
        'openai_reasoning_effort',
        'openai_max_output_tokens',
        'auth_token',
        'tool_mode',
    )

    names = tuple(field.name for field in fields(Settings))

    assert names[:len(legacy_fields)] == legacy_fields
    assert names[len(legacy_fields):] == (
        'rai_sidecar_python',
        'rai_sidecar_working_directory',
        'rai_sidecar_timeout_seconds',
        'rai_model',
        'openai_general_model',
        'openai_robot_planner_model',
    )


def test_rai_sidecar_environment_settings_are_explicit_and_bounded(
    tmp_path: Path,
) -> None:
    """All process-affecting RAI values come from named settings."""
    settings = Settings.from_env(
        {
            'MALBUT_AGENT_PROVIDER': 'rai-sidecar',
            'MALBUT_AGENT_AUTH_TOKEN': 'local-http-token',
            'MALBUT_RAI_SIDECAR_PYTHON': str(
                _isolated_rai_python(tmp_path)
            ),
            'MALBUT_RAI_SIDECAR_CWD': str(tmp_path.resolve()),
            'MALBUT_RAI_SIDECAR_TIMEOUT_SECONDS': '9',
            'MALBUT_RAI_MODEL': 'test-rai-model',
            'OPENAI_API_KEY': 'test-only-sidecar-key',
        }
    )

    settings.validate_for_server()

    assert settings.rai_sidecar_timeout_seconds == 9
    assert settings.rai_model == 'test-rai-model'
    with pytest.raises(ValueError):
        Settings.from_env(
            {'MALBUT_RAI_SIDECAR_TIMEOUT_SECONDS': '121'}
        )


def test_rai_mode_requires_auth_absolute_python_and_isolated_cwd(
    tmp_path: Path,
) -> None:
    """Unsafe implicit executable and working-directory lookup fail closed."""
    valid = _rai_settings(tmp_path)
    valid.validate_for_server()

    with pytest.raises(ValueError, match='AUTH_TOKEN'):
        replace(valid, auth_token='').validate_for_server()
    with pytest.raises(ValueError, match='TIMEOUT_SECONDS'):
        replace(
            valid,
            rai_sidecar_timeout_seconds=0,
        ).validate_for_server()
    with pytest.raises(ValueError, match='SIDECAR_PYTHON'):
        replace(valid, rai_sidecar_python='python3').validate_for_server()
    with pytest.raises(ValueError, match='virtual environment'):
        replace(
            valid,
            rai_sidecar_python=str(Path(sys.executable).resolve()),
        ).validate_for_server()
    with pytest.raises(ValueError, match='SIDECAR_PYTHON'):
        replace(
            valid,
            rai_sidecar_python=str(tmp_path / 'missing-python'),
        ).validate_for_server()
    with pytest.raises(ValueError, match='SIDECAR_CWD'):
        replace(
            valid,
            rai_sidecar_working_directory='relative-directory',
        ).validate_for_server()
    with pytest.raises(ValueError, match='SIDECAR_CWD'):
        replace(
            valid,
            rai_sidecar_working_directory='/',
        ).validate_for_server()
    with pytest.raises(ValueError, match='outside'):
        replace(
            valid,
            rai_sidecar_working_directory=str(
                _isolated_rai_python(tmp_path).parent
            ),
        ).validate_for_server()
    with pytest.raises(ValueError, match='OPENAI_API_KEY'):
        replace(valid, openai_api_key='').validate_for_server()
    with pytest.raises(ValueError, match='MALBUT_RAI_MODEL'):
        replace(valid, rai_model='').validate_for_server()
    with pytest.raises(ValueError, match='protocol limit'):
        replace(
            valid,
            max_model_input_chars=MAX_MODEL_INPUT_LENGTH + 1,
        ).validate_for_server()


def test_factory_builds_direct_one_attempt_sidecar_with_allowlisted_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RAI never inherits the server shell or a reliability fallback."""
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'must-not-be-forwarded')
    monkeypatch.setenv('MALBUT_AGENT_AUTH_TOKEN', 'must-not-be-forwarded')
    monkeypatch.setenv('PYTHONPATH', '/must/not/be/forwarded')
    monkeypatch.setenv('PATH', '/attacker-controlled-path')
    settings = _rai_settings(
        tmp_path,
        openai_api_key='test-only-sidecar-key',
        openai_fallback_model='ignored-openai-fallback',
        provider_max_retries=3,
        rai_model='test-rai-model',
    )

    provider = build_provider(settings)

    assert isinstance(provider, RaiSidecarProvider)
    assert not isinstance(provider, ReliableProvider)
    assert provider.client.timeout_seconds == 7.0
    transport = provider.client._transport
    assert transport.argv == (
        settings.rai_sidecar_python,
        '-I',
        '-m',
        RAI_SIDECAR_MODULE,
    )
    assert transport.working_directory == str(tmp_path.resolve())
    assert transport.environment['OPENAI_API_KEY'] == (
        'test-only-sidecar-key'
    )
    assert transport.environment['MALBUT_RAI_MODEL'] == 'test-rai-model'
    assert transport.environment['LANGCHAIN_TRACING_V2'] == 'false'
    assert 'AWS_SECRET_ACCESS_KEY' not in transport.environment
    assert 'MALBUT_AGENT_AUTH_TOKEN' not in transport.environment
    assert 'PYTHONPATH' not in transport.environment
    assert transport.environment['PATH'] == '/usr/bin:/bin'


def test_rai_settings_repr_redacts_credentials_and_private_paths(
    tmp_path: Path,
) -> None:
    """Startup diagnostics reveal neither secrets nor filesystem layout."""
    settings = _rai_settings(
        tmp_path,
        openai_api_key='sidecar-secret-key',
    )

    rendered = repr(settings)

    assert 'sidecar-secret-key' not in rendered
    assert str(tmp_path.resolve()) not in rendered
    assert 'rai_sidecar_python=<redacted>' in rendered
    assert 'rai_sidecar_working_directory=<redacted>' in rendered


def test_cli_rai_check_validates_without_starting_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator check is free of RAI, network, and subprocess I/O."""
    monkeypatch.setenv('MALBUT_AGENT_AUTH_TOKEN', 'local-http-token')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-sidecar-key')
    monkeypatch.setenv('MALBUT_RAI_MODEL', 'test-rai-model')
    monkeypatch.setenv(
        'MALBUT_RAI_SIDECAR_PYTHON',
        str(_isolated_rai_python(tmp_path)),
    )
    monkeypatch.setenv('MALBUT_RAI_SIDECAR_CWD', str(tmp_path.resolve()))

    def unexpected_process(*_args, **_kwargs):
        raise AssertionError('configuration check must not launch RAI')

    monkeypatch.setattr(subprocess, 'Popen', unexpected_process)

    assert server_main(
        [
            '--env-file',
            str(tmp_path / 'missing.env'),
            '--provider',
            'rai-sidecar',
            '--database',
            ':memory:',
            '--check',
        ]
    ) == 0
