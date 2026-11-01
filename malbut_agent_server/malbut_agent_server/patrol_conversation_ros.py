"""Wake-free everyday conversation on the existing Agent, detector and speech stack."""

from concurrent.futures import ThreadPoolExecutor
from collections import deque
from datetime import datetime
import json
import math
import time
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from malbut_agent_server.patrol_conversation import (
    EncounterGate, PatrolConversationTopics, conversation_preference, ends_conversation,
    homecam_person_count,
)


class RosPatrolConversation:
    """Use existing detections; never subscribe to pixels, infer identity, or drive."""

    def __init__(self, node, *, clock=time.monotonic, wall_clock=time.time, pool=None):
        from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest, SystemState
        from malbut_interfaces.srv import ControlSpeechPlayback, ControlSpeechSession
        from nav_msgs.msg import Odometry
        from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
        from std_msgs.msg import String
        from yolo_msgs.msg import DetectionArray

        self.node, self.clock = node, clock
        self.wall_clock = wall_clock
        node.declare_parameter('patrol_chat_quiet_start_hour', 22)
        node.declare_parameter('patrol_chat_quiet_end_hour', 8)
        node.declare_parameter('patrol_chat_timezone', 'Asia/Seoul')
        self.gate = EncounterGate(clock=clock)
        self._pool = pool or ThreadPoolExecutor(max_workers=1, thread_name_prefix='patrol-chat')
        self._future = None
        self._control_future = None
        self._preference_future = None
        self._preference_ids = deque(maxlen=64)
        self._ended_reply_ids = deque(maxlen=64)
        self._closed = False
        self._system_idle = False
        self._odom_at = None
        self._stationary_since = None
        self._playing = {}
        self._last_detection_stamp = -math.inf
        self._presence = {}
        self._presence_stamps = {}
        self._last_report = None
        self._report_at = 0.
        self.phase = 'idle'
        self.opener = None
        self.session_id = ''
        self.request_id = ''
        self.reply_id = ''
        self._playback_id = ''
        self._first_reply = False
        self.deadline = 0.
        self._retry_at = 0.
        self._session_type, self._stop_type = ControlSpeechSession, ControlSpeechPlayback
        self._input = node.create_client(ControlSpeechSession, '/malbut/speech/session_control')
        self._stop = node.create_client(ControlSpeechPlayback, '/malbut/speech/playback_control')
        detection_topic = node.declare_parameter('patrol_chat_detections_topic', '/yolo/detections').value
        pose_topic = node.declare_parameter('patrol_chat_person_poses_topic', '/homecam/person_poses').value
        odom_topic = node.declare_parameter('patrol_chat_odom_topic', '/odom').value
        self._subscriptions = [
            node.create_subscription(DetectionArray, detection_topic, self._detections,
                                     qos_profile_sensor_data),
            # The tracking detector sleeps outside FollowPerson missions. Reuse
            # existing fall-pose results while idle; do not wake another model.
            node.create_subscription(String, pose_topic, self._person_poses,
                                     qos_profile_sensor_data),
            node.create_subscription(Odometry, odom_topic, self._odometry, qos_profile_sensor_data),
            node.create_subscription(SystemState, '/malbut/state', self._system,
                                     QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)),
            node.create_subscription(SpeechPlaybackStatus, '/malbut/speech/playback_status',
                                     self._playback, node._speech_qos),
            node.create_subscription(SpeechRequest, '/malbut/speech/response',
                                     self._speech_request, node._speech_qos),
        ]
        self._timer = node.create_timer(.25, self.tick)

    @property
    def active(self):
        return self.phase != 'idle'

    def _quiet(self):
        """Read live settings; equal hours disable quiet time. Fail closed on errors."""
        try:
            start = self.node.get_parameter('patrol_chat_quiet_start_hour').value
            end = self.node.get_parameter('patrol_chat_quiet_end_hour').value
            if any(type(hour) is not int or not 0 <= hour <= 23 for hour in (start, end)):
                return True
            zone = ZoneInfo(self.node.get_parameter('patrol_chat_timezone').value)
            hour = datetime.fromtimestamp(self.wall_clock(), zone).hour
            return start <= hour < end if start < end else (
                start != end and (hour >= start or hour < end))
        except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
            return True

    def _detections(self, message):
        source = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
        age = self.node.get_clock().now().nanoseconds / 1e9 - source
        if not 0 <= age <= 1.5 or source <= self._last_detection_stamp:
            return
        self._last_detection_stamp = source
        people = sum(d.class_name.casefold() == 'person' and math.isfinite(d.score)
                     and .65 <= d.score <= 1 for d in message.detections[:100])
        # A single camera-visible person is only an encounter, not identity.
        self._observe_people('yolo', source, people)

    def _person_poses(self, message):
        if len(message.data) > 256 * 1024:
            return
        try:
            payload = json.loads(message.data)
            stamp = payload['captureStamp']
            sec, ns = stamp['sec'], stamp['nanosec']
            if type(sec) is not int or type(ns) is not int or not 0 <= ns < 10**9:
                return
            source = sec + ns / 1e9
        except (ValueError, TypeError, KeyError, OverflowError):
            return
        self._observe_people('homecam_pose', source, homecam_person_count(payload))

    def _observe_people(self, source, stamp, count):
        age = self.node.get_clock().now().nanoseconds / 1e9 - stamp
        if not 0 <= age <= 1.5 or stamp <= self._presence_stamps.get(source, -math.inf):
            return
        self._presence_stamps[source] = stamp
        now = self.clock()
        self._presence[source] = (now, count)
        counts = {count for at, count in self._presence.values() if 0 <= now - at <= 1.5}
        # Never add counts across models (the same person may appear in both).
        # Disagreement/uncertainty or a second person prevents private dialogue.
        self.gate.observe(True if counts == {1} else False if counts == {0} else None)

    def _odometry(self, message):
        source = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
        age = self.node.get_clock().now().nanoseconds / 1e9 - source
        velocity = message.twist.twist
        values = (velocity.linear.x, velocity.linear.y, velocity.angular.z)
        if self._odom_at is None or self.clock() - self._odom_at > 1.5:
            self._stationary_since = None
        self._odom_at = self.clock()
        if (not 0 <= age <= 1.5 or not all(math.isfinite(v) for v in values)
                or math.hypot(*values[:2]) > .02 or abs(values[2]) > .03):
            self._stationary_since = None
        elif self._stationary_since is None:
            self._stationary_since = self.clock()

    def _system(self, message):
        self._system_idle = (message.system_state == message.IDLE
                             and message.control_mode == message.AUTONOMOUS
                             and not message.active_foreground_missions
                             and not message.pending_missions
                             and not message.suspended_missions)

    def _safe(self):
        now = self.clock()
        scene = getattr(self.node, '_scene', None)
        return (not self._closed and not self.node._closing
                and not self.node._in_fall_window
                and not (self.node.situation and self.node.situation.active)
                and self._system_idle and self.node.count_publishers('/malbut/state') > 0
                and self._odom_at is not None and 0 <= now - self._odom_at <= 1.5
                and self._stationary_since is not None and now - self._stationary_since >= 2
                and (not self.active or self.gate.present_since is not None
                     and self.gate.last_present is not None
                     and 0 <= now - self.gate.last_present <= 1.5)
                and (scene is None or scene._images.permitted()))

    def _idle(self):
        worker = self.node.dialogue
        return (self._safe() and worker.ready and self.node._speech_ready
                and not self._quiet()
                and not worker.has_pending and not self.node._input_utterance_id
                and not self._playing and self.gate.ready()
                and self._preference_future is None
                and self._input.service_is_ready())

    def _speech_request(self, message):
        self.gate.activity()
        if message.playback_id:
            self._playing[message.playback_id] = self.clock() + 180
        acknowledgement = 'ack:' + str(uuid5(NAMESPACE_URL, self.reply_id)) if self.reply_id else ''
        if message.request_id in (self.request_id, self.reply_id, acknowledgement) and message.request_id:
            if not message.interim:
                self._playback_id = message.playback_id
        elif self.active:
            self.end()

    def _playback(self, message):
        terminal = message.state in ('finished', 'failed', 'stopped')
        if terminal:
            self._playing.pop(message.playback_id, None)
        elif message.state in ('accepted', 'playing', 'paused'):
            self._playing[message.playback_id] = self.clock() + 180
        self.gate.activity()
        if (not self.active or message.interim or message.playback_id != self._playback_id
                or message.request_id not in (self.request_id, self.reply_id)):
            return
        if message.state == 'finished' and self.phase in ('speaking', 'responding'):
            self._playback_id = ''
            self._listen()
        elif message.state in ('failed', 'stopped'):
            self.end()

    def _listen(self):
        if not self._safe() or not self._input.service_is_ready():
            self.end()
            return
        self.session_id = 'patrol-chat-' + uuid4().hex
        self.phase, self.deadline = 'opening', self.clock() + 3
        self._control_future = self._input.call_async(
            self._session_type.Request(session_id=self.session_id, active=True))

    def input_status(self, message):
        self.gate.activity()
        if message.session_id and message.session_id == self.session_id:
            if message.state == 'failed':
                self.end()
            elif message.state == 'started':
                self.deadline = self.clock() + 60
        elif message.state == 'started' and self.active:
            self.end()

    def receive(self, message):
        """Route our wake-free reply into ordinary dialogue, leaving other sessions alone."""
        if self.node._in_fall_window or (self.node.situation and self.node.situation.active):
            return False
        if not message.utterance_id.strip() or len(message.utterance_id) > 256:
            return False
        preference = conversation_preference(message.text)
        if (preference is not None
                and (not message.session_id or message.session_id == self.session_id)):
            if message.utterance_id in self._preference_ids:
                return True
            if self._preference_future is not None:
                return False
            self._preference_ids.append(message.utterance_id)
            self.end()
            if self._future is not None:
                # A queued selection cannot speak after a later explicit opt-out.
                self._future.cancel()
                self._future = None
            self._preference_future = (self._pool.submit(PatrolConversationTopics(
                self.node.dialogue.patrol_memory).set_enabled, preference),
                message.utterance_id, preference)
            return True
        if not message.session_id or message.session_id != self.session_id:
            if not message.session_id and self.active and message.utterance_id != self.reply_id:
                self.end()
            return False
        if self.phase not in ('opening', 'listening') or not self._safe():
            self.end()
            return True
        self._stop_listening()
        self.gate.activity()
        if ends_conversation(message.text):
            self.end()
            return True
        context = self.opener.context() if self._first_reply else None
        self._first_reply = False
        self.phase, self.deadline = 'responding', self.clock() + 120
        self.reply_id = message.utterance_id
        try:
            accepted = self.node.dialogue.submit(
                message.utterance_id, message.text, opener_context=context,
                companion_session=self.request_id)
        except ValueError:
            accepted = False
        if accepted:
            self.node._awaiting_reply = message.utterance_id
        else:
            self.end()
        return True

    def allows_reply(self, utterance_id):
        """An expired encounter must not speak a late answer or dispatch its proposal."""
        if self.active and utterance_id == self.reply_id and not self._safe():
            self.end()
        return utterance_id not in self._ended_reply_ids

    def tick(self):
        if self._closed:
            return
        now = self.clock()
        if now >= self._report_at:
            if self.active:
                reason = self.phase
            elif not self._presence or all(now - at > 1.5 for at, _ in self._presence.values()):
                reason = 'person_input_stale'
            elif self.gate.present_since is None:
                reason = 'no_single_confirmed_person'
            elif not self._safe():
                reason = 'robot_unavailable_or_moving'
            elif self._quiet():
                reason = 'quiet_hours'
            elif not self._idle():
                reason = 'waiting_for_idle_or_new_encounter'
            else:
                reason = 'waiting_for_preference_or_cooldown'
            if reason != self._last_report:
                self.node.get_logger().info('patrol_conversation_state reason=' + reason)
                self._last_report = reason
            self._report_at = now + 5
        if self._preference_future is not None and self._preference_future[0].done():
            future, identity, enabled = self._preference_future
            self._preference_future = None
            try:
                future.result()
                text = ('알겠어요. 대화하기 좋은 때 먼저 말을 걸게요.' if enabled
                        else '알겠어요. 먼저 말 걸지 않을게요.')
            except Exception:
                text = '먼저 말 걸기 설정을 저장하지 못했어요.'
            self.node.say(text, request_id=identity)
        self._playing = {key: expiry for key, expiry in self._playing.items() if expiry > now}
        if (self.phase == 'responding' and self._playback_id
                and not self.node.dialogue.companion_reply_allowed(self.request_id)):
            self.end()
        if self.active and (not self._safe() or now >= self.deadline):
            self.end()
        if self._control_future is not None and self._control_future.done():
            future, self._control_future = self._control_future, None
            try:
                accepted = future.result().accepted
            except Exception:
                accepted = False
            if self.phase == 'opening':
                if accepted:
                    self.phase, self.deadline = 'listening', now + 20
                else:
                    self.end()
        if self._future is not None and self._future.done():
            future, self._future = self._future, None
            try:
                opener = future.result()
            except Exception:
                opener = None
                self.node.get_logger().warning('patrol_conversation_memory_unavailable')
            if opener is not None and self._idle():
                self.opener = opener
                self.gate.used = True
                self._first_reply = True
                self.phase, self.deadline = 'speaking', now + 120
                self.request_id = 'patrol-chat-' + uuid4().hex
                self.node.dialogue.begin_companion(self.request_id)
                self.reply_id = ''
                self._playback_id = uuid4().hex
                if not self.node.say(opener.text, request_id=self.request_id,
                                     playback_id=self._playback_id):
                    self.end()
        if (not self.active and self._future is None and now >= self._retry_at
                and self._idle()):
            store = self.node.dialogue.patrol_memory
            if store is not None:
                self._future = self._pool.submit(PatrolConversationTopics(store).reserve)
            self._retry_at = now + 30

    def _stop_listening(self):
        if self.session_id:
            # Reserve closure even if an earlier start request has not arrived yet.
            self._input.call_async(self._session_type.Request(
                session_id=self.session_id, active=False))
        self.session_id = ''
        self._control_future = None

    def end(self):
        self.node.dialogue.end_companion(self.request_id)
        if self.phase == 'responding' and self.reply_id:
            self._ended_reply_ids.append(self.reply_id)
        self._stop_listening()
        if self._playback_id and self._stop.service_is_ready():
            self._stop.call_async(self._stop_type.Request(
                playback_id=self._playback_id, command=self._stop_type.Request.STOP))
        self._playback_id = ''
        self.phase = 'idle'
        self.opener = None
        self.gate.activity()

    def close(self):
        self._closed = True
        self.end()
        self.node.destroy_timer(self._timer)
        for subscription in self._subscriptions:
            self.node.destroy_subscription(subscription)
        self._pool.shutdown(wait=True, cancel_futures=True)
        self.node.destroy_client(self._input)
        self.node.destroy_client(self._stop)
