"""Check VLM startup choices without hardware, credentials or Cloud requests."""

from pathlib import Path
import json

import pytest

from malbut_bringup.fall_setup import prepare_fall_monitor, prepare_fall_upload


def test_auto_skips_missing_configuration(tmp_path):
    """Keep unconfigured development machines usable without enabling analysis."""
    assert prepare_fall_monitor('auto', str(tmp_path / 'missing.json')) is None


def test_false_never_opens_configuration(monkeypatch):
    """Disabling VLM must work even if its files cannot be read."""
    monkeypatch.setattr(Path, 'stat', lambda _: pytest.fail('unexpected file access'))
    assert prepare_fall_monitor('false', '/private/fall.json') is None


@pytest.mark.parametrize('mode', ['auto', 'true'])
def test_empty_path_is_rejected(mode):
    """Do not interpret an empty filename as the working directory."""
    with pytest.raises(RuntimeError, match='must name'):
        prepare_fall_monitor(mode, '')


@pytest.mark.parametrize('mode', ['auto', 'true'])
@pytest.mark.parametrize('kind', ['directory', 'oversized', 'invalid_json'])
def test_existing_invalid_file_is_never_silently_skipped(tmp_path, mode, kind):
    """Auto mode only skips missing files, not broken configured deployments."""
    config = tmp_path / 'fall.json'
    if kind == 'directory':
        config.mkdir()
    else:
        config.write_text(' ' * 16385 if kind == 'oversized' else '{not json}')
    with pytest.raises(RuntimeError):
        prepare_fall_monitor(mode, str(config))


def test_unknown_startup_mode_is_rejected(tmp_path):
    """An unrecognized flag cannot be treated as an implicit enable."""
    with pytest.raises(RuntimeError, match='must be auto, true or false'):
        prepare_fall_monitor('yes', str(tmp_path / 'fall.json'))


@pytest.fixture
def upload_config(tmp_path):
    example = Path(__file__).parents[2] / 'malbut_agent_server/config/fall_runtime.example.json'
    data = json.loads(example.read_text())
    data.update(device_id='robot-a', journal_path=str(tmp_path / 'private/events.sqlite'))
    path = tmp_path / 'fall.json'
    path.write_text(json.dumps(data))
    return path


def web_environment():
    return {'HOMECAM_BACKEND_URL': 'https://web.example.com',
            'HOMECAM_DEVICE_TOKEN_FILE': '/protected/device.token',
            'HOMECAM_DEVICE_ID': 'robot-a'}


def test_upload_uses_same_journal_and_device_without_opening_credentials(upload_config):
    args = prepare_fall_upload(upload_config, web_environment())
    settings = json.loads(upload_config.read_text())
    assert args == ['--journal', settings['journal_path'], '--device-id', 'robot-a',
                    '--base-url', 'https://web.example.com', '--allow-host', 'web.example.com',
                    '--token-file', '/protected/device.token', '--execute', '--upload-clips']
    assert not Path(settings['journal_path']).parent.exists()
    assert '--once' not in args and '--retry-auth-failed' not in args
    assert settings['cloud_key_file'] not in args


def test_missing_upload_settings_do_not_invent_an_origin(upload_config):
    assert prepare_fall_upload(upload_config, {}) is None


def test_upload_can_reuse_a_complete_key_sync_configuration(upload_config):
    data = json.loads(upload_config.read_text())
    data['key_sync'] = dict(base_url='https://keys.example.com',
                            allow_hosts=['keys.example.com'], token_file='/protected/sync.token')
    upload_config.write_text(json.dumps(data))
    args = prepare_fall_upload(upload_config, {})
    assert args[args.index('--base-url') + 1] == 'https://keys.example.com'
    assert args[args.index('--token-file') + 1] == '/protected/sync.token'
    # An explicitly configured web pair has priority, but a partial pair may
    # never borrow the token from the other origin.
    assert '/protected/device.token' in prepare_fall_upload(upload_config, web_environment())
    with pytest.raises(RuntimeError, match='configuration invalid'):
        prepare_fall_upload(upload_config, {'HOMECAM_BACKEND_URL': 'https://web.example.com'})


@pytest.mark.parametrize('changes', [
    {'HOMECAM_BACKEND_URL': ''}, {'HOMECAM_DEVICE_TOKEN_FILE': ''},
    {'HOMECAM_BACKEND_URL': 'http://web.example.com'},
    {'HOMECAM_BACKEND_URL': 'https://'},
    {'HOMECAM_BACKEND_URL': 'https://web.example.com/api'},
    {'HOMECAM_BACKEND_URL': 'https://user:private-secret@web.example.com'},
    {'HOMECAM_DEVICE_ID': 'different-robot'}, {'HOMECAM_DEVICE_TOKEN_FILE': 'relative.token'},
])
def test_upload_invalid_settings_are_explicit_and_sanitized(upload_config, changes):
    with pytest.raises(RuntimeError, match='configuration invalid') as error:
        prepare_fall_upload(upload_config, web_environment() | changes)
    assert 'private-secret' not in str(error.value)
