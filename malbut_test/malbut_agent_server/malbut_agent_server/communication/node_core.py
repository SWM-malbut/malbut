"""Top-level mixin backing the ROS CommunicationNode lifecycle."""

from collections import deque
from dataclasses import dataclass
import time
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from malbut_agent_server.communication.addressee import (
    MAX_PENDING_ADDRESSEE_REQUESTS,
    cancel_addressee_waiters,
    classify_addressee_request,
    resolve_addressee_result,
)
from malbut_agent_server.communication.dialogue_pump import (
    drain_dialogue,
    drain_preparations,
    end_voice_turn,
    handle_mission_event,
)
from malbut_agent_server.communication.fall_window import (
    answer_during_fall_check,
    follow_fall_window,
    watch_fall_check,
)
from malbut_agent_server.mission_speech import MissionAnnouncer
from malbut_agent_server.speech_dialogue import MAX_SPEECH_ID_LENGTH
from malbut_agent_server.speech_mission_policy import configure_speech_missions
from malbut_agent_server.speech_missions import SpeechMissions
from malbut_agent_server.speech_receipts import SpeechReceiptStore
from malbut_agent_server.turn_metrics import TurnWaterfallRecorder
from malbut_agent_server.weather_query import ManagerWeatherQuery


RESPONSE_TOPIC = '/malbut/speech/response'
END_DIALOGUE_TOPIC = '/malbut/speech/end_dialogue'
INPUT_STATUS_TOPIC = '/malbut/speech/input_status'
ADDRESSEE_SERVICE = '/malbut/speech/classify_addressee'


@dataclass(frozen=True)
class CommunicationDeps:
    """Injected ROS message types and runtime factories for CommunicationNode."""

    speech_input_status_cls: Any
    speech_request_cls: Any
    speech_transcript_cls: Any
    classify_addressee_srv: Any
    reentrant_callback_group_cls: Any
    durability_policy: Any
    history_policy: Any
    qos_profile_cls: Any
    reliability_policy: Any
    future_cls: Any
    string_msg_cls: Any
    manager_client_cls: Any
    dialogue_worker_cls: Any
    situation_action_server_cls: Any
    build_orchestrator_fn: Callable[..., Any]
    build_situation_factory_fn: Callable[..., Any]
    receive_transcript_fn: Callable[..., Any]
    transcript_topic: str
    max_pending_addressee_provider: Callable[[], int] = (
        lambda: MAX_PENDING_ADDRESSEE_REQUESTS
    )


class CommunicationNodeCore:
    """Core communication behavior decoupled from lazy rclpy Node subclassing."""

    _deps: CommunicationDeps
    _settings: Any
    _on_event: Optional[Callable[[Dict[str, Any]], None]]
    _prerecorded_audio: bool
    missions: Any
    speech_missions: Any
    _foreground_cancel: Any
    dialogue: Any
    _receipts: Any
    _closing: bool
    _speech_ready: bool
    _preparation: Any
    _preparation_type: Any
    _input_utterance_id: Optional[str]
    _seen_input_ids: Any
    _addressee_waiters: Dict[Any, Any]
    _addressee_callbacks: int
    weather_query: Any
    situation: Any
    _fall_check: Any
    _patrol_chat: Any
    _in_fall_window: bool
    _awaiting_reply: Optional[str]
    _speech_qos: Any
    _speech: Any
    _dialogue_end: Any
    _announcer: Any
    _dialogue_wake: Any
    _scene: Any
    turn_metrics: Any
    _key_health: Any
    _fall_voice: Any
    _fall_voice_message: Any
    _speech_stop: Any

    def _init_communication(
        self,
        *,
        deps: CommunicationDeps,
        settings: Any,
        speech_db_path: str,
        on_event: Optional[Callable[[Dict[str, Any]], None]],
        goal_response_timeout_s: float,
        dialogue_factory: Optional[Callable[[], Any]],
        weather_query_timeout_s: float,
        situation_factory: Optional[Callable[..., Any]],
        enable_manager_commands: bool,
        prerecorded_audio: bool,
        confirmation_answers: Any,
    ) -> None:
        self._deps = deps
        self._settings = settings
        self._on_event = on_event
        self._prerecorded_audio = prerecorded_audio
        self.missions = None
        self.speech_missions = None
        self._foreground_cancel = None
        self.dialogue = None
        self._receipts = None
        self._closing = False
        self._speech_ready = False
        self._preparation = None
        self._input_utterance_id = None
        self._seen_input_ids = deque(maxlen=128)
        self._addressee_waiters = {}
        self._addressee_callbacks = 0
        self.weather_query = None
        self.situation = None
        self._fall_check = None
        self._patrol_chat = None
        # Between the suspicion and the end of the question (2026-10-13).
        self._in_fall_window = False
        # The last utterance STT waits on until its reply has played.
        self._awaiting_reply = None
        try:
            self._wire_communication_runtime(
                speech_db_path=speech_db_path,
                goal_response_timeout_s=goal_response_timeout_s,
                dialogue_factory=dialogue_factory,
                weather_query_timeout_s=weather_query_timeout_s,
                situation_factory=situation_factory,
                enable_manager_commands=enable_manager_commands,
                prerecorded_audio=prerecorded_audio,
                confirmation_answers=confirmation_answers,
            )
        except Exception:
            self.destroy_node()  # type: ignore[attr-defined]
            raise

    def _wire_communication_runtime(
        self,
        *,
        speech_db_path: str,
        goal_response_timeout_s: float,
        dialogue_factory: Optional[Callable[[], Any]],
        weather_query_timeout_s: float,
        situation_factory: Optional[Callable[..., Any]],
        enable_manager_commands: bool,
        prerecorded_audio: bool,
        confirmation_answers: Any,
    ) -> None:
        from rcl_interfaces.msg import ParameterDescriptor

        node: Any = self
        deps = self._deps
        settings = self._settings
        # Startup-only override, matching Piper's per-robot YAML setting.
        prefetch_enabled = node.declare_parameter(
            'speech_prefetch_enabled', settings.speech_prefetch_enabled,
            ParameterDescriptor(read_only=True),
        ).value
        if type(prefetch_enabled) is not bool:
            raise ValueError('speech_prefetch_enabled must be a boolean')
        qos = deps.qos_profile_cls(
            history=deps.history_policy.KEEP_LAST, depth=10,
            reliability=deps.reliability_policy.RELIABLE,
            durability=deps.durability_policy.VOLATILE,
        )
        self._speech_qos = qos
        self._speech = node.create_publisher(
            deps.speech_request_cls, RESPONSE_TOPIC, qos,
        )
        self._dialogue_end = node.create_publisher(
            deps.string_msg_cls,
            END_DIALOGUE_TOPIC,
            deps.qos_profile_cls(
                depth=1,
                reliability=deps.reliability_policy.RELIABLE,
                durability=deps.durability_policy.TRANSIENT_LOCAL,
            ),
        )
        if prefetch_enabled:
            from malbut_interfaces.msg import SpeechPreparation  # type: ignore[attr-defined]

            self._preparation_type = SpeechPreparation
            self._preparation = node.create_publisher(
                SpeechPreparation, '/malbut/tts/prepare', qos,
            )
        if settings.provider == 'openai':
            # How the OpenAI key is doing, for key_sync to tell the web.
            from malbut_agent_server.factory import openai_key_for
            from malbut_agent_server.ros_key_health import KeyHealthPublisher

            self._key_health = KeyHealthPublisher(self, openai_key_for(settings))
        self._announcer = MissionAnnouncer(
            lambda text: self.say(
                text, request_type=deps.speech_request_cls.NOTIFICATION,
            ),
            speak_audio=(lambda text, audio_id: self.say(
                text,
                request_type=deps.speech_request_cls.NOTIFICATION,
                audio_id=audio_id,
            )) if prerecorded_audio else None,
        )
        self._receipts = SpeechReceiptStore(speech_db_path)
        self.missions = deps.manager_client_cls(
            self,
            on_event=self._mission_event,
            goal_response_timeout_s=goal_response_timeout_s,
        )
        if enable_manager_commands:
            from malbut_agent_server.foreground_cancel import ForegroundCancellation

            self._foreground_cancel = ForegroundCancellation(self)
            # Without Manager no fall check drives or asks: no window either.
            self._fall_check = self._watch_fall_check()
            self.speech_missions = SpeechMissions(
                self.missions,
                cancel_foreground=self._foreground_cancel,
                move_refusal=self._fall_check.move_refusal,
            )
            state_qos = deps.qos_profile_cls(depth=1)
            state_qos.reliability = deps.reliability_policy.RELIABLE
            state_qos.durability = deps.durability_policy.TRANSIENT_LOCAL
            node.create_subscription(
                deps.string_msg_cls,
                '/malbut/localization/state',
                lambda message: self.speech_missions.observe_localization(message.data),
                state_qos,
            )
        self._dialogue_wake = node.create_guard_condition(self._drain_dialogue)
        self.weather_query = ManagerWeatherQuery(
            self.missions,
            timeout_s=weather_query_timeout_s,
            on_pending=self._dialogue_wake.trigger,
        )

        def runtime_factory() -> Any:
            runtime = (
                dialogue_factory() if dialogue_factory is not None
                else deps.build_orchestrator_fn(settings, http_server=False)
            )
            runtime.weather_executor = self.weather_query.execute
            runtime.weather_location_executor = self.weather_query.set_location
            if self.speech_missions is not None:
                configure_speech_missions(runtime)
            return runtime

        from malbut_agent_server.speech_scene import RosSpeechScene

        try:
            self._scene = RosSpeechScene(self, self.speech_missions)
        except ImportError:
            self._scene = None
            node.get_logger().warning('speech_scene_unavailable; continuing text dialogue')
        self.turn_metrics = TurnWaterfallRecorder(
            capacity=settings.turn_metrics_capacity,
        )
        self.dialogue = deps.dialogue_worker_cls(
            runtime_factory,
            settings.user_id,
            on_output=self._dialogue_wake.trigger,
            publish_before_save=settings.speech_publish_before_save,
            turn_metrics=self.turn_metrics,
            **({'prefetch_enabled': True} if prefetch_enabled else {}),
            **({'scene_source': self._scene} if self._scene is not None else {}),
            **({'missions': self.speech_missions}
               if self.speech_missions is not None else {}),
        )
        self.situation = deps.situation_action_server_cls(
            self,
            situation_factory or deps.build_situation_factory_fn(settings),
            on_begin=self._begin_situation,
            on_end=self._end_situation,
            prerecorded_audio=prerecorded_audio,
            confirmation_answers=confirmation_answers,
        )
        # Replies, startup and tool requests wake the executor directly.
        # Keep a slow fallback for a coalesced/missed notification.
        node.create_timer(1.0, self._drain_dialogue)
        node.create_timer(0.25, self._follow_fall_window)
        self._start_speech_inputs()
        # Keep spontaneous conversation opt-in while it is being stabilized.
        if node.declare_parameter('patrol_conversation_enabled', False).value:
            try:
                from malbut_agent_server.patrol_conversation_ros import (
                    RosPatrolConversation,
                )
                self._patrol_chat = RosPatrolConversation(self)
            except ImportError:
                node.get_logger().warning('patrol_conversation_detector_unavailable')

    def _watch_fall_check(self) -> Any:
        return watch_fall_check(
            self,
            qos_profile_cls=self._deps.qos_profile_cls,
            reliability_policy=self._deps.reliability_policy,
            durability_policy=self._deps.durability_policy,
        )

    def _start_speech_inputs(self) -> None:
        # Bringup discovers these endpoints before starting the microphone.
        # A running worker thread alone does not mean its DB/session is ready.
        if self._speech_ready or not self.dialogue.ready:
            return
        node: Any = self
        deps = self._deps
        node.create_subscription(
            deps.speech_transcript_cls,
            deps.transcript_topic,
            node._receive_speech,
            self._speech_qos,
        )
        node.create_subscription(
            deps.speech_input_status_cls,
            INPUT_STATUS_TOPIC,
            self._receive_input_status,
            self._speech_qos,
        )
        node.create_service(
            deps.classify_addressee_srv,
            ADDRESSEE_SERVICE,
            self._classify_addressee,
            callback_group=deps.reentrant_callback_group_cls(),
        )
        self._speech_ready = True
        node.get_logger().info('speech_dialogue_ready; speech input endpoints started')

    def say(
        self,
        text: Any,
        request_type: Any = None,
        *,
        interim: bool = False,
        request_id: str = '',
        audio_id: str = '',
        playback_id: str = '',
        prepared_id: str = '',
        generation_id: str = '',
    ) -> bool:
        """Publish text without claiming playback completion."""
        node: Any = self
        speech_request_cls = self._deps.speech_request_cls
        if request_type is None:
            request_type = speech_request_cls.DIALOGUE
        if not isinstance(text, str) or not text.strip():
            return False
        if self._closing or not node.context.ok():
            return False
        if self.situation is not None and self.situation.active:
            return False
        if self._prerecorded_audio and not audio_id:
            from malbut_agent_server.mission_audio import CATALOG, TEXT_IDS
            audio_id = TEXT_IDS.get(text, '')
            if audio_id:
                text = CATALOG[audio_id]
        options: Dict[str, Any] = {'audio_id': audio_id} if audio_id else {}
        if (self._preparation is not None and prepared_id and generation_id
                and not audio_id and not interim
                and request_type == speech_request_cls.DIALOGUE):
            options.update(prepared_id=prepared_id, generation_id=generation_id)
        self._speech.publish(speech_request_cls(
            text=text,
            request_type=request_type,
            interim=interim,
            playback_id=playback_id or str(uuid4()),
            request_id=request_id,
            **options,
        ))
        return True

    def _receive_input_status(self, message: Any) -> None:
        if self._patrol_chat is not None:
            self._patrol_chat.input_status(message)
        if self._closing or message.session_id:
            return
        uid = message.utterance_id
        if (not isinstance(uid, str) or not uid.strip()
                or len(uid) > MAX_SPEECH_ID_LENGTH):
            return
        unavailable = (
            not self.dialogue.ready
            or self.dialogue.startup_error
            or not self.dialogue.has_capacity()
            or (self.situation is not None and self.situation.active)
        )
        status_cls = self._deps.speech_input_status_cls
        if message.state == status_cls.STARTED:
            if uid in self._seen_input_ids:
                return
            self._seen_input_ids.append(uid)
            self._input_utterance_id = None if unavailable else uid
        elif message.state == status_cls.ENDED and uid == self._input_utterance_id:
            self._input_utterance_id = None
            if not unavailable:
                self.say_dialogue_end()  # type: ignore[attr-defined]
        elif (message.state in (status_cls.FAILED, status_cls.FILTERED)
              and uid == self._input_utterance_id):
            self._input_utterance_id = None
            if not unavailable:
                text = (
                    '다시 말씀해 주실래요?'
                    if message.state == status_cls.FILTERED
                    else '잘 알아듣지 못했어요. 다시 제이크라고 불러 주세요.'
                )
                self.say(text, interim=False, request_id=uid)

    async def _classify_addressee(self, request: Any, response: Any) -> Any:
        return await classify_addressee_request(
            self,
            request,
            response,
            response_cls=self._deps.classify_addressee_srv.Response,
            future_cls=self._deps.future_cls,
            max_pending=self._deps.max_pending_addressee_provider(),
        )

    def _resolve_addressee(self, result: Dict[str, Any]) -> None:
        resolve_addressee_result(
            self, result, response_cls=self._deps.classify_addressee_srv.Response,
        )

    def _begin_situation(self) -> None:
        if self._patrol_chat is not None:
            self._patrol_chat.end()
        if self._fall_check is not None and self._fall_check.in_window():
            self._in_fall_window = True
        self._awaiting_reply = None
        self._input_utterance_id = None
        self.dialogue.suspend()
        cancel_addressee_waiters(
            self, unknown_decision=self._deps.classify_addressee_srv.Response.UNKNOWN,
        )

    def _end_situation(self) -> None:
        # A failed question may be asked again: the window still holds the dialogue.
        if not self._in_fall_window:
            self.dialogue.resume()

    def _follow_fall_window(self) -> None:
        follow_fall_window(self)

    def _answer_during_fall_check(self, utterance_id: str, text: str) -> None:
        answer_during_fall_check(self, utterance_id, text)

    def _drain_preparations(self) -> None:
        drain_preparations(self)

    def _end_voice_turn(self, reply: Dict[str, Any]) -> bool:
        return end_voice_turn(self, reply, string_msg_cls=self._deps.string_msg_cls)

    def _drain_dialogue(self) -> None:
        drain_dialogue(
            self,
            settings=self._settings,
            prerecorded_audio=self._prerecorded_audio,
            response_cls=self._deps.classify_addressee_srv.Response,
        )

    def _mission_event(self, event: Dict[str, Any]) -> None:
        handle_mission_event(self, event, on_event=self._on_event)

    def begin_shutdown(self) -> None:
        """Reject new work and release classification responses as unknown."""
        self._closing = True
        if self._preparation is not None and self.dialogue is not None:
            self.dialogue.cancel_preparations()
            self._drain_preparations()
        if self.situation is not None:
            self.situation.close()
        cancel_addressee_waiters(
            self, unknown_decision=self._deps.classify_addressee_srv.Response.UNKNOWN,
        )

    def _destroy_communication(self, base_destroy: Callable[[], Any]) -> Any:
        """Release communication from the owning thread outside callbacks."""
        node: Any = self
        self.begin_shutdown()
        if getattr(self, '_key_health', None) is not None:
            self._key_health.close()
        if node.executor is not None and node.context.ok():
            from rclpy.executors import ExternalShutdownException

            deadline = time.monotonic() + 1.0
            try:
                while (self._addressee_callbacks and node.context.ok()
                       and time.monotonic() < deadline):
                    node.executor.spin_once(timeout_sec=0.01)
            except (KeyboardInterrupt, ExternalShutdownException):
                pass
        if self.weather_query is not None:
            self.weather_query.close()
            self.weather_query.drain()
        try:
            if self._patrol_chat is not None:
                self._patrol_chat.close()
            if self.dialogue is not None:
                self.dialogue.close()
        finally:
            try:
                if self.missions is not None:
                    self.missions.close()
                if self._foreground_cancel is not None:
                    self._foreground_cancel.close()
            finally:
                try:
                    if self._receipts is not None:
                        self._receipts.close()
                finally:
                    if self.situation is not None:
                        self.situation.destroy()
                    if getattr(self, '_scene', None) is not None:
                        self._scene.close()
                    destroyed = base_destroy()
        return destroyed
