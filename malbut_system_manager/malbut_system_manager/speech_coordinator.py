"""
Admit speech against mission ownership and track the real playback lifetime.

The TTS worker retains its bounded FIFO/priority queue, interim replacement,
prerecorded audio and device controls. Only this manager adapter can submit to
that worker. Admission and cancellation policy share the mission effects lock.
"""

from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest
from malbut_interfaces.srv import ControlSpeechPlayback
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.clock import Clock, ClockType
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy.task import Future

from .models import ExecutionResource, MissionState


TERMINAL = frozenset(('finished', 'failed', 'stopped'))
MAX_TRACKED = 256


@dataclass
class _Playback:
    request: SpeechRequest
    mission_id: str | None
    deadline: float
    stopping: bool = False
    timed_out: bool = False


class SpeechCoordinator:
    """Own public speech endpoints, with private TTS execution endpoints."""

    def __init__(self, node, *, lock, state, mission_for_goal, changed,
                 control_timeout_s=5.0, playback_timeout_s=180.0):
        self.node = node
        self.lock = lock
        self.state = state
        self.mission_for_goal = mission_for_goal
        self.changed = changed
        self.control_timeout_s = control_timeout_s
        self.playback_timeout_s = playback_timeout_s
        self.playbacks = {}
        self.retired = OrderedDict()
        self.pending_statuses = OrderedDict()
        self.controls = []
        self.closed = False
        qos = QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE)
        group = ReentrantCallbackGroup()
        self.requests = node.create_publisher(SpeechRequest, '/malbut/tts/request', qos)
        self.statuses = node.create_publisher(
            SpeechPlaybackStatus, '/malbut/speech/playback_status', qos)
        self.client = node.create_client(
            ControlSpeechPlayback, '/malbut/tts/playback_control', callback_group=group)
        self.subscription = node.create_subscription(
            SpeechRequest, '/malbut/speech/response', self.receive, qos,
            callback_group=group)
        self.status_subscription = node.create_subscription(
            SpeechPlaybackStatus, '/malbut/tts/playback_status', self.status, qos,
            callback_group=group)
        self.service = node.create_service(
            ControlSpeechPlayback, '/malbut/speech/playback_control', self.control,
            callback_group=group)
        self.timer = node.create_timer(
            0.05, self.tick, callback_group=group,
            clock=Clock(clock_type=ClockType.STEADY_TIME))

    def _emit(self, request, state):
        self.pending_statuses[request.playback_id] = SpeechPlaybackStatus(
            playback_id=request.playback_id, state=state, interim=request.interim,
            request_id=(request.request_id if request.request_type
                        != SpeechRequest.CONFIRMATION else ''),
        )
        while len(self.pending_statuses) > MAX_TRACKED:
            self.pending_statuses.popitem(last=False)
        self._flush_statuses()

    def _flush_statuses(self):
        for playback_id, message in list(self.pending_statuses.items()):
            try:
                self.statuses.publish(message)
            except Exception as error:
                self.node.get_logger().warning(f'Could not publish speech status: {error}')
                break
            self.pending_statuses.pop(playback_id)

    def _retire(self, request, state):
        self._emit(request, state)
        self.retired[request.playback_id] = state
        self.retired.move_to_end(request.playback_id)
        while len(self.retired) > MAX_TRACKED:
            self.retired.popitem(last=False)

    def receive(self, request):
        """Allow ordinary speech or speech delegated by a live SPEAKER owner."""
        with self.lock:
            if not request.playback_id:
                request.playback_id = uuid4().hex
            if request.playback_id in self.playbacks or request.playback_id in self.retired:
                return
            if (not request.text.strip() or len(request.playback_id) > 200
                    or not request.playback_id.strip() or len(request.request_id) > 256
                    or (request.request_id and not request.request_id.strip())
                    or len(request.audio_id) > 128
                    or request.request_type not in (0, 1, 2)):
                self._retire(request, 'failed')
                return
            owners = [mission for mission in self.state.active()
                      if ExecutionResource.SPEAKER in mission.resources]
            pending = [mission for mission in self.state.pending.values()
                       if ExecutionResource.SPEAKER in mission.resources]
            mission_id = None
            if request.owner_goal_id:
                mission_id = self.mission_for_goal(request.owner_goal_id)
                if not any(mission.mission_id == mission_id
                           and mission.state is MissionState.RUNNING for mission in owners):
                    self._retire(request, 'stopped')
                    return
            elif owners or pending or request.request_type == SpeechRequest.CONFIRMATION:
                self._retire(request, 'stopped')
                return
            try:
                available = (self.client.service_is_ready()
                             and self.requests.get_subscription_count() > 0)
            except Exception as error:
                available = False
                self.node.get_logger().warning(f'Could not inspect TTS endpoints: {error}')
            if (self.closed or len(self.playbacks) >= MAX_TRACKED
                    or any(item.timed_out for item in self.playbacks.values())
                    or not available):
                self._retire(request, 'failed')
                return
            self.playbacks[request.playback_id] = _Playback(
                request, mission_id, monotonic() + self.playback_timeout_s)
            # Admission is distinct from audible playback. In particular, a
            # confirmation may report its judgment after its closing utterance
            # is admitted, while the manager retains that utterance's lifetime.
            self._emit(request, 'accepted')
            try:
                self.requests.publish(request)
            except Exception as error:
                # A failed send is not proof that TTS received nothing. Keep
                # the playback owned until its terminal status confirms stop.
                self.node.get_logger().error(f'Could not send speech request: {error}')
                self._stop(self.playbacks[request.playback_id])

    def status(self, message):
        """Forward only actual states for known requests, once at termination."""
        with self.lock:
            playback = self.playbacks.get(message.playback_id)
            if playback is None or message.state not in TERMINAL | {'playing', 'paused'}:
                return
            if message.state in TERMINAL:
                del self.playbacks[message.playback_id]
                if not playback.timed_out:
                    self._retire(playback.request, message.state)
                self.changed()
            elif not playback.timed_out:
                self._emit(playback.request, message.state)

    def _send_control(self, request):
        result = Future()
        try:
            if not self.client.service_is_ready():
                result.set_result(False)
                return result
            downstream = self.client.call_async(request)
        except Exception:
            result.set_result(False)
            return result
        self.controls.append((downstream, result, monotonic() + self.control_timeout_s))
        return result

    async def control(self, request, response):
        """Relay STT/Agent controls; acceptance never means playback finished."""
        with self.lock:
            if (self.closed or request.command not in ('pause', 'resume', 'stop', 'stop_all')
                    or (request.command != 'stop_all' and (
                        not request.playback_id.strip() or len(request.playback_id) > 200))
                    or len(self.controls) >= MAX_TRACKED):
                response.accepted = False
                return response
            result = self._send_control(request)
        response.accepted = await result
        return response

    def _stop(self, playback):
        if not playback.stopping:
            playback.stopping = True
            self._send_control(ControlSpeechPlayback.Request(
                playback_id=playback.request.playback_id, command='stop'))

    def prepare_mission(self):
        """Stop earlier speech and wait for terminal status before using SPEAKER."""
        for playback in self.playbacks.values():
            self._stop(playback)
        return not self.playbacks

    def cancel_owner(self, mission_id):
        """Cancel a mission's outstanding audio without affecting other owners."""
        for playback in self.playbacks.values():
            if playback.mission_id == mission_id:
                self._stop(playback)

    def tick(self):
        """Bound service waits and report lost playback without assuming silence."""
        with self.lock:
            self._flush_statuses()
            now = monotonic()
            pending, self.controls = self.controls, []
            for downstream, result, deadline in pending:
                if downstream.done() or now >= deadline:
                    accepted = False
                    if downstream.done():
                        try:
                            accepted = downstream.result().accepted
                        except Exception:
                            pass
                    else:
                        try:
                            downstream.cancel()
                            self.client.remove_pending_request(downstream)
                        except Exception as error:
                            self.node.get_logger().warning(
                                f'Could not clean up speech control: {error}')
                    if not result.done():
                        result.set_result(accepted)
                else:
                    self.controls.append((downstream, result, deadline))
            for playback in self.playbacks.values():
                if now >= playback.deadline and not playback.timed_out:
                    playback.timed_out = True
                    self._retire(playback.request, 'failed')
                    self._stop(playback)
                    self.node.get_logger().error(
                        'Speech playback timed out; SPEAKER stays blocked until TTS stops')
            self.changed()

    def close(self):
        """Stop admission and request cancellation of every tracked playback."""
        with self.lock:
            self.closed = True
            for playback in self.playbacks.values():
                self._stop(playback)
