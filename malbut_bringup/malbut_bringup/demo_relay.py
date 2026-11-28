"""Relay admitted dialogue and actual playback only during an owner-viewer lease."""

from collections import OrderedDict
import json
import threading
import time
from uuid import UUID, uuid5, NAMESPACE_URL


class DemoRelay:
    """Keep demo HTTP, camera telemetry and speech off the robot command worker."""

    def __init__(self, node, client):
        """Create a lease publisher and a separate bounded upload worker."""
        from std_msgs.msg import String
        self.node = node
        self.client = client
        self.string = String
        self.lease_pub = node.create_publisher(String, '/malbut/demo/lease', 1)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.session = ''
        self.deadline = 0.0
        self.sub_session = ''
        self.subscriptions = []
        self.lines = OrderedDict()
        self.requests = OrderedDict()
        self.started = OrderedDict()
        self.pose = (0.0, None, False)
        self.timer = node.create_timer(0.25, self._subscriptions)
        self.worker = threading.Thread(target=self._upload, daemon=True, name='demo-feed')
        self.worker.start()

    def lease(self, data, requested_at):
        """Use the request time so network latency cannot extend the server lease."""
        session = ''
        deadline = 0.0
        if isinstance(data, dict) and type(data.get('ttlMs')) is int:
            try:
                session = str(UUID(data['session']))
                deadline = requested_at + min(20000, max(0, data['ttlMs'])) / 1000
            except (KeyError, ValueError, TypeError, AttributeError):
                session = ''
        if time.monotonic() >= deadline:
            session = ''
        with self.lock:
            if self.session != session or time.monotonic() >= self.deadline:
                self.lines.clear()
                self.pose = (0.0, None, False)
            changed = self.session != session
            self.session, self.deadline = session, deadline
        if changed or session:
            try:
                self.lease_pub.publish(self.string(data=json.dumps([session, deadline])))
            except RuntimeError:
                self.node.get_logger().debug('Demo lease publisher unavailable')

    def _subscriptions(self):
        with self.lock:
            session = self.session if time.monotonic() < self.deadline else ''
        if self.sub_session == session:
            return
        for sub in self.subscriptions:
            self.node.destroy_subscription(sub)
        self.subscriptions.clear()
        self.requests.clear()
        self.started.clear()
        self.sub_session = session
        if session:
            from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest
            for cls, topic, callback in (
                    (self.string, '/malbut/demo/user', self._user),
                    (self.string, '/malbut/demo/pose_status', self._pose),
                    (SpeechRequest, '/malbut/tts/request', self._request),
                    (SpeechPlaybackStatus, '/malbut/tts/playback_status', self._status)):
                def receive(msg, callback=callback, session=session):
                    with self.lock:
                        if self.session == session and time.monotonic() < self.deadline:
                            callback(msg)
                self.subscriptions.append(self.node.create_subscription(cls, topic, receive, 32))

    def _line(self, speaker, key, text, confirmation, at):
        if not isinstance(text, str) or not text.strip() or not key:
            return
        event = str(uuid5(NAMESPACE_URL, speaker + ':' + key))
        self.lines[event] = dict(id=event, speaker=speaker, text=text[:1800],
                                 confirmation=confirmation, at=at)
        if len(self.lines) > 100:
            self.lines.popitem(last=False)

    def _user(self, msg):
        try:
            data = json.loads(msg.data)
            self._line('user', data['id'], data['text'], data['confirmation'],
                       int(time.time() * 1000))
        except (ValueError, KeyError, TypeError):
            return

    def _pose(self, msg):
        try:
            count, suspected = json.loads(msg.data)
            if type(count) is int and type(suspected) is bool:
                self.pose = (time.monotonic(), min(100, max(0, count)), suspected)
        except (ValueError, TypeError):
            return

    def _request(self, msg):
        # The manager assigns playback_id before forwarding to the TTS topic.
        key = msg.playback_id
        if key:
            self.requests[key] = (msg.text, msg.request_type == 2)
            if len(self.requests) > 96:
                self.requests.popitem(last=False)
            self._match(key)

    def _status(self, msg):
        if msg.state == 'playing':
            self.started.setdefault(msg.playback_id, int(time.time() * 1000))
            if len(self.started) > 96:
                self.started.popitem(last=False)
            self._match(msg.playback_id)

    def _match(self, key):
        if key in self.started and key in self.requests:
            text, confirmation = self.requests.pop(key)
            self._line('robot', key, text, confirmation, self.started.pop(key))

    def _upload(self):
        while not self.stop.wait(0.65):
            now = time.monotonic()
            with self.lock:
                if not self.session or now >= self.deadline:
                    continue
                session = self.session
                at, count, suspect = self.pose
                # Four full-length Korean utterances fit the 64 KiB wire limit.
                lines = list(self.lines.values())[:4]
                payload = dict(session=session, lines=lines,
                               count=count if now-at < 2 else None,
                               suspect=suspect and now-at < 2)
            try:
                reply = self.client.request('/api/device/v1/robot/demo', 'POST', payload)
            except RuntimeError:
                continue
            if isinstance(reply, dict) and reply.get('accepted') is True:
                with self.lock:
                    if self.session == session:
                        for line in lines:
                            self.lines.pop(line['id'], None)

    def close(self):
        """Interrupt the dedicated network client before destroying ROS publishers."""
        self.stop.set()
        self.client.close()
        self.worker.join(timeout=5)
