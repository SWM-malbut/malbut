"""ROS speech dialogue with independent Manager communication."""

import argparse
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import select
import sqlite3
import sys
import time
from typing import Optional, Sequence

from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.mission_speech import MissionAnnouncer
from malbut_agent_server.speech_dialogue import (
    DialogueWorker, validate_dialogue_input, validate_interruption_input,
)
from malbut_agent_server.speech_receipts import SpeechReceiptStore
from malbut_agent_server.speech_receiver import (
    DEFAULT_DB_PATH, TRANSCRIPT_TOPIC, receive_transcript,
)
from malbut_agent_server.weather_query import ManagerWeatherQuery


RESPONSE_TOPIC = '/malbut/speech/response'
ADDRESSEE_SERVICE = '/malbut/speech/classify_addressee'
MAX_PENDING_ADDRESSEE_REQUESTS = 128
MAX_COMMAND_BYTES = 65536
DEFAULT_CONVERSATION_DB = '~/.local/state/malbut/speech-dialogue.sqlite3'
DEFAULT_SPEECH_USER = 'speech-development-user'


def create_communication_node(
    *, speech_db_path=DEFAULT_DB_PATH, on_event=None,
    goal_response_timeout_s=5.0,
    dialogue_settings=None, dialogue_factory=None,
    weather_query_timeout_s=20.0,
):
    """Compose communication on one owning thread with a single executor."""
    from malbut_interfaces.msg import SpeechRequest, SpeechTranscript
    from malbut_interfaces.srv import ClassifySpeechAddressee
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
    )
    from rclpy.task import Future

    from malbut_agent_server.manager_client import ManagerClient

    settings = dialogue_settings or Settings(user_id=DEFAULT_SPEECH_USER)
    settings.validate_for_dialogue()

    class CommunicationNode(Node):
        """Expose communication functions without invoking inference."""

        def __init__(self):
            super().__init__('malbut_agent_communication')
            self.missions = None
            self.dialogue = None
            self._receipts = None
            self._closing = False
            self._speech_ready = False
            self._addressee_waiters = {}
            self._addressee_callbacks = 0
            self.weather_query = None
            try:
                qos = QoSProfile(
                    history=HistoryPolicy.KEEP_LAST, depth=10,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.VOLATILE,
                )
                self._speech_qos = qos
                self._speech = self.create_publisher(
                    SpeechRequest, RESPONSE_TOPIC, qos,
                )
                self._announcer = MissionAnnouncer(
                    lambda text: self.say(
                        text, request_type=SpeechRequest.NOTIFICATION,
                    ),
                )
                self._receipts = SpeechReceiptStore(speech_db_path)
                self.missions = ManagerClient(
                    self, on_event=self._mission_event,
                    goal_response_timeout_s=goal_response_timeout_s,
                )
                self.weather_query = ManagerWeatherQuery(
                    self.missions, timeout_s=weather_query_timeout_s,
                )

                def runtime_factory():
                    runtime = (
                        dialogue_factory() if dialogue_factory is not None
                        else build_orchestrator(settings, http_server=False)
                    )
                    runtime.weather_executor = self.weather_query.execute
                    runtime.weather_location_executor = self.weather_query.set_location
                    return runtime

                self.dialogue = DialogueWorker(
                    runtime_factory, settings.user_id,
                )
                self.create_timer(0.05, self._drain_dialogue)
                self._start_speech_inputs()
            except Exception:
                self.destroy_node()
                raise

        def _start_speech_inputs(self):
            # Bringup discovers these endpoints before starting the microphone.
            # A running worker thread alone does not mean its DB/session is ready.
            if self._speech_ready or not self.dialogue.ready:
                return
            self.create_subscription(
                SpeechTranscript, TRANSCRIPT_TOPIC,
                self._receive_speech, self._speech_qos,
            )
            self.create_service(
                ClassifySpeechAddressee, ADDRESSEE_SERVICE,
                self._classify_addressee,
                callback_group=ReentrantCallbackGroup(),
            )
            self._speech_ready = True
            self.get_logger().info('speech_dialogue_ready; speech input endpoints started')

        def say(self, text, request_type=SpeechRequest.DIALOGUE):
            """Publish text without claiming playback completion."""
            if not isinstance(text, str) or not text.strip():
                return False
            if self._closing or not self.context.ok():
                return False
            self._speech.publish(SpeechRequest(
                text=text, request_type=request_type,
            ))
            return True

        def _receive_speech(self, message):
            if self._closing:
                return
            utterance_id, text = message.utterance_id, message.text
            try:
                validate_dialogue_input(utterance_id, text)
                previous = self._receipts.lookup(utterance_id, text)
            except ValueError:
                self.get_logger().warning('speech_dialogue invalid input')
                return
            except sqlite3.Error:
                self.get_logger().error('speech_dialogue receipt unavailable')
                return
            if previous is not None:
                receive_transcript(
                    self._receipts, utterance_id, text, self.get_logger(),
                )
                return
            if self.dialogue.startup_error:
                self.get_logger().error(
                    f'speech_dialogue startup failed: {self.dialogue.startup_error}')
                self.say('대화 처리를 준비하지 못했어요. 실행 설정을 확인해 주세요.')
                return
            if not self.dialogue.has_capacity():
                self.get_logger().warning('speech_dialogue busy; not accepted')
                self.say('앞선 대화를 처리하고 있어요. 잠시 뒤 다시 말씀해 주세요.')
                return
            outcome = receive_transcript(
                self._receipts, utterance_id, text, self.get_logger(),
            )
            if outcome == 'received' and not self.dialogue.submit(
                utterance_id, text,
            ):
                self.get_logger().error('speech_dialogue submission failed')
                self.say('대화를 처리하지 못했어요. 다시 말씀해 주세요.')

        async def _classify_addressee(self, request, response):
            """Yield to the executor while the dialogue worker classifies."""
            response.decision = ClassifySpeechAddressee.Response.UNKNOWN
            if self._closing or not self.context.ok():
                return response
            if self._addressee_callbacks >= MAX_PENDING_ADDRESSEE_REQUESTS:
                return response
            try:
                validate_interruption_input(
                    request.utterance_id, request.playback_id, request.text,
                )
                accepted = self.dialogue.submit_interruption(
                    request.utterance_id, request.playback_id, request.text,
                )
            except ValueError:
                accepted = False
            if not accepted:
                return response
            key = (request.utterance_id, request.playback_id)
            future = Future(executor=self.executor)
            self._addressee_waiters.setdefault(key, []).append(future)
            self._addressee_callbacks += 1
            try:
                response.decision = await future
                if self._closing:
                    response.decision = ClassifySpeechAddressee.Response.UNKNOWN
                return response
            finally:
                self._addressee_callbacks -= 1
                waiters = self._addressee_waiters.get(key, [])
                if future in waiters:
                    waiters.remove(future)
                    if not waiters:
                        del self._addressee_waiters[key]

        def _resolve_addressee(self, result):
            decision = result['decision']
            if decision not in (
                ClassifySpeechAddressee.Response.ADDRESSED,
                ClassifySpeechAddressee.Response.NOT_ADDRESSED,
                ClassifySpeechAddressee.Response.UNKNOWN,
            ):
                decision = ClassifySpeechAddressee.Response.UNKNOWN
            key = (result['utterance_id'], result['playback_id'])
            for future in self._addressee_waiters.pop(key, []):
                if not future.done():
                    future.set_result(decision)

        def _drain_dialogue(self):
            if self._closing:
                return
            self.weather_query.drain()
            if self.dialogue.startup_error:
                self.get_logger().error(
                    f'speech_dialogue startup failed: {self.dialogue.startup_error}')
                raise RuntimeError('speech_dialogue_startup_failed')
            self._start_speech_inputs()
            for response in self.dialogue.drain():
                if response.get('kind') == 'addressee':
                    self._resolve_addressee(response)
                    continue
                published = self.dialogue.publish_reply(response, self.say)
                if published is not None:
                    self.get_logger().info(json.dumps({
                        'event': 'dialogue_response_published', **published,
                    }, ensure_ascii=False))

        def _mission_event(self, event):
            self.get_logger().info(json.dumps(
                {'event': 'mission_event', **event}, ensure_ascii=False,
            ))
            weather_event = (
                self.weather_query is not None
                and self.weather_query.handle(event)
            )
            text = None if weather_event else self._announcer.handle(event)
            if text is not None:
                self.get_logger().info(json.dumps({
                    'event': 'speech_published',
                    'request_id': event['request_id'], 'text': text,
                }, ensure_ascii=False))
            if on_event is not None:
                on_event(dict(event))

        def begin_shutdown(self):
            """Reject new work and release classification responses as unknown."""
            self._closing = True
            for waiters in self._addressee_waiters.values():
                for future in waiters:
                    if not future.done():
                        future.set_result(ClassifySpeechAddressee.Response.UNKNOWN)
            self._addressee_waiters.clear()

        def destroy_node(self):
            """Release communication from the owning thread outside callbacks."""
            self.begin_shutdown()
            if self.executor is not None and self.context.ok():
                from rclpy.executors import ExternalShutdownException

                deadline = time.monotonic() + 1.0
                try:
                    while (self._addressee_callbacks and self.context.ok()
                           and time.monotonic() < deadline):
                        self.executor.spin_once(timeout_sec=0.01)
                except (KeyboardInterrupt, ExternalShutdownException):
                    pass
            if self.weather_query is not None:
                self.weather_query.close()
                self.weather_query.drain()
            try:
                if self.dialogue is not None:
                    self.dialogue.close()
            finally:
                try:
                    if self.missions is not None:
                        self.missions.close()
                finally:
                    try:
                        if self._receipts is not None:
                            self._receipts.close()
                    finally:
                        destroyed = super().destroy_node()
            return destroyed

    return CommunicationNode()


def apply_command(node, command):
    """Run a structured developer command, not natural speech."""
    if not isinstance(command, dict):
        raise ValueError('command must be a JSON object')
    op = command.get('op')
    allowed = {
        'say': {'op', 'text'},
        'submit': {'op', 'capability_id', 'arguments', 'request_id'},
        'cancel': {'op', 'request_id'},
        'status': {'op', 'request_id'},
    }
    if op not in allowed or set(command) - allowed[op]:
        raise ValueError('unsupported operation or command fields')
    if op == 'say':
        if not node.say(command.get('text')):
            raise ValueError('text must be non-empty and ROS must be running')
        return {'published': True}
    if op == 'submit':
        request_id = node.missions.submit(
            command.get('capability_id'), command.get('arguments'),
            request_id=command.get('request_id'),
        )
    else:
        request_id = command.get('request_id')
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError('request_id is required')
        if op == 'cancel':
            node.missions.cancel(request_id)
    return node.missions.snapshot(request_id)


class CommandLines:
    """Bound partial stdin input without blocking ROS feedback and cancel."""

    def __init__(self):
        """Start an empty command buffer."""
        self._buffer = bytearray()
        self._discarding = False

    def feed(self, data):
        """Yield complete lines, with None for each oversized command."""
        for fragment in data.splitlines(keepends=True):
            complete = fragment.endswith(b'\n')
            if not self._discarding:
                if len(self._buffer) + len(fragment) > MAX_COMMAND_BYTES:
                    self._buffer.clear()
                    self._discarding = True
                    yield None
                else:
                    self._buffer.extend(fragment)
            if complete:
                if not self._discarding:
                    yield bytes(self._buffer)
                self._buffer.clear()
                self._discarding = False


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run speech dialogue and explicit Manager commands independently."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-path', default=DEFAULT_DB_PATH)
    parser.add_argument('--goal-response-timeout-s', type=float, default=5.0)
    parser.add_argument('--env-file')
    parser.add_argument(
        '--provider', choices=('mock', 'openai', 'rai-sidecar'),
    )
    parser.add_argument('--model')
    parser.add_argument('--conversation-db', default=DEFAULT_CONVERSATION_DB)
    parser.add_argument('--user-id', default=DEFAULT_SPEECH_USER)
    parser.add_argument('--check', action='store_true')
    args, ros_args = parser.parse_known_args(argv)
    try:
        settings = dialogue_settings_from_args(args)
    except (ValueError, OSError):
        print('Invalid speech dialogue settings. Check provider and paths.',
              file=sys.stderr)
        return 2
    if args.check:
        print('speech dialogue configuration: ok')
        return 0
    try:
        import rclpy
        from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
    except ImportError:
        print('Source ROS 2 and the built workspace first.', file=sys.stderr)
        return 2
    node = None
    executor = None
    initialized = False
    try:
        rclpy.init(args=ros_args)
        initialized = True
        executor = SingleThreadedExecutor()
        node = create_communication_node(
            speech_db_path=args.db_path,
            goal_response_timeout_s=args.goal_response_timeout_s,
            dialogue_settings=settings,
        )
        executor.add_node(node)
        node.get_logger().info(
            'Agent communication started; initializing speech dialogue worker. '
            'Enter JSON lines: '
            'say, submit, status, cancel. '
            'STT speech uses the existing dialogue policy; '
            'robot execution uses explicit submit commands.'
        )
        descriptor = sys.stdin.fileno()
        lines = CommandLines()
        while rclpy.ok():
            executor.spin_once(timeout_sec=0.05)
            if descriptor is None:
                continue
            if not select.select([descriptor], [], [], 0)[0]:
                continue
            data = os.read(descriptor, 4096)
            if not data:
                descriptor = None
                continue
            for line in lines.feed(data):
                if line is None:
                    node.get_logger().warning(
                        'Command exceeds 65536 bytes; discarded.',
                    )
                    continue
                if not line.strip():
                    continue
                try:
                    result = apply_command(node, json.loads(line))
                    node.get_logger().info(json.dumps(
                        {'event': 'command_result', 'result': result},
                        ensure_ascii=False,
                    ))
                except (ValueError, KeyError, TypeError) as error:
                    node.get_logger().warning('Invalid command: ' + str(error))
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception as error:
        print('Agent communication failed: ' + type(error).__name__,
              file=sys.stderr)
        return 2
    finally:
        try:
            if node is not None:
                node.destroy_node()
        finally:
            if executor is not None:
                executor.shutdown(timeout_sec=0)
        if initialized and rclpy.ok():
            rclpy.shutdown()
    return 0


def dialogue_settings_from_args(args):
    """Keep voice development data separate from the HTTP user database."""
    if not args.conversation_db.strip() or not args.db_path.strip():
        raise ValueError('Speech database paths must not be blank')
    if (not math.isfinite(args.goal_response_timeout_s)
            or args.goal_response_timeout_s <= 0):
        raise ValueError('Goal response timeout must be positive and finite')
    if args.env_file:
        load_env_file(Path(args.env_file).expanduser())
    settings = Settings.from_env(os.environ)
    overrides = {
        'database_path': str(Path(args.conversation_db).expanduser()),
        'user_id': args.user_id,
    }
    if args.provider is not None:
        overrides['provider'] = args.provider
    if args.model is not None:
        overrides['openai_model'] = args.model
    settings = replace(settings, **overrides)
    settings.validate_for_dialogue()
    return settings


if __name__ == '__main__':
    raise SystemExit(main())
