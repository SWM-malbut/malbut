"""Switch between SLAM mapping and saved-map AMCL inside the system manager."""

import ctypes
import json
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Callable
from uuid import uuid4

from action_msgs.msg import GoalStatus
from malbut_interfaces.action import Relocalize
from malbut_interfaces.msg import LocalizationState
from malbut_interfaces.srv import PrepareLocalization
from nav2_msgs.srv import LoadMap, ManageLifecycleNodes, SetInitialPose
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger

from .models import LocalizationMode


LOAD_MAP_SERVICE = '/malbut/localization/load_map'
START_MAPPING_SERVICE = '/malbut/localization/start_mapping'
STOP_MAPPING_SERVICE = '/malbut/localization/stop_mapping'
STATE_TOPIC = '/malbut/localization/state'
STATUS_TOPIC = '/malbut/localization/status'
PREPARE_SERVICE = '/malbut/localization/prepare'
_PR_SET_PDEATHSIG = 1


class LocalizationError(RuntimeError):
    """A localization switch could not be completed."""


class SlamProcess:
    """Own one slam_toolbox process; it must never outlive the manager."""

    def __init__(self, command: list[str], stop_timeout_s: float) -> None:
        self.command = command
        self.stop_timeout_s = stop_timeout_s
        self._process: subprocess.Popen | None = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self.alive:
            return
        self._process = subprocess.Popen(
            self.command, stdin=subprocess.DEVNULL,
            preexec_fn=_die_with_parent)
        # A bad parameter file or missing package exits immediately.
        time.sleep(1.0)
        if not self.alive:
            code = self._process.returncode
            self._process = None
            raise LocalizationError(f'slam_toolbox exited during startup ({code})')

    def stop(self) -> None:
        if self._process is None:
            return
        # slam_toolbox publishes map->odom until it exits, so wait for exit.
        for sig, timeout in ((signal.SIGINT, self.stop_timeout_s),
                             (signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
            if self._process.poll() is not None:
                break
            self._process.send_signal(sig)
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
        if self._process.poll() is None:
            raise LocalizationError('slam_toolbox did not exit')
        self._process = None


class LocalizationController:
    """Expose map selection services and keep one map->odom source running."""

    def __init__(
        self,
        node,
        group,
        *,
        slam: SlamProcess,
        on_mode: Callable[[LocalizationMode], None],
        can_switch: Callable[[], bool],
        lifecycle_service: str,
        map_server_load_service: str,
        service_timeout_s: float,
        relocalize_action: str = '',
        relocalize_timeout_s: float = 90.0,
        default_map: str = '',
        can_mapping: Callable[[], bool] | None = None,
        on_pose_ready: Callable[[bool], None] = lambda _: None,
        admission_lock=None,
        movement_state: Callable[[], tuple[str, int]] = lambda: ('', 0),
    ) -> None:
        self._node = node
        self._slam = slam
        self._on_mode = on_mode
        self._on_pose_ready = on_pose_ready
        self._can_switch = can_switch
        self._can_mapping = can_mapping or can_switch
        self._admission_lock = admission_lock or threading.RLock()
        self._movement_state = movement_state
        self._timeout_s = service_timeout_s
        self._relocalize_timeout_s = relocalize_timeout_s
        self._default_map = str(Path(default_map).resolve()) if default_map else ''
        self._closing = False
        self._relocalizing = None
        self._relocalize_goal_future = None
        self._relocalize_result = None
        self._movement_lock = threading.RLock()
        # Reserve the initial transition before its startup timer can run.
        self._transition_active = True
        self._started = False
        self._stop_requested = False
        self.runtime_id = uuid4().hex
        self.transition_id = 1
        self.pose_ready = False
        self._switch_lock = threading.Lock()
        self._localization_started = False
        self._loaded_map: str | None = None
        self._last_message = ''
        self.mode = LocalizationMode.SWITCHING
        self.map_path: str | None = None
        state_qos = QoSProfile(depth=1)
        state_qos.reliability = ReliabilityPolicy.RELIABLE
        state_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self._state_publisher = node.create_publisher(String, STATE_TOPIC, state_qos)
        self._status_publisher = node.create_publisher(LocalizationState, STATUS_TOPIC, state_qos)
        self._manage = node.create_client(
            ManageLifecycleNodes, lifecycle_service, callback_group=group)
        self._map_server = node.create_client(
            LoadMap, map_server_load_service, callback_group=group)
        self._initial_pose = (node.create_client(
            SetInitialPose, '/set_initial_pose', callback_group=group)
            if default_map else None)
        # Finds the robot on each loaded map; empty leaves it to the operator.
        self._relocalize = (ActionClient(node, Relocalize, relocalize_action,
                                         callback_group=group)
                            if relocalize_action else None)
        self._services = [
            node.create_service(LoadMap, LOAD_MAP_SERVICE, self._load_map_request,
                                callback_group=group),
            node.create_service(Trigger, START_MAPPING_SERVICE,
                                self._start_mapping_request, callback_group=group),
            node.create_service(Trigger, STOP_MAPPING_SERVICE,
                                self._stop_mapping_request, callback_group=group),
            node.create_service(PrepareLocalization, PREPARE_SERVICE,
                                self._prepare_request, callback_group=group),
        ]
        self._monitor = node.create_timer(1.0, self._check_slam, callback_group=group)
        self._set(LocalizationMode.SWITCHING, None, 'starting localization')

    def start(self, initial_map: str) -> None:
        """Enter the launch-selected state; runs on an executor thread."""
        with self._switch_lock:
            if self._started:
                return
            self._started = True
            try:
                if initial_map:
                    self._to_localization(_map_file(initial_map))
                else:
                    self._to_mapping()
            except (LocalizationError, OSError) as error:
                self._fail(str(error))
            finally:
                self._transition_active = False

    def close(self) -> None:
        """Stop the owned SLAM process during manager shutdown."""
        self._closing = True
        if self._relocalizing is not None:
            self._relocalizing.cancel_goal_async()
        self._monitor.cancel()
        try:
            self._slam.stop()
        except LocalizationError as error:
            self._node.get_logger().error(str(error))

    def _start_mapping_request(self, request, response):
        del request
        response.success, response.message = self._switch(None, mapping=True)
        return response

    def _stop_mapping_request(self, request, response):
        del request
        if not self._default_map:
            response.success, response.message = False, 'default map is not configured'
        else:
            response.success, response.message = self._switch(self._default_map, mapping=True)
        return response

    def _load_map_request(self, request, response):
        try:
            path = _map_file(request.map_url)
        except LocalizationError as error:
            response.result = LoadMap.Response.RESULT_MAP_DOES_NOT_EXIST
            self._node.get_logger().warning(str(error))
            return response
        success, message = self._switch(path)
        response.result = (LoadMap.Response.RESULT_SUCCESS if success
                           else LoadMap.Response.RESULT_UNDEFINED_FAILURE)
        if not success:
            self._node.get_logger().warning(message)
        return response

    def _prepare_request(self, request, response):
        if not request.movement_runtime_id or request.mapping and request.map_url:
            response.code = 'invalid_request'
            response.message = 'A movement binding and an unambiguous map mode are required'
            return response
        try:
            path = None if request.mapping else _map_file(request.map_url)
        except LocalizationError as error:
            response.code, response.message = 'invalid_map', str(error)
            return response
        response.success, response.message = self._switch(
            path, expected_movement=(request.movement_runtime_id, request.movement_epoch))
        response.code = (
            'completed' if response.success else 'movement_epoch_changed'
            if response.message == 'movement_epoch_changed' else 'localization_failed')
        if response.code == 'movement_epoch_changed':
            response.message = 'Movement was stopped after this preparation was requested'
        return response

    def _switch(self, map_path: str | None, *, mapping: bool = False,
                expected_movement=None) -> tuple[bool, str]:
        if not self._switch_lock.acquire(blocking=False):
            return False, 'another localization switch is in progress'
        transition_started = False
        try:
            if not self._started:
                return False, 'initial localization startup is pending'
            target = (LocalizationMode.LOCALIZATION if map_path
                      else LocalizationMode.MAPPING)
            with self._admission_lock:
                if (expected_movement is not None
                        and expected_movement != self._movement_state()):
                    return False, 'movement_epoch_changed'
                if self.mode is target and self.map_path == map_path:
                    return True, f'already {target.value.lower()}'
                if not (self._can_mapping if mapping else self._can_switch)():
                    return False, 'cancel missions that use the base before switching maps'
                self._begin_transition()
                transition_started = True
                self._set(LocalizationMode.SWITCHING, map_path, 'switching localization')
            if map_path:
                self._to_localization(map_path)
            else:
                self._to_mapping()
            return True, self._last_message
        except (LocalizationError, OSError) as error:
            self._fail(str(error))
            return False, str(error)
        finally:
            if transition_started:
                self._transition_active = False
            self._switch_lock.release()

    def _begin_transition(self):
        with self._movement_lock:
            self.transition_id += 1
            self._transition_active = True
            self._stop_requested = False
        self.pose_ready = False

    @property
    def movement_identity(self):
        """Name a switch or unresolved internal pose action for stop reporting."""
        with self._movement_lock:
            pending = (self._transition_active
                       or self._relocalize_goal_future is not None
                       or self._relocalize_result is not None)
            return f'localization:{self.runtime_id}:{self.transition_id}' if pending else ''

    @property
    def stop_pending(self):
        """Retain the stop gate until a canceled internal action actually ends."""
        return self._stop_requested and bool(self.movement_identity)

    def movement_pending(self, identity):
        """Check only the original transition, never a newer map operation."""
        return bool(identity) and self.movement_identity == identity

    def stop_movement(self):
        """Fence a map switch and cancel its current or late pose action."""
        with self._movement_lock:
            if not self.movement_identity:
                return
            self._stop_requested = True
            handle = self._relocalizing
        self.record_pose_result(False)
        if handle is not None:
            handle.cancel_goal_async()

    def record_pose_result(self, ready):
        """Update readiness after a managed pose correction on the selected map."""
        self.pose_ready = bool(ready) and self.mode is LocalizationMode.LOCALIZATION
        self._on_pose_ready(self.pose_ready)
        self._publish(self._last_message)

    def _to_mapping(self) -> None:
        self._set(LocalizationMode.SWITCHING, None, 'switching to mapping')
        if self._localization_started:
            # RESET also removes map_server's latched map and AMCL's map->odom.
            self._reset_localization()
        self._slam.start()
        self._set(LocalizationMode.MAPPING, None, self._message(LocalizationMode.MAPPING))

    def _to_localization(self, map_path: str) -> None:
        self._set(LocalizationMode.SWITCHING, map_path, 'switching to the saved map')
        self._slam.stop()
        if self._localization_started and self._loaded_map != map_path:
            # AMCL keeps its particles across maps; start the new map without a pose.
            self._reset_localization()
        if not self._localization_started:
            self._manage_nodes(ManageLifecycleNodes.Request.STARTUP)
            self._localization_started = True
        request = LoadMap.Request()
        request.map_url = map_path
        response = self._call(self._map_server, request, 'map_server load_map')
        if response.result != LoadMap.Response.RESULT_SUCCESS:
            raise LocalizationError(
                f'map_server rejected {map_path} (result {response.result})')
        self._loaded_map = map_path
        self._set(LocalizationMode.LOCALIZATION, map_path, self._find_pose(map_path))

    def _reset_localization(self) -> None:
        self._manage_nodes(ManageLifecycleNodes.Request.RESET)
        self._localization_started = False
        self._loaded_map = None

    def _find_pose(self, map_path: str) -> str:
        """Find the robot on the loaded map; missions using the base wait meanwhile."""
        if map_path == self._default_map:
            # An all-unknown map has no landmarks for AMCL global localization.
            # Use AMCL's existing initial-pose service, never another TF source.
            request = SetInitialPose.Request()
            request.pose.header.frame_id = 'map'
            request.pose.header.stamp = self._node.get_clock().now().to_msg()
            request.pose.pose.pose.orientation.w = 1.0
            self._call(self._initial_pose, request, 'AMCL initial pose')
            with self._movement_lock:
                self.pose_ready = not self._stop_requested
            return 'default unknown map loaded; AMCL initialized at (0, 0)'
        if self._relocalize is None:
            return self._message(LocalizationMode.LOCALIZATION)
        # Relocalization may rotate the robot, so stay SWITCHING until it ends.
        self._set(LocalizationMode.SWITCHING, map_path,
                  'saved map loaded; finding the robot pose')
        retry = 'set the initial pose before driving'
        if not self._relocalize.wait_for_server(timeout_sec=self._timeout_s):
            return f'saved map loaded; relocalization is unavailable, {retry}'
        goal = Relocalize.Goal()
        goal.method = Relocalize.Goal.AUTO
        try:
            with self._movement_lock:
                if self._stop_requested:
                    return 'saved map loaded; pose search was stopped'
                future = self._relocalize.send_goal_async(goal)
                self._relocalize_goal_future = future
                future.add_done_callback(self._observe_pose_goal)
            handle = self._wait(future, 'relocalization request', self._timeout_s)
        except LocalizationError as error:
            self.stop_movement()
            return f'saved map loaded; finding the pose failed ({error}), {retry}'
        if not handle.accepted:
            return f'saved map loaded; another pose correction is running, {retry}'
        try:
            response = self._wait(handle.get_result_async(), 'relocalization',
                                  self._relocalize_timeout_s)
            result = response.result
        except LocalizationError as error:
            self.stop_movement()
            return f'saved map loaded; finding the pose failed ({error}), {retry}'
        if (result.success and response.status == GoalStatus.STATUS_SUCCEEDED
                and not self._stop_requested):
            self.pose_ready = True
            return f'saved map loaded; {result.message}'
        return f'saved map loaded; pose not found ({result.message}), {retry}'

    def _observe_pose_goal(self, future):
        with self._movement_lock:
            try:
                handle = future.result()
            except Exception:
                # Delivery remains unknown; retain the gate and the future.
                self._stop_requested = True
                return
            self._relocalize_goal_future = None
            if not handle.accepted:
                return
            self._relocalizing = handle
            result = handle.get_result_async()
            self._relocalize_result = result
            result.add_done_callback(self._observe_pose_terminal)
            should_cancel = self._stop_requested
        if should_cancel:
            handle.cancel_goal_async()

    def _observe_pose_terminal(self, future):
        with self._movement_lock:
            try:
                if future.result().status not in (
                        GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_CANCELED,
                        GoalStatus.STATUS_ABORTED):
                    self._stop_requested = True
                    return
            except Exception:
                self._stop_requested = True
                return
            self._relocalizing = None
            self._relocalize_result = None

    def _manage_nodes(self, command: int) -> None:
        request = ManageLifecycleNodes.Request()
        request.command = command
        if not self._call(self._manage, request, 'localization lifecycle').success:
            raise LocalizationError('localization lifecycle transition failed')

    def _call(self, client, request, label: str):
        if not client.wait_for_service(timeout_sec=self._timeout_s):
            raise LocalizationError(f'{label} service is unavailable')
        future = client.call_async(request)
        try:
            return self._wait(future, label, self._timeout_s)
        except LocalizationError:
            client.remove_pending_request(future)
            raise

    def _wait(self, future, label: str, timeout_s: float):
        # Service handlers run on a reentrant group of a multithreaded executor,
        # so other executor threads complete this future while we wait.
        deadline = time.monotonic() + timeout_s
        while not future.done():
            if self._closing:
                raise LocalizationError('system manager is shutting down')
            if time.monotonic() >= deadline:
                raise LocalizationError(f'{label} did not respond')
            time.sleep(0.02)
        return future.result()

    def _check_slam(self) -> None:
        if (self.mode is LocalizationMode.MAPPING and not self._slam.alive
                and not self._switch_lock.locked()):
            self._fail('slam_toolbox exited while mapping')

    def _fail(self, message: str) -> None:
        self._node.get_logger().error(f'Localization: {message}')
        self._set(LocalizationMode.ERROR, self.map_path, message)

    def _set(self, mode: LocalizationMode, map_path: str | None, message: str) -> None:
        self.mode, self.map_path = mode, map_path
        if mode is not LocalizationMode.LOCALIZATION:
            self.pose_ready = False
        self._last_message = message
        self._on_pose_ready(self.pose_ready)
        self._on_mode(mode)
        self._publish(message)

    def _message(self, mode: LocalizationMode | None = None) -> str:
        mode = mode or self.mode
        if mode is LocalizationMode.MAPPING:
            return 'mapping: no saved map selected; only mapping missions are available'
        if mode is LocalizationMode.LOCALIZATION:
            return 'saved map loaded; confirm the robot pose before driving'
        return mode.value.lower()

    def _publish(self, message: str) -> None:
        self._status_publisher.publish(LocalizationState(
            runtime_id=self.runtime_id, transition_id=self.transition_id,
            mode=self.mode.value, map_path=self.map_path or '',
            pose_ready=self.pose_ready, message=message,
        ))
        self._state_publisher.publish(String(data=json.dumps({
            'mode': self.mode.value, 'map': self.map_path, 'message': message,
            'runtime_id': self.runtime_id, 'transition_id': self.transition_id,
            'pose_ready': self.pose_ready,
        })))


def _map_file(value: str) -> str:
    path = Path(value).expanduser()
    if (not value or path.suffix.lower() not in ('.yaml', '.yml')
            or not path.is_file()):
        raise LocalizationError(f'saved map YAML not found: {value!r}')
    return str(path.resolve())


def _die_with_parent() -> None:
    # Keep SLAM from publishing map->odom after the manager is gone.
    ctypes.CDLL('libc.so.6', use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
