"""Tests for explicit operator CLI gates and content-safe output."""

import json
from types import SimpleNamespace

import pytest

from malbut_voice.cli import _microphone_stt_main_for_test
from malbut_voice.errors import ModelSecurityError


class _Audit:
    def to_dict(self):
        return {
            'capture_origin': 'microphone',
            'execution_authority': False,
            'microphone_provenance_verified': True,
            'speaker_identity_verified': False,
            'text_chars': 13,
        }


class _FakeSource:
    instances = []
    prepare_error = None
    capture_error = None

    def __init__(self, config):
        self.config = config
        self.prepared = 0
        self.captured = []
        type(self).instances.append(self)

    def prepare(self):
        self.prepared += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        return {'microphone_opened': False}

    def capture_final(self, duration):
        self.captured.append(duration)
        if self.capture_error is not None:
            raise self.capture_error
        return SimpleNamespace(
            audit=_Audit(),
            event=SimpleNamespace(text='비밀\n문장\u2028끝'),
        )

    def verify_final(self, _result):
        return True


@pytest.fixture(autouse=True)
def reset_fake_source():
    """Reset fake CLI state between tests."""
    _FakeSource.instances = []
    _FakeSource.prepare_error = None
    _FakeSource.capture_error = None


def _run(config_file, *arguments):
    return _microphone_stt_main_for_test(
        ['--config', str(config_file), *arguments],
        _FakeSource,
    )


def test_check_never_invokes_capture_and_projects_no_digests(
    config_file,
    capsys,
):
    """Keep `--check` static and its output content-free."""
    assert _run(config_file, '--check') == 0

    output = json.loads(capsys.readouterr().out)
    source = _FakeSource.instances[0]
    assert source.prepared == 1
    assert source.captured == []
    assert output == {
        'device_attested': True,
        'execution_authority': False,
        'microphone_opened': False,
        'model_attested': True,
        'speaker_identity_verified': False,
        'status': 'ready',
    }
    assert 'digest' not in json.dumps(output)


def test_microphone_requires_explicit_flag_and_hides_text_by_default(
    config_file,
    capsys,
):
    """Capture only on `--microphone` and omit transcript text by default."""
    assert _run(
        config_file,
        '--microphone',
        '--duration-seconds',
        '2',
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert _FakeSource.instances[0].captured == [2]
    assert output['status'] == 'final_transcript_ready'
    assert output['audit']['capture_origin'] == 'microphone'
    assert 'transcript' not in output
    assert '비밀' not in json.dumps(output, ensure_ascii=False)


def test_show_transcript_is_opt_in_and_strips_terminal_controls(
    config_file,
    capsys,
):
    """Print text only by opt-in and replace unsafe Unicode categories."""
    assert _run(
        config_file,
        '--microphone',
        '--show-transcript',
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output['transcript'] == '비밀 문장 끝'


def test_cli_requires_exactly_one_action(config_file):
    """Do not infer microphone authorization from configuration presence."""
    with pytest.raises(SystemExit):
        _run(config_file)
    with pytest.raises(SystemExit):
        _run(config_file, '--check', '--microphone')


def test_check_rejects_transcript_and_duration_options(config_file):
    """Keep capture-only arguments unavailable to static check mode."""
    with pytest.raises(SystemExit):
        _run(config_file, '--check', '--show-transcript')
    with pytest.raises(SystemExit):
        _run(config_file, '--check', '--duration-seconds', '1')


@pytest.mark.parametrize('option', ['--device', '--model', '--wake'])
def test_cli_has_no_device_model_or_wake_override(config_file, option):
    """Never let CLI text replace protected provenance bindings."""
    with pytest.raises(SystemExit):
        _run(config_file, '--check', option, 'attacker-value')


def test_domain_and_unexpected_failures_are_content_free(
    config_file,
    capsys,
):
    """Emit only stable codes, without model paths or exception messages."""
    _FakeSource.prepare_error = ModelSecurityError(
        '/private/model/path'
    )
    assert _run(config_file, '--check') == 2
    error = json.loads(capsys.readouterr().err)
    assert error == {
        'error': 'model_security_failed',
        'status': 'rejected',
    }

    _FakeSource.prepare_error = RuntimeError('/private/secret')
    assert _run(config_file, '--check') == 3
    error = json.loads(capsys.readouterr().err)
    assert error == {
        'error': 'voice_internal_error',
        'status': 'rejected',
    }


def test_keyboard_interrupt_has_stable_content_free_exit(config_file, capsys):
    """Return shell-standard 130 without a traceback or private values."""
    _FakeSource.prepare_error = KeyboardInterrupt()

    assert _run(config_file, '--check') == 130

    captured = capsys.readouterr()
    assert captured.out == ''
    assert json.loads(captured.err) == {
        'error': 'interrupted',
        'status': 'rejected',
    }
    assert 'Traceback' not in captured.err
