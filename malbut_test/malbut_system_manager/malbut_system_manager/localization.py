"""Switch between SLAM mapping and saved-map AMCL inside the system manager."""

import ctypes
import json
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Callable

from malbut_interfaces.action import Relocalize
from nav2_msgs.srv import LoadMap, ManageLifecycleNodes
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger

from .models import LocalizationMode


LOAD_MAP_SERVICE = '/malbut/localization/load_map'
START_MAPPING_SERVICE = '/malbut/localization/start_mapping'
STATE_TOPIC = '/malbut/localization/state'
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
    ) -> None:
        self._node = node
        self._slam = slam
        self._on_mode = on_mode
        self._can_switch = can_switch
        self._timeout_s = service_timeout_s
        self._relocalize_timeout_s = relocalize_timeout_s
        self._closing = False
        self._relocalizing = None
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
        self._manage = node.create_client(
            ManageLifecycleNodes, lifecycle_service, callback_group=group)
        self._map_server = node.create_client(
            LoadMap, map_server_load_service, callback_group=group)
        # Finds the robot on each loaded map; empty leaves it to the operator.
        self._relocalize = (ActionClient(node, Relocalize, relocalize_action,
                                         callback_group=group)
                            if relocalize_action else None)
        self._services = [
            node.create_service(LoadMap, LOAD_MAP_SERVICE, self._load_map_request,
                                callback_group=group),
            node.create_service(Trigger, START_MAPPING_SERVICE,
                                self._start_mapping_request, callback_group=group),
        ]
        self._monitor = node.create_timer(1.0, self._check_slam, callback_group=group)
        self._publish('starting localization')

    def start(self, initial_map: str) -> None:
        """Enter the launch-selected state; runs on an executor thread."""
        with self._switch_lock:
            try:
                if initial_map:
                    self._to_localization(_map_file(initial_map))
                else:
                    self._to_mapping()
            except (LocalizationError, OSError) as error:
                self._fail(str(error))

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
        response.success, response.message = self._switch(None)
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

    def _switch(self, map_path: str | None) -> tuple[bool, str]:
        if not self._switch_lock.acquire(blocking=False):
            return False, 'another localization switch is in progress'
        try:
            target = (LocalizationMode.LOCALIZATION if map_path
                      else LocalizationMode.MAPPING)
            if self.mode is target and self.map_path == map_path:
                return True, f'already {target.value.lower()}'
            if not self._can_switch():
                return False, 'cancel missions that use the base before switching maps'
            if map_path:
                self._to_localization(map_path)
            else:
                self._to_mapping()
            return True, self._last_message
        except (LocalizationError, OSError) as error:
            self._fail(str(error))
            return False, str(error)
        finally:
            self._switch_lock.release()

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
            handle = self._wait(self._relocalize.send_goal_async(goal),
                                'relocalization request', self._timeout_s)
        except LocalizationError as error:
            return f'saved map loaded; finding the pose failed ({error}), {retry}'
        if not handle.accepted:
            return f'saved map loaded; another pose correction is running, {retry}'
        self._relocalizing = handle
        try:
            result = self._wait(handle.get_result_async(), 'relocalization',
                                self._relocalize_timeout_s).result
        except LocalizationError as error:
            handle.cancel_goal_async()
            return f'saved map loaded; finding the pose failed ({error}), {retry}'
        finally:
            self._relocalizing = None
        if result.success:
            return f'saved map loaded; {result.message}'
        return f'saved map loaded; pose not found ({result.message}), {retry}'

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
        self._last_message = message
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
        self._state_publisher.publish(String(data=json.dumps({
            'mode': self.mode.value, 'map': self.map_path, 'message': message,
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
