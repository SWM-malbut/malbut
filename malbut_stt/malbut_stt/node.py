"""ROS entry point for a microphone-driven STT publisher."""

from pathlib import Path
from math import isfinite
import os
import sys
from time import monotonic
from typing import Optional, Sequence

from malbut_stt.audio import CaptureSettings, SoundDeviceRecorder
from malbut_stt.chime import play_endpoint_chime, play_failure_chime, play_wake_chime
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.fall_check import FallCheckWindow
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
        from malbut_interfaces.msg import (
            SpeechInputStatus, SpeechPlaybackStatus, SpeechRequest, SpeechTranscript,
        )
        from malbut_interfaces.srv import (
            ClassifySpeechAddressee, ControlSpeechPlayback, ControlSpeechSession,
            ControlWebTalk,
        )
        from std_msgs.msg import String
    except ImportError:
        print('STT requires sourced ROS 2 and built malbut_interfaces.', file=sys.stderr)
        return 1

    node = None
    initialized = False
    pipeline = None
    close_requests = None
    close_transcriber = None
    diagnostics = None
    phase = 'initializing_ros'
    try:
        rclpy.init(args=args)
        initialized = True
        node = Node('malbut_stt')
        phase = 'validating_configuration'

        def parameter(name, default):
            return node.declare_parameter(name, default).value

        backend = parameter('backend', 'faster_whisper')
        wake_model_path = parameter('wake_model_path', '')
        stt_model_path = parameter('stt_model_path', '') or wake_model_path
        stt_library_path = parameter('stt_library_path', '')
        cpp_use_gpu = parameter('cpp_use_gpu', True)
        cpp_threads = parameter('cpp_threads', 6)
        stt_decode_timeout_s = parameter('stt_decode_timeout_s', 30.0)
        compute_type = parameter('compute_type', 'int8')
        device_index = parameter('device_index', 0)
        wake_chime_device_index = parameter('wake_chime_device_index', -1)
        chime_tail_s = parameter('chime_tail_s', 0.3)
        short_speech_guard = parameter('short_speech_guard', False)
        followup_timeout_s = parameter('followup_timeout_s', 10.0)
        vad_mode = parameter('vad_mode', 2)
        input_has_aec = parameter('input_has_aec', False)
        control_timeout = parameter('playback_control_timeout_s', 5.0)
        max_utterance_s = parameter('max_utterance_s', 0.0)
        max_buffer_s = parameter('max_buffer_s', 60.0)
        endpoint_predecode_s = parameter('endpoint_predecode_s', 0.8)
        if (type(max_utterance_s) not in (float, int)
                or not isfinite(max_utterance_s) or max_utterance_s < 0):
            raise ValueError('max_utterance_s must be finite and nonnegative; zero disables duration limit')
        if (type(wake_chime_device_index) is not int or wake_chime_device_index < -1):
            raise ValueError('wake_chime_device_index must be -1 or an output device index')
        if (type(chime_tail_s) not in (int, float) or not isfinite(chime_tail_s)
                or not 0 <= chime_tail_s <= 3):
            raise ValueError('chime_tail_s must be finite and between zero and three seconds')
        if type(short_speech_guard) is not bool:
            raise ValueError('short_speech_guard must be a boolean')
        if (type(followup_timeout_s) not in (int, float)
                or not isfinite(followup_timeout_s) or followup_timeout_s < 0):
            raise ValueError('followup_timeout_s must be finite and nonnegative')
        if (type(stt_decode_timeout_s) not in (int, float)
                or not isfinite(stt_decode_timeout_s) or stt_decode_timeout_s <= 0):
            raise ValueError('stt_decode_timeout_s must be finite and positive')
        if (type(endpoint_predecode_s) not in (float, int)
                or not isfinite(endpoint_predecode_s)
                or not 0 < endpoint_predecode_s <= 1.0):
            raise ValueError('endpoint_predecode_s must be positive and at most one second')
        settings = CaptureSettings(
            start_timeout_s=parameter('start_timeout_s', 5.0),
            silence_timeout_s=parameter('silence_timeout_s', 2.0),
            max_utterance_s=None if max_utterance_s == 0 else max_utterance_s,
            max_buffer_s=max_buffer_s,
            pre_roll_s=parameter('pre_roll_s', 0.3),
            min_speech_s=parameter('min_speech_s', 0.08),
        )
        if not isinstance(vad_mode, int) or vad_mode not in range(4):
            raise ValueError('vad_mode must be 0 through 3')
        if type(input_has_aec) is not bool:
            raise ValueError('input_has_aec must be a boolean')
        if backend not in ('faster_whisper', 'whisper_cpp'):
            raise ValueError('backend must be faster_whisper or whisper_cpp')
        if backend == 'faster_whisper' and compute_type not in ('int8', 'float32'):
            raise ValueError('compute_type must be int8 or float32')
        if (type(control_timeout) not in (float, int) or not isfinite(control_timeout)
                or control_timeout <= 0):
            raise ValueError('playback_control_timeout_s must be finite and positive')
        wake_path = Path(wake_model_path).expanduser()
        stt_path = Path(stt_model_path).expanduser()
        if backend == 'whisper_cpp':
            if type(cpp_use_gpu) is not bool:
                raise ValueError('cpp_use_gpu must be a boolean')
            if type(cpp_threads) is not int or cpp_threads <= 0:
                raise ValueError('cpp_threads must be a positive integer')
            library_path = Path(stt_library_path).expanduser()
            if not stt_model_path or not stt_path.is_file():
                raise ValueError('whisper_cpp requires a local stt_model_path file')
            if not stt_library_path or not library_path.is_file():
                raise ValueError('whisper_cpp requires a local stt_library_path file')
            if wake_model_path and (not wake_path.is_file() or not wake_path.samefile(stt_path)):
                raise ValueError('whisper_cpp shares one model; wake_model_path must match stt_model_path')
        else:
            if not wake_model_path or not wake_path.is_dir():
                node.get_logger().error('Missing local model directory for parameter: wake_model_path')
                return 1
            if not stt_model_path or not stt_path.is_dir():
                node.get_logger().error('Missing local model directory for parameter: stt_model_path')
                return 1

        phase = 'loading_runtime_dependencies'

        import webrtcvad
        from malbut_stt.wake import LocalWakeRecognizer
        from malbut_stt.silero_gate import from_environment as create_silero_gate

        phase = 'initializing_silero_gate'
        silero_gate = create_silero_gate(os.environ)
        if silero_gate is not None:
            node.get_logger().info('stt_silero_gate_enabled:threshold=0.5,min_speech_ms=80')

        if backend == 'whisper_cpp':
            from malbut_stt.cpp_transcription import CppWhisperTranscriber

            phase = 'initializing_stt'
            transcriber = CppWhisperTranscriber(
                stt_path, library_path, use_gpu=cpp_use_gpu, n_threads=cpp_threads,
                decode_timeout_s=stt_decode_timeout_s,
            )
            close_transcriber = transcriber.close
            phase = 'initializing_wake'
            wake = LocalWakeRecognizer.from_transcriber(transcriber)
        elif wake_path.samefile(stt_path):
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
        from dataclasses import asdict
        from malbut_stt.incident import from_environment

        diagnostics = from_environment(
            os.environ,
            metadata=dict(backend=backend, stt_model_path=str(stt_path),
                          wake_model_path=str(wake_path), device_index=device_index,
                          vad_mode=vad_mode, input_has_aec=input_has_aec,
                          chime_tail_s=chime_tail_s, short_speech_guard=short_speech_guard,
                          followup_timeout_s=followup_timeout_s,
                          capture_settings=asdict(settings),
                          silero_gate=silero_gate.metadata if silero_gate is not None else None,
                          model=getattr(transcriber, 'metadata', {})),
            on_saved=lambda path: node.get_logger().info('stt_incident_saved:' + str(path)),
            on_error=lambda error: node.get_logger().warning('stt_incident_stopped:' + error),
        )
        if diagnostics is not None:
            from rcl_interfaces.msg import Log

            node.create_subscription(Log, '/rosout', diagnostics.ros_log, QoSProfile(
                history=HistoryPolicy.KEEP_LAST, depth=100,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            ))
            node.get_logger().info('stt_incident_capture_enabled:observation_only')
        diagnostic_directory = os.environ.get('MALBUT_STT_DIAGNOSTIC_DIR', '').strip()
        if diagnostic_directory:
            from malbut_stt.diagnostics import TranscriptionDiagnostics

            diagnostics = TranscriptionDiagnostics(diagnostic_directory)
            node.get_logger().info('stt_diagnostics_enabled:local_audio_limit_64MiB')
        from malbut_stt.shadow_vad import from_environment as observe_vad

        diagnostics = observe_vad(
            os.environ, diagnostics, vad_mode=vad_mode,
            on_error=lambda error: node.get_logger().warning('stt_shadow_vad_stopped:' + error),
        )
        if diagnostics is not None:
            shared = wake.model is transcriber.model
            transcriber.model = diagnostics.wrap_model(transcriber.model)
            wake.model = transcriber.model if shared else diagnostics.wrap_model(wake.model)
        if silero_gate is not None:
            # Keep the gate outside decoder diagnostics: a rejected input must
            # never appear as an actual Whisper decode_input/decode_result.
            # Wake recognition keeps the original diagnostic model, even when
            # both recognizers share one underlying Whisper instance.
            transcriber.model = silero_gate.wrap_model(transcriber.model, diagnostics)
        phase = 'initializing_vad'
        vad = webrtcvad.Vad(vad_mode)
        phase = 'creating_publisher'
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        publisher = node.create_publisher(SpeechTranscript, '/malbut/speech/transcript', qos)
        input_publisher = node.create_publisher(
            SpeechInputStatus, '/malbut/speech/input_status', qos)
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
            'stop_all': ControlSpeechPlayback.Request.STOP_ALL,
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
                    session_id=pipeline.session.session_id,
                ))
                if diagnostics is not None:
                    diagnostics.event('transcript_published', utterance_id=utterance_id,
                                      text=text, session_id=pipeline.session.session_id)

        def publish_input_status(session_id, utterance_id, state):
            if rclpy.ok():
                input_publisher.publish(SpeechInputStatus(
                    session_id=session_id, utterance_id=utterance_id, state=state))

        def publish_control(playback_id, command, *, web_talk=False):
            # Web talk stops do not follow the dialogue playback, so they are never stale.
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
            control_pending[future] = (playback_id, monotonic() + control_timeout, web_talk)

            def control_done(done):
                pending = control_pending.pop(done, None)
                if pending is None or requests_closed or not rclpy.ok():
                    return
                control_client.remove_pending_request(done)
                if not pending[2] and pending[0] != pipeline.session.playback_id:
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
            for future, (pid, deadline, web_talk) in list(control_pending.items()):
                if monotonic() >= deadline or (
                        not web_talk and pid != pipeline.session.playback_id):
                    control_pending.pop(future)
                    remove_request(control_client, future)
                    node.get_logger().warning('playback_control_response_timeout'
                                              if monotonic() >= deadline
                                              else 'playback_control_response_stale')

        def report(event):
            if event.startswith((
                'transcription_failed', 'empty_transcript', 'utterance_discarded',
                'addressee_unknown', 'audio_queue_overflow', 'barge_in_requires_aec',
                'speech_discarded', 'wake_chime_unavailable', 'wake_chime_failed',
                'endpoint_chime_failed', 'failure_chime_failed',
            )):
                node.get_logger().warning(event)
            else:
                node.get_logger().info(event)

        phase = 'creating_pipeline'
        pipeline = DialoguePipeline(
            recorder_factory=lambda: SoundDeviceRecorder(
                frame_length=512, device_index=device_index,
            ),
            wake=wake,
            is_speech=vad.is_speech,
            transcriber=transcriber,
            publish_transcript=publish,
            publish_control=publish_control,
            stop_speech=lambda playback_id: publish_control(
                playback_id, 'stop' if playback_id else 'stop_all', web_talk=True),
            publish_interruption=publish_interruption,
            publish_input_status=publish_input_status,
            diagnostics=diagnostics,
            report=report,
            on_wake=lambda: play_wake_chime(wake_chime_device_index),
            on_endpoint=lambda: play_endpoint_chime(wake_chime_device_index),
            on_failure=lambda: play_failure_chime(wake_chime_device_index),
            settings=settings,
            input_has_aec=input_has_aec,
            chime_tail_s=chime_tail_s,
            short_speech_guard=short_speech_guard,
            followup_timeout_s=followup_timeout_s,
            validate_wake_command=(
                (lambda pcm: silero_gate.require_speech(pcm, diagnostics))
                if silero_gate is not None else None),
            # A shorter explicit fallback keeps its existing behavior without predecode.
            endpoint_predecode_s=(endpoint_predecode_s
                                  if settings.silence_timeout_s > 1.0 else None),
        )

        def playback_status(message):
            if diagnostics is not None:
                diagnostics.event('tts_playback', playback_id=message.playback_id,
                                  state=message.state, interim=getattr(message, 'interim', None),
                                  request_id=getattr(message, 'request_id', None))
            if message.state == SpeechPlaybackStatus.ACCEPTED:
                return  # Manager admission is not audible playback.
            if rclpy.ok():
                try:
                    pipeline.on_playback_status(
                        message.playback_id, playback_states[message.state],
                        interim=message.interim, request_id=message.request_id)
                except (KeyError, ValueError):
                    node.get_logger().warning('invalid_playback_status')

        node.create_subscription(
            SpeechPlaybackStatus, '/malbut/speech/playback_status', playback_status, qos,
        )

        def speech_request(message):
            if diagnostics is not None:
                diagnostics.event('tts_request', playback_id=message.playback_id,
                                  text=getattr(message, 'text', None),
                                  request_type=getattr(message, 'request_type', None),
                                  interim=getattr(message, 'interim', None),
                                  request_id=getattr(message, 'request_id', None))
            if rclpy.ok():
                pipeline.on_speech_request(message.playback_id)

        # Seeing each request with TTS lets a web talk cancel it before any sound.
        node.create_subscription(
            SpeechRequest, '/malbut/speech/response', speech_request, qos,
        )

        def control_session(request, response):
            response.barge_in_available = pipeline.input_has_aec
            check_only = getattr(request, 'check_only', False)
            if check_only:
                response.accepted = pipeline.session_is_active(request.session_id)
            else:
                response.accepted = (
                    pipeline.start_session(request.session_id) if request.active
                    else pipeline.stop_session(request.session_id))
            if response.accepted and not check_only:
                clear_classification()
            return response

        node.create_service(
            ControlSpeechSession, '/malbut/speech/session_control', control_session)

        # A fall check comes before the guardian's talk (the user, 2026-10-13): from
        # the suspicion until its question ends the web microphone is refused, and a
        # talk already going ends at its next renewal (the media agent renews each second).
        fall_check = FallCheckWindow()
        node.create_subscription(
            String, '/malbut/falls/check/status',
            lambda message: fall_check.observe(message.data), qos)

        def control_web_talk(request, response):
            if request.active and fall_check.active():
                pipeline.control_web_talk(request.lease_id, False, 0.0)  # This talk ends.
                response.accepted = False
                return response
            response.accepted = pipeline.control_web_talk(
                request.lease_id, request.active, request.ttl_s)
            if response.accepted and request.active:
                clear_classification()
            return response

        node.create_service(
            ControlWebTalk, '/malbut/speech/web_talk_control', control_web_talk)
        # The media agent's previous 3 s lease may survive this STT process restart.
        pipeline.control_web_talk('startup-quarantine', True, 3.0, quiet=False)
        pipeline.start()
        ready_publisher = None
        while rclpy.ok():
            pipeline.poll()
            if ready_publisher is None and pipeline.capture_ready.is_set() and rclpy.ok():
                ready_publisher = node.create_publisher(String, '/malbut/speech/status', QoSProfile(
                    history=HistoryPolicy.KEEP_LAST, depth=1,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                ))
                ready_publisher.publish(String(data='ready'))
                # Report actual input, not merely a started capture thread.
                print('malbut_speech_capture_ready', flush=True)
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
        for cleanup in (close_requests, pipeline.close if pipeline is not None else None,
                        close_transcriber, getattr(diagnostics, 'close', None)):
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
