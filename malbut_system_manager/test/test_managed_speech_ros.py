"""Exercise Manager admission with real DDS and the real TTS queue, without audio."""

from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace

from action_msgs.msg import GoalStatus
import pytest
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.task import Future
import yaml

from malbut_interfaces.action import ExecuteMission, FollowPerson
from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest
from malbut_interfaces.srv import ControlSpeechPlayback
from malbut_system_manager.system_manager_node import SystemManagerNode
from malbut_tts.node import create_tts_node
from malbut_tts.runtime import SpeechRuntime


class _Source:
    def __init__(self):
        self.calls = []

    def generate(self, text, cancel):
        self.calls.append(text)
        if text == 'fail':
            raise RuntimeError('test synthesis failure')
        yield text, 24000


class _Player:
    def __init__(self, on_state, cancel_event):
        self.on_state = on_state
        self.cancel = cancel_event
        self.drain = Event()
        self.stopped = Event()
        self.release_on_stop = True

    def write(self, audio, sample_rate):
        self.on_state('playing')

    def finish(self):
        assert self.drain.wait(20), 'test did not drain audio'

    def stop(self):
        self.stopped.set()
        if self.release_on_stop:
            self.drain.set()

    def pause(self):
        self.on_state('paused')
        return True

    def resume(self):
        self.on_state('playing')
        return True

    def close(self):
        pass


@pytest.fixture
def speech(tmp_path, request):
    rclpy.init()
    request.addfinalizer(rclpy.try_shutdown)
    executor = SingleThreadedExecutor()
    source, recordings = _Source(), _Source()
    players, statuses, goals, completions = [], [], [], []

    def player(**kwargs):
        value = _Player(**kwargs)
        players.append(value)
        return value

    tts = create_tts_node(lambda on_status: SpeechRuntime(
        source, player, on_status, prerecorded=recordings))
    peer = Node('managed_speech_test')
    manifests = Path(__file__).resolve().parents[2] / 'malbut_interfaces/capabilities'
    manifest = yaml.safe_load((manifests / 'follow_person.yaml').read_text())
    for name, resources in (('base', ['BASE']), ('speaker', ['SPEAKER'])):
        manifest['capability']['id'] = name
        manifest['command']['name'] = '/managed_speech_test/' + name
        manifest['execution']['resources'] = resources
        (tmp_path / f'{name}.yaml').write_text(yaml.safe_dump(manifest))
    manager = SystemManagerNode(manifest_directory=str(tmp_path))

    async def execute(goal):
        goals.append(goal)
        completion = Future()
        completions.append(completion)
        await completion
        if goal.is_cancel_requested:
            goal.canceled()
        else:
            goal.succeed()
        return FollowPerson.Result()

    servers = [ActionServer(
        peer, FollowPerson, '/managed_speech_test/' + name, execute_callback=execute,
        cancel_callback=lambda _: CancelResponse.ACCEPT,
        callback_group=ReentrantCallbackGroup()) for name in ('base', 'speaker')]
    missions = ActionClient(peer, ExecuteMission, '/malbut/mission/execute')
    requests = peer.create_publisher(SpeechRequest, '/malbut/speech/response', 10)
    peer.create_subscription(
        SpeechPlaybackStatus, '/malbut/speech/playback_status', statuses.append, 100)
    control = peer.create_client(ControlSpeechPlayback, '/malbut/speech/playback_control')
    for node in (manager, peer, tts):
        executor.add_node(node)

    def until(predicate, timeout=5):
        deadline = monotonic() + timeout
        while not predicate():
            assert monotonic() < deadline, [(s.playback_id, s.state) for s in statuses]
            executor.spin_once(timeout_sec=0.005)

    def has(pid, state):
        return any(s.playback_id == pid and s.state == state for s in statuses)

    def say(pid, **kwargs):
        requests.publish(SpeechRequest(playback_id=pid, text=kwargs.pop('text', pid), **kwargs))

    def command(pid, command):
        result = control.call_async(ControlSpeechPlayback.Request(
            playback_id=pid, command=command))
        until(result.done)
        return result.result().accepted

    def mission(capability='speaker'):
        result = missions.send_goal_async(ExecuteMission.Goal(
            capability_id=capability, arguments_yaml='{}'))
        until(result.done)
        assert result.result().accepted
        return result.result()

    until(lambda: requests.get_subscription_count() == 1
          and manager._speech.requests.get_subscription_count() == 1
          and manager._speech.statuses.get_subscription_count() == 1
          and tts._status_publisher.get_subscription_count() == 1
          and manager._speech.client.service_is_ready()
          and control.service_is_ready() and missions.server_is_ready())
    try:
        yield SimpleNamespace(**locals())
    finally:
        for value in players:
            value.release_on_stop = True
            value.drain.set()
        for value in completions:
            if not value.done():
                value.set_result(None)
        manager.begin_shutdown()
        until(lambda: manager.downstream_execution_count == 0)
        tts.destroy_node()
        missions.destroy()
        for server in servers:
            server.destroy()
        manager.destroy_node()
        peer.destroy_node()
        executor.shutdown(timeout_sec=0)
        rclpy.shutdown()


def test_fifo_interim_replacement_and_recordings_survive_manager(speech):
    h = speech
    h.say('first')
    h.until(lambda: h.has('first', 'playing'))
    h.say('progress', interim=True, request_id='turn')
    h.until(lambda: h.has('progress', 'accepted'))
    h.say('answer', request_id='turn', audio_id='recorded-answer')
    h.until(lambda: h.has('progress', 'stopped'))
    assert not h.has('first', 'stopped') and h.source.calls == ['first']
    h.players[0].drain.set()
    h.until(lambda: h.has('answer', 'playing'))
    assert h.recordings.calls == ['recorded-answer']
    h.players[-1].drain.set()
    h.until(lambda: h.has('answer', 'finished'))
    assert [(s.state, s.request_id) for s in h.statuses if s.playback_id == 'answer'] == [
        ('accepted', 'turn'), ('playing', 'turn'), ('finished', 'turn')]
    h.say('answer', request_id='turn')  # A repeated ID cannot replay audio.
    h.say('barrier')
    h.until(lambda: h.has('barrier', 'playing'))
    assert h.source.calls == ['first', 'barrier']


def test_stt_controls_and_stop_before_receipt_use_manager(speech):
    h = speech
    assert h.command('late', 'stop')
    h.say('late', request_id='late-turn')
    h.until(lambda: h.has('late', 'stopped'))
    assert h.source.calls == []
    h.say('talk')
    h.until(lambda: h.has('talk', 'playing'))
    assert h.command('talk', 'pause')
    h.until(lambda: h.has('talk', 'paused'))
    assert h.command('talk', 'resume')
    assert h.command('', 'stop_all')
    h.until(lambda: h.has('talk', 'stopped'))
    assert not h.command('missing', 'pause')


def test_base_runs_with_speech_and_speaker_waits_for_actual_stop(speech):
    h = speech
    h.say('talk')
    h.until(lambda: h.has('talk', 'playing'))
    h.players[0].release_on_stop = False
    h.mission('base')
    h.until(lambda: len(h.goals) == 1)
    assert not h.has('talk', 'stopped')
    handle = h.mission()
    h.until(lambda: h.players[0].stopped.is_set())
    assert len(h.goals) == 1  # STOP acceptance has not released the device.
    h.say('blocked-normal')
    h.until(lambda: h.has('blocked-normal', 'stopped'))
    h.players[0].drain.set()
    h.until(lambda: len(h.goals) == 2)
    owner = bytes(h.goals[-1].goal_id.uuid).hex()
    h.say('question', request_type=SpeechRequest.CONFIRMATION, owner_goal_id=owner)
    h.until(lambda: h.has('question', 'playing'))
    h.completions[-1].set_result(None)
    result = handle.get_result_async()
    h.until(result.done)
    assert result.result().status == GoalStatus.STATUS_SUCCEEDED
    assert not h.has('question', 'stopped')  # Admitted closing audio outlives the result.
    h.say('stale-owner', owner_goal_id=owner)
    h.until(lambda: h.has('stale-owner', 'stopped'))
    h.players[-1].drain.set()
    h.until(lambda: h.has('question', 'finished'))


def test_cancel_owner_stops_audio_and_unknown_owner_is_rejected(speech):
    h = speech
    h.say('unowned', request_type=SpeechRequest.CONFIRMATION)
    h.say('foreign', owner_goal_id='f' * 32)
    h.until(lambda: h.has('unowned', 'stopped') and h.has('foreign', 'stopped'))
    assert h.source.calls == []
    handle = h.mission()
    h.until(lambda: len(h.goals) == 1)
    h.say('question', owner_goal_id=bytes(h.goals[0].goal_id.uuid).hex())
    h.until(lambda: h.has('question', 'playing'))
    cancel = handle.cancel_goal_async()
    h.until(cancel.done)
    h.until(lambda: h.has('question', 'stopped'))
    h.completions[0].set_result(None)
    result = handle.get_result_async()
    h.until(result.done)
    assert result.result().status == GoalStatus.STATUS_CANCELED


def test_missing_tts_and_synthesis_failure_are_terminal(speech, monkeypatch):
    h = speech
    with monkeypatch.context() as patch:
        patch.setattr(h.manager._speech.client, 'service_is_ready', lambda: False)
        h.say('unavailable', request_id='turn')
        h.until(lambda: h.has('unavailable', 'failed'))
    assert h.source.calls == []
    h.say('fail')
    h.until(lambda: h.has('fail', 'failed'))
    assert not h.manager._speech.playbacks


def test_speaker_stop_timeout_aborts_mission_without_claiming_silence(speech):
    h = speech
    h.manager._speech_stop_timeout_s = 0.15
    h.say('talk')
    h.until(lambda: h.has('talk', 'playing'))
    h.players[0].release_on_stop = False
    handle = h.mission()
    result = handle.get_result_async()
    h.until(result.done)
    assert result.result().status == GoalStatus.STATUS_ABORTED
    assert 'SPEAKER release' in result.result().result.message
    assert not h.goals and 'talk' in h.manager._speech.playbacks
    h.players[0].drain.set()
    h.until(lambda: h.has('talk', 'stopped'))


def test_cancel_while_waiting_for_speaker_never_dispatches(speech):
    h = speech
    h.say('talk')
    h.until(lambda: h.has('talk', 'playing'))
    h.players[0].release_on_stop = False
    handle = h.mission()
    h.until(lambda: bool(h.manager._speaker_waiting))
    cancel = handle.cancel_goal_async()
    h.until(cancel.done)
    result = handle.get_result_async()
    h.until(result.done)
    assert result.result().status == GoalStatus.STATUS_CANCELED
    assert not h.goals


def test_service_timeout_and_playback_timeout_remain_bounded(speech, monkeypatch):
    h = speech
    bridge = h.manager._speech
    bridge.control_timeout_s = 0.1
    pending = Future()
    with monkeypatch.context() as patch:
        patch.setattr(bridge.client, 'call_async', lambda _: pending)
        assert not h.command('unresponsive', 'stop')
    h.say('talk')
    h.until(lambda: h.has('talk', 'playing'))
    h.players[0].release_on_stop = False
    bridge.playbacks['talk'].deadline = monotonic() - 1
    h.until(lambda: h.has('talk', 'failed'))
    assert 'talk' in bridge.playbacks
    h.say('blocked')
    h.until(lambda: h.has('blocked', 'failed'))
    h.players[0].drain.set()
    h.until(lambda: 'talk' not in bridge.playbacks)
    assert len([s for s in h.statuses if s.playback_id == 'talk'
                and s.state in ('failed', 'finished', 'stopped')]) == 1
