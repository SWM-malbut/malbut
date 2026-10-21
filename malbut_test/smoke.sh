#!/usr/bin/env bash
# One entry point, no installs, no duplicate Bringup, no global process killing.
set -eo pipefail
umask 077

smoke_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export MALBUT_SMOKE_SCRIPT="$smoke_dir/smoke.sh"

# Help/plan work even on a development computer without ROS. Never source .zshrc
# (secrets, interactive commands), and never source a zsh setup from bash.
case " ${*} " in
  *" --help "*|*" -h "*|"  ") ;;
  *)
    if [ -f /opt/ros/humble/setup.bash ]; then
      source /opt/ros/humble/setup.bash
    fi
    smoke_ws="${MALBUT_SMOKE_WORKSPACE:-/home/ubuntu/ros2_ws}"
    if [ -f "$smoke_ws/install/setup.bash" ]; then
      source "$smoke_ws/install/setup.bash"
    fi
    if [ -f "$smoke_ws/install/malbut_test/local_setup.bash" ]; then
      source "$smoke_ws/install/malbut_test/local_setup.bash"
    fi
    ;;
esac
set -u

# The embedded program opens /dev/tty for the one final review: stdin contains
# this program, not keyboard input. No model environments are loaded here.
exec "${MALBUT_SMOKE_PYTHON:-/usr/bin/python3}" - "$@" <<'MALBUT_SMOKE_PY'
"""Guided real-robot smoke; all production commands use existing public routes."""

import argparse
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import sqlite3
import sys
import time
from uuid import uuid4


STEPS = ('준비', '음성대화', '사람추적', '낙상', '대화 복귀', 'TTS 청취')
MISSION_FIELDS = ('active_foreground_missions', 'active_background_missions',
                  'pending_missions', 'suspended_missions')
FALL_CAPABILITIES = frozenset(('fall_approach', 'fall_confirmation'))
TERMINAL_AUDIO = frozenset(('finished', 'failed', 'stopped'))
EVENT_KINDS = frozenset(('transcript', 'reply', 'audio', 'worker_audio',
                         'mission', 'fall_event', 'fall_decision'))


class SmokeError(Exception):
    """A fixed diagnostic code, never raw API/device errors or credentials."""

    def __init__(self, code, *, unsafe=False):
        super().__init__(code)
        self.code, self.unsafe = code, unsafe


def finite_seconds(value):
    number = float(value)
    if not math.isfinite(number) or not 0.1 <= number <= 900:
        raise argparse.ArgumentTypeError('0.1~900 사이의 유한한 초를 지정하세요.')
    return number


def parser():
    result = argparse.ArgumentParser(description=(
        '실행 중인 Bringup에 연결: 음성대화 → 자동 추적 → 낙상 → 대화 복귀. '
        '인자 없이는 계획만 표시하며, --execute가 실제 이동·API·알림을 허용합니다.'))
    mode = result.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--check', action='store_true', help='읽기 전용 준비 검사만 수행')
    result.add_argument('--test-alerts-confirmed', action='store_true',
                        help='테스트 기기/알림 수신 대상과 안전한 이동 공간을 확인했음')
    result.add_argument('--conversation-db', type=Path,
                        default=Path('~/.local/state/malbut/speech-dialogue.sqlite3'))
    result.add_argument('--fall-config', type=Path,
                        default=Path(os.environ.get('MALBUT_FALL_CONFIG',
                                                    '/etc/malbut/fall_runtime.json')))
    result.add_argument('--report-dir', type=Path, default=Path(
        os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'malbut/smoke')
    for name, default in (('ready-timeout', 180), ('rpc-timeout', 30),
                          ('transition-timeout', 120), ('turn-timeout', 90),
                          ('follow-seconds', 30), ('fall-timeout', 240),
                          ('cleanup-timeout', 60), ('prepare-seconds', 3),
                          ('review-timeout', 60)):
        result.add_argument('--' + name, type=finite_seconds, default=float(default))
    result.add_argument('--no-review', action='store_true', help='사람 평가 없이 미평가로 저장')
    return result


def read_rows(path, statement, arguments=()):
    """Read the existing ledger only; never create, migrate, or lock for writing."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise SmokeError('ledger_missing')
    try:
        with closing(sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True,
                                     timeout=0.1)) as connection:
            connection.execute('PRAGMA query_only=ON')
            return connection.execute(statement, arguments).fetchall()
    except sqlite3.OperationalError as error:
        if 'locked' in str(error).lower():
            raise SmokeError('ledger_busy') from None
        raise SmokeError('ledger_unavailable_or_schema_mismatch') from None
    except sqlite3.Error:
        raise SmokeError('ledger_unavailable_or_schema_mismatch') from None


def api_evidence(path, utterance_id):
    """Bind a real OpenAI result to this STT turn, not an error/guide utterance."""
    request = 'speech-request-' + hashlib.sha256(utterance_id.encode()).hexdigest()
    try:
        rows = read_rows(path, 'SELECT response_json FROM conversation_turns '
                         "WHERE request_id=? AND status='completed'", (request,))
    except SmokeError as error:
        if error.code == 'ledger_busy':
            return None  # Retry only the read, through the caller's bounded ROS loop.
        raise
    if not rows:
        return None
    if len(rows) != 1:
        raise SmokeError('ambiguous_dialogue_ledger')
    try:
        value = json.loads(rows[0][0])
        provider = value['provider']
        if (value['request_id'] != request or provider['provider'] != 'openai'
                or not isinstance(provider.get('response_id'), str)
                or not provider['response_id'].strip()):
            raise ValueError
        return {'provider': 'openai', 'response_id': provider['response_id'],
                'model': provider.get('model'), 'usage': provider.get('usage')}
    except (KeyError, TypeError, ValueError):
        raise SmokeError('real_llm_api_result_not_verified') from None


class Evidence:
    """Bounded, receive-ordered live evidence; no persisted audio/images/text."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.sequence = clock, 0
        self.events = deque(maxlen=4096)
        self.latest = {}

    def add(self, kind, value):
        self.sequence += 1
        item = (self.sequence, self.clock(), kind, value)
        if kind in EVENT_KINDS:
            self.events.append(item)
        self.latest[kind] = item
        return item

    def since(self, boundary, kind):
        return [item for item in self.events if item[0] > boundary and item[2] == kind]

    def value(self, kind, default=None):
        item = self.latest.get(kind)
        return item[3] if item else default

    def fresh(self, kind, seconds=3):
        item = self.latest.get(kind)
        return bool(item and self.clock() - item[1] <= seconds)

    def audio(self, playback_id, after=0, kind='audio'):
        states = [item[3]['state'] for item in self.since(after, kind)
                  if item[3]['playback_id'] == playback_id]
        return next((state for state in reversed(states) if state in TERMINAL_AUDIO), None)

    def dialogue(self, after, excluded=()):
        transcripts = self.since(after, 'transcript')
        for event in transcripts:
            transcript = event[3]
            if transcript['session_id']:
                continue  # A fall confirmation is not ordinary dialogue.
            uid = transcript['utterance_id']
            if not uid or uid in excluded:
                continue
            # DDS does not guarantee callback order across different topics.
            replies = [entry for entry in self.since(after, 'reply') if (
                entry[3]['request_id'] == uid and not entry[3]['interim']
                and not entry[3]['owner_goal_id'] and entry[3]['has_text'])]
            for entry in replies:
                state = self.audio(entry[3]['playback_id'], after)
                if state == 'finished':
                    return uid
                if state in ('failed', 'stopped'):
                    raise SmokeError('dialogue_playback_' + state)
        return None


class Report:
    def __init__(self, directory):
        self.directory = directory.expanduser() / (
            datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:10])
        self.directory.mkdir(parents=True, mode=0o700)
        self.path = self.directory / 'report.json'
        self.data = {'schema_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
                     'mode': 'execute', 'cleanup': 'NOT_RUN', 'overall': 'INCOMPLETE',
                     'steps': [dict(name=name, automatic='NOT_RUN', observation='UNREVIEWED',
                                    findings=[], evidence={}) for name in STEPS]}
        self.save()

    def save(self):
        temporary = self.directory / 'report.json.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(self.data, output, ensure_ascii=False, allow_nan=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)

    def set(self, index, status, code=None, **evidence):
        item = self.data['steps'][index]
        item['automatic'] = status
        if code and code not in item['findings']:
            item['findings'].append(code)
        item['evidence'].update(evidence)
        self.save()
        print(f"[{status}] {index + 1}. {item['name']}" + (f' · {code}' if code else ''),
              flush=True)

    def review(self, answer):
        """Blank means an explicit human Enter, never EOF or a timeout."""
        answer, _, note = answer.partition(':')
        failed = set()
        if answer.strip():
            try:
                failed = {int(part.strip()) for part in answer.split(',')}
            except ValueError:
                return False
            if not failed or not failed <= set(range(1, len(STEPS) + 1)):
                return False
            if any(self.data['steps'][index - 1]['automatic'] == 'NOT_RUN' for index in failed):
                return False
        for index, item in enumerate(self.data['steps'], 1):
            if item['automatic'] != 'NOT_RUN':
                item['observation'] = 'FAIL' if index in failed else 'PASS'
                if index in failed and note.strip():
                    item['note'] = ''.join(c for c in note.strip() if c.isprintable())[:300]
        self.save()
        return True

    def finish(self, interrupted=False):
        steps = self.data['steps']
        if interrupted:
            overall, code = 'INTERRUPTED', 130
        elif self.data['cleanup'] == 'UNCONFIRMED':
            overall, code = 'UNSAFE', 3
        elif (self.data.get('infrastructure_errors') or any(
                item['automatic'] == 'FAIL' or item['observation'] == 'FAIL' for item in steps)):
            overall, code = 'FAIL', 1
        elif any(item['automatic'] != 'PASS' or item['observation'] != 'PASS' for item in steps):
            overall, code = 'INCOMPLETE', 2
        else:
            overall, code = 'PASS', 0
        self.data.update(overall=overall, finished_at=datetime.now(timezone.utc).isoformat())
        saved = True
        try:
            self.save()
        except OSError:
            saved = False
            code = 1 if code != 130 else code
            print('결과 파일 저장 실패. 아래 터미널 결과를 확인하세요.', file=sys.stderr)
        print('\n=== 스모크 검사 결과 ===', flush=True)
        for index, item in enumerate(steps, 1):
            print(f"{index}. {item['name']}: 자동={item['automatic']} / "
                  f"관찰={item['observation']} / {', '.join(item['findings']) or '-'}")
        print(f"정리={self.data['cleanup']} · 최종={overall}", flush=True)
        print(f"결과 파일: {self.path}" if saved else '최종 결과 파일 저장 실패', flush=True)
        return code


class RosRobot:
    """Temporary observer; models and the existing Bringup remain in their owners."""

    def __init__(self, *, rpc_timeout=30.0):
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from rcl_interfaces.srv import GetParameters
        from sensor_msgs.msg import CameraInfo, Image, LaserScan
        from nav_msgs.msg import Odometry
        from std_msgs.msg import String
        from malbut_interfaces.msg import (
            FallRuntimeStatus, SpeechPlaybackStatus, SpeechRequest, SpeechTranscript, SystemState)
        from malbut_interfaces.srv import ControlSpeechPlayback
        from malbut_interfaces.srv import ClassifySpeechAddressee
        from malbut_interfaces.action import ConfirmSituation, FallApproach, FollowPerson
        from rclpy.action import ActionClient
        from std_srvs.srv import Trigger
        from malbut_agent_server.manager_client import ManagerClient
        from tf2_ros import Buffer, TransformListener
        self.rclpy, self.String, self.SpeechRequest = rclpy, String, SpeechRequest
        self.GetParameters, self.ControlSpeechPlayback = GetParameters, ControlSpeechPlayback
        # Own signal handling: keep spinning during bounded cancellation on Ctrl+C.
        from rclpy.signals import SignalHandlerOptions
        rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
        self.node = Node('malbut_smoke')
        self.rpc_timeout = rpc_timeout
        self._graph_at, self._graph = -math.inf, {}
        self.evidence, self.sensor_subscriptions = Evidence(), []
        self.tf = Buffer()
        self.listener = TransformListener(self.tf, self.node)
        self.manager = ManagerClient(self.node, on_event=lambda value: self.evidence.add(
            'mission', value), goal_response_timeout_s=rpc_timeout)
        self.action_probes = {
            name: ActionClient(self.node, kind, name) for name, kind in (
                ('/follow_person', FollowPerson), ('/malbut/falls/approach', FallApproach),
                ('/malbut/agent/confirm_situation', ConfirmSituation))}
        self.prepare_probe = self.node.create_client(
            Trigger, '/malbut/runtime/prepare/follow_person')
        reliable = QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        sensor = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)

        def subscribe(kind, message_type, topic, convert, qos=reliable):
            return self.node.create_subscription(message_type, topic, lambda message: (
                self.evidence.add(kind, convert(message))), qos)

        def object_message(message):
            try:
                value = json.loads(message.data)
                return value if isinstance(value, dict) else {}
            except (ValueError, TypeError):
                return {}

        def state(message):
            return {'system_state': message.system_state, 'control_mode': message.control_mode,
                    'missions': [{'id': entry.mission_id, 'capability': entry.capability_id}
                                 for field in MISSION_FIELDS for entry in getattr(message, field)]}

        subscribe('state', SystemState, '/malbut/state', state, latched)
        subscribe('runtime', String, '/malbut/runtime/state', object_message, latched)
        subscribe('fall_check', String, '/malbut/falls/check/status', object_message)
        subscribe('speech_ready', String, '/malbut/speech/status', lambda m: m.data, latched)
        subscribe('fall_status', FallRuntimeStatus, '/malbut/falls/status', lambda m: {
            key: getattr(m, key) for key in (
                'runtime_id', 'sequence', 'enabled', 'camera_enabled', 'cloud_consent',
                'accepting_images', 'last_frame_age_s', 'runtime_state', 'pause_reason',
                'analysis_state', 'request_id', 'last_error_code')})
        subscribe('transcript', SpeechTranscript, '/malbut/speech/transcript', lambda m: {
            'utterance_id': m.utterance_id, 'session_id': m.session_id,
            'stop_word': any(word in m.text.replace(' ', '')
                             for word in ('그만', '멈춰', '정지'))})
        subscribe('reply', SpeechRequest, '/malbut/speech/response', lambda m: {
            'playback_id': m.playback_id, 'request_id': m.request_id,
            'interim': m.interim, 'owner_goal_id': m.owner_goal_id,
            'has_text': bool(m.text.strip()), 'audio_id': m.audio_id})
        subscribe('audio', SpeechPlaybackStatus, '/malbut/speech/playback_status', lambda m: {
            'playback_id': m.playback_id, 'request_id': m.request_id, 'state': m.state})
        subscribe('worker_audio', SpeechPlaybackStatus, '/malbut/tts/playback_status', lambda m: {
            'playback_id': m.playback_id, 'request_id': m.request_id, 'state': m.state})
        # Ignore high-volume pose frames; retain only lifecycle/Cloud evidence.
        for topic, kind in (('/malbut/falls/runtime/events', 'fall_event'),
                            ('/malbut/falls/runtime/decision', 'fall_decision')):
            self.node.create_subscription(String, topic, lambda message, kind=kind: (
                self._fall_event(kind, object_message(message))), reliable)
        self.speech = self.node.create_publisher(
            SpeechRequest, '/malbut/speech/response', reliable)
        self.fall_stop = self.node.create_publisher(String, '/malbut/falls/check/voice', reliable)
        self.speech_control = self.node.create_client(
            ControlSpeechPlayback, '/malbut/speech/playback_control')
        self.worker_control = self.node.create_client(
            ControlSpeechPlayback, '/malbut/tts/playback_control')
        self.classifier = self.node.create_client(
            ClassifySpeechAddressee, '/malbut/speech/classify_addressee')

        def sensor_data(message):
            header = message.header
            stamp = header.stamp.sec + header.stamp.nanosec * 1e-9
            valid = bool(header.frame_id)
            if hasattr(message, 'ranges'):
                valid = valid and bool(message.ranges)
            elif hasattr(message, 'k'):
                valid = valid and message.k[0] > 0 and message.k[4] > 0
            elif hasattr(message, 'data'):
                valid = valid and message.width > 0 and message.height > 0 and bool(message.data)
            return {'valid': bool(valid), 'stamp': stamp, 'frame': header.frame_id}

        self.sensor_topics = (
            ('scan', LaserScan, '/scan_raw'), ('rgb', Image, '/depth_cam/rgb0/image_raw'),
            ('depth', Image, '/depth_cam/depth0/image_raw'),
            ('camera_info', CameraInfo, '/depth_cam/rgb0/camera_info'))
        self.subscribe_sensor = lambda kind, message_type, topic: subscribe(
            kind, message_type, topic, sensor_data, sensor)

        def odometry(message):
            twist = message.twist.twist
            sample = sensor_data(message)
            sample['valid'] = sample['valid'] and -0.5 <= time.time() - sample['stamp'] <= 3
            return {**sample, 'linear': math.hypot(twist.linear.x, twist.linear.y),
                    'angular': abs(twist.angular.z)}

        subscribe('odom', Odometry, '/odom', odometry, sensor)

    def _fall_event(self, kind, value):
        relevant = value.get('kind') in ('fall_sighting', 'fall_look_result')
        if relevant or (kind == 'fall_decision'
                        and value.get('action') in ('record', 'look', 'notify')):
            self.evidence.add(kind, value)

    def spin(self):
        self.rclpy.spin_once(self.node, timeout_sec=0.05)

    def parameters(self, name, keys, wait):
        client = self.node.create_client(self.GetParameters, name + '/get_parameters')
        future = None
        try:
            wait(client.service_is_ready, self.rpc_timeout, 'parameter_service_unavailable')
            future = client.call_async(self.GetParameters.Request(names=keys))
            wait(future.done, self.rpc_timeout, 'parameter_response_timeout')
            values = future.result().values
            if len(values) != len(keys):
                raise SmokeError('parameter_response_invalid')
            return {key: value for key, value in zip(keys, values)}
        finally:
            if future is not None and not future.done():
                client.remove_pending_request(future)
                future.cancel()
            self.node.destroy_client(client)

    def endpoints(self):
        return self._topology()['endpoints']

    def _topology(self):
        # Graph inspection is comparatively expensive on a busy robot. Cache
        # presence for half a second, never sensor/stop or actual RPC results.
        now = time.monotonic()
        if now - self._graph_at < 0.5:
            return self._graph
        expected = ('/malbut/state', '/malbut/runtime/state', '/malbut/speech/status',
                    '/malbut/speech/transcript', '/malbut/speech/playback_status',
                    '/malbut/falls/status', '/malbut/falls/check/status')
        endpoints = all(self.node.count_publishers(topic) == 1 for topic in expected)
        observer = (self.node.get_namespace(), self.node.get_name())

        def owners(topic, kind, publishing):
            query = (self.node.get_publishers_info_by_topic if publishing
                     else self.node.get_subscriptions_info_by_topic)
            return {(info.node_namespace, info.node_name) for info in query(topic)
                    if info.topic_type == 'malbut_interfaces/msg/' + kind
                    and (info.node_namespace, info.node_name) != observer}

        manager = owners('/malbut/speech/playback_status', 'SpeechPlaybackStatus', True)
        worker = owners('/malbut/tts/playback_status', 'SpeechPlaybackStatus', True)
        # The observer publishes AND subscribes to speech/response itself. Do
        # not mistake that self-match, or an STT subscriber, for Manager/Agent.
        agent = owners('/malbut/speech/response', 'SpeechRequest', True)
        route = bool(
            len(manager) == len(worker) == 1 and agent
            and manager <= owners('/malbut/speech/response', 'SpeechRequest', False)
            and manager <= owners('/malbut/tts/request', 'SpeechRequest', True)
            and manager <= owners('/malbut/tts/playback_status', 'SpeechPlaybackStatus', False)
            and worker <= owners('/malbut/tts/request', 'SpeechRequest', False)
            and agent & owners('/malbut/speech/transcript', 'SpeechTranscript', False))
        self._graph = dict(endpoints=endpoints, speech=route)
        self._graph_at = now
        return self._graph

    def speech_ready(self):
        return bool(self._topology()['speech']
                    and self.speech_control.service_is_ready()
                    and self.worker_control.service_is_ready()
                    and self.classifier.service_is_ready()
                    and self.speech.get_subscription_count() >= 2)

    def actions_ready(self, stage):
        names = ('/follow_person',) if stage == 'follow' else (
            '/malbut/falls/approach', '/malbut/agent/confirm_situation')
        return (self.manager._client.server_is_ready()
                and self.prepare_probe.service_is_ready()
                and all(self.action_probes[name].server_is_ready() for name in names))

    def start_sensors(self):
        # Do not deserialize RGB/depth while models and Nav2 are still booting.
        if not self.sensor_subscriptions:
            for kind, message_type, topic in self.sensor_topics:
                self.sensor_subscriptions.append(self.subscribe_sensor(kind, message_type, topic))

    def sensors_ready(self):
        from rclpy.time import Time
        for name in ('scan', 'rgb', 'depth', 'camera_info', 'odom'):
            value = self.evidence.value(name, {})
            if (not self.evidence.fresh(name) or not value.get('valid')
                    or not -0.5 <= time.time() - value.get('stamp', 0) <= 3):
                return False
            if not self.tf.can_transform('base_footprint', value['frame'], Time()):
                return False
        return self.tf.can_transform('map', 'base_footprint', Time())

    def release_images(self):
        # Avoid adding a second high-volume RGB/depth consumer during inference.
        for subscription in self.sensor_subscriptions:
            self.node.destroy_subscription(subscription)
        self.sensor_subscriptions.clear()

    def say(self, text, playback_id):
        self.speech.publish(self.SpeechRequest(
            text=text, request_type=self.SpeechRequest.NOTIFICATION,
            playback_id=playback_id, request_id=playback_id, interim=False))

    def stop_audio(self, playback_id):
        if self.speech_control.service_is_ready():
            return self.speech_control.call_async(self.ControlSpeechPlayback.Request(
                playback_id=playback_id, command='stop'))
        return None

    def stop_fall(self):
        self.fall_stop.publish(self.String(data=json.dumps({'kind': 'stop'})))

    def close(self):
        self.release_images()
        for client in self.action_probes.values():
            client.destroy()
        self.manager.close()  # close() alone is NOT cancellation: Runner cleans up first.
        self.node.destroy_node()
        self.rclpy.shutdown()


class Runner:
    def __init__(self, robot, args, report, clock=time.monotonic):
        self.robot, self.args, self.report, self.clock = robot, args, report, clock
        self.events = robot.evidence
        self.interrupted = False
        self.tracking = None
        self.incident = None
        self.ambiguous_fall = False
        self.guides = set()
        self.tts_finished = 0
        self.tts_failed = 0
        self.still_since = None
        self.journal = None
        self.allow_fall = False
        self.started_work = False
        self.consumed_utterances = set()
        self.dialogue_boundary = None

    def tick(self, *, cleanup=False):
        if self.interrupted and not cleanup:
            raise SmokeError('interrupted')
        self.robot.spin()
        check = self.events.value('fall_check', {})
        if not cleanup and check.get('active'):
            different = self.incident and check.get('incident_id') not in (None, self.incident)
            if not self.allow_fall or different:
                raise SmokeError('unexpected_fall_check', unsafe=True)

    def wait(self, predicate, seconds, code, *, cleanup=False):
        started = self.clock()
        deadline, next_report = started + seconds, started + 5
        while self.clock() < deadline:
            self.tick(cleanup=cleanup)
            value = predicate()
            if value:
                return value
            if self.clock() >= next_report:
                print(f'대기 중: {code} ({self.clock() - started:.0f}/{seconds:g}초)', flush=True)
                next_report = self.clock() + 10
        raise SmokeError(code)

    def idle(self):
        state = self.events.value('state', {})
        runtime = self.events.value('runtime', {})
        odom = self.events.value('odom', {})
        return bool(
            self.robot.endpoints()
            and state.get('system_state') == 1 and state.get('control_mode') == 0
            and not state.get('missions') and runtime.get('ready') is True
            and not runtime.get('error') and runtime.get('active_groups') == []
            and runtime.get('capabilities') == []
            and self.events.fresh('odom') and odom.get('valid')
            and math.isfinite(odom.get('linear', math.inf))
            and math.isfinite(odom.get('angular', math.inf))
            and odom['linear'] < 0.03 and odom['angular'] < 0.05
            and self.events.fresh('fall_check')
            and self.events.value('fall_check', {}).get('active') is False)

    def settled(self):
        if not self.idle():
            self.still_since = None
            return False
        if self.still_since is None:
            self.still_since = self.clock()
        return self.clock() - self.still_since >= 1.0

    def require_idle(self, *, cleanup=False, timeout=None):
        # A latched idle value is insufficient if its executor has since hung.
        self.robot.parameters('/runtime_control', ['use_sim_time'],
                              lambda predicate, seconds, code: self.wait(
                                  predicate, seconds, code, cleanup=cleanup))
        self.still_since = None
        self.wait(self.settled, timeout or self.args.transition_timeout,
                  'idle_or_stop_unconfirmed', cleanup=cleanup)

    def ledger(self, path, statement, arguments=(), *, startup=False):
        def read():
            try:
                return (read_rows(path, statement, arguments),)
            except SmokeError as error:
                if error.code == 'ledger_busy' or (startup and error.code in (
                        'ledger_missing', 'ledger_unavailable_or_schema_mismatch')):
                    return None
                raise
        return self.wait(read, self.args.ready_timeout if startup else self.args.rpc_timeout,
                         'ledger_read_timeout')[0]

    def prepare(self):
        self.allow_fall = True  # Initial state has not been received yet.
        self.wait(lambda: self.robot.endpoints() and self.events.value('speech_ready') == 'ready',
                  self.args.ready_timeout, 'required_endpoints_or_speech_unavailable')
        self.require_idle(timeout=self.args.ready_timeout)
        self.allow_fall = False
        self.wait(self.robot.speech_ready, self.args.ready_timeout, 'speech_route_not_ready')
        self.wait(lambda: self.robot.actions_ready('follow') and self.robot.actions_ready('fall'),
                  self.args.ready_timeout, 'feature_actions_not_discovered')
        self.robot.start_sensors()
        try:
            self.wait(self.robot.sensors_ready, self.args.ready_timeout,
                      'sensor_or_tf_unavailable')
        finally:
            self.robot.release_images()
        params = self.robot.parameters('/system_manager', ['use_sim_time'], self.wait)
        if params['use_sim_time'].type != 1 or params['use_sim_time'].bool_value:
            raise SmokeError('real_robot_wall_clock_required')
        params = self.robot.parameters('/malbut_cloud_fall_monitor', ['flow_enabled'], self.wait)
        if not params['flow_enabled'].bool_value:
            raise SmokeError('current_fall_flow_required')
        params = self.robot.parameters('/malbut_tts', ['backend'], self.wait)
        backend = params['backend'].string_value
        if backend not in ('openai', 'piper', 'qwen-cuda'):
            raise SmokeError('tts_backend_unknown')
        self.ledger(self.args.conversation_db, 'SELECT request_id FROM conversation_turns LIMIT 0',
                    startup=True)
        from malbut_agent_server.fall_preflight import read_settings
        settings = read_settings(self.args.fall_config.expanduser())
        self.journal = settings.journal_path
        self.wait(self.fall_ready, self.args.ready_timeout,
                  'fall_settings_camera_consent_not_ready')
        open_incidents = self.ledger(
            self.journal, 'SELECT incident_id FROM flow_incidents WHERE open=1 LIMIT 1',
            startup=True)
        if open_incidents:
            raise SmokeError('existing_open_fall_incident')
        self.report.set(0, 'PASS', tts_backend=backend,
                        llm_api='verified_in_dialogue_not_preflight',
                        guardian_delivery='not_verified')

    def fall_ready(self):
        value = self.events.value('fall_status', {})
        return bool(self.events.fresh('fall_status') and value.get('sequence', 0) > 0
                    and all(value.get(key) is True for key in (
                        'enabled', 'camera_enabled', 'cloud_consent', 'accepting_images'))
                    and value.get('runtime_state') == 'ready'
                    and 0 <= value.get('last_frame_age_s', -1) <= 3
                    # A periodic Cloud scan in progress is normal operation,
                    # not an unready camera or a reason to wait for a lucky gap.
                    and value.get('analysis_state') != 'failed')

    def guide(self, text, *, may_yield_to_fall=False, may_yield_to_dialogue=False):
        self.wait(self.robot.speech_ready, self.args.ready_timeout, 'speech_route_not_ready')
        print('\n안내: ' + text, flush=True)
        self.started_work = True
        playback = 'smoke-' + uuid4().hex
        self.guides.add(playback)
        boundary = self.events.sequence
        try:
            self.robot.say(text, playback)
            state = self.wait(lambda: self.events.audio(playback, boundary),
                              self.args.turn_timeout, 'guide_playback_timeout')
            if state != 'finished':
                raise SmokeError('guide_playback_' + state)
            self.tts_finished += 1
            self.guides.discard(playback)
        except SmokeError as error:
            if error.code == 'interrupted' or error.unsafe:
                raise
            fall_active = self.events.value('fall_check', {}).get('active') is True
            yielded = (error.code == 'guide_playback_stopped'
                       and (may_yield_to_dialogue or (may_yield_to_fall and fall_active)))
            if not yielded:
                self.tts_failed += 1
                self.report.set(5, 'FAIL', error.code)
            self.robot.stop_audio(playback)
            try:
                self.wait(lambda: self.events.audio(playback, boundary, 'worker_audio'),
                          self.args.cleanup_timeout, 'guide_stop_unconfirmed')
            except SmokeError:
                raise SmokeError('guide_stop_unconfirmed', unsafe=True) from None
            self.guides.discard(playback)
            print('사용자 발화 또는 낙상 절차에 안내 양보' if yielded else
                  'TTS 실패: 위 터미널 안내를 따라 진행합니다.', flush=True)

    def dialogue_turn(self, instruction):
        self.require_idle()
        boundary = self.events.sequence
        self.dialogue_boundary = boundary
        self.guide(instruction, may_yield_to_dialogue=True)

        def completed():
            # A user accidentally requesting motion must not advance this speech test.
            if self.events.value('state', {}).get('missions'):
                raise SmokeError('unexpected_mission_during_dialogue', unsafe=True)
            return self.events.dialogue(boundary, self.consumed_utterances)

        uid = self.wait(completed, self.args.turn_timeout, 'dialogue_turn_timeout')
        proof = self.wait(lambda: api_evidence(self.args.conversation_db, uid),
                          self.args.rpc_timeout, 'dialogue_api_ledger_timeout')
        self.consumed_utterances.add(uid)
        self.dialogue_boundary = None
        self.tts_finished += 1
        return proof

    def conversation(self):
        first = self.dialogue_turn('호출어를 부르고, 지금 테스트 중이라고 말하세요.')
        second = self.dialogue_turn('호출어를 부르고, 방금 무엇을 한다고 했는지 물어보세요.')
        self.report.set(1, 'PASS', llm_responses=[first, second], turns=2)

    def follow(self):
        self.require_idle()
        self.wait(lambda: self.robot.actions_ready('follow'), self.args.ready_timeout,
                  'follow_dependencies_not_discovered')
        self.guide('사람 추적을 시작합니다. 잠시 후 천천히 이동하고 방향을 바꿔 주세요. '
                   '멈추라고 말해도 되고, 시간이 지나면 자동으로 종료합니다.')
        deadline = self.clock() + self.args.prepare_seconds
        self.wait(lambda: self.clock() >= deadline, self.args.prepare_seconds + 1,
                  'preparation_timeout')
        self.tracking = self.robot.manager.submit('follow_person', {
            'target_mode': 0, 'target_person_id': '', 'desired_distance_m': 1.0},
            request_id='smoke-follow-' + uuid4().hex)
        boundary = self.events.sequence

        def acquired():
            import yaml
            snapshot = self.robot.manager.snapshot(self.tracking)
            if snapshot['terminal'] or snapshot['state'] == 'UNKNOWN':
                raise SmokeError('follow_start_' + snapshot['state'].lower())
            try:
                feedback = yaml.safe_load(snapshot.get('feedback_yaml') or '')
            except yaml.YAMLError:
                raise SmokeError('invalid_follow_feedback') from None
            return (snapshot['state'] == 'RUNNING' and isinstance(feedback, dict)
                    and feedback.get('target_visible') is True)

        self.wait(acquired, self.args.transition_timeout, 'follow_target_or_execution_timeout')
        started = self.clock()
        while self.clock() - started < self.args.follow_seconds:
            self.tick()
            snapshot = self.robot.manager.snapshot(self.tracking)
            if snapshot['terminal']:
                break
            if snapshot['state'] == 'UNKNOWN':
                raise SmokeError('follow_transport_unknown')
        observed_seconds = self.clock() - started
        stopping_at = self.clock()
        snapshot = self.robot.manager.snapshot(self.tracking)
        auto_cancel = not snapshot['terminal']
        if auto_cancel:
            self.robot.manager.cancel(self.tracking)
        self.wait(lambda: self.robot.manager.snapshot(self.tracking)['terminal'],
                  self.args.cleanup_timeout, 'follow_result_unconfirmed')
        snapshot = self.robot.manager.snapshot(self.tracking)
        if snapshot['state'] != 'CANCELED':
            raise SmokeError('follow_unexpected_terminal_' + snapshot['state'].lower())
        self.require_idle()
        stop_seen = any(entry[3]['stop_word']
                        for entry in self.events.since(boundary, 'transcript'))
        voice = not auto_cancel and stop_seen
        self.report.set(2, 'PASS', observed_seconds=round(observed_seconds, 2),
                        stop_and_idle_seconds=round(self.clock() - stopping_at, 2),
                        cancellation='timer' if auto_cancel else 'external',
                        voice_stop='OBSERVED' if voice else 'NOT_TESTED',
                        target_seen=True, terminal=snapshot['state'])

    def fall(self):
        self.require_idle()
        self.wait(lambda: self.robot.actions_ready('fall') and self.fall_ready(),
                  self.args.ready_timeout, 'fall_dependencies_not_ready')
        # The person may react before the last syllable of the guide. Keep that
        # genuine detection, rather than losing it behind a post-guide boundary.
        boundary = self.events.sequence
        self.allow_fall = True
        self.guide('낙상 상황을 보여 주세요. 실제로 넘어지지 말고 안전하게 연출하세요. '
                   '로봇이 질문하면 괜찮다고 답해 주세요.', may_yield_to_fall=True)

        def incident_started():
            records = [entry[3] for entry in self.events.since(boundary, 'fall_decision')
                       if entry[3].get('action') == 'record'
                       and entry[3].get('event') == 'incident_opened'
                       and entry[3].get('incident_id')]
            ids = {record['incident_id'] for record in records}
            if len(ids) > 1:
                self.ambiguous_fall = True
                raise SmokeError('multiple_fall_incidents', unsafe=True)
            return records[0] if records else None

        opened = self.wait(incident_started, self.args.fall_timeout, 'fall_incident_not_detected')
        self.incident = opened['incident_id']

        def resolved():
            decisions = [entry[3] for entry in self.events.since(boundary, 'fall_decision')]
            if any(value.get('event') == 'incident_opened'
                   and value.get('incident_id') != self.incident for value in decisions):
                self.ambiguous_fall = True
                raise SmokeError('multiple_fall_incidents', unsafe=True)
            matching = [value for value in decisions if value.get('incident_id') == self.incident]
            failures = ('agent_check_failed', 'analysis_unavailable', 'check_stopped')
            if any(value.get('event') in failures for value in matching):
                raise SmokeError('fall_check_failed_or_stopped')
            final = next((value for value in matching
                          if value.get('event') == 'incident_resolved'), None)
            if final is None:
                return None
            looks = {value['look_id'] for value in decisions if value.get('action') == 'look'
                     and value.get('look_id') and (value.get('incident_id') == self.incident
                     or (opened.get('sighting_id')
                         and value.get('sighting_id') == opened['sighting_id']))}
            cloud = [entry[3] for entry in self.events.since(boundary, 'fall_event')
                     if entry[3].get('kind') == 'fall_look_result'
                     and entry[3].get('look_id') in looks]
            if not cloud:
                # Cloud results and Agent decisions have different publishers.
                # A busy executor may observe closure before the matching look.
                return None
            if any(value.get('verdict') in ('failed', 'guard') for value in cloud):
                raise SmokeError('fall_cloud_result_not_verified')
            # A decision is not proof of a durable final record. Do not mutate it.
            rows = self.ledger(self.journal, 'SELECT open FROM flow_incidents WHERE incident_id=?',
                               (self.incident,))
            if not rows or rows[0][0] != 0:
                return None
            durable = self.ledger(self.journal,
                                  'SELECT payload FROM incident_events WHERE incident_id=? '
                                  'ORDER BY sequence DESC LIMIT 32', (self.incident,))
            if not any(json.loads(row[0]).get('eventKind') == 'incident_resolved'
                       for row in durable):
                return None
            requested = any(value.get('event') == 'question_requested' for value in matching)
            answered = any(value.get('event') == 'confirmation_completed'
                           and value.get('answer') == 'okay' for value in matching)
            if requested and not answered:
                raise SmokeError('fall_question_answer_not_verified')
            return {'cloud_looks_completed': len(cloud), 'question_requested': requested,
                    'user_answer': 'okay' if answered else 'NOT_REQUIRED',
                    'resolved_reason': final.get('reason'),
                    'guardian_delivery': 'NOT_VERIFIED'}

        proof = self.wait(resolved, self.args.fall_timeout, 'fall_final_result_timeout')
        self.require_idle()
        self.allow_fall = False
        self.report.set(3, 'PASS', **proof)

    def recovery(self):
        # Current FallFlow can recheck a closed incident when the person stands
        # up. Let that existing check finish, rather than talking over it or
        # misclassifying its legitimate preemption as a TTS failure.
        self.allow_fall = True
        self.require_idle(timeout=self.args.fall_timeout)
        self.guide('낙상 검사를 마쳤습니다. 안전하게 천천히 일어나 주세요.',
                   may_yield_to_fall=True)
        self.require_idle(timeout=self.args.fall_timeout)
        self.allow_fall = False
        proof = self.dialogue_turn('호출어를 부르고, 테스트가 끝났으니 인사해 달라고 말하세요.')
        self.require_idle()
        self.report.set(4, 'PASS', llm_responses=[proof])

    def cleanup(self):
        """Never cancel-all, pkill, forge guardian closure, or publish cmd_vel."""
        if not self.started_work:
            self.report.data['cleanup'] = 'NOT_NEEDED'
            self.report.save()
            return
        try:
            if self.tracking and not self.robot.manager.snapshot(self.tracking)['terminal']:
                self.robot.manager.cancel(self.tracking)
                self.wait(lambda: self.robot.manager.snapshot(self.tracking)['terminal'],
                          self.args.cleanup_timeout, 'tracking_cleanup_timeout', cleanup=True)
            # Only the incident first observed in our fall stage is eligible.
            check = self.events.value('fall_check', {})
            if (self.incident and not self.ambiguous_fall and check.get('active')
                    and check.get('incident_id') == self.incident):
                self.robot.stop_fall()
            for playback in tuple(self.guides):
                self.robot.stop_audio(playback)
            if self.guides:
                def guides_stopped():
                    return all(self.events.audio(pid, kind='worker_audio') in TERMINAL_AUDIO
                               for pid in self.guides)
                self.wait(guides_stopped, self.args.cleanup_timeout,
                          'speech_cleanup_timeout', cleanup=True)
            if self.dialogue_boundary is not None:
                stopping = set()

                def dialogue_drained():
                    uids = {item[3]['utterance_id'] for item in self.events.since(
                        self.dialogue_boundary, 'transcript') if not item[3]['session_id']}
                    replies = [item[3] for item in self.events.since(
                        self.dialogue_boundary, 'reply') if item[3]['request_id'] in uids]
                    if any(not any(reply['request_id'] == uid and not reply['interim']
                                   for reply in replies) for uid in uids):
                        return False  # A late API result can still produce speech.
                    for reply in replies:
                        pid = reply['playback_id']
                        if self.events.audio(pid, kind='worker_audio') not in TERMINAL_AUDIO:
                            if pid not in stopping:
                                self.robot.stop_audio(pid)
                                stopping.add(pid)
                            return False
                    return True

                self.wait(dialogue_drained, self.args.cleanup_timeout,
                          'late_dialogue_result_unconfirmed', cleanup=True)
            self.require_idle(cleanup=True)
            self.report.data['cleanup'] = 'CONFIRMED'
        except Exception:
            self.report.data['cleanup'] = 'UNCONFIRMED'
            print('정지/정리가 확인되지 않았습니다. 로봇을 직접 정지시키세요.', flush=True)
        self.report.save()

    def run(self):
        for index, method in enumerate((self.prepare, self.conversation, self.follow,
                                        self.fall, self.recovery)):
            try:
                method()
                if self.args.check:
                    return
                if index == 0:
                    self.guide('검사를 시작합니다.')
            except SmokeError as error:
                self.report.set(index, 'FAIL', error.code)
                # No cascaded movement after a failed stage or unknown transport.
                break
            except Exception as error:
                self.report.set(index, 'FAIL', type(error).__name__)
                break
        if self.args.execute:
            self.report.set(5, 'FAIL' if self.tts_failed else (
                'PASS' if self.tts_finished else 'NOT_RUN'),
                finished_requests=self.tts_finished, failed_guides=self.tts_failed,
                api_call_count='not_exposed_by_tts_worker')


def final_review(report, seconds):
    print('\n자동 검사가 끝났습니다. 관찰 실패 항목 번호를 쉼표로 입력하세요. '
          '예: 2,4:질문이 안 들림 / Enter=관찰한 항목 모두 정상, q=미평가. '
          '입력 제한 시간 후 미평가로 저장합니다.', flush=True)
    for index, item in enumerate(report.data['steps'], 1):
        print(f"{index}. {item['name']}: {item['automatic']}", flush=True)
    terminal = None
    try:
        terminal = os.open('/dev/tty', os.O_RDWR | os.O_NONBLOCK)
        deadline, pending = time.monotonic() + seconds, b''
        os.write(terminal, '관찰 결과: '.encode())
        while time.monotonic() < deadline:
            ready, _, _ = select.select([terminal], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                return
            chunk = os.read(terminal, 4096)
            if not chunk:
                return  # EOF is not an explicit Enter.
            pending += chunk
            if len(pending) > 4096:
                return
            while b'\n' in pending:
                line, _, pending = pending.partition(b'\n')
                answer = line.decode('utf-8', errors='replace').rstrip('\r')
                if answer.strip().lower() == 'q':
                    return
                if report.review(answer):
                    return
                os.write(terminal, '항목 번호(예: 2,4) 또는 Enter를 입력하세요: '.encode())
    except (OSError, ValueError, KeyboardInterrupt):
        return
    finally:
        if terminal is not None:
            os.close(terminal)


def main(argv=None):
    args = parser().parse_args(argv)
    if not (args.execute or args.check):
        print('계획: 준비 → 실제 음성대화 2턴 → 자동 사람추적/취소 → 낙상 → 대화 복귀 → 일괄 평가\n'
              '기존 Bringup을 먼저 켜세요. --check는 읽기 전용 검사입니다.\n'
              '실행: bash smoke.sh --execute --test-alerts-confirmed\n'
              '주의: 실제 이동·외부 API 과금·사건 기록·보호자 알림이 발생할 수 있습니다.')
        return 0
    if args.execute and not args.test_alerts_confirmed:
        parser().error('--test-alerts-confirmed로 테스트 수신 대상과 안전 공간을 먼저 확인하세요.')
    domain = os.environ.get('ROS_DOMAIN_ID', '0')
    if not domain.isdecimal() or not 0 <= int(domain) <= 232:
        parser().error('ROS_DOMAIN_ID는 0~232여야 합니다.')
    lock_path = Path('/tmp') / f'malbut-smoke-{os.getuid()}-{domain}.lock'
    # No unlink: another waiter must never lock a new inode while the old one is held.
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    lock_fd = None
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if lock_fd is not None:
            os.close(lock_fd)
        print('다른 스모크 검사 실행 중이거나 잠금 파일을 사용할 수 없습니다.', file=sys.stderr)
        return 1
    robot = runner = report = None
    previous = {}
    try:
        report = Report(args.report_dir)
        report.data['mode'] = 'execute' if args.execute else 'check'
        robot = RosRobot(rpc_timeout=args.rpc_timeout)
        runner = Runner(robot, args, report)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: setattr(runner, 'interrupted', True))
        runner.run()
    except Exception as error:
        code = error.code if isinstance(error, SmokeError) else type(error).__name__
        print('스모크 실행 실패: ' + code, file=sys.stderr, flush=True)
        if report:
            try:
                report.set(0, 'FAIL', code)
            except OSError:
                report.data.setdefault('infrastructure_errors', []).append('report_write_failed')
    finally:
        try:
            if runner:
                runner.cleanup()
            elif report:
                report.data['cleanup'] = 'NOT_STARTED'
        except Exception:
            print('정리 또는 결과 저장 실패. 로봇 정지 상태를 직접 확인하세요.', file=sys.stderr)
            if report:
                report.data['cleanup'] = 'UNCONFIRMED'
        finally:
            try:
                if robot:
                    robot.close()
            except Exception:
                print('관측 노드 종료 실패', file=sys.stderr)
                if report:
                    errors = report.data.setdefault('infrastructure_errors', [])
                    errors.append('observer_close_failed')
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            os.close(lock_fd)
    if report is None:
        return 1
    interrupted = bool(runner and runner.interrupted)
    if args.check:
        passed = (report.data['steps'][0]['automatic'] == 'PASS'
                  and not report.data.get('infrastructure_errors') and not interrupted)
        report.data['overall'] = 'CHECK_PASS' if passed else 'CHECK_FAIL'
        try:
            report.save()
        except OSError:
            print('준비 검사 결과 저장 실패', file=sys.stderr)
            return 1
        print(f"준비 검사: {report.data['overall']} · {report.path}")
        return 130 if interrupted else 0 if passed else 1
    if not args.no_review and not interrupted:
        final_review(report, args.review_timeout)
    return report.finish(interrupted)


if __name__ == '__main__':
    raise SystemExit(main())
MALBUT_SMOKE_PY
