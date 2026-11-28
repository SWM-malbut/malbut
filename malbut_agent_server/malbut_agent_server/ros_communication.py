"""ROS speech dialogue with independent Manager communication."""

import argparse
import json
from dataclasses import replace
import math
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Optional, Sequence

from malbut_agent_server.communication import (
    ADDRESSEE_SERVICE,
    CommandLines,
    CommunicationDeps,
    CommunicationNodeCore,
    END_DIALOGUE_TOPIC,
    INPUT_STATUS_TOPIC,
    MAX_COMMAND_BYTES,
    MAX_PENDING_ADDRESSEE_REQUESTS,
    RESPONSE_TOPIC,
    apply_command,
    read_commands,
    run_with_fall_rules as _run_with_fall_rules,
    spin_fall_rules,
)
from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.ros_situation import (
    SituationActionServer,
    build_situation_factory,
)
from malbut_agent_server.speech_dialogue import (
    DialogueWorker,
    SpeechInputTooLongError,
    validate_dialogue_input,
)
from malbut_agent_server.speech_receiver import (
    DEFAULT_DB_PATH,
    TRANSCRIPT_TOPIC,
    receive_transcript,
)

__all__ = [
    'ADDRESSEE_SERVICE',
    'CommandLines',
    'DEFAULT_CONVERSATION_DB',
    'DEFAULT_DB_PATH',
    'DEFAULT_SPEECH_USER',
    'END_DIALOGUE_TOPIC',
    'INPUT_STATUS_TOPIC',
    'MAX_COMMAND_BYTES',
    'MAX_PENDING_ADDRESSEE_REQUESTS',
    'RESPONSE_TOPIC',
    'TRANSCRIPT_TOPIC',
    'apply_command',
    'create_communication_node',
    'dialogue_settings_from_args',
    'main',
    'read_commands',
    'run_with_fall_rules',
    'spin_fall_rules',
]

DEFAULT_CONVERSATION_DB = '~/.local/state/malbut/speech-dialogue.sqlite3'
DEFAULT_SPEECH_USER = 'speech-development-user'


class _CommunicationSpeechMixin(CommunicationNodeCore):
    """Speech transcript admission and fixed user-facing dialogue notices."""

    def say_dialogue_end(self) -> None:
        """Speak the closing notice without starting a new turn."""
        # Playback can arrive before the end signal on the other topic. Leave
        # request_id empty so finishing this notice never opens a follow-up.
        if not self._in_fall_window:
            self.say('대화 종료할게요.')

    _demo_user_pub: Any

    def _demo_user(self, utterance_id: str, text: str, confirmation: bool) -> None:
        pub = self._demo_user_pub
        if pub.get_subscription_count():
            pub.publish(self._deps.string_msg_cls(data=json.dumps({
                'id': utterance_id, 'text': text, 'confirmation': confirmation,
            }, ensure_ascii=False)))

    def _receive_speech(self, message: Any) -> None:
        if self._closing:
            return
        if self._patrol_chat is not None and self._patrol_chat.receive(message):
            if self._patrol_chat.reply_id == message.utterance_id == self._awaiting_reply:
                self._demo_user(message.utterance_id, message.text, False)
            if message.utterance_id == self._input_utterance_id:
                self._input_utterance_id = None
            if message.utterance_id not in self._seen_input_ids:
                self._seen_input_ids.append(message.utterance_id)
            return
        if self.situation is not None and self.situation.active:
            if self.situation.transcript(message):
                self._demo_user(message.utterance_id, message.text, True)
            return
        if getattr(message, 'session_id', ''):
            # A late answer from an ended confirmation is not ordinary chat.
            return
        node: Any = self
        utterance_id, text = message.utterance_id, message.text
        try:
            validate_dialogue_input(utterance_id, text)
            # Transcript and status Topics may be delivered in either order.
            if utterance_id == self._input_utterance_id:
                self._input_utterance_id = None
            if utterance_id not in self._seen_input_ids:
                self._seen_input_ids.append(utterance_id)
            if self._fall_check is not None and self._fall_check.in_window():
                if self._answer_during_fall_check(utterance_id, text):
                    self._demo_user(utterance_id, text, True)
                return
            previous = self._receipts.lookup(utterance_id, text)
        except SpeechInputTooLongError as error:
            node.get_logger().warning(f'speech_dialogue invalid input: {error}')
            self.say(
                '한 번에 처리할 수 있는 16000자를 넘었어요. 나누어 말씀해 주세요.',
                request_id=utterance_id,
            )
            return
        except ValueError:
            node.get_logger().warning('speech_dialogue invalid input')
            return
        except sqlite3.Error:
            node.get_logger().error('speech_dialogue receipt unavailable')
            self.say(
                '대화를 접수하지 못했어요. 다시 말씀해 주세요.',
                request_id=utterance_id,
            )
            return
        if previous is not None:
            self._deps.receive_transcript_fn(
                self._receipts, utterance_id, text, node.get_logger(),
            )
            return
        if self.dialogue.startup_error:
            node.get_logger().error('speech_dialogue startup failed')
            self.say(
                '대화 처리를 준비하지 못했어요. 실행 설정을 확인해 주세요.',
                request_id=utterance_id,
            )
            return
        if not self.dialogue.has_capacity():
            node.get_logger().warning('speech_dialogue busy; not accepted')
            self.say(
                '앞선 대화를 처리하고 있어요. 잠시 뒤 다시 말씀해 주세요.',
                request_id=utterance_id,
            )
            return
        outcome = self._deps.receive_transcript_fn(
            self._receipts, utterance_id, text, node.get_logger(),
        )
        if outcome == 'storage_error':
            self.say(
                '대화를 접수하지 못했어요. 다시 말씀해 주세요.',
                request_id=utterance_id,
            )
            return
        if outcome == 'received' and not self.dialogue.submit(
            utterance_id, text,
        ):
            node.get_logger().error('speech_dialogue submission failed')
            self.say(
                '대화를 처리하지 못했어요. 다시 말씀해 주세요.',
                request_id=utterance_id,
            )
        elif outcome == 'received':
            self._awaiting_reply = utterance_id
            self._demo_user(utterance_id, text, False)


def create_communication_node(
    *,
    speech_db_path: str = DEFAULT_DB_PATH,
    on_event: Any = None,
    goal_response_timeout_s: float = 5.0,
    dialogue_settings: Optional[Settings] = None,
    dialogue_factory: Any = None,
    weather_query_timeout_s: float = 20.0,
    situation_factory: Any = None,
    enable_manager_commands: bool = False,
    prerecorded_audio: bool = False,
    confirmation_answers: Any = None,
) -> Any:
    """Compose communication on one owning thread with a single executor."""
    from malbut_interfaces.msg import (  # type: ignore[attr-defined]
        SpeechInputStatus,
        SpeechRequest,
        SpeechTranscript,
    )
    from malbut_interfaces.srv import ClassifySpeechAddressee  # type: ignore[attr-defined]
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
    )
    from rclpy.task import Future
    from std_msgs.msg import String

    from malbut_agent_server.manager_client import ManagerClient

    settings = dialogue_settings or Settings(user_id=DEFAULT_SPEECH_USER)
    settings.validate_for_dialogue(require_api_key=False)

    deps = CommunicationDeps(
        speech_input_status_cls=SpeechInputStatus,
        speech_request_cls=SpeechRequest,
        speech_transcript_cls=SpeechTranscript,
        classify_addressee_srv=ClassifySpeechAddressee,
        reentrant_callback_group_cls=ReentrantCallbackGroup,
        durability_policy=DurabilityPolicy,
        history_policy=HistoryPolicy,
        qos_profile_cls=QoSProfile,
        reliability_policy=ReliabilityPolicy,
        future_cls=Future,
        string_msg_cls=String,
        manager_client_cls=ManagerClient,
        dialogue_worker_cls=DialogueWorker,
        situation_action_server_cls=SituationActionServer,
        build_orchestrator_fn=build_orchestrator,
        build_situation_factory_fn=build_situation_factory,
        receive_transcript_fn=receive_transcript,
        transcript_topic=TRANSCRIPT_TOPIC,
        max_pending_addressee_provider=lambda: MAX_PENDING_ADDRESSEE_REQUESTS,
    )

    class CommunicationNode(_CommunicationSpeechMixin, Node):
        """Expose communication functions without invoking inference."""

        def __init__(self) -> None:
            super().__init__('malbut_agent_communication')
            self._demo_user_pub = self.create_publisher(String, '/malbut/demo/user', 10)
            self._init_communication(
                deps=deps,
                settings=settings,
                speech_db_path=speech_db_path,
                on_event=on_event,
                goal_response_timeout_s=goal_response_timeout_s,
                dialogue_factory=dialogue_factory,
                weather_query_timeout_s=weather_query_timeout_s,
                situation_factory=situation_factory,
                enable_manager_commands=enable_manager_commands,
                prerecorded_audio=prerecorded_audio,
                confirmation_answers=confirmation_answers,
            )

        def destroy_node(self) -> Any:
            return self._destroy_communication(super().destroy_node)

    return CommunicationNode()


def run_with_fall_rules(args: Any, ros_args: Sequence[str], settings: Any) -> int:
    """Run the fall rules beside the dialogue so that either can fail alone."""
    return _run_with_fall_rules(
        args,
        ros_args,
        settings,
        create_node_fn=create_communication_node,
        command_lines_cls=CommandLines,
        read_commands_fn=read_commands,
    )


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
    parser.add_argument('--enable-manager-commands', action='store_true')
    parser.add_argument(
        '--prerecorded-audio',
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    # Speech is off when Bringup's speech module did not start; the fall rules
    # (the former fall coordinator) then still run here, without a dialogue.
    parser.add_argument('--speech', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--fall-rules', action='store_true')
    # The one fall check (SWM25-261, Bringup fall_flow): FallFlow decides every step.
    parser.add_argument('--fall-flow', action='store_true')
    parser.add_argument(
        '--no-fall-approach',
        action='store_true',
        help='Run fall checks from the current position',
    )
    for peer in ('', 'bridge-', 'vlm-'):
        parser.add_argument(f'--fall-{peer}runtime-id', default='')
    args, ros_args = parser.parse_known_args(argv)
    if args.fall_rules and not all(value.strip() for value in (
            args.fall_runtime_id, args.fall_bridge_runtime_id, args.fall_vlm_runtime_id)):
        print('Fall rules need the runtime, bridge and VLM runtime IDs.', file=sys.stderr)
        return 2
    if not (args.speech or args.fall_rules or args.check):
        print('Nothing to run: enable speech or the fall rules.', file=sys.stderr)
        return 2
    settings = None
    if args.speech or args.check:
        try:
            settings = dialogue_settings_from_args(args)
        except (ValueError, OSError):
            print(
                'Invalid speech dialogue settings. Check provider and paths.',
                file=sys.stderr,
            )
            if args.check or not args.fall_rules:
                return 2
    if args.check:
        print('speech dialogue configuration: ok')
        return 0
    if args.fall_rules:
        return run_with_fall_rules(args, ros_args, settings)
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
            enable_manager_commands=args.enable_manager_commands,
            prerecorded_audio=args.prerecorded_audio,
        )
        executor.add_node(node)
        node.get_logger().info(
            'Agent communication started; initializing speech dialogue worker. '
            'Enter JSON lines: '
            'say, submit, status, cancel. '
            + ('Voice mission requests are routed through Manager.'
               if args.enable_manager_commands else 'Voice mission requests are disabled.')
        )
        descriptor: Optional[int] = sys.stdin.fileno()
        lines = CommandLines()
        while rclpy.ok():
            executor.spin_once(timeout_sec=0.05)
            descriptor = read_commands(node, descriptor, lines)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception as error:
        print(
            'Agent communication failed: ' + type(error).__name__,
            file=sys.stderr,
        )
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


def dialogue_settings_from_args(args: Any) -> Settings:
    """Keep voice development data separate from the HTTP user database."""
    if not args.conversation_db.strip() or not args.db_path.strip():
        raise ValueError('Speech database paths must not be blank')
    if (not math.isfinite(args.goal_response_timeout_s)
            or args.goal_response_timeout_s <= 0):
        raise ValueError('Goal response timeout must be positive and finite')
    if args.env_file:
        load_env_file(Path(args.env_file).expanduser())
    settings = Settings.from_env(os.environ)
    overrides: dict[str, Any] = {
        'database_path': str(Path(args.conversation_db).expanduser()),
        'user_id': args.user_id,
    }
    if args.provider is not None:
        overrides['provider'] = args.provider
    if args.model is not None:
        overrides['openai_model'] = args.model
    settings = replace(settings, **overrides)
    # The owner may set the OpenAI key on the web after bringup (key_sync).
    settings.validate_for_dialogue(require_api_key=False)
    return settings


if __name__ == '__main__':
    raise SystemExit(main())
