"""Isolated, disposable ROS graph for terminal voice-command experiments.

The real Agent and Manager communicate with fake, namespaced actuators. No
hardware, microphone or production map is opened. Dialogue defaults to a mock;
explicit settings can select a live provider while storage remains disposable.
Start, spin, send and close on the constructing thread; actuator output is
queued back to that thread. Each instance owns the default ROS context exclusively.
"""

from copy import deepcopy
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from tempfile import TemporaryDirectory
from threading import Event, RLock, Thread, get_ident
import time
from uuid import uuid4

import yaml


_CAPABILITIES = ('follow_person', 'patrol', 'navigate_to_pose')
_REMAPS = (
    '/malbut/mission/execute', '/malbut/state', '/malbut/localization/state',
    '/malbut/speech/transcript', '/malbut/speech/response',
    '/malbut/speech/input_status', '/malbut/speech/playback_status',
    '/malbut/speech/classify_addressee', '/malbut/speech/control_session',
    '/malbut/speech/control_playback', '/malbut/agent/confirm_situation',
)


@dataclass(frozen=True)
class _Behavior:
    outcome: str = 'success'
    delay_s: float = 0.3
    cancel_delay_s: float = 0.0


class VoiceLabGraph:
    """Run production orchestration against fake ROS application servers."""

    def __init__(self, output, domain_id=197, dialogue_settings=None):
        """Validate explicit dialogue settings without opening ROS or user storage."""
        from malbut_agent_server.config import Settings
        from malbut_agent_server.providers.mock import MockProvider

        if not callable(output):
            raise TypeError('output must be callable')
        if type(domain_id) is not int or not 0 <= domain_id <= 232:
            raise ValueError('domain_id must be an integer from 0 to 232')
        if dialogue_settings is not None and not isinstance(dialogue_settings, Settings):
            raise TypeError('dialogue_settings must be Settings or None')
        self.output = output
        self.domain_id = domain_id
        self.namespace = '/voice_lab_' + uuid4().hex[:12]
        self.dialogue_settings = replace(
            dialogue_settings if dialogue_settings is not None else Settings(provider='mock'),
            database_path=':memory:', user_id=self.namespace.lstrip('/'),
        )
        self.dialogue_settings.validate_for_dialogue()
        self.provider = self.dialogue_settings.provider
        self.model = (
            MockProvider.model if self.provider == 'mock'
            else self.dialogue_settings.rai_model if self.provider == 'rai-sidecar'
            else self.dialogue_settings.openai_model
        )
        self.reply_timeout_s = (
            8.0 if self.provider == 'mock' else max(
                8.0, float(self.dialogue_settings.provider_total_timeout_seconds) + 10,
                (float(self.dialogue_settings.rai_sidecar_timeout_seconds) + 10
                 if self.provider == 'rai-sidecar' else 0),
            )
        )
        self.events = []
        self.replies = []
        self.speech = []
        self.goals = []
        self.directory = None
        self.user_map_paths = {}
        self.map_paths = {}
        self.agent = None
        self.manager = None
        self._sender = None
        self._actuators = None
        self._executor = None
        self._background = None
        self._thread = None
        self._rclpy = None
        self._owns_context = False
        self._started = False
        self._closed = False
        self._owner = get_ident()
        self._lock = RLock()
        self._output = SimpleQueue()
        self._environment = {}
        self._temporary = None
        self._behaviors = {}
        self.manager_enabled = False
        self.navigation_enabled = False
        self.reset_behaviors()

    def _require_owner(self):
        if get_ident() != self._owner:
            raise RuntimeError('VoiceLabGraph methods require its owning thread')

    def _emit(self, event, **values):
        self._output.put({'event': event, **deepcopy(values),
                          'observed_at': time.monotonic()})

    def _drain_output(self):
        while True:
            try:
                self.output(self._output.get_nowait())
            except Empty:
                return

    def start(self, *, manager_enabled=True, navigation_enabled=True):
        """Create temporary inputs and start only the isolated laboratory graph."""
        self._require_owner()
        if self._started or self._closed:
            raise RuntimeError('this graph cannot be started again')
        if type(manager_enabled) is not bool or type(navigation_enabled) is not bool:
            raise TypeError('startup options must be booleans')
        import rclpy
        from rclpy.executors import MultiThreadedExecutor, SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from rclpy.signals import SignalHandlerOptions
        from malbut_interfaces.msg import SpeechRequest, SpeechTranscript
        from std_msgs.msg import String
        from malbut_agent_server.factory import build_orchestrator
        from malbut_agent_server.ros_communication import create_communication_node
        from malbut_system_manager.system_manager_node import SystemManagerNode

        if rclpy.ok():
            raise RuntimeError('VoiceLabGraph requires its own ROS context/process')
        self._rclpy = rclpy
        self.manager_enabled = manager_enabled
        self.navigation_enabled = navigation_enabled
        try:
            for key, value in (('ROS_DOMAIN_ID', str(self.domain_id)),
                               ('ROS_LOCALHOST_ONLY', '1')):
                self._environment[key] = os.environ.get(key)
                os.environ[key] = value
            self._temporary = TemporaryDirectory(prefix='malbut-voice-lab-')
            self.directory = Path(self._temporary.name)
            self.dialogue_settings = replace(
                self.dialogue_settings,
                database_path=str(self.directory / 'dialogue.sqlite3'),
            )
            self._write_inputs()
            args = ['--ros-args', '--log-level', 'warn', '-r', '__ns:=' + self.namespace]
            for endpoint in _REMAPS:
                args += ['-r', endpoint + ':=' + self.namespace + endpoint.removeprefix('/malbut')]
            # Humble rcl_action expands the base name without applying a plain
            # name remap. Remap its actual service/topic entities as well.
            for action in ('/malbut/mission/execute', '/malbut/agent/confirm_situation'):
                for suffix in ('send_goal', 'get_result', 'cancel_goal', 'feedback', 'status'):
                    endpoint = action + '/_action/' + suffix
                    target = self.namespace + endpoint.removeprefix('/malbut')
                    args += ['-r', endpoint + ':=' + target]
            # Python's KeyboardInterrupt must enter close() while ROS is still
            # alive; the default ROS SIGINT handler shuts the context too early.
            rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
            self._owns_context = True
            self._executor = SingleThreadedExecutor()
            self._background = MultiThreadedExecutor(num_threads=6)
            self._actuators = _create_actuators(self)
            self._background.add_node(self._actuators)
            if manager_enabled:
                self.manager = SystemManagerNode(
                    manifest_directory=str(self.directory / 'manifests'))
                self._background.add_node(self.manager)
            self._thread = Thread(target=self._background.spin,
                                  name='voice-lab-ros', daemon=True)
            self._thread.start()
            self._sender = Node('voice_lab_input')
            self._executor.add_node(self._sender)
            self._transcript_type = SpeechTranscript
            self._state_type = String
            self._transcripts = self._sender.create_publisher(
                SpeechTranscript, '/malbut/speech/transcript', 10)
            self._sender.create_subscription(
                SpeechRequest, '/malbut/speech/response', self._speech_event, 10)
            qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
            self._localization = self._sender.create_publisher(
                String, '/malbut/localization/state', qos)
            settings = self.dialogue_settings

            def runtime_factory():
                return build_orchestrator(settings, http_server=False)

            self.agent = create_communication_node(
                speech_db_path=str(self.directory / 'receipts.sqlite3'),
                dialogue_settings=settings, dialogue_factory=runtime_factory,
                enable_manager_commands=True,
                on_event=self._mission_event,
            )
            publish_reply = self.agent.dialogue.publish_reply

            def record_reply(response, publish):
                result = publish_reply(response, publish)
                if result is not None:
                    value = deepcopy(dict(result))
                    self.replies.append(value)
                    self._emit('reply', **value)
                return result

            self.agent.dialogue.publish_reply = record_reply
            self._executor.add_node(self.agent)
            self._started = True
            self._wait(lambda: self._transcripts.get_subscription_count() == 1,
                       'Agent speech input did not become ready',
                       timeout=max(self.reply_timeout_s,
                                   8.0 if self.provider == 'mock' else 30.0))
            if manager_enabled:
                self._wait(self.agent.missions._client.server_is_ready,
                           'isolated Manager did not become ready')
            self._wait_for_actuators()
            if navigation_enabled:
                self.set_map()
            self._emit('system', kind='ready', namespace=self.namespace,
                       domain_id=self.domain_id, provider=self.provider, model=self.model,
                       manager_enabled=manager_enabled,
                       navigation_enabled=navigation_enabled)
            self._drain_output()
        except BaseException:
            self.close()
            raise

    def _write_inputs(self):
        source = Path(__file__).resolve().parents[2] / 'malbut_interfaces/capabilities'
        if not source.is_dir():
            from ament_index_python.packages import get_package_share_directory
            source = Path(get_package_share_directory('malbut_interfaces')) / 'capabilities'
        manifests = self.directory / 'manifests'
        manifests.mkdir()
        for name in _CAPABILITIES:
            document = yaml.safe_load((source / (name + '.yaml')).read_text())
            document['command']['name'] = self.namespace + '/actuators/' + name
            (manifests / (name + '.yaml')).write_text(yaml.safe_dump(document))
        for name in ('home', 'other'):
            image = self.directory / (name + '.pgm')
            image.write_bytes(b'P5\n100 100\n255\n' + b'\xff' * 10_000)
            path = self.directory / (name + '.yaml')
            path.write_text(yaml.safe_dump({
                'image': image.name, 'resolution': 0.1, 'origin': [0.0, 0.0, 0.0],
                'negate': 0, 'occupied_thresh': 0.65, 'free_thresh': 0.196,
            }))
            self.map_paths[name] = str(path)
        from malbut_agent_server.speech_navigation import NavigationTargets

        for name, selected in self.map_paths.items():
            path = Path(selected)
            self.user_map_paths[name] = path.with_suffix('.user-map.geojson')
            features = ([{
                'type': 'Feature',
                'properties': {'role': 'room', 'name': label, 'representative_point': point},
            } for label, point in (('거실', [1.0, 1.0]), ('주방', [2.0, 1.0]),
                                   ('현관', [1.0, 2.0]))] if name == 'home' else [])
            self.user_map_paths[name].write_text(json.dumps({
                'type': 'FeatureCollection', 'format': 'malbut-user-map-v1',
                'map_id': 'test-' + name, 'frame_id': 'map',
                'map_revision': NavigationTargets._map_binding(path)[2],
                'features': features,
            }, ensure_ascii=False))

    def _wait_for_actuators(self):
        from rclpy.action import ActionClient
        for capability, action_type in self._actuators.action_types.items():
            probe = ActionClient(self._sender, action_type,
                                 self.namespace + '/actuators/' + capability)
            try:
                self._wait(probe.server_is_ready, 'fake actuator discovery failed')
            finally:
                probe.destroy()

    def _mission_event(self, event):
        self.events.append(deepcopy(event))
        self._emit('mission', **event)

    def _speech_event(self, message):
        value = {name: getattr(message, name) for name in (
            'text', 'request_type', 'interim', 'playback_id')}
        self.speech.append(value)
        self._emit('speech', **value)

    def _wait(self, predicate, message, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.spin()
        raise TimeoutError(message)

    def spin(self, timeout=0.02):
        """Pump the Agent owner executor and deliver queued output on this thread."""
        self._require_owner()
        if not self._started or self._closed:
            raise RuntimeError('graph is not running')
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 <= timeout <= 1):
            raise ValueError('spin timeout must be 0..1 seconds')
        self._executor.spin_once(timeout_sec=float(timeout))
        self._drain_output()

    def send(self, text, utterance_id=None):
        """Publish terminal input as a final STT transcript; no microphone is used."""
        self._require_owner()
        if not self._started or self._closed:
            raise RuntimeError('graph is not running')
        from malbut_agent_server.speech_dialogue import validate_dialogue_input
        uid = str(uuid4()) if utterance_id is None else utterance_id
        validate_dialogue_input(uid, text)
        self._transcripts.publish(self._transcript_type(utterance_id=uid, text=text))
        self._emit('system', kind='transcript', utterance_id=uid, text=text)
        return uid

    def wait_reply(self, uid, timeout=None):
        """Wait for one final published dialogue reply, including blocked requests."""
        self._require_owner()
        if timeout is None:
            timeout = self.reply_timeout_s
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('reply timeout must be positive and finite')
        self._wait(lambda: any(reply.get('utterance_id') == uid
                               and reply.get('kind') != 'progress' for reply in self.replies),
                   'no final dialogue reply for ' + str(uid), timeout)
        self._drain_output()
        return deepcopy(next(
            reply for reply in self.replies
            if reply.get('utterance_id') == uid and reply.get('kind') != 'progress'))

    def status(self):
        """Return local laboratory evidence without polling a production graph."""
        self._require_owner()
        latest = {event['request_id']: event for event in self.events}
        with self._lock:
            return deepcopy({
                'running': self._started and not self._closed,
                'namespace': self.namespace, 'domain_id': self.domain_id,
                'provider': self.provider, 'model': self.model,
                'manager_enabled': self.manager_enabled,
                'navigation_enabled': self.navigation_enabled,
                'missions': list(latest.values()), 'goals': self.goals,
                'replies': len(self.replies),
            })

    def set_map(self, mode='LOCALIZATION', variant='home'):
        """Publish a synthetic selected-map state and wait for Agent observation."""
        self._require_owner()
        if not self._started or self._closed:
            raise RuntimeError('graph is not running')
        if mode not in {'LOCALIZATION', 'MAPPING', 'SWITCHING', 'ERROR'}:
            raise ValueError('unknown localization mode')
        if variant not in {'home', 'other', 'none'}:
            raise ValueError('map variant must be home, other or none')
        path = None if variant == 'none' else self.map_paths[variant]
        payload = {'mode': mode, 'map': path, 'message': 'synthetic laboratory map'}
        self._localization.publish(self._state_type(data=json.dumps(payload)))
        self._wait(lambda: self.agent.speech_missions._localization_identity == (mode, path),
                   'Agent did not observe synthetic map state')
        self._emit('system', kind='map', **payload)
        return payload

    def set_behavior(self, capability, outcome='success', delay_s=0.3, cancel_delay_s=0.0):
        """Configure the next fake Goal; existing Goals keep their captured behavior."""
        self._require_owner()
        if (capability not in _CAPABILITIES
                or outcome not in {'success', 'hold', 'abort', 'reject'}):
            raise ValueError('unsupported fake capability or outcome')
        for value in (delay_s, cancel_delay_s):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 60:
                raise ValueError('fake delay must be 0..60 seconds')
        with self._lock:
            self._behaviors[capability] = _Behavior(outcome, float(delay_s), float(cancel_delay_s))

    def reset_behaviors(self):
        """Use continuous follow and successful finite navigation/patrol by default."""
        self._require_owner()
        for capability in _CAPABILITIES:
            self.set_behavior(capability, 'hold' if capability == 'follow_person' else 'success')

    def close(self):
        """Cancel fake work, finish callbacks, close owned ROS and remove temporary data."""
        self._require_owner()
        if self._closed:
            return
        try:
            if self._actuators is not None:
                self._actuators.stopping.set()
            if self.manager is not None:
                self.manager.begin_shutdown()
                deadline = time.monotonic() + 3
                while self.manager.public_context_count and time.monotonic() < deadline:
                    if self._executor is not None:
                        self._executor.spin_once(timeout_sec=0.02)
                if self.manager.public_context_count:
                    self.manager.force_shutdown()
            if self.agent is not None:
                self._executor.remove_node(self.agent)
                self.agent.destroy_node()
            if self._background is not None:
                self._background.shutdown(timeout_sec=3.0)
            if self._thread is not None:
                self._thread.join(timeout=3.0)
            for node in (self.manager, self._actuators):
                if node is not None:
                    node.destroy_node()
            if self._sender is not None:
                self._executor.remove_node(self._sender)
                self._sender.destroy_node()
            if self._executor is not None:
                self._executor.shutdown(timeout_sec=2.0)
        finally:
            if self._owns_context and self._rclpy.ok():
                self._rclpy.shutdown()
            self._owns_context = False
            self._closed = True
            if self._temporary is not None:
                self._temporary.cleanup()
            for key, previous in self._environment.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous
            self._emit('system', kind='closed')
            self._drain_output()


def _create_actuators(lab):
    from malbut_interfaces.action import FollowPerson, Patrol
    from nav2_msgs.action import NavigateToPose
    from rclpy.action import ActionServer, CancelResponse, GoalResponse
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.node import Node
    from rosidl_runtime_py.convert import message_to_ordereddict

    class Actuators(Node):
        action_types = {'follow_person': FollowPerson, 'patrol': Patrol,
                        'navigate_to_pose': NavigateToPose}

        def __init__(self):
            super().__init__('voice_lab_actuators')
            self.stopping = Event()
            self._pending = {}
            self._servers = []
            for name, action_type in self.action_types.items():
                self._servers.append(ActionServer(
                    self, action_type, lab.namespace + '/actuators/' + name,
                    goal_callback=lambda request, name=name: self.goal(name, request),
                    execute_callback=lambda handle, name=name: self.execute(name, handle),
                    cancel_callback=lambda _handle: CancelResponse.ACCEPT,
                    callback_group=ReentrantCallbackGroup(),
                ))

        def record(self, capability, request, behavior, *, accepted, goal_id=None):
            value = {'capability_id': capability,
                     'arguments': dict(message_to_ordereddict(request)),
                     'accepted': accepted, 'goal_id': goal_id,
                     'outcome': behavior.outcome, 'received_at': time.monotonic()}
            with lab._lock:
                lab.goals.append(deepcopy(value))
            lab._emit('goal', **value)

        def goal(self, capability, request):
            with lab._lock:
                behavior = lab._behaviors[capability]
                if not self.stopping.is_set() and behavior.outcome != 'reject':
                    self._pending[id(request)] = behavior
                    return GoalResponse.ACCEPT
            self.record(capability, request, behavior, accepted=False)
            return GoalResponse.REJECT

        def execute(self, capability, handle):
            with lab._lock:
                behavior = self._pending.pop(id(handle.request), lab._behaviors[capability])
            goal_id = bytes(handle.goal_id.uuid).hex()
            self.record(capability, handle.request, behavior, accepted=True, goal_id=goal_id)
            started, canceled_at, last_feedback = time.monotonic(), None, 0.0
            while True:
                now = time.monotonic()
                if handle.is_cancel_requested:
                    canceled_at = now if canceled_at is None else canceled_at
                    if self.stopping.is_set() or now - canceled_at >= behavior.cancel_delay_s:
                        outcome = 'canceled'
                        break
                elif self.stopping.is_set():
                    outcome = 'aborted'
                    break
                elif behavior.outcome != 'hold' and now - started >= behavior.delay_s:
                    outcome = 'succeeded' if behavior.outcome == 'success' else 'aborted'
                    break
                if now - last_feedback >= 0.1:
                    if capability == 'follow_person':
                        feedback = FollowPerson.Feedback(state='TRACKING', target_visible=True)
                    elif capability == 'patrol':
                        feedback = Patrol.Feedback(state='OBSERVING', coverage_ratio=0.5,
                                                   viewpoints_visited=1)
                    else:
                        feedback = NavigateToPose.Feedback(distance_remaining=1.0)
                    handle.publish_feedback(feedback)
                    last_feedback = now
                self.stopping.wait(0.01)
            lab._emit('system', kind='actuator_terminal', capability_id=capability,
                      goal_id=goal_id, status=outcome)
            if outcome == 'canceled':
                handle.canceled()
            elif outcome == 'succeeded':
                handle.succeed()
            else:
                handle.abort()
            if capability == 'follow_person':
                return FollowPerson.Result(success=outcome == 'succeeded', final_state='STOPPED',
                                           message='voice lab follow ' + outcome)
            if capability == 'patrol':
                return Patrol.Result(success=outcome == 'succeeded',
                                     message='voice lab patrol ' + outcome,
                                     coverage_ratio=1.0 if outcome == 'succeeded' else 0.5,
                                     viewpoints_visited=1)
            return NavigateToPose.Result()

        def destroy_node(self):
            for server in self._servers:
                server.destroy()
            return super().destroy_node()

    return Actuators()
