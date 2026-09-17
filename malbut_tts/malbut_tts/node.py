"""Stream queued Agent speech and expose playback controls through ROS 2."""

import argparse
from queue import Empty, Queue
import sys
from typing import Optional, Sequence


RESPONSE_TOPIC = '/malbut/speech/response'
STATUS_TOPIC = '/malbut/speech/playback_status'
CONTROL_SERVICE = '/malbut/speech/playback_control'


def create_tts_node(runtime_factory=None):
    """Create a TTS node, optionally injecting runtime_factory(on_status)."""
    from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest
    from malbut_interfaces.srv import ControlSpeechPlayback
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
    )

    class TtsNode(Node):
        """Keep ROS publication on the executor while audio runs separately."""

        def __init__(self):
            super().__init__('malbut_tts')
            self._runtime = None
            self._closing = False
            self._statuses = Queue()
            try:
                self.declare_parameter('model_path', '')
                self.declare_parameter('backend', 'openai')
                self.declare_parameter('api_model', 'gpt-4o-mini-tts')
                self.declare_parameter('api_voice', 'marin')
                self.declare_parameter('api_timeout_seconds', 8.0)
                self.declare_parameter('cuda_dtype', 'float32')
                self.declare_parameter('cuda_sentence_mode', True)
                self.declare_parameter('sentence_max_chars', 80)
                self.declare_parameter('speaker', 'Sohee')
                self.declare_parameter('language', 'Korean')
                self.declare_parameter('output_device', -1)
                qos = QoSProfile(
                    history=HistoryPolicy.KEEP_LAST,
                    depth=10,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.VOLATILE,
                )
                self._status_publisher = self.create_publisher(
                    SpeechPlaybackStatus, STATUS_TOPIC, qos,
                )
                if runtime_factory is None:
                    self._runtime = self._create_runtime()
                else:
                    self._runtime = runtime_factory(self._queue_status)
                self.create_subscription(
                    SpeechRequest, RESPONSE_TOPIC, self._receive, qos,
                )
                self.create_service(
                    ControlSpeechPlayback, CONTROL_SERVICE, self._control,
                )
                self.create_timer(0.01, self._publish_statuses)
            except Exception:
                self.destroy_node()
                raise

        def _create_runtime(self):
            model_path = self.get_parameter('model_path').value
            backend = self.get_parameter('backend').value
            if backend not in ('openai', 'qwen-cuda'):
                raise ValueError('backend must be openai or qwen-cuda')
            if backend != 'openai' and not model_path.strip():
                raise ValueError(
                    'Set the local TTS model directory with '
                    '--ros-args -p model_path:=/absolute/model/path'
                )
            device = self.get_parameter('output_device').value
            if device < -1:
                raise ValueError('output_device must be -1 or a device index')
            from malbut_tts.audio import StreamingPlayer
            from malbut_tts.runtime import SpeechRuntime
            from malbut_tts.backends import create_synthesizer

            synthesizer = create_synthesizer(
                model_path,
                backend=backend,
                api_model=self.get_parameter('api_model').value,
                api_voice=self.get_parameter('api_voice').value,
                api_timeout_seconds=self.get_parameter('api_timeout_seconds').value,
                cuda_dtype=self.get_parameter('cuda_dtype').value,
                cuda_sentence_mode=self.get_parameter('cuda_sentence_mode').value,
                sentence_max_chars=self.get_parameter('sentence_max_chars').value,
                speaker=self.get_parameter('speaker').value,
                language=self.get_parameter('language').value,
            )
            if backend == 'openai':
                self.get_logger().info(
                    'OpenAI TTS: speech text is sent to a paid external API; '
                    'the output voice is AI-generated, not a human voice.'
                )
            return SpeechRuntime(
                synthesizer,
                lambda on_state, cancel_event: StreamingPlayer(
                    on_state, cancel_event,
                    device=None if device == -1 else device,
                ),
                self._queue_status,
                logger=self.get_logger(),
            )

        def _receive(self, message):
            if not self._closing:
                self._runtime.submit(message.text, message.request_type)

        def _control(self, request, response):
            response.accepted = (
                not self._closing and self._runtime.control(
                    request.playback_id, request.command,
                )
            )
            return response

        def _queue_status(self, playback_id, state):
            if not self._closing:
                self._statuses.put((playback_id, state))

        def _publish_statuses(self):
            while not self._closing:
                try:
                    playback_id, state = self._statuses.get_nowait()
                except Empty:
                    return
                self._status_publisher.publish(SpeechPlaybackStatus(
                    playback_id=playback_id, state=state,
                ))

        def destroy_node(self):
            """Stop audio and synthesis before releasing the ROS entities."""
            if self._closing:
                return False
            self._closing = True
            try:
                if self._runtime is not None:
                    self._runtime.close()
            finally:
                result = super().destroy_node()
            return result

    return TtsNode()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the selected streaming TTS backend with ROS playback controls."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            'OpenAI is the default. Example: tts_node --ros-args '
            '-p output_device:=1. Local CUDA: tts_node --ros-args '
            '-p backend:=qwen-cuda -p model_path:=/absolute/model/path'
        ),
    )
    _, ros_args = parser.parse_known_args(argv)
    try:
        import rclpy
        from rclpy.executors import ExternalShutdownException
    except ImportError:
        print('ROS 2 rclpy is required; source the ROS environment.',
              file=sys.stderr)
        return 2

    node = None
    initialized = False
    try:
        rclpy.init(args=ros_args)
        initialized = True
        node = create_tts_node()
        node.get_logger().info(
            f'TTS listening on {RESPONSE_TOPIC}; controls: {CONTROL_SERVICE}'
        )
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except (ImportError, ValueError, OSError, RuntimeError) as error:
        print(f'TTS node failed: {error}', file=sys.stderr)
        return 2
    finally:
        try:
            if node is not None:
                node.destroy_node()
        finally:
            if initialized and rclpy.ok():
                rclpy.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
