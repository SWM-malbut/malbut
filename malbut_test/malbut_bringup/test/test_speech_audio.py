"""Resolve the shared hardware source without changing desktop audio defaults."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_bringup.speech_audio import shared_xfm_source
from malbut_bringup import speech_audio


XFM = {'name': 'alsa_input.usb-xfm.mono-fallback',
       'properties': {'alsa.card_name': 'XFM-DP-V0.0.18'}}


def test_resolves_xfm_by_identity_not_device_order(monkeypatch):
    sources = [
        {'name': 'alsa_input.other', 'properties': {'alsa.card_name': 'USB Audio Device'}},
        {**XFM, 'name': 'xfm.monitor'}, XFM,
    ]
    run = Mock(return_value=SimpleNamespace(stdout=json.dumps(sources)))
    monkeypatch.setattr(subprocess, 'run', run)
    environment = {'PULSE_SERVER': 'unix:/test/audio'}
    assert shared_xfm_source(environment) == XFM['name']
    run.assert_called_once_with(
        ['pactl', '--format=json', 'list', 'sources'], env=environment,
        capture_output=True, text=True, check=True, timeout=3)


@pytest.mark.parametrize('sources', [[], [XFM, {**XFM, 'name': 'second_xfm'}]])
def test_no_arbitrary_fallback_when_xfm_missing_or_ambiguous(monkeypatch, sources):
    monkeypatch.setattr(subprocess, 'run', Mock(
        return_value=SimpleNamespace(stdout=json.dumps(sources))))
    with pytest.raises(RuntimeError, match='requires one XFM-DP'):
        shared_xfm_source({})


@pytest.mark.parametrize('error', [
    FileNotFoundError(), subprocess.CalledProcessError(1, 'pactl'),
    subprocess.TimeoutExpired('pactl', 3),
])
def test_audio_server_failure_is_actionable(monkeypatch, error):
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=error))
    with pytest.raises(RuntimeError, match='desktop audio user'):
        shared_xfm_source({})


@pytest.mark.parametrize('scope', ['/**', 'malbut_stt'])
def test_pulse_reference_routes_come_from_selected_stt_config(tmp_path, monkeypatch, scope):
    config = tmp_path / 'stt.yaml'
    config.write_text(f'{scope}:\n  ros__parameters:\n'
                      '    aec_backend: pulse_reference\n'
                      '    aec_source: xfm-source\n    aec_sink: robot-speaker\n')
    run = Mock(side_effect=AssertionError('configuration must not operate audio'))
    monkeypatch.setattr(subprocess, 'run', run)
    assert speech_audio.pulse_reference_environment(config) == {
        'MALBUT_SHARED_MICROPHONE': 'xfm-source', 'PULSE_SOURCE': 'xfm-source',
        'PULSE_SINK': 'robot-speaker',
    }
    run.assert_not_called()


@pytest.mark.parametrize('parameters', [
    'turn_detection: legacy',
    'aec_backend: none\n    aec_source: unused\n    aec_sink: unused',
])
def test_default_and_none_backend_add_no_environment(tmp_path, parameters):
    config = tmp_path / 'stt.yaml'
    config.write_text('malbut_stt:\n  ros__parameters:\n    ' + parameters + '\n')
    assert speech_audio.pulse_reference_environment(config) == {}


@pytest.mark.parametrize('node_backend,expected', [
    ('none', {}),
    ('pulse_reference', {'MALBUT_SHARED_MICROPHONE': 'specific-source',
                         'PULSE_SOURCE': 'specific-source', 'PULSE_SINK': 'robot-speaker'}),
])
def test_specific_node_overrides_wildcard_and_retains_other_parameters(
        tmp_path, node_backend, expected):
    config = tmp_path / 'stt.yaml'
    config.write_text('malbut_stt:\n  ros__parameters:\n'
                      f'    aec_backend: {node_backend}\n    aec_source: specific-source\n'
                      '/**:\n  ros__parameters:\n    aec_backend: pulse_reference\n'
                      '    aec_source: wildcard-source\n    aec_sink: robot-speaker\n')
    assert speech_audio.pulse_reference_environment(config) == expected


@pytest.mark.parametrize('document', [
    '[invalid', '', '[]',
    'malbut_stt: []', 'malbut_stt: {}',
    'malbut_stt:\n  ros__parameters: []',
    '/**:\n  ros__parameters:\n    aec_backend: unknown',
    'malbut_stt:\n  ros__parameters:\n    aec_backend: false',
    'malbut_stt:\n  ros__parameters:\n    aec_backend: pulse_reference',
    'malbut_stt:\n  ros__parameters:\n    aec_backend: pulse_reference\n'
    '    aec_source: xfm-source\n    aec_sink: " "',
    'malbut_stt:\n  ros__parameters:\n    aec_backend: pulse_reference\n'
    '    aec_source: 1\n    aec_sink: robot-speaker',
])
def test_invalid_pulse_configuration_fails_clearly(tmp_path, document):
    config = tmp_path / 'stt.yaml'
    config.write_text(document)
    with pytest.raises(RuntimeError, match='STT audio configuration'):
        speech_audio.pulse_reference_environment(config)


def test_unreadable_stt_configuration_fails_clearly(tmp_path):
    with pytest.raises(RuntimeError, match='STT audio configuration'):
        speech_audio.pulse_reference_environment(tmp_path / 'missing.yaml')
