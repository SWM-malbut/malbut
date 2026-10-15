"""Isolate TTS transport failures without inventing a playback completion."""

from collections import OrderedDict
from threading import RLock
from unittest.mock import Mock

from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest
from malbut_system_manager.speech_coordinator import SpeechCoordinator
from malbut_system_manager.state_store import StateStore


def _speech():
    speech = object.__new__(SpeechCoordinator)
    speech.node = Mock()
    speech.lock = RLock()
    speech.state = StateStore()
    speech.mission_for_goal = Mock(return_value=None)
    speech.changed = Mock()
    speech.control_timeout_s = 1.0
    speech.playback_timeout_s = 1.0
    speech.playbacks = {}
    speech.retired = OrderedDict()
    speech.pending_statuses = OrderedDict()
    speech.controls = []
    speech.closed = False
    speech.statuses = Mock()
    speech.requests = Mock()
    speech.requests.get_subscription_count.return_value = 1
    speech.client = Mock()
    return speech


def test_status_failure_does_not_interrupt_terminal_bookkeeping():
    """Reporting must not strand an already-finished SPEAKER owner."""
    speech = _speech()
    speech.statuses.publish.side_effect = RuntimeError('DDS unavailable')
    request = SpeechRequest(playback_id='speech', text='hello')
    speech.receive(request)
    assert 'speech' in speech.playbacks
    speech.status(SpeechPlaybackStatus(playback_id='speech', state='finished'))
    assert not speech.playbacks
    assert speech.retired['speech'] == 'finished'
    assert speech.pending_statuses['speech'].state == 'finished'
    speech.changed.assert_called_once()
    speech.statuses.publish.side_effect = None
    speech.tick()
    assert not speech.pending_statuses
    assert speech.statuses.publish.call_args.args[0].state == 'finished'


def test_ambiguous_request_failure_keeps_playback_until_real_stop_confirmation():
    """A transport exception is not evidence of silence or a free speaker."""
    speech = _speech()
    speech.requests.publish.side_effect = RuntimeError('send unconfirmed')
    request = SpeechRequest(playback_id='speech', text='hello')
    speech.receive(request)
    assert 'speech' in speech.playbacks
    assert speech.playbacks['speech'].stopping
    speech.client.call_async.assert_called_once()
    assert not speech.prepare_mission()
    speech.status(SpeechPlaybackStatus(playback_id='speech', state='stopped'))
    assert speech.prepare_mission()


def test_tts_graph_failure_does_not_escape_into_manager_callbacks():
    """A broken optional peer may reject speech, not kill mission management."""
    speech = _speech()
    speech.client.service_is_ready.side_effect = RuntimeError('transport unavailable')
    speech.receive(SpeechRequest(playback_id='speech', text='hello'))
    assert not speech.playbacks
    assert speech.retired['speech'] == 'failed'
    assert not speech._send_control(Mock()).result()
