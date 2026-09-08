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
from typing import Optional, Sequence

from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.mission_speech import MissionAnnouncer
from malbut_agent_server.speech_dialogue import (
    DialogueWorker, validate_dialogue_input,
)
from malbut_agent_server.speech_receipts import SpeechReceiptStore
from malbut_agent_server.speech_receiver import (
    DEFAULT_DB_PATH, TRANSCRIPT_TOPIC, receive_transcript,
)


RESPONSE_TOPIC = '/malbut/speech/response'
MAX_COMMAND_BYTES = 65536
DEFAULT_CONVERSATION_DB = '~/.local/state/malbut/speech-dialogue.sqlite3'
DEFAULT_SPEECH_USER = 'speech-development-user'


def create_communication_node(
    *, speech_db_path=DEFAULT_DB_PATH, on_event=None,
    goal_response_timeout_s=5.0,
    dialogue_settings=None, dialogue_factory=None,
):
    """Compose communication on one owning thread with a single executor."""
    from malbut_interfaces.msg import SpeechRequest, SpeechTranscript
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
    )

    from malbut_agent_server.manager_client import ManagerClient

    settings = dialogue_settings or Settings(user_id=DEFAULT_SPEECH_USER)
    settings.validate_for_dialogue()
    runtime_factory = dialogue_factory or (
        lambda: build_orchestrator(settings, http_server=False)
    )

    class CommunicationNode(Node):
        """Expose communication functions without invoking inference."""

        def __init__(self):
            super().__init__('malbut_agent_communication')
            self.missions = None
            self.dialogue = None
            self._receipts = None
            self._closing = False
            self._dialogue_error_seen = False
            try:
                qos = QoSProfile(
                    history=HistoryPolicy.KEEP_LAST, depth=10,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.VOLATILE,
                )
                self._speech = self.create_publisher(
                    SpeechRequest, RESPONSE_TOPIC, qos,
                )
                self._announcer = MissionAnnouncer(self.say)
                self._receipts = SpeechReceiptStore(speech_db_path)
                self.dialogue = DialogueWorker(
                    runtime_factory, settings.user_id,
                )
                self.create_subscription(
                    SpeechTranscript, TRANSCRIPT_TOPIC,
                    self._receive_speech, qos,
                )
                self.create_timer(0.05, self._drain_dialogue)
                self.missions = ManagerClient(
                    self, on_event=self._mission_event,
                    goal_response_timeout_s=goal_response_timeout_s,
                )
            except Exception:
                self.destroy_node()
                raise

        def say(self, text):
            """Publish text without claiming playback completion."""
            if not isinstance(text, str) or not text.strip():
                return False
            if self._closing or not self.context.ok():
                return False
            self._speech.publish(SpeechRequest(text=text))
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
                self.get_logger().error('speech_dialogue startup failed')
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

        def _drain_dialogue(self):
            if self._closing:
                return
            if self.dialogue.startup_error and not self._dialogue_error_seen:
                self._dialogue_error_seen = True
                self.get_logger().error('speech_dialogue startup failed')
            for response in self.dialogue.drain():
                if self.say(response['text']):
                    self.get_logger().info(json.dumps({
                        'event': 'dialogue_response_published', **response,
                    }, ensure_ascii=False))

        def _mission_event(self, event):
            self.get_logger().info(json.dumps(
                {'event': 'mission_event', **event}, ensure_ascii=False,
            ))
            text = self._announcer.handle(event)
            if text is not None:
                self.get_logger().info(json.dumps({
                    'event': 'speech_published',
                    'request_id': event['request_id'], 'text': text,
                }, ensure_ascii=False))
            if on_event is not None:
                on_event(dict(event))

        def destroy_node(self):
            """Release communication without claiming to stop any mission."""
            self._closing = True
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
        from rclpy.executors import ExternalShutdownException
    except ImportError:
        print('Source ROS 2 and the built workspace first.', file=sys.stderr)
        return 2
    node = None
    initialized = False
    try:
        rclpy.init(args=ros_args)
        initialized = True
        node = create_communication_node(
            speech_db_path=args.db_path,
            goal_response_timeout_s=args.goal_response_timeout_s,
            dialogue_settings=settings,
        )
        node.get_logger().info(
            'Agent communication ready; speech dialogue worker started. '
            'Enter JSON lines: '
            'say, submit, status, cancel. '
            'STT speech uses the existing dialogue policy; '
            'robot execution uses explicit submit commands.'
        )
        descriptor = sys.stdin.fileno()
        lines = CommandLines()
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.05)
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
        if node is not None:
            node.destroy_node()
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
