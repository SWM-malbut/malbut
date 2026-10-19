"""Regression coverage for USB removal, re-enumeration and restored streams."""

from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_bringup import audio_routing as routing


def device(name, index, identity, profile):
    return {'name': name, 'index': index, 'properties': {
        'device.bus': 'usb', 'device.vendor.id': identity[0],
        'device.product.id': identity[1], 'device.profile.name': profile}}


class Pulse:
    def __init__(self):
        self.mic = device('xfm', 9, routing.MICROPHONE, 'mono-fallback')
        self.speaker = device('usb-speaker', 4, routing.SPEAKER, 'analog-stereo')
        mic_card = device('mic-card', 1, routing.MICROPHONE, '')
        mic_card.update(active_profile='input:mono-fallback', profiles={'input:mono-fallback': {}})
        speaker_card = device('speaker-card', 2, routing.SPEAKER, '')
        speaker_card.update(active_profile='output:analog-stereo',
                            profiles={'output:analog-stereo': {}})
        self.snapshot = {
            'cards': [mic_card, speaker_card], 'sources': [self.mic], 'sinks': [self.speaker],
            'source_outputs': [{'index': 12, 'source': 11}],
            'sink_inputs': [{'index': 13, 'sink': 3}],
        }
        self.defaults = {'default_source_name': 'wrong-mic', 'default_sink_name': 'hdmi'}
        self.changes = []

    def __call__(self, *args):
        if args == ('--format=json', 'list'):
            return json.dumps(self.snapshot)
        if args == ('--format=json', 'info'):
            return json.dumps(self.defaults)
        self.changes.append(args)
        if args[0].startswith('set-default-'):
            self.defaults['default_' + args[0].removeprefix('set-default-') + '_name'] = args[1]
        elif args[0].startswith('move-'):
            kind = 'source' if args[0] == 'move-source-output' else 'sink'
            streams = 'source_outputs' if kind == 'source' else 'sink_inputs'
            target = next(d for d in self.snapshot[kind + 's'] if d['name'] == args[2])
            stream = next(s for s in self.snapshot[streams] if s['index'] == args[1])
            stream[kind] = target['index']
        elif args[0] == 'set-card-profile':
            card = next(c for c in self.snapshot['cards'] if c['name'] == args[1])
            card['active_profile'] = args[2]
            # PulseAudio replaces the output device during a profile transition.
            if args[1] == 'speaker-card':
                self.speaker['index'] += 10
        return ''


@pytest.fixture
def pulse(monkeypatch):
    pulse = Pulse()
    monkeypatch.setattr(routing, 'pactl', pulse)
    return pulse


def test_repairs_defaults_and_existing_streams_then_is_idle(pulse):
    routing.reconcile()
    assert pulse.defaults == {'default_source_name': 'xfm', 'default_sink_name': 'usb-speaker'}
    assert pulse.snapshot['source_outputs'][0]['source'] == 9
    assert pulse.snapshot['sink_inputs'][0]['sink'] == 4
    pulse.changes.clear()
    routing.reconcile()
    assert pulse.changes == []


def test_devices_return_with_new_indices_and_streams_follow(pulse):
    routing.reconcile()
    pulse.snapshot['sources'] = []
    pulse.snapshot['sinks'] = []
    pulse.changes.clear()
    routing.reconcile()
    assert pulse.changes == []
    pulse.mic['index'] = 31
    pulse.speaker['index'] = 42
    pulse.snapshot['sources'] = [pulse.mic]
    pulse.snapshot['sinks'] = [pulse.speaker]
    routing.reconcile()
    assert pulse.snapshot['source_outputs'][0]['source'] == 31
    assert pulse.snapshot['sink_inputs'][0]['sink'] == 42


def test_new_stream_restored_to_wrong_device_is_moved(pulse):
    routing.reconcile()
    pulse.snapshot['source_outputs'].append({'index': 20, 'source': 11})
    pulse.snapshot['sink_inputs'].append({'index': 21, 'sink': 3})
    pulse.changes.clear()
    routing.reconcile()
    assert pulse.changes == [('move-source-output', 20, 'xfm'),
                             ('move-sink-input', 21, 'usb-speaker')]


@pytest.mark.parametrize('kind', ['source', 'sink'])
def test_duplicate_identity_is_not_selected(pulse, kind):
    duplicate = deepcopy(pulse.snapshot[kind + 's'][0])
    duplicate.update(name='second-device', index=88)
    pulse.snapshot[kind + 's'].append(duplicate)
    routing.reconcile()
    assert not any(kind in change[0] for change in pulse.changes)


def test_monitor_recording_and_unrelated_profiles_are_preserved(pulse):
    monitor = device('loopback.monitor', 72, routing.MICROPHONE, 'mono-fallback')
    monitor['properties']['device.class'] = 'monitor'
    pulse.snapshot['sources'].append(monitor)
    pulse.snapshot['source_outputs'].append({'index': 22, 'source': 72})
    other_card = device('other-card', 50, ('9999', '0001'), '')
    other_card.update(active_profile='off', profiles={'input:mono-fallback': {}})
    pulse.snapshot['cards'].append(other_card)
    routing.reconcile()
    assert pulse.snapshot['source_outputs'][1]['source'] == 72
    assert other_card['active_profile'] == 'off'


def test_second_disabled_microphone_is_still_ambiguous(pulse):
    duplicate = deepcopy(pulse.snapshot['cards'][0])
    duplicate.update(name='second-mic-card', index=88, active_profile='off')
    pulse.snapshot['cards'].append(duplicate)
    routing.reconcile()
    assert not any('source' in change[0] or change[0] == 'set-card-profile'
                   for change in pulse.changes)


def test_restores_profiles_and_refreshes_replaced_device_indices(pulse):
    mic_card = device('mic-card', 1, routing.MICROPHONE, '')
    mic_card.update(active_profile='off', profiles={'input:mono-fallback': {}})
    speaker_card = device('speaker-card', 2, routing.SPEAKER, '')
    speaker_card.update(active_profile='output:analog-stereo+input:mono-fallback',
                        profiles={'output:analog-stereo': {}})
    pulse.snapshot['cards'] = [mic_card, speaker_card]
    routing.reconcile()
    assert mic_card['active_profile'] == 'input:mono-fallback'
    assert speaker_card['active_profile'] == 'output:analog-stereo'
    assert pulse.snapshot['sink_inputs'][0]['sink'] == pulse.speaker['index'] == 14


def test_usb_identity_and_profile_exclude_other_audio_devices():
    wrong = device('wrong', 0, ('9999', '0001'), 'mono-fallback')
    hdmi = device('hdmi', 1, routing.SPEAKER, 'hdmi-stereo')
    assert routing.unique_device([wrong], routing.MICROPHONE) is None
    assert routing.unique_device([hdmi], routing.SPEAKER, 'analog-stereo') is None


def test_subscribes_before_snapshot_and_retries_only_audio_events(monkeypatch):
    events = SimpleNamespace(stdout=iter([
        "Event 'new' on client #1\n", "Event 'new' on source #15\n",
        "Event 'change' on server #4294967295\n",
    ]), terminate=Mock(), wait=Mock())
    popen = Mock(return_value=events)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    reconcile = Mock(side_effect=lambda: popen.assert_called_once())
    monkeypatch.setattr(routing, 'reconcile', reconcile)
    with pytest.raises(RuntimeError, match='connection closed'):
        routing.watch()
    assert reconcile.call_count == 3
    events.terminate.assert_called_once()


def test_hotplug_race_exits_for_systemd_retry(monkeypatch):
    monkeypatch.setattr(routing, 'reconcile', Mock(
        side_effect=subprocess.CalledProcessError(1, 'pactl')))
    assert routing.main(['--once']) == 1
