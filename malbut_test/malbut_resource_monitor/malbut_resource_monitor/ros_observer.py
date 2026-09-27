"""Only subscribe. Never send a goal, call a service, or publish robot input."""

from hashlib import sha256
import threading
import time

from .store import slug


STATES = {1: 'ACCEPTED', 2: 'EXECUTING', 3: 'CANCELING', 4: 'SUCCEEDED',
          5: 'CANCELED', 6: 'ABORTED'}
ACTION_NAMES = (
    '/malbut/mission/execute', '/autoslam', '/follow_person', '/patrol',
    '/navigate_to_pose', '/assisted_teleop', '/relocalize',
    '/compute_path_to_pose', '/spin', '/backup', '/drive_on_heading',
)


def channel_name(kind, name):
    # Namespace slashes and underscores must not collapse different topics.
    return f'{kind}/{slug(name)}-{sha256(name.encode()).hexdigest()[:8]}'


class ActionEvents:
    def __init__(self, store):
        self.store = store
        self.states = {}

    def observe(self, endpoint, message):
        channel = channel_name('actions', endpoint)
        self.store.register(channel, kind='action', label=endpoint)
        for item in message.status_list:
            goal = bytes(item.goal_info.goal_id.uuid).hex()
            key = (endpoint, goal)
            state = STATES.get(item.status, 'UNKNOWN')
            previous = self.states.get(key)
            if previous == state:
                continue
            stamp = item.goal_info.stamp
            accepted_ns = stamp.sec * 1000000000 + stamp.nanosec
            self.store.write(channel, {
                'goal_id': goal, 'state': state, 'previous': previous,
                'accepted_ros_ns': accepted_ns,
                'first_observation': previous is None,
                'initial_terminal': previous is None and item.status in (4, 5, 6),
                'request_wall_ns': None, 'motion_start_wall_ns': None,
            })
            self.states[key] = state


class Observer:
    def __init__(self, store, topics):
        import rclpy
        from action_msgs.msg import GoalStatusArray
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

        self.store, self.rclpy = store, rclpy
        self.node = Node('malbut_resource_observer', enable_rosout=False,
                         start_parameter_services=False)
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.topics = set(topics)
        self.subscriptions, self.status_subscriptions = {}, {}
        self.counters, self.publishers = {}, {}
        self.lock = threading.Lock()
        self.actions = ActionEvents(store)
        self.status_type = GoalStatusArray
        self.status_qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.data_qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE)
        self.failed_types = set()
        self.previous_window = time.monotonic()
        self.missions = {}
        self.error = None
        self.stopping = False
        for endpoint in ACTION_NAMES:
            self._action(endpoint + '/_action/status')
        self._phases()
        self.discover()
        self.node.create_timer(2.0, self.discover)
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def _action(self, topic):
        if topic not in self.status_subscriptions:
            endpoint = topic.removesuffix('/_action/status')
            self.status_subscriptions[topic] = self.node.create_subscription(
                self.status_type, topic, lambda msg: self.actions.observe(endpoint, msg),
                self.status_qos)

    def _phases(self):
        from malbut_interfaces.msg import (
            SpeechInputStatus, SpeechPlaybackStatus, SpeechTranscript, SystemState,
        )
        self.phase_subscriptions = []
        specs = [
            (SpeechInputStatus, '/malbut/speech/input_status', 'speech_input',
             ('session_id', 'utterance_id', 'state')),
            (SpeechPlaybackStatus, '/malbut/speech/playback_status', 'tts_playback',
             ('playback_id', 'state')),
            (SpeechTranscript, '/malbut/speech/transcript', 'stt_transcript',
             ('session_id', 'utterance_id')),
        ]
        for message_type, topic, label, fields in specs:
            channel = channel_name('phases', label)
            self.store.register(channel, kind='phase', label=label)

            def callback(msg, fields=fields, channel=channel, label=label):
                self.store.write(channel, {
                    'phase': label, **{f: getattr(msg, f) for f in fields}})

            self.phase_subscriptions.append(self.node.create_subscription(
                message_type, topic, callback, self.data_qos))
        self.store.register('missions', kind='mission', label='manager mission observations')
        self.phase_subscriptions.append(self.node.create_subscription(
            SystemState, '/malbut/state', self._missions, self.data_qos))

    def _missions(self, msg):
        current = {}
        for field in ('active_foreground_missions', 'active_background_missions',
                      'suspended_missions', 'pending_missions'):
            for mission in getattr(msg, field):
                value = (mission.capability_id, mission.state)
                current[mission.mission_id] = value
                if self.missions.get(mission.mission_id) != value:
                    self.store.write('missions', {
                        'mission_id': mission.mission_id, 'capability': value[0],
                        'state': {0: 'PENDING', 1: 'RUNNING', 2: 'CANCELING',
                                  3: 'SUSPENDED'}.get(value[1], 'UNKNOWN')})
        for mission, (capability, _) in self.missions.items():
            if mission not in current:
                # Disappearance is NOT proof of success/cancel or an exact end time.
                self.store.write('missions', {'mission_id': mission, 'capability': capability,
                                              'state': 'NO_LONGER_LISTED'})
        self.missions = current

    def discover(self):
        from rosidl_runtime_py.utilities import get_message

        available = dict(self.node.get_topic_names_and_types())
        for topic, types in available.items():
            if topic.endswith('/_action/status') and 'action_msgs/msg/GoalStatusArray' in types:
                self._action(topic)
        for topic in self.topics:
            with self.lock:
                self.publishers[topic] = self.node.count_publishers(topic)
            if topic in self.subscriptions or topic not in available:
                continue
            types = available[topic]
            if len(types) != 1 or types[0] in self.failed_types:
                continue
            try:
                message_type = get_message(types[0])
            except (ImportError, AttributeError, ValueError) as error:
                self.failed_types.add(types[0])
                self.store.write('observer', {'error': f'Cannot observe {topic}: {error}'})
                continue
            with self.lock:
                self.counters[topic] = {
                    'count': 0, 'bytes': 0, 'total': 0, 'last': None,
                    'since': time.monotonic()}
            self.store.register(channel_name('topics', topic), kind='topic', label=topic,
                                ros_type=types[0], qos='best_effort/volatile/keep_last(1)')
            self.subscriptions[topic] = self.node.create_subscription(
                message_type, topic, lambda data, topic=topic: self._count(topic, data),
                self.data_qos, raw=True)

    def _count(self, topic, data):
        with self.lock:
            entry = self.counters[topic]
            entry['count'] += 1
            entry['total'] += 1
            entry['bytes'] += len(data)
            entry['last'] = time.monotonic()

    def sample(self):
        now = time.monotonic()
        with self.lock:
            snapshots = []
            for topic in sorted(self.topics):
                entry = self.counters.get(topic)
                window = now - max(self.previous_window, entry['since']) if entry else None
                snapshots.append((topic, {
                    'received_hz': entry['count'] / window if window else None,
                    'received_bytes_per_s': entry['bytes'] / window if window else None,
                    'received_total': entry['total'] if entry else 0,
                    'last_message_age_s': now - entry['last'] if entry and entry['last'] else None,
                    'window_s': window, 'subscribed': entry is not None,
                    'publishers': self.publishers.get(topic, 0),
                }))
                if entry:
                    entry['count'] = entry['bytes'] = 0
            self.previous_window = now
        for topic, record in snapshots:
            channel = channel_name('topics', topic)
            self.store.register(channel, kind='topic', label=topic)
            self.store.write(channel, record)

    def _spin(self):
        try:
            self.executor.spin()
        except Exception as error:
            if not self.stopping:
                self.error = f'{type(error).__name__}: {error}'

    def close(self):
        self.stopping = True
        self.executor.shutdown(timeout_sec=3)
        self.thread.join(timeout=3)
        self.node.destroy_node()
