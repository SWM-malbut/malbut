"""Resolve the shared hardware source without changing desktop audio defaults."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_bringup.speech_audio import shared_xfm_source


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
