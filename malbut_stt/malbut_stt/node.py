"""ROS entry point for a microphone-driven STT publisher."""

from contextlib import ExitStack
import os
from pathlib import Path
import sys
from typing import Optional, Sequence

from malbut_stt.audio import CaptureSettings
from malbut_stt.pipeline import SpeechPipeline
from malbut_stt.transcription import OpenAITranscriber


def main(args: Optional[Sequence[str]] = None) -> int:
    """Start only when ROS, credentials, microphone, and models are ready."""
    # Keep hardware/ROS/SDK imports out of pure Python collection and tests.
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import (
            DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
        )
        from malbut_interfaces.msg import SpeechTranscript
    except ImportError:
        print('STT requires sourced ROS 2 and built malbut_interfaces.', file=sys.stderr)
        return 1

    node = None
    initialized = False
    pipeline = None
    phase = 'initializing_ros'
    try:
        rclpy.init(args=args)
        initialized = True
        node = Node('malbut_stt')
        phase = 'validating_configuration'

        def parameter(name, default):
            return node.declare_parameter(name, default).value

        keyword_path = parameter('keyword_path', '')
        language_model_path = parameter('language_model_path', '')
        device_index = parameter('device_index', -1)
        vad_mode = parameter('vad_mode', 2)
        settings = CaptureSettings(
            start_timeout_s=parameter('start_timeout_s', 5.0),
            silence_timeout_s=parameter('silence_timeout_s', 1.0),
            max_utterance_s=parameter('max_utterance_s', 20.0),
            pre_roll_s=parameter('pre_roll_s', 0.3),
        )
        timeout_s = parameter('api_timeout_s', 30.0)
        if not isinstance(vad_mode, int) or vad_mode not in range(4):
            raise ValueError('vad_mode must be 0 through 3')
        if not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 120:
            raise ValueError('api_timeout_s must be positive and at most 120')
        for name in ('OPENAI_API_KEY', 'PICOVOICE_ACCESS_KEY'):
            if not os.environ.get(name, '').strip():
                node.get_logger().error('Missing environment variable: ' + name)
                return 1
        for name, path in (
            ('keyword_path', keyword_path),
            ('language_model_path', language_model_path),
        ):
            if not path or not Path(path).expanduser().is_file():
                node.get_logger().error('Missing model file for parameter: ' + name)
                return 1

        phase = 'loading_runtime_dependencies'
        import pvporcupine
        from pvrecorder import PvRecorder
        import webrtcvad
        from openai import OpenAI

        with ExitStack() as resources:
            phase = 'initializing_wake'
            wake = pvporcupine.create(
                access_key=os.environ['PICOVOICE_ACCESS_KEY'],
                keyword_paths=[str(Path(keyword_path).expanduser())],
                model_path=str(Path(language_model_path).expanduser()),
            )
            resources.callback(wake.delete)
            phase = 'creating_api_client'
            client = OpenAI(
                api_key=os.environ['OPENAI_API_KEY'],
                base_url='https://api.openai.com/v1',
                timeout=timeout_s,
                max_retries=0,
            )
            resources.callback(client.close)
            phase = 'initializing_vad'
            vad = webrtcvad.Vad(vad_mode)
            phase = 'creating_publisher'
            publisher = node.create_publisher(
                SpeechTranscript, '/malbut/speech/transcript',
                QoSProfile(
                    history=HistoryPolicy.KEEP_LAST, depth=10,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.VOLATILE,
                ),
            )

            def publish(utterance_id, text):
                if rclpy.ok():
                    publisher.publish(SpeechTranscript(
                        utterance_id=utterance_id, text=text,
                    ))

            def report(event):
                if event.startswith(('transcription_failed', 'empty_transcript', 'too_long')):
                    node.get_logger().warning(event)
                else:
                    node.get_logger().info(event)

            phase = 'creating_pipeline'
            pipeline = SpeechPipeline(
                recorder_factory=lambda: PvRecorder(
                    frame_length=wake.frame_length, device_index=device_index,
                ),
                wake=wake,
                is_speech=vad.is_speech,
                transcriber=OpenAITranscriber(client),
                publish=publish,
                should_stop=lambda: not rclpy.ok(),
                report=report,
                settings=settings,
            )
            pipeline.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        if initialized and not rclpy.ok():
            return 0
        # Avoid printing SDK exceptions containing credentials or audio contents.
        failed_phase = pipeline.phase if pipeline is not None else phase
        message = 'STT stopped during ' + failed_phase + ': ' + type(error).__name__
        if node is not None:
            node.get_logger().error(message)
        else:
            print(message, file=sys.stderr)
        return 1
    finally:
        if node is not None:
            node.destroy_node()
        if initialized and rclpy.ok():
            rclpy.shutdown()
