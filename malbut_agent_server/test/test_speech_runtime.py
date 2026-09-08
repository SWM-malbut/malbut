"""Verify speech startup and receipt admission without a live provider."""

from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_provider
from malbut_agent_server import ros_communication
from malbut_agent_server.speech_receipts import SpeechReceiptStore


def test_ros_openai_validates_provider_without_http_auth():
    """ROS settings preserve API validation without claiming HTTP identity."""
    configured = Settings(
        provider='openai', openai_api_key='test-key-not-for-network',
    )
    configured.validate_for_dialogue()
    with pytest.raises(ValueError, match='AUTH_TOKEN'):
        configured.validate_for_server()
    with pytest.raises(ValueError, match='OPENAI_API_KEY'):
        replace(configured, openai_api_key='').validate_for_dialogue()
    with pytest.raises(ValueError, match='official'):
        replace(
            configured, openai_base_url='https://example.invalid',
        ).validate_for_dialogue()
    with pytest.raises(ValueError, match='OPENAI_MODEL'):
        replace(configured, openai_model='').validate_for_dialogue()
    with pytest.raises(ValueError, match='proposal-only'):
        replace(configured, tool_mode='simulation').validate_for_dialogue()


def test_ros_rai_uses_sidecar_checks_without_http_token(tmp_path):
    """Only the transport-specific token check differs from HTTP mode."""
    interpreter = tmp_path / 'venv' / 'bin' / 'python'
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable).resolve())
    (interpreter.parent.parent / 'pyvenv.cfg').write_text('home = /test\n')
    cwd = tmp_path / 'sidecar'
    cwd.mkdir()
    settings = Settings(
        provider='rai-sidecar', openai_api_key='test-key-not-for-network',
        rai_sidecar_python=str(interpreter),
        rai_sidecar_working_directory=str(cwd), rai_model='test-model',
    )
    settings.validate_for_dialogue()
    provider = build_provider(settings, http_server=False)
    assert provider.name == 'rai-sidecar'
    with pytest.raises(ValueError, match='AUTH_TOKEN'):
        build_provider(settings)
    with pytest.raises(ValueError, match='SIDECAR_PYTHON'):
        replace(settings, rai_sidecar_python='python').validate_for_dialogue()


def test_settings_do_not_reuse_http_database_or_user(monkeypatch, tmp_path):
    """Voice development receives a deliberate data and user scope."""
    monkeypatch.setattr(ros_communication.os, 'environ', {
        'MALBUT_AGENT_DB': '/private/http-user.sqlite3',
        'MALBUT_AGENT_USER_ID': 'http-user',
    })
    args = SimpleNamespace(
        env_file=None, conversation_db=str(tmp_path / 'speech.sqlite3'),
        db_path=str(tmp_path / 'receipts.sqlite3'),
        user_id='speech-test', provider='mock', model=None,
        goal_response_timeout_s=5.0,
    )
    result = ros_communication.dialogue_settings_from_args(args)
    assert result.database_path == args.conversation_db
    assert result.user_id == 'speech-test'
    assert result.openai_model == 'gpt-5.6-luna'
    assert result.openai_fallback_model == ''
    assert not Path(result.database_path).exists()


def test_check_needs_no_ros_database_or_model(monkeypatch, tmp_path, capsys):
    """Configuration validation must not start a worker or contact a model."""
    monkeypatch.setattr(ros_communication.os, 'environ', {})
    monkeypatch.setitem(sys.modules, 'rclpy', None)

    def unexpected(*args, **kwargs):
        raise AssertionError('Check cannot build runtime or ROS Node')

    monkeypatch.setattr(ros_communication, 'build_orchestrator', unexpected)
    monkeypatch.setattr(ros_communication, 'create_communication_node',
                        unexpected)
    db = tmp_path / 'absent' / 'dialogue.sqlite3'
    assert ros_communication.main([
        '--check', '--provider', 'mock', '--conversation-db', str(db),
    ]) == 0
    assert not db.parent.exists()
    assert 'configuration: ok' in capsys.readouterr().out


@pytest.mark.parametrize('arguments', [
    ['--provider', 'openai'], ['--user-id', ' '],
    ['--conversation-db', ''], ['--goal-response-timeout-s', 'nan'],
    ['--goal-response-timeout-s', '0'],
])
def test_invalid_check_exits_before_startup(monkeypatch, capsys, arguments):
    """Invalid settings cannot quietly produce a ready service."""
    monkeypatch.setattr(ros_communication.os, 'environ', {})
    monkeypatch.setitem(sys.modules, 'rclpy', None)
    assert ros_communication.main(['--check', *arguments]) == 2
    assert 'Invalid speech dialogue settings' in capsys.readouterr().err


def test_receipt_lookup_leaves_unaccepted_id_available(tmp_path):
    """A queue-full admission check does not consume a future transcript."""
    store = SpeechReceiptStore(str(tmp_path / 'receipts.sqlite3'))
    try:
        assert store.lookup('pending', '안녕') is None
        assert store.lookup('pending', '안녕') is None
        assert store.receive('pending', '안녕') == 'received'
        assert store.lookup('pending', '안녕') == 'duplicate'
        assert store.lookup('pending', '다른 원문') == 'conflict'
        assert store.receive('pending', '안녕') == 'duplicate'
    finally:
        store.close()
