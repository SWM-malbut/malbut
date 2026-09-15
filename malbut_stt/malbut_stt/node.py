"""ROS entry point for a microphone-driven STT publisher."""

from pathlib import Path
from math import isfinite
import sys
from time import monotonic
from typing import Optional, Sequence

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.transcription import LocalWhisperTranscriber


def main(args: Optional[Sequence[str]] = None) -> int:
    """Start only when ROS, microphone, and local models are ready."""
    # Keep hardware/ROS/SDK imports out of pure Python collection and tests.
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import (
            DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
        )
        from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechTranscript
        from malbut_interfaces.srv import (
            ClassifySpeechAddressee, ControlSpeechPlayback,
        )
    except ImportError:
        print('STT requires sourced ROS 2 and built malbut_interfaces.', file=sys.stderr)
        return 1

    node = None
    initialized = False
    pipeline = None
    close_requests = None
    phase = 'initializing_ros'
    try:
        rclpy.init(args=args)
        initialized = True
        node = Node('malbut_stt')
        phase = 'validating_configuration'

        def parameter(name, default):
            return node.declare_parameter(name, default).value

        wake_model_path = parameter('wake_model_path', '')
        stt_model_path = parameter('stt_model_path', '') or wake_model_path
        compute_type = parameter('compute_type', 'int8')
        device_index = parameter('device_index', -1)
        vad_mode = parameter('vad_mode', 2)
        input_has_aec = parameter('input_has_aec', False)
        control_timeout = parameter('playback_control_timeout_s', 5.0)
        settings = CaptureSettings(
            start_timeout_s=parameter('start_timeout_s', 5.0),
            silence_timeout_s=parameter('silence_timeout_s', 2.0),
            max_utterance_s=parameter('max_utterance_s', 20.0),
            pre_roll_s=parameter('pre_roll_s', 0.3),
        )
        if not isinstance(vad_mode, int) or vad_mode not in range(4):
            raise ValueError('vad_mode must be 0 through 3')
        if type(input_has_aec) is not bool:
            raise ValueError('input_has_aec must be a boolean')
        if compute_type not in ('int8', 'float32'):
            raise ValueError('compute_type must be int8 or float32')
        if (type(control_timeout) not in (float, int) or not isfinite(control_timeout)
                or control_timeout <= 0):
            raise ValueError('playback_control_timeout_s must be finite and positive')
        if not wake_model_path or not Path(wake_model_path).expanduser().is_dir():
            node.get_logger().error('Missing local model directory for parameter: wake_model_path')
            return 1
        if not stt_model_path or not Path(stt_model_path).expanduser().is_dir():
            node.get_logger().error('Missing local model directory for parameter: stt_model_path')
            return 1

        phase = 'loading_runtime_dependencies'
        from pvrecorder import PvRecorder
        import webrtcvad
        from malbut_stt.wake import LocalWakeRecognizer

        wake_path = Path(wake_model_path).expanduser()
        stt_path = Path(stt_model_path).expanduser()
        if wake_path.samefile(stt_path):
            phase = 'initializing_stt'
            transcriber = LocalWhisperTranscriber(stt_path, compute_type=compute_type)
            phase = 'initializing_wake'
            # DialoguePipeline has one ASR worker, so the shared model is used serially.
            wake = LocalWakeRecognizer.from_transcriber(transcriber)
        else:
            phase = 'initializing_wake'
            wake = LocalWakeRecognizer(wake_path, compute_type=compute_type)
            phase = 'initializing_stt'
            transcriber = LocalWhisperTranscriber(stt_path, compute_type=compute_type)
        phase = 'initializing_vad'
        vad = webrtcvad.Vad(vad_mode)
        phase = 'creating_publisher'
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        publisher = node.create_publisher(SpeechTranscript, '/malbut/speech/transcript', qos)
        control_client = node.create_client(
            ControlSpeechPlayback, '/malbut/speech/playback_control',
        )
        classify_client = node.create_client(
            ClassifySpeechAddressee, '/malbut/speech/classify_addressee',
        )
        control_commands = {
            'pause': ControlSpeechPlayback.Request.PAUSE,
            'resume': ControlSpeechPlayback.Request.RESUME,
            'stop': ControlSpeechPlayback.Request.STOP,
        }
        addressee_decisions = {
            ClassifySpeechAddressee.Response.ADDRESSED: 'addressed',
            ClassifySpeechAddressee.Response.NOT_ADDRESSED: 'not_addressed',
            ClassifySpeechAddressee.Response.UNKNOWN: 'unknown',
        }
        playback_states = {
            SpeechPlaybackStatus.PLAYING: 'playing',
            SpeechPlaybackStatus.PAUSED: 'paused',
            SpeechPlaybackStatus.FINISHED: 'finished',
            SpeechPlaybackStatus.FAILED: 'failed',
            SpeechPlaybackStatus.STOPPED: 'stopped',
        }
        control_pending = {}
        classify_pending = None
        requests_closed = False

        def remove_request(client, future):
            # Forget the local wait; cancelling a Future does not cancel the service remotely.
            client.remove_pending_request(future)
            future.cancel()

        def clear_classification():
            nonlocal classify_pending
            if classify_pending is not None:
                future = classify_pending[0]
                classify_pending = None
                remove_request(classify_client, future)

        def close_requests():
            nonlocal requests_closed
            requests_closed = True
            clear_classification()
            for future in list(control_pending):
                control_pending.pop(future)
                remove_request(control_client, future)

        def publish(utterance_id, text):
            if rclpy.ok():
                publisher.publish(SpeechTranscript(
                    utterance_id=utterance_id, text=text,
                ))

        def publish_control(playback_id, command):
            if requests_closed or not rclpy.ok():
                return
            if not control_client.service_is_ready():
                node.get_logger().warning('playback_control_service_unavailable')
                return
            if len(control_pending) >= 8:
                node.get_logger().warning('playback_control_requests_full')
                return
            try:
                future = control_client.call_async(ControlSpeechPlayback.Request(
                    playback_id=playback_id, command=control_commands[command],
                ))
            except Exception as error:
                node.get_logger().warning('playback_control_failed:' + type(error).__name__)
                return
            control_pending[future] = (playback_id, monotonic() + control_timeout)

            def control_done(done):
                pending = control_pending.pop(done, None)
                if pending is None or requests_closed or not rclpy.ok():
                    return
                control_client.remove_pending_request(done)
                if pending[0] != pipeline.session.playback_id:
                    node.get_logger().warning('playback_control_response_stale')
                elif monotonic() >= pending[1]:
                    node.get_logger().warning('playback_control_response_timeout')
                else:
                    try:
                        accepted = done.result().accepted
                    except Exception as error:
                        node.get_logger().warning(
                            'playback_control_failed:' + type(error).__name__)
                    else:
                        if accepted:
                            node.get_logger().info('playback_control_accepted:' + command)
                        else:
                            node.get_logger().warning('playback_control_rejected:' + command)

            future.add_done_callback(control_done)

        def publish_interruption(utterance_id, playback_id, text):
            nonlocal classify_pending
            if requests_closed or not rclpy.ok():
                return
            clear_classification()
            if not classify_client.service_is_ready():
                node.get_logger().warning('addressee_service_unavailable')
                pipeline.on_addressee(utterance_id, playback_id, 'unknown')
                return
            try:
                future = classify_client.call_async(ClassifySpeechAddressee.Request(
                    utterance_id=utterance_id, playback_id=playback_id, text=text,
                ))
            except Exception as error:
                node.get_logger().warning('addressee_service_failed:' + type(error).__name__)
                pipeline.on_addressee(utterance_id, playback_id, 'unknown')
                return
            classify_pending = (future, utterance_id, playback_id, pipeline.pending_addressee[2])

            def classification_done(done):
                nonlocal classify_pending
                if (classify_pending is None or classify_pending[0] is not done
                        or requests_closed or not rclpy.ok()):
                    return
                _, uid, pid, deadline = classify_pending
                classify_pending = None
                classify_client.remove_pending_request(done)
                current = pipeline.pending_addressee
                if current is None or current[:2] != (uid, pid):
                    return
                decision = 'unknown'
                if monotonic() >= deadline:
                    node.get_logger().warning('addressee_service_response_timeout')
                else:
                    try:
                        decision = addressee_decisions[done.result().decision]
                    except Exception as error:
                        node.get_logger().warning(
                            'addressee_service_failed:' + type(error).__name__)
                pipeline.on_addressee(uid, pid, decision)

            future.add_done_callback(classification_done)

        def poll_requests():
            if classify_pending is not None:
                _, uid, pid, deadline = classify_pending
                current = pipeline.pending_addressee
                if current is None or current[:2] != (uid, pid):
                    clear_classification()
                elif monotonic() >= deadline:
                    clear_classification()
                    node.get_logger().warning('addressee_service_response_timeout')
                    pipeline.on_addressee(uid, pid, 'unknown')
            for future, (pid, deadline) in list(control_pending.items()):
                if monotonic() >= deadline or pid != pipeline.session.playback_id:
                    control_pending.pop(future)
                    remove_request(control_client, future)
                    node.get_logger().warning('playback_control_response_timeout'
                                              if monotonic() >= deadline
                                              else 'playback_control_response_stale')

        def report(event):
            if event.startswith((
                'transcription_failed', 'empty_transcript', 'utterance_discarded',
                'addressee_unknown', 'audio_queue_overflow', 'barge_in_requires_aec',
                'speech_discarded', 'wake_chime_unavailable',
            )):
                node.get_logger().warning(event)
            else:
                node.get_logger().info(event)

        phase = 'creating_pipeline'
        pipeline = DialoguePipeline(
            recorder_factory=lambda: PvRecorder(
                frame_length=512, device_index=device_index,
            ),
            wake=wake,
            is_speech=vad.is_speech,
            transcriber=transcriber,
            publish_transcript=publish,
            publish_control=publish_control,
            publish_interruption=publish_interruption,
            report=report,
            settings=settings,
            input_has_aec=input_has_aec,
        )

        def playback_status(message):
            if rclpy.ok():
                try:
                    pipeline.on_playback_status(
                        message.playback_id, playback_states[message.state])
                except (KeyError, ValueError):
                    node.get_logger().warning('invalid_playback_status')

        node.create_subscription(
            SpeechPlaybackStatus, '/malbut/speech/playback_status', playback_status, qos,
        )
        pipeline.start()
        while rclpy.ok():
            pipeline.poll()
            poll_requests()
            if rclpy.ok():
                rclpy.spin_once(node, timeout_sec=0.02)
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
        cleanup_failed = False
        for cleanup in (close_requests, pipeline.close if pipeline is not None else None):
            if cleanup is None:
                continue
            try:
                cleanup()
            except Exception as error:
                cleanup_failed = True
                node.get_logger().error('STT cleanup failed: ' + type(error).__name__)
        try:
            if node is not None:
                node.destroy_node()
        finally:
            if initialized and rclpy.ok():
                rclpy.shutdown()
        if cleanup_failed:
            return 1
