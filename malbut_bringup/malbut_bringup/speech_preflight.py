"""Check robot speech dependencies and peers without sending text to an API."""

import argparse
import json
import math
import sys
import time


# Fixed messages raised by Malbut's own speech code. Anything else (API errors,
# device text) stays out of the log; only these exact strings are reported.
SAFE_DETAILS = frozenset({
    'whisper.cpp requires rebuilding the packaged ABI 3 bridge',
    'whisper.cpp requires existing local model and bridge library files',
    'whisper.cpp failed to load the local model',
    'STT microphone must provide 16 kHz PCM',
    'STT microphone returned an incomplete frame',
    'device_index must be -1 or a microphone index',
    'invalid_agent_configuration',
    'invalid_preflight_configuration',
})


def safe_detail(error):
    """Return a known Malbut failure message, or a PortAudio device error, else ''."""
    text = str(error)
    if text in SAFE_DETAILS:
        return text
    if type(error).__module__ == 'sounddevice':
        # Device index and PortAudio status only; no credentials or audio pass here.
        return f'{type(error).__name__}: {text[:120]}'
    return ''


def check_interfaces():
    """Require generated ROS speech types with usable native type support."""
    from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest, SpeechTranscript
    from malbut_interfaces.srv import ClassifySpeechAddressee, ControlSpeechPlayback
    from rclpy.type_support import check_for_type_support

    for message in (SpeechPlaybackStatus, SpeechRequest, SpeechTranscript,
                    ClassifySpeechAddressee, ControlSpeechPlayback):
        check_for_type_support(message)


def check_agent(provider):
    """Use the same configuration validation as the launched dialogue process."""
    from malbut_agent_server.ros_communication import main

    if main(['--check', '--provider', provider]) != 0:
        raise ValueError('invalid_agent_configuration')


def check_tts(output_device):
    """Validate API settings and briefly write silence to the actual output."""
    import numpy as np
    import sounddevice as sd

    from malbut_tts.api_synthesis import OpenAISynthesizer

    # load() checks the key and SDK; it does not create a client or send a request.
    synthesizer = OpenAISynthesizer()
    synthesizer.load()
    device = None if output_device == -1 else output_device
    sd.check_output_settings(device=device, channels=1, dtype='float32', samplerate=24000)
    with sd.OutputStream(device=device, channels=1, dtype='float32', samplerate=24000) as stream:
        stream.write(np.zeros((2400, 1), dtype=np.float32))


def wait_for_control(server, timeout_s):
    """Wait for the owning robot controller without sending a mission Goal."""
    import rclpy
    from rclpy.action import ActionClient
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from malbut_interfaces.action import AutoSlam, ExecuteMission
    from malbut_interfaces.msg import SystemState

    action_type, name = {
        'manager': (ExecuteMission, '/malbut/mission/execute'),
        'autoslam': (AutoSlam, '/autoslam'),
    }[server]
    node = client = None
    booted = server != 'manager'

    def receive_state(message):
        nonlocal booted
        booted = message.system_state != SystemState.BOOTING

    rclpy.init()
    try:
        node = Node('speech_control_check')
        client = ActionClient(node, action_type, name)
        if server == 'manager':
            node.create_subscription(SystemState, '/malbut/state', receive_state, QoSProfile(
                depth=1, reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL))
        deadline = time.monotonic() + timeout_s
        while rclpy.ok() and time.monotonic() < deadline:
            if booted and client.server_is_ready():
                return
            rclpy.spin_once(node, timeout_sec=0.1)
        raise RuntimeError('robot_control_not_ready')
    finally:
        try:
            if client is not None:
                client.destroy()
            if node is not None:
                node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()


def wait_for_peers(timeout_s):
    """Wait for typed Agent/TTS endpoints; do not issue requests or play speech."""
    import rclpy
    from rclpy.node import Node
    from malbut_interfaces.srv import ClassifySpeechAddressee, ControlSpeechPlayback

    node = None
    rclpy.init()
    try:
        node = Node('speech_peer_check')
        clients = [
            node.create_client(ClassifySpeechAddressee, '/malbut/speech/classify_addressee'),
            node.create_client(ControlSpeechPlayback, '/malbut/speech/playback_control'),
        ]
        endpoints = [
            (node.get_subscriptions_info_by_topic, '/malbut/speech/transcript',
             'malbut_interfaces/msg/SpeechTranscript'),
            (node.get_subscriptions_info_by_topic, '/malbut/speech/response',
             'malbut_interfaces/msg/SpeechRequest'),
            (node.get_publishers_info_by_topic, '/malbut/speech/response',
             'malbut_interfaces/msg/SpeechRequest'),
            (node.get_publishers_info_by_topic, '/malbut/speech/playback_status',
             'malbut_interfaces/msg/SpeechPlaybackStatus'),
        ]
        deadline = time.monotonic() + timeout_s
        while rclpy.ok() and time.monotonic() < deadline:
            if (all(client.service_is_ready() for client in clients)
                    and all(any(info.topic_type == expected for info in query(topic))
                            for query, topic, expected in endpoints)):
                return
            rclpy.spin_once(node, timeout_sec=0.1)
        raise RuntimeError('speech_peers_not_ready')
    finally:
        try:
            if node is not None:
                node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()


def main(argv=None):
    """Return nonzero on failed checks, keeping credentials and audio out of logs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stt-model-path', default='')
    parser.add_argument('--stt-library-path', default='')
    parser.add_argument('--input-device', type=int, default=0)
    parser.add_argument('--output-device', type=int, default=-1)
    parser.add_argument('--cpp-threads', type=int, default=6)
    parser.add_argument('--agent-provider', choices=('openai', 'mock'), default='openai')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--wait-for-peers', action='store_true')
    mode.add_argument('--wait-for-control', choices=('manager', 'autoslam'))
    parser.add_argument('--timeout-s', type=float, default=30.0)
    args = parser.parse_args(argv)
    phase = 'configuration'
    try:
        if (args.input_device < -1 or args.output_device < -1 or args.cpp_threads < 1
                or not math.isfinite(args.timeout_s) or args.timeout_s <= 0):
            raise ValueError('invalid_preflight_configuration')
        phase = 'ros_interfaces'
        check_interfaces()
        if args.wait_for_control:
            phase = 'robot_control'
            wait_for_control(args.wait_for_control, args.timeout_s)
            print(json.dumps({'event': 'speech_control_ready',
                              'server': args.wait_for_control}), flush=True)
            return 0
        if args.wait_for_peers:
            phase = 'speech_peers'
            wait_for_peers(args.timeout_s)
            print(json.dumps({'event': 'speech_peers_ready'}), flush=True)
            return 0
        phase = 'agent_configuration'
        check_agent(args.agent_provider)
        phase = 'tts_output'
        check_tts(args.output_device)
        phase = 'stt_model_and_microphone'
        from malbut_stt.preflight import check_stt

        result = check_stt(
            args.stt_model_path, args.stt_library_path,
            device_index=args.input_device, cpp_threads=args.cpp_threads,
        )
        print(json.dumps({
            'event': 'speech_preflight_passed', **result,
            'api_request_verified': False, 'transcription_verified': False,
        }), flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        detail = safe_detail(error)
        print(json.dumps({
            'event': 'speech_preflight_failed', 'phase': phase,
            'error_type': type(error).__name__,
            **({'detail': detail} if detail else {}),
        }), file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
