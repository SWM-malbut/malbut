"""
Bringup-owned map sources and fixed, mission-based Nav2 usage.

No subscriber counting, process respawn, or mission arbitration lives here.
The system manager reports active missions and waits for a generic preparation
service; each lifecycle group keeps its existing Nav2 implementation.
"""

import json
from functools import partial
import math
from pathlib import Path
import signal
import threading
import time

from geometry_msgs.msg import PoseWithCovarianceStamped, TransformStamped
from lifecycle_msgs.msg import State, Transition
from lifecycle_msgs.srv import ChangeState, GetState
from malbut_interfaces.action import Relocalize
from malbut_interfaces.msg import SystemState
from nav2_msgs.srv import LoadMap, ManageLifecycleNodes, SetInitialPose
import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.action import ActionClient
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.signals import SignalHandlerOptions
from rclpy.task import Future
from std_msgs.msg import String
from std_srvs.srv import SetBool, Trigger
from rclpy.time import Time
from tf2_ros import Buffer, TransformListener
from .nav2_stack import MOTION_NODES, PLANNING_NODES
from .map_selection import remember_map


LOAD_MAP_SERVICE = '/malbut/localization/load_map'
START_MAPPING_SERVICE = '/malbut/localization/start_mapping'
STOP_MAPPING_SERVICE = '/malbut/localization/stop_mapping'
READY_MAPPING_SERVICE = '/malbut/localization/mapping_ready'
STATE_TOPIC = '/malbut/localization/state'
PREPARE_PREFIX = '/malbut/runtime/prepare/'

# Depend on the function, not its BASE arbitration lock. In particular,
# fall_confirmation and speech do not require a running navigation stack.
PROFILES = {
    'manual_drive': ('motion',),
    'follow_person': ('motion', 'planning', 'person_perception'),
    'fall_approach': ('motion', 'planning'),
    'navigate_to_pose': ('motion', 'planning'),
    'compute_path_to_pose': ('planning',),
    'patrol': ('motion', 'planning', 'patrol_inputs'),
    'relocalize': ('motion',),
    'autoslam': (),  # No costmap consumers before SLAM supplies map/TF.
    'recovery': (),  # Yield transitions to the existing manual recovery Action.
}
GROUPS = {
    'motion': '/lifecycle_manager_navigation/manage_nodes',
    'planning': '/lifecycle_manager_global_navigation/manage_nodes',
}
PROCESSING_GROUPS = {
    # Wake receivers first, detector last; stop in reverse order.
    'person_perception': (
        '/person_localizer/set_processing', '/person_reidentifier/set_processing',
        '/lidar_foreground_preprocessor/set_processing', '/yolo/yolo_node/set_processing'),
    'patrol_inputs': ('/patrol_manager/set_processing',),
}


def required_groups(capabilities, mapping_ready=False):
    """Return the union of fixed dependencies, including canceling missions."""
    needed = {group for capability in capabilities for group in PROFILES.get(capability, ())}
    # start/ready/stop also own an explicitly requested /autoslam Action.
    # Retain its dependencies through cleanup, even if manager demand ended.
    if mapping_ready:
        needed.update(('motion', 'planning'))
    return needed


class ProcessingRejected(RuntimeError):
    """A processing server explicitly refused the requested state."""


class RuntimeControl(Node):
    """Prepare dependencies before dispatch; pause them after confirmed completion."""

    def __init__(self, **kwargs):
        super().__init__('runtime_control', **kwargs)
        self.group = ReentrantCallbackGroup()
        # Serialize operator RPCs without parking many executor threads on the
        # same lock. State/TF/client replies and the initialization tick stay
        # independent, so they can complete a preparation request.
        self.control_group = MutuallyExclusiveCallbackGroup()
        self.timeout = float(self.declare_parameter('transition_timeout_s', 30.0).value)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('transition_timeout_s must be positive and finite')
        self.default_map = self.declare_parameter('default_map', '').value
        self.map_path = self.declare_parameter('initial_map', self.default_map).value
        self.map_selection_file = self.declare_parameter('map_selection_file', '').value
        relocalize = self.declare_parameter('relocalize_action', '').value
        self._relocalize = (ActionClient(self, Relocalize, relocalize,
                                         callback_group=self.group) if relocalize else None)
        self._lock = threading.Lock()
        self._data_lock = threading.Lock()
        self._report_lock = threading.Lock()
        self._pending_reports = {}
        self._capabilities = set()
        self._have_state = False
        self._synced_demand = None
        self._sync_retry_at = None
        self._verify_requested = False
        self._ready_groups = set()
        self._group_errors = {}
        self._preparations = []
        self._closing = False
        self._initialized = False
        self._mapping = False
        self._mapping_ready = False
        self._localization_started = False
        self._localization_active = False
        self._recovery_pending = False
        self._configured = set()
        self._map_initialized = False
        self._uncertain = False
        self._pending_command = None
        self._pending_restore = None
        self._active = {name: False for name in GROUPS}
        self._processing_active = {
            service: False for services in PROCESSING_GROUPS.values() for service in services}
        self._processing_clients = {
            service: self.create_client(SetBool, service, callback_group=self.group)
            for service in self._processing_active}
        self._processing_pending = {}
        self._pose = None
        self._pose_sequence = 0
        self._cached_map_odom = None
        self._cached_pose_sequence = -1
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._status = self.create_publisher(String, STATE_TOPIC, qos)
        self._usage = self.create_publisher(String, '/malbut/runtime/state', qos)
        self._demand_changed = self.create_guard_condition(
            self._on_demand_changed, callback_group=self.control_group)
        self.create_subscription(SystemState, '/malbut/state', self._state, qos,
                                 callback_group=self.group)
        self.create_subscription(PoseWithCovarianceStamped, '/amcl_pose', self._receive_pose,
                                 qos, callback_group=self.group)
        self.tf = Buffer()
        self.listener = TransformListener(self.tf, self)
        self._managers = {
            name: self.create_client(ManageLifecycleNodes, service, callback_group=self.group)
            for name, service in GROUPS.items()
        }
        self._localization = self.create_client(
            ManageLifecycleNodes, '/lifecycle_manager_localization/manage_nodes',
            callback_group=self.group)
        self._manager_states = {
            name: self.create_client(Trigger, service.replace('/manage_nodes', '/is_active'),
                                     callback_group=self.group)
            for name, service in {
                **GROUPS, 'localization': '/lifecycle_manager_localization/manage_nodes'
            }.items()
        }
        self._map_server = self.create_client(
            LoadMap, '/map_server/load_map', callback_group=self.group)
        self._initial_pose = self.create_client(
            SetInitialPose, '/set_initial_pose', callback_group=self.group)
        self._node_states = {
            name: (self.create_client(GetState, f'/{name}/get_state', callback_group=self.group),
                   self.create_client(ChangeState, f'/{name}/change_state',
                                      callback_group=self.group))
            for name in (*MOTION_NODES, *PLANNING_NODES)
        }
        for capability in PROFILES:
            self.create_service(
                Trigger, PREPARE_PREFIX + capability,
                partial(self._prepare, capability), callback_group=self.group)
        self.create_service(LoadMap, LOAD_MAP_SERVICE, self._load_map,
                            callback_group=self.control_group)
        self.create_service(Trigger, START_MAPPING_SERVICE, self._start_mapping,
                            callback_group=self.control_group)
        self.create_service(Trigger, READY_MAPPING_SERVICE, self._ready_mapping,
                            callback_group=self.control_group)
        self.create_service(Trigger, STOP_MAPPING_SERVICE, self._stop_mapping,
                            callback_group=self.control_group)
        self._publish_map(False, 'starting map provider')
        self._report_usage('initializing dependencies')
        self.create_timer(0.5, self._tick, callback_group=self.group)

    def _state(self, message):
        capabilities = {
            mission.capability_id for mission in (
                *message.active_foreground_missions, *message.active_background_missions)
        }
        with self._data_lock:
            if self._have_state and capabilities == self._capabilities:
                return
            self._capabilities = capabilities
            self._have_state = True
            if 'recovery' in self._capabilities:
                self._recovery_pending = True
        self._demand_changed.trigger()

    def _on_demand_changed(self):
        # State reception stays non-blocking. Serialize lifecycle work with
        # map RPCs, and reconcile the latest (possibly coalesced) list.
        with self._lock:
            if self._closing or not self._initialized:
                return
            self._settle_command()
            self._reconcile_demand()

    def _reconcile_demand(self):
        """Retry a confirmed processing refusal, never an unconfirmed RPC."""
        self._sync_retry_at = None
        try:
            self._sync()
        except Exception as error:
            if isinstance(error, ProcessingRejected) and not self._uncertain:
                # Action completion and processing cleanup can overlap. State
                # demand need not change again once the server releases busy.
                self._sync_retry_at = time.monotonic() + 1.0
            self.get_logger().error(str(error))
            self._report_usage(str(error))
            self._resolve_preparations(str(error))
        else:
            self._resolve_preparations()

    def _receive_pose(self, message):
        with self._data_lock:
            self._pose = message
            self._pose_sequence += 1
        self._refresh_map_odom()

    def _refresh_map_odom(self):
        with self._data_lock:
            pose, sequence = self._pose, self._pose_sequence
        if pose is None or sequence == self._cached_pose_sequence:
            return
        try:
            transform = self._map_odom_from_pose(pose)
        except Exception:
            # Odom TF can arrive just after the pose; the next tick retries.
            return
        with self._data_lock:
            if sequence == self._pose_sequence:
                self._cached_map_odom = transform
                self._cached_pose_sequence = sequence

    def _capabilities_now(self):
        with self._data_lock:
            return self._capabilities.copy()

    def _call(self, client, request, label, *, read_only=False):
        deadline = time.monotonic() + self.timeout
        while not client.service_is_ready():
            if self._closing or time.monotonic() >= deadline:
                raise RuntimeError(f'{label}: service unavailable')
            time.sleep(0.02)
        try:
            future = client.call_async(request)
        except Exception:
            if not read_only:
                self._uncertain = True
            raise
        try:
            return self._wait(future, label, self.timeout)
        except Exception:
            if read_only:
                client.remove_pending_request(future)
                future.cancel()
            else:
                # Keep the original reply: timeout is not cancellation. Do
                # not resend a mutation just because the machine is slow.
                self._pending_command = (client, request, future)
                self._uncertain = True
                self._report_usage(f'{label}: response unconfirmed')
            raise

    def _settle_command(self):
        if self._pending_command is None:
            return False
        client, request, future = self._pending_command
        if not future.done() or future.cancelled():
            return False
        try:
            reply = future.result()
        except Exception:
            return False  # Transport completion is not a remote acknowledgement.
        if reply is None:
            return False
        self._pending_command = None
        if hasattr(reply, 'success') and not reply.success:
            return False  # A rejected command can leave a partially changed group.
        if client is self._localization:
            command = request.command
            self._localization_active = command in (
                ManageLifecycleNodes.Request.STARTUP, ManageLifecycleNodes.Request.RESUME)
            self._localization_started = command != ManageLifecycleNodes.Request.RESET
        self._uncertain = False
        self._synced_demand = None
        return True

    def _wait(self, future, label, timeout):
        deadline = time.monotonic() + timeout
        while not future.done():
            if self._closing or time.monotonic() >= deadline:
                raise RuntimeError(f'{label}: response unconfirmed')
            time.sleep(0.02)
        return future.result()

    def _manage(self, client, command):
        request = ManageLifecycleNodes.Request(command=command)
        if not self._call(client, request, 'Nav2 lifecycle').success:
            self._uncertain = True
            self._report_usage(f'Nav2 lifecycle rejected command {command}')
            raise RuntimeError(f'Nav2 lifecycle rejected command {command}')

    def _map_provider(self, path, pose=None):
        self._map_initialized = False
        self._synced_demand = None
        self._publish_map(False, 'preparing map provider')
        request = LoadMap.Request(map_url=path)
        # The configured map_server is activated together with AMCL, as before.
        if not self._localization_started:
            self._manage(self._localization, ManageLifecycleNodes.Request.STARTUP)
            self._localization_started = True
            self._localization_active = True
        elif not self._localization_active:
            self._manage(self._localization, ManageLifecycleNodes.Request.RESUME)
            self._localization_active = True
        reply = self._call(self._map_server, request, 'load map')
        if reply.result != LoadMap.Response.RESULT_SUCCESS:
            raise RuntimeError(f'map_server rejected {path}: {reply.result}')
        self.map_path = path
        self._publish_map(False, 'map loaded; preparing robot pose')
        if pose is not None or path == self.default_map:
            before = self._pose_sequence
            request = SetInitialPose.Request()
            if pose is not None:
                request.pose = pose
            else:
                request.pose.header.frame_id = 'map'
                request.pose.pose.pose.orientation.w = 1.0
            request.pose.header.stamp = self.get_clock().now().to_msg()
            self._call(self._initial_pose, request, 'AMCL initial pose')
            # Map-source replacement needs a real estimate, not just the
            # service acknowledgement. Normal idle never pauses AMCL.
            self._wait_amcl_pose(before)
        elif self._relocalize is not None:
            deadline = time.monotonic() + self.timeout
            while not self._relocalize.server_is_ready():
                if self._closing or time.monotonic() >= deadline:
                    raise RuntimeError('pose restore: Action server unavailable')
                time.sleep(0.02)
            # Preserve the existing optional saved-pose restore, but keep it
            # out of the manager. Only its local Spin dependencies wake.
            if not self._active['motion']:
                self._manage(self._managers['motion'], ManageLifecycleNodes.Request.RESUME)
                self._active['motion'] = True
            goal = Relocalize.Goal(method=Relocalize.Goal.AUTO)
            pending = self._pending_restore = {
                'path': path, 'response': None, 'handle': None, 'result': None,
                'cancel_sent': False,
            }
            try:
                pending['response'] = self._relocalize.send_goal_async(goal)
                handle = self._wait(pending['response'], 'pose restore', self.timeout)
                pending['handle'] = handle
                if handle.accepted:
                    finished = pending['result'] = handle.get_result_async()
                    try:
                        result = self._wait(finished, 'pose restore', 90.0).result
                        if not result.success:
                            self.get_logger().warning(result.message)
                    except RuntimeError:
                        handle.cancel_goal_async()
                        pending['cancel_sent'] = True
                        # Cancellation acknowledgement is not completion.
                        self._wait(finished, 'pose restore cancellation', self.timeout)
            except Exception:
                self._uncertain = True
                raise
            self._pending_restore = None
        self._finish_map_provider(path)

    def _settle_restore(self):
        pending = self._pending_restore
        if pending is None or pending['response'] is None:
            return False
        try:
            if pending['handle'] is None:
                if not pending['response'].done():
                    return False
                pending['handle'] = pending['response'].result()
            handle = pending['handle']
            if handle.accepted:
                if pending['result'] is None:
                    pending['result'] = handle.get_result_async()
                if not pending['cancel_sent'] and not pending['result'].done():
                    handle.cancel_goal_async()
                    pending['cancel_sent'] = True
                if not pending['result'].done():
                    return False
                # A terminal result, not just a CancelGoal response, proves
                # that the old Spin can no longer overlap the next mission.
                pending['result'].result()
        except Exception:
            return False
        self._pending_restore = None
        self._uncertain = False
        self._finish_map_provider(pending['path'])
        return True

    def _finish_map_provider(self, path):
        self._map_initialized = True
        # Pose restore can temporarily wake motion without a mission owner.
        self._synced_demand = None
        self._publish_map(True, 'map provider ready')
        if path != self.default_map:
            try:
                remember_map(self.map_selection_file, path)
            except OSError as error:
                # Persistence failure must not undo a working map provider.
                self.get_logger().warning(f'Could not remember selected map: {error}')

    def _sync(self):
        # Map RPCs also call _sync directly. A new attempt supersedes an old
        # retry; only its own confirmed refusal may schedule another one.
        self._sync_retry_at = None
        capabilities = self._capabilities_now()
        if 'recovery' in capabilities:
            self._report_usage('manual recovery in progress')
            return
        if self._recovery_pending:
            # One readback after the existing recovery Action, not a new
            # recovery loop. It may have activated or replaced our servers.
            self._synced_demand = None
            try:
                self._adopt_recovered_groups()
            except Exception:
                self._uncertain = True
                raise
            self._recovery_pending = False
            capabilities = self._capabilities_now()
            if 'recovery' in capabilities:
                self._report_usage('manual recovery in progress')
                return
        if self._uncertain:
            raise RuntimeError('previous lifecycle transition is unconfirmed')
        demand = (frozenset(capabilities), self._mapping_ready)
        with self._data_lock:
            verify = self._verify_requested
            self._verify_requested = False
        if demand == self._synced_demand and not verify and not self._group_errors:
            return
        # A partial transition must not leave the previous successful demand
        # cached: that previous list can return while cleanup is still needed.
        self._synced_demand = None
        self._ready_groups.clear()
        self._group_errors.clear()
        needed = required_groups(capabilities, self._mapping_ready)
        if self._mapping and (capabilities - {'autoslam'}) & set(PROFILES):
            raise RuntimeError('mapping source handoff has not finished')
        if 'planning' in needed and not (self._map_initialized or self._mapping_ready):
            raise RuntimeError('map provider unavailable; select a valid map')
        # Navigation and feature input processing sleep. map_server/AMCL stay
        # active: particles, location, TF and fall geometry need no wake logic.
        for group in ('motion', 'planning'):
            if group not in needed:
                continue
            try:
                # Native bond failure/recovery can change state without this
                # process knowing. Check the manager AND its member nodes.
                self._active[group] = self._group_active(group)
                if not self._active[group]:
                    if group == 'planning':
                        self._wait_map_tf()
                    self._manage(self._managers[group], ManageLifecycleNodes.Request.RESUME)
                    self._active[group] = True
                    if not self._group_active(group):
                        raise RuntimeError(f'{group}: activation was not confirmed')
                self._ready_groups.add(group)
            except Exception as error:
                self._group_errors[group] = str(error)
                if self._uncertain:
                    raise  # Never overlap an unconfirmed lifecycle command.
        for group in PROCESSING_GROUPS:
            try:
                if group in needed:
                    self._process_group(group, True)
                    self._ready_groups.add(group)
                else:
                    # Cleanup is best effort, without waiting for a dead
                    # optional module before admitting unrelated navigation.
                    self._process_group(group, False, wait=False)
            except Exception as error:
                self._group_errors[group] = str(error)
                if self._uncertain:
                    raise
        # Preserve the old conservative cleanup order: a busy former consumer
        # keeps its Nav2 dependencies awake until it confirms processing stop.
        # This must not prevent preparing an unrelated function above.
        cleanup_pending = any(group not in needed and group in self._group_errors
                              for group in PROCESSING_GROUPS)
        if not cleanup_pending:
            for group in ('planning', 'motion'):
                if self._active[group] and group not in needed:
                    try:
                        self._active[group] = self._group_active(group)
                        if self._active[group]:
                            self._manage(self._managers[group], ManageLifecycleNodes.Request.PAUSE)
                            self._active[group] = False
                    except Exception as error:
                        self._group_errors[group] = str(error)
                        if self._uncertain:
                            raise
        self._synced_demand = demand
        if self._group_errors:
            self._sync_retry_at = time.monotonic() + 1.0
        self._report_usage()

    def _process_group(self, group, enabled, *, wait=True):
        services = PROCESSING_GROUPS[group]
        for service in (services if enabled else reversed(services)):
            pending = self._processing_pending.get(service)
            if pending is not None:
                previous, future = pending
                if future is None:
                    raise RuntimeError(f'{service}: processing dispatch is unconfirmed')
                if wait:
                    self._wait(future, service, self.timeout)
                if not future.done():
                    raise ProcessingRejected(f'{service}: processing response pending')
                # Keep an exceptional/canceled transport result unresolved.
                # It is not proof that the remote side effect did not occur.
                reply = future.result()
                self._processing_pending.pop(service)
                if not reply.success:
                    raise ProcessingRejected(
                        reply.message or f'{service}: processing change rejected')
                self._processing_active[service] = previous
            if self._processing_active[service] == enabled:
                continue
            client = self._processing_clients[service]
            if not client.service_is_ready():
                raise ProcessingRejected(f'{service}: service unavailable')
            try:
                future = client.call_async(SetBool.Request(data=enabled))
            except Exception:
                self._processing_pending[service] = (enabled, None)
                raise
            # Retain the response, even after timeout, and never send an
            # opposite command while this one can still finish remotely.
            self._processing_pending[service] = (enabled, future)
            if wait:
                self._wait(future, service, self.timeout)
            elif not future.done():
                future.add_done_callback(self._processing_changed)
            if not future.done():
                raise ProcessingRejected(f'{service}: processing response pending')
            reply = future.result()
            self._processing_pending.pop(service)
            if not reply.success:
                raise ProcessingRejected(
                    reply.message or f'{service}: processing change rejected')
            self._processing_active[service] = enabled

    def _processing_changed(self, _future):
        # Resume cleanup on the actual response, not after a fixed sleep per
        # receiver. This callback never sends a command or owns the control lock.
        with self._data_lock:
            self._verify_requested = True
        self._demand_changed.trigger()

    def _group_active(self, group):
        active = self._call(
            self._manager_states[group], Trigger.Request(), group + ' state',
            read_only=True).success
        expected = (State.PRIMARY_STATE_ACTIVE if active else State.PRIMARY_STATE_INACTIVE)
        names = MOTION_NODES if group == 'motion' else PLANNING_NODES
        for name in names:
            state = self._call(
                self._node_states[name][0], GetState.Request(), name,
                read_only=True).current_state.id
            if state != expected:
                raise RuntimeError(f'{group}: {name} lifecycle state is inconsistent ({state})')
        return active

    def _adopt_recovered_groups(self):
        """Read native lifecycle state once after manual recovery finishes."""
        if self._pending_restore is not None and not self._settle_restore():
            raise RuntimeError('previous pose restore still has no terminal result')
        if self._pending_command is not None:
            self._settle_command()
            if self._pending_command is not None:
                raise RuntimeError('previous lifecycle command is still awaiting acknowledgement')
        active = {}
        for group, names in (('motion', MOTION_NODES), ('planning', PLANNING_NODES)):
            active[group] = self._call(
                self._manager_states[group], Trigger.Request(), group + ' state',
                read_only=True).success
            expected = (State.PRIMARY_STATE_ACTIVE if active[group]
                        else State.PRIMARY_STATE_INACTIVE)
            for name in names:
                state = self._call(
                    self._node_states[name][0], GetState.Request(), name,
                    read_only=True).current_state.id
                if state != expected:
                    raise RuntimeError(f'{group}: lifecycle state remains inconsistent')
        localization_active = self._call(
            self._manager_states['localization'], Trigger.Request(), 'map provider state',
            read_only=True).success
        if not self._mapping and not localization_active:
            raise RuntimeError('manual recovery did not restore the map provider')
        self._active = active
        self._localization_active = localization_active
        self._localization_started = localization_active
        self._uncertain = False

    def _report_usage(self, error=''):
        if self._closing:
            return
        error = error or '; '.join(self._group_errors.values())
        self._publish_report(self._usage, {
            'ready': (self._initialized and not self._uncertain and not error
                      and (self._map_initialized or self._mapping_ready)),
            'error': error, 'active_groups': sorted(
                [name for name, active in self._active.items() if active]
                + [group for group, services in PROCESSING_GROUPS.items()
                   if any(self._processing_active[service] for service in services)]),
            'capabilities': sorted(self._capabilities_now()),
            'localization_active': self._localization_active,
        })

    def _publish_report(self, publisher, payload):
        with self._report_lock:
            self._pending_reports[publisher] = String(data=json.dumps(payload))
            self._flush_reports_locked()

    def _flush_reports_locked(self):
        for publisher, message in list(self._pending_reports.items()):
            try:
                publisher.publish(message)
            except Exception as error:
                self.get_logger().warning(f'Could not publish runtime status: {error}')
            else:
                self._pending_reports.pop(publisher)

    def _tick(self):
        with self._report_lock:
            self._flush_reports_locked()
        self._resolve_preparations()
        if self._closing:
            return
        self._refresh_map_odom()
        if not self._lock.acquire(blocking=False):
            return
        try:
            settled = self._settle_command() or self._settle_restore()
            if self._uncertain and not self._recovery_pending:
                return
            if not self._initialized:
                # Configure without activate: the Action endpoints exist while
                # costmap update threads remain asleep. RESUME is Nav2's
                # standard activation command for these configured groups.
                for name, (state_client, change_client) in self._node_states.items():
                    if name in self._configured:
                        continue
                    state = self._call(
                        state_client, GetState.Request(), name, read_only=True).current_state.id
                    if state == State.PRIMARY_STATE_UNCONFIGURED:
                        request = ChangeState.Request()
                        request.transition.id = Transition.TRANSITION_CONFIGURE
                        if not self._call(change_client, request, f'configure {name}').success:
                            raise RuntimeError(f'could not configure {name}')
                    elif state != State.PRIMARY_STATE_INACTIVE:
                        raise RuntimeError(f'{name}: unexpected startup state {state}')
                    self._configured.add(name)
                if not self._map_initialized:
                    self._map_provider(self.map_path)
                    self._map_initialized = True
                self._initialized = True
                # The first state event may precede initialization. After this
                # one reconciliation, only demand/map events and confirmed
                # processing-refusal retries perform _sync.
                if self._have_state:
                    self._reconcile_demand()
            elif (settled or (self._sync_retry_at is not None
                              and time.monotonic() >= self._sync_retry_at)):
                with self._data_lock:
                    self._verify_requested = True
                self._reconcile_demand()
        except Exception as error:
            if not self._closing:
                self.get_logger().error(str(error))
                self._report_usage(str(error))
        finally:
            self._lock.release()
            self._resolve_preparations()

    async def _prepare(self, capability, request, response):
        # A canceled Trigger RPC cannot be recalled. Await readiness without
        # holding the transition callback group or parking an executor worker.
        # Only the existing state-change handler performs dependency changes.
        future = Future()
        with self._data_lock:
            self._verify_requested = True
            # Do not let an earlier cached success resolve this new request
            # before its endpoints have been checked again.
            self._preparations.append(
                (capability, response, time.monotonic() + self.timeout, future))
        self._demand_changed.trigger()
        self._resolve_preparations()
        try:
            return await future
        finally:
            with self._data_lock:
                self._preparations = [item for item in self._preparations if item[3] is not future]

    def _resolve_preparations(self, error=''):
        with self._data_lock:
            current = (frozenset(self._capabilities), self._mapping_ready)
            checked = not self._verify_requested and current == self._synced_demand
            for capability, response, deadline, future in self._preparations:
                # Keep resolved RPCs until their coroutine actually returned.
                if future.done():
                    continue
                ready = (self._initialized and capability in self._capabilities
                         and (capability == 'recovery'
                              or (not self._uncertain and checked
                                  and all(group in self._ready_groups
                                          for group in PROFILES[capability]))))
                problem = error or (next((message for group in PROFILES[capability]
                                          if (message := self._group_errors.get(group))), '')
                                    if checked else '')
                failed = (problem or self._closing or self._uncertain
                          or time.monotonic() >= deadline)
                if ready or failed:
                    response.success = bool(ready and not self._closing)
                    response.message = ('dependencies ready' if response.success else
                                        problem or 'mission demand/preparation unavailable')
                    # This private Future schedules the waiting ROS callback;
                    # finish it atomically with its response, including shutdown.
                    future.set_result(response)

    def _wait_map_tf(self):
        deadline = time.monotonic() + self.timeout
        requested_at = self.get_clock().now().nanoseconds
        while not self._closing:
            if self.tf.can_transform('map', 'base_footprint', Time()):
                transform = self.tf.lookup_transform('map', 'base_footprint', Time())
                stamp = Time.from_msg(transform.header.stamp).nanoseconds
                if stamp >= requested_at:
                    return
            if time.monotonic() >= deadline:
                raise RuntimeError('fresh map->base TF unavailable; set or restore the robot pose')
            time.sleep(0.02)
        raise RuntimeError('runtime is shutting down')

    def _wait_amcl_pose(self, before):
        deadline = time.monotonic() + self.timeout
        while (self._pose_sequence <= before
               or self._cached_pose_sequence != self._pose_sequence):
            self._refresh_map_odom()
            if self._closing or time.monotonic() >= deadline:
                raise RuntimeError('AMCL did not publish its restored pose')
            time.sleep(0.02)

    def _start_mapping(self, request, response):
        del request
        with self._lock:
            try:
                if not self._initialized or self._uncertain:
                    raise RuntimeError('runtime preparation is not confirmed')
                if self._mapping:
                    raise RuntimeError('SLAM source is already reserved')
                if (self._capabilities_now() - {'autoslam'}) & set(PROFILES):
                    raise RuntimeError('finish motion before changing the map provider')
                if not self._map_initialized:
                    raise RuntimeError('map provider unavailable; select a valid map')
                if self._active['planning']:
                    self._manage(self._managers['planning'], ManageLifecycleNodes.Request.PAUSE)
                    self._active['planning'] = False
                if not self._localization_active:
                    raise RuntimeError('AMCL map provider is not active')
                self._restore_pose = self._pose
                self._refresh_map_odom()
                if self._cached_map_odom is None:
                    raise RuntimeError('AMCL map/odom estimate is unavailable')
                self._map_odom = self._cached_map_odom
                # Cleanup removes the old transient-local /map publisher too.
                self._reset_localization()
                self._mapping, self._mapping_ready = True, False
                self._publish_map(False, 'SLAM source reserved')
                response.success = True
            except Exception as error:
                response.success, response.message = False, str(error)
                self._report_usage(str(error))
        return response

    def _ready_mapping(self, request, response):
        del request
        with self._lock:
            try:
                if self._uncertain:
                    raise RuntimeError('runtime transition is unconfirmed')
                if not self._mapping:
                    raise RuntimeError('SLAM source is not reserved')
                self._mapping_ready = True
                self._sync()
                self._publish_map(True, 'SLAM source ready')
                response.success = True
            except Exception as error:
                # A failed preparation must not let a demand event reactivate
                # consumers while AutoSLAM is already stopping its provider.
                if self._mapping:
                    self._mapping_ready = False
                    self._publish_map(False, str(error))
                response.success, response.message = False, str(error)
                self._report_usage(str(error))
        return response

    def _stop_mapping(self, request, response):
        del request
        with self._lock:
            try:
                if self._uncertain:
                    raise RuntimeError('runtime transition is unconfirmed')
                if self._mapping:
                    self._mapping_ready = False
                    if self._active['planning']:
                        self._manage(self._managers['planning'],
                                     ManageLifecycleNodes.Request.PAUSE)
                        self._active['planning'] = False
                    self._map_provider(self.map_path, self._pose_after_mapping())
                    self._mapping = False
                    self._publish_map(True, 'map provider restored')
                    self._sync()
                response.success = True
            except Exception as error:
                response.success, response.message = False, str(error)
                self._publish_map(False, str(error))
                self._report_usage(str(error))
        return response

    def _load_map(self, request, response):
        path = Path(request.map_url).expanduser()
        response.result = LoadMap.Response.RESULT_UNDEFINED_FAILURE
        changing_provider = False
        with self._lock:
            try:
                if not self._initialized or self._uncertain:
                    raise RuntimeError('runtime preparation is not confirmed')
                if self._mapping or self._capabilities_now() & set(PROFILES):
                    raise RuntimeError('finish motion/mapping before selecting a map')
                if path.suffix not in ('.yaml', '.yml') or not path.is_file():
                    response.result = LoadMap.Response.RESULT_MAP_DOES_NOT_EXIST
                    return response
                changing_provider = True
                self._reset_localization()
                self._map_provider(str(path.resolve()))
                self._sync()
                response.result = LoadMap.Response.RESULT_SUCCESS
            except Exception as error:
                self.get_logger().error(str(error))
                if changing_provider:
                    self._publish_map(False, str(error))
                    self._report_usage(str(error))
        return response

    def _reset_localization(self):
        self._map_initialized = False
        # Nav2 RESET expects active nodes. PAUSE retained configuration; resume
        # before RESET rather than asking it to deactivate an inactive node.
        if self._localization_started:
            if not self._localization_active:
                self._manage(self._localization, ManageLifecycleNodes.Request.RESUME)
                self._localization_active = True
            self._manage(self._localization, ManageLifecycleNodes.Request.RESET)
            self._localization_started = self._localization_active = False
        with self._data_lock:
            self._pose = None
            self._pose_sequence += 1
            self._cached_map_odom = None
            self._cached_pose_sequence = -1
            # Invalidate TF work that began with an estimate from the old map.
            # Mapping already saved its own restore pose/transform separately.

    def _pose_after_mapping(self):
        # Preserve the old map's coordinate system using continuous odometry;
        # SLAM may have chosen a different origin. No extra TF publisher.
        from copy import deepcopy
        pose = deepcopy(self._restore_pose) or PoseWithCovarianceStamped()
        pose.header.frame_id = 'map'
        old = self._map_odom.transform
        current = self.tf.lookup_transform('odom', 'base_footprint', Time()).transform
        q = old.rotation
        yaw = math.atan2(2 * (q.w*q.z + q.x*q.y), 1 - 2 * (q.y*q.y + q.z*q.z))
        x, y = current.translation.x, current.translation.y
        pose.pose.pose.position.x = old.translation.x + math.cos(yaw)*x - math.sin(yaw)*y
        pose.pose.pose.position.y = old.translation.y + math.sin(yaw)*x + math.cos(yaw)*y
        q = current.rotation
        yaw += math.atan2(2 * (q.w*q.z + q.x*q.y), 1 - 2 * (q.y*q.y + q.z*q.z))
        pose.pose.pose.orientation.z = math.sin(yaw / 2)
        pose.pose.pose.orientation.w = math.cos(yaw / 2)
        return pose

    def _map_odom_from_pose(self, message=None):
        # After SLAM exits, a TF buffer may still contain its last map->odom.
        # Derive the old map transform from the AMCL-only pose topic instead,
        # using odometry at exactly that estimate's acquisition timestamp.
        message = self._pose if message is None else message
        if message is None:
            raise RuntimeError('AMCL pose has not been observed')
        pose = message.pose.pose
        odom = self.tf.lookup_transform(
            'odom', 'base_footprint', Time.from_msg(message.header.stamp)).transform

        def yaw(q):
            return math.atan2(2 * (q.w*q.z + q.x*q.y), 1 - 2 * (q.y*q.y + q.z*q.z))

        angle = yaw(pose.orientation) - yaw(odom.rotation)
        result = TransformStamped()
        result.header.frame_id, result.child_frame_id = 'map', 'odom'
        x, y = odom.translation.x, odom.translation.y
        result.transform.translation.x = pose.position.x - math.cos(angle)*x + math.sin(angle)*y
        result.transform.translation.y = pose.position.y - math.sin(angle)*x - math.cos(angle)*y
        result.transform.rotation.z = math.sin(angle / 2)
        result.transform.rotation.w = math.cos(angle / 2)
        return result

    def _publish_map(self, ready, message):
        # Legacy field is a view of the actual provider, never a manager gate.
        self._publish_report(self._status, {
            'source': 'slam' if self._mapping else 'amcl', 'ready': ready,
            'mode': ('MAPPING' if self._mapping else 'LOCALIZATION') if ready else 'SWITCHING',
            'map': None if self._mapping else self.map_path, 'message': message,
        })


def main(args=None):
    """Run the common dependency owner independently of optional modules."""
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = RuntimeControl()
    executor = MultiThreadedExecutor(num_threads=8)
    executor.add_node(node)
    signaled = False

    def stop(_signum, _frame):
        # Finish callbacks before invalidating their ROS clients/publishers.
        nonlocal signaled
        signaled = True
        node._closing = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        while rclpy.ok() and not node._closing:
            executor.spin_once(timeout_sec=0.2)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node._closing = True
        node._resolve_preparations()
        # Let awaited service callbacks send their final response before the
        # executor stops. Joining workers alone cannot resume a suspended RPC.
        while rclpy.ok() and node._preparations:
            executor.spin_once(timeout_sec=0.02)
        executor.shutdown()
        # Humble does not join MultiThreadedExecutor's worker pool (#893).
        # Drain queued callbacks before destroying their ROS entities.
        executor._executor.shutdown(wait=True)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        # Launch can forward SIGINT after the process group already got it.
        # Keep the idempotent handler until exit on an interrupted CLI run.
        if not signaled:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
