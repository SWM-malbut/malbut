"""Manager-owned confirmation Action; no direct fall-runtime subscriptions."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import time

from malbut_agent_server.situation_session import SituationSpeechSession


ACTION_NAME = '/malbut/agent/confirm_situation'
MAX_CONFIRMATION_RECEIPTS = 128


@dataclass
class _ConfirmationReceipt:
    """Bind one logical request to its original payload and terminal result."""

    fingerprint: str
    status: str | None = None
    outcome: object = None


def _fingerprint(request):
    payload = json.dumps([request.situation_type, request.summary],
                         ensure_ascii=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('ascii')).hexdigest()


def valid_request(request):
    return all(
        isinstance(value, str) and bool(value.strip()) and len(value) <= limit
        and not any(ord(char) < 32 and char not in '\n\t' for char in value)
        for value, limit in (
            (request.request_id, 200), (request.situation_type, 100),
            (request.summary, 4000),
        )
    )


def build_situation_factory(settings):
    """Reuse the configured provider and credentials; never perform tools."""
    from malbut_agent_server.situation_dialogue import (
        MockSituationProvider, SituationDialogue,
    )
    from malbut_agent_server.situation_provider import OpenAISituationProvider

    if settings.provider == 'mock':
        return lambda: SituationDialogue(MockSituationProvider())
    if settings.provider == 'openai':
        from malbut_agent_server.factory import _openai_adapter

        return lambda: SituationDialogue(OpenAISituationProvider(
            _openai_adapter(
                settings, settings.openai_general_model or settings.openai_model,
                include_reasoning=False,
            ), user_id=settings.user_id,
        ))
    # Unsupported adapters must not silently use a keyword-only production path.
    return None


class SituationActionServer:
    def __init__(self, node, engine_factory, *, on_begin, on_end):
        from malbut_interfaces.action import ConfirmSituation
        from malbut_interfaces.msg import (
            SpeechRequest, SpeechInputStatus, SpeechPlaybackStatus,
        )
        from malbut_interfaces.srv import ControlSpeechPlayback, ControlSpeechSession
        from rclpy.action import ActionServer, CancelResponse, GoalResponse
        from rclpy.callback_groups import ReentrantCallbackGroup
        from rclpy.task import Future

        self.node = node
        self.engine_factory = engine_factory
        self.on_begin = on_begin
        self.on_end = on_end
        self._action_type = ConfirmSituation
        self._speech_type = SpeechRequest
        self._control_type = ControlSpeechPlayback
        self._session_type = ControlSpeechSession
        self._GoalResponse = GoalResponse
        self._CancelResponse = CancelResponse
        self._Future = Future
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='situation')
        self._work = set()
        self._contexts = {}
        self._recent = OrderedDict()
        self._reserved = False
        self._closed = False
        self._session = None
        self._active_goal = None
        self._stop_future = None
        self._started_at = None
        self._initializing = False
        qos = node._speech_qos
        self._speech = node.create_publisher(SpeechRequest, '/malbut/speech/response', qos)
        self._input = node.create_client(
            ControlSpeechSession, '/malbut/speech/session_control',
        )
        self._playback = node.create_client(
            ControlSpeechPlayback, '/malbut/speech/playback_control',
        )
        self._subscriptions = [
            node.create_subscription(
                SpeechPlaybackStatus, '/malbut/speech/playback_status',
                self._playback_status, qos,
            ),
            node.create_subscription(
                SpeechInputStatus, '/malbut/speech/input_status', self._input_status, qos,
            ),
        ]
        self._timer = node.create_timer(0.02, self._tick)
        self._server = ActionServer(
            node, ConfirmSituation, ACTION_NAME,
            execute_callback=self._execute,
            goal_callback=self._goal,
            cancel_callback=self._cancel,
            handle_accepted_callback=self._accepted,
            callback_group=ReentrantCallbackGroup(),
        )

    @property
    def active(self):
        return self._reserved or self._session is not None

    def _goal(self, request):
        self._work = {future for future in self._work if not future.done()}
        if (self._closed or not valid_request(request)
                or len(self._contexts) >= MAX_CONFIRMATION_RECEIPTS):
            return self._GoalResponse.REJECT
        previous = self._recent.get(request.request_id)
        if previous is not None:
            # Manager may have restarted after sending the original Goal.
            # A matching completed request needs neither audio nor a model.
            if previous.fingerprint != _fingerprint(request) or previous.status is None:
                return self._GoalResponse.REJECT
            return self._GoalResponse.ACCEPT
        if (self.active or self.engine_factory is None or len(self._work) >= 2
                or not self.node.dialogue.ready
                or not self._input.service_is_ready()
                or not self._playback.service_is_ready()
                or self._speech.get_subscription_count() == 0):
            return self._GoalResponse.REJECT
        self._reserved = True
        return self._GoalResponse.ACCEPT

    @staticmethod
    def _key(goal):
        return bytes(goal.goal_id.uuid)

    def _accepted(self, goal):
        key = self._key(goal)
        future = self._Future(executor=self.node.executor)
        self._contexts[key] = (goal, future)
        receipt = self._recent.get(goal.request.request_id)
        if receipt is not None and receipt.status is not None:
            # Replay has a separate ROS Goal UUID and must not acquire or
            # release the live dialogue's reservation, microphone, or audio.
            self._recent.move_to_end(goal.request.request_id)
            future.set_result((receipt.status, receipt.outcome))
            goal.execute()
            return
        self._active_goal = goal
        receipt = _ConfirmationReceipt(_fingerprint(goal.request))
        self._recent[goal.request.request_id] = receipt
        while len(self._recent) > MAX_CONFIRMATION_RECEIPTS:
            self._recent.popitem(last=False)
        self._started_at = time.monotonic()

        def report(result):
            # Save before sending the Action result and before closing audio.
            # A later playback failure cannot retract a delivered judgment.
            receipt.status, receipt.outcome = 'succeeded', result
            if not future.done():
                future.set_result(('succeeded', result))

        def finish(status, result):
            if receipt.status is None:
                receipt.status, receipt.outcome = status, result
            if not future.done():
                future.set_result((status, result))
            self._session = None
            self._active_goal = None
            self._reserved = False
            self._initializing = False
            self.on_end()

        self._session = SituationSpeechSession(
            goal.request, self.engine_factory, self, finish,
            executor=self, on_result=report,
        )
        self._initializing = True
        try:
            self.on_begin()
            self._stop_future = self._playback.call_async(self._control_type.Request(
                playback_id='', command=self._control_type.Request.STOP_ALL,
            ))
        except Exception:
            self._session.abort()
        goal.execute()

    def submit(self, fn, *args, **kwargs):
        future = self._pool.submit(fn, *args, **kwargs)
        self._work.add(future)
        return future

    async def _execute(self, goal):
        key = self._key(goal)
        try:
            status, outcome = await self._contexts[key][1]
            response = self._action_type.Result()
            if status == 'succeeded' and outcome is not None:
                response.situation_assessment = outcome.situation_assessment
                response.help_needed = outcome.help_needed
                goal.succeed()
            elif status == 'canceled' and goal.is_cancel_requested:
                goal.canceled()
            else:
                # A replay of a canceled original is a new executing Goal,
                # not a CANCELING Goal. Abort it without a fabricated result.
                goal.abort()
            return response
        finally:
            self._contexts.pop(key, None)

    def _cancel(self, goal):
        if self._active_goal is not None and self._key(goal) == self._key(self._active_goal):
            return self._CancelResponse.ACCEPT
        return self._CancelResponse.REJECT

    def _tick(self):
        session = self._session
        if session is None or self._closed:
            return
        if self._active_goal.is_cancel_requested:
            session.cancel()
            return
        if time.monotonic() - self._started_at >= 600:
            session.abort()
            return
        if self._initializing:
            if not self._stop_future.done():
                if time.monotonic() - self._started_at >= 5:
                    session.abort()
                return
            try:
                if not self._stop_future.result().accepted:
                    raise RuntimeError('speech preemption rejected')
                self._initializing = False
                session.start()
            except Exception:
                session.abort()
                return
        session.tick()

    def open_session(self, session_id):
        if not self._input.service_is_ready():
            raise RuntimeError('speech input unavailable')
        future = self._input.call_async(self._session_type.Request(
            session_id=session_id, active=True,
        ))

        def ready(result):
            if result.cancelled():
                return
            try:
                if result.result().accepted and not result.result().barge_in_available:
                    self.node.get_logger().warning(
                        'confirmation_barge_in_unavailable: configure microphone AEC',
                    )
            except Exception:
                pass

        future.add_done_callback(ready)
        return future

    def verify_session(self, session_id):
        """Check the existing STT session without reviving it after a restart."""
        if not self._input.service_is_ready():
            raise RuntimeError('speech input unavailable')
        return self._input.call_async(self._session_type.Request(
            session_id=session_id, check_only=True,
        ))

    def close_session(self, session_id):
        try:
            if not self._input.service_is_ready():
                raise RuntimeError('speech input unavailable')
            self._input.call_async(self._session_type.Request(
                session_id=session_id, active=False,
            ))
        except Exception as error:
            self.node.get_logger().warning(
                'confirmation_input_close_failed:' + type(error).__name__)
            raise

    def speak(self, text, playback_id):
        if self._closed or not self.node.context.ok():
            return False
        self._speech.publish(self._speech_type(
            text=text, request_type=self._speech_type.CONFIRMATION,
            playback_id=playback_id,
        ))
        return True

    def stop(self, playback_id):
        try:
            if not self._playback.service_is_ready():
                raise RuntimeError('speech playback control unavailable')
            self._playback.call_async(self._control_type.Request(
                playback_id=playback_id, command=self._control_type.Request.STOP,
            ))
        except Exception as error:
            self.node.get_logger().warning(
                'confirmation_playback_stop_failed:' + type(error).__name__)
            raise

    def _playback_status(self, message):
        if self._session is not None:
            self._session.playback(message.playback_id, message.state)

    def _input_status(self, message):
        if self._session is not None:
            self._session.input_status(
                message.session_id, message.utterance_id, message.state,
            )

    def transcript(self, message):
        if self._session is not None:
            return self._session.transcript(
                message.session_id, message.utterance_id, message.text,
            )
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._session is not None:
            self._session.abort()
        for _, future in tuple(self._contexts.values()):
            if not future.done():
                future.set_result(('aborted', None))
        self._pool.shutdown(wait=False, cancel_futures=True)

    def destroy(self):
        self.close()
        self._server.destroy()
