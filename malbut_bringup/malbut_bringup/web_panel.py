"""Show robot data and supervise explicitly requested Bringup on a trusted LAN."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import copy
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
from pathlib import Path
import queue
import re
import secrets
import signal
import threading
import time
from urllib.parse import urlsplit
import uuid

from .web_map import MapCache
from .web_runtime import RuntimeSupervisor, SavedMapCatalog


TERMINAL = {'SUCCEEDED', 'CANCELED', 'ABORTED', 'REJECTED', 'ERROR'}
RUNTIME_ACTIONS = {
    'mapping': ('/autoslam', '/navigate_to_pose', '/follow_path', '/spin', '/backup', '/wait'),
    'navigation': ('/malbut/mission/execute', '/follow_person', '/patrol',
                   '/navigate_to_pose', '/follow_path', '/spin', '/backup', '/wait'),
}


def validate_command(payload):
    """Allow explicit test actions and fixed Bringup commands, never shell text."""
    if not isinstance(payload, dict):
        raise ValueError('JSON object required')
    if payload == {'command': 'cancel'}:
        return payload
    if payload == {'command': 'bringup_stop'}:
        return payload
    if payload.get('command') == 'bringup_start':
        if payload == {'command': 'bringup_start', 'mode': 'mapping'}:
            return payload
        if (set(payload) == {'command', 'mode', 'map'}
                and payload['mode'] == 'navigation'
                and isinstance(payload['map'], str)
                and Path(payload['map']).name == payload['map']
                and Path(payload['map']).suffix in ('.yaml', '.yml')):
            return payload
        raise ValueError('Choose mapping, or navigation with a listed map filename')
    if set(payload) != {'command', 'capability', 'arguments'}:
        raise ValueError('Expected command, capability, arguments')
    if payload['command'] != 'start' or not isinstance(payload['arguments'], dict):
        raise ValueError('Invalid command')
    capability = payload['capability']
    args = payload['arguments']
    if capability == 'autoslam':
        if set(args) != {'map_name'} or not isinstance(args['map_name'], str):
            raise ValueError('map_name is required')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args['map_name']):
            raise ValueError('Use a map name without paths or extension')
    elif capability == 'follow_person':
        if set(args) != {'target_mode', 'target_person_id', 'desired_distance_m'}:
            raise ValueError('Invalid FollowPerson arguments')
        if type(args['target_mode']) is not int or args['target_mode'] not in (0, 1):
            raise ValueError('target_mode must be 0 or 1')
        if not isinstance(args['target_person_id'], str):
            raise ValueError('target_person_id must be a string')
        if args['target_mode'] == 1 and not args['target_person_id'].strip():
            raise ValueError('Registered person mode requires a person ID')
        distance = args['desired_distance_m']
        if (type(distance) not in (float, int) or not math.isfinite(distance)
                or distance <= 0):
            raise ValueError('desired_distance_m must be positive and finite')
    elif capability == 'patrol':
        if (set(args) != {'thoroughness'} or type(args['thoroughness']) is not int
                or args['thoroughness'] not in (0, 1, 2)):
            raise ValueError('thoroughness must be 0, 1 or 2')
    else:
        raise ValueError('Unknown capability')
    return payload


def image_jpeg(message):
    """Encode common raw camera formats with row padding, without cv_bridge."""
    import cv2
    import numpy as np

    channels = {'rgb8': 3, 'bgr8': 3, 'rgba8': 4, 'bgra8': 4, 'mono8': 1}
    count = channels.get(message.encoding)
    if count is None:
        raise ValueError(f'Unsupported raw encoding: {message.encoding}')
    width, height, step = message.width, message.height, message.step
    if (width <= 0 or height <= 0 or step < width * count
            or len(message.data) != height * step):
        raise ValueError('Invalid image dimensions or row stride')
    rows = np.frombuffer(bytes(message.data), dtype=np.uint8).reshape(height, step)
    pixels = rows[:, :width * count].reshape(height, width, count)
    conversion = {
        'rgb8': cv2.COLOR_RGB2BGR, 'rgba8': cv2.COLOR_RGBA2BGR,
        'bgra8': cv2.COLOR_BGRA2BGR,
    }
    if message.encoding in conversion:
        pixels = cv2.cvtColor(pixels, conversion[message.encoding])
    ok, encoded = cv2.imencode('.jpg', pixels, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        raise ValueError('JPEG encoding failed')
    return encoded.tobytes()


class PanelData:
    """Keep only latest sensor frames and bounded command/status history."""

    def __init__(self):
        """Initialize bounded in-memory state without subscriptions or commands."""
        self.lock = threading.RLock()
        self.encode_lock = threading.Lock()
        self.requests = OrderedDict()
        self.servers = {'manager': False, 'autoslam': False}
        self.system = None
        self.tracking = None
        self.frames = {}
        self.encoded = (None, b'')
        self.map_cache = MapCache()
        self.map_active = False
        self.robot_pose = None
        self.runtime = {'enabled': False, 'state': 'STOPPED', 'ready': False,
                        'mode': None, 'map': None, 'message': '', 'log_path': None}
        self.closed = False

    def update(self, request_id, **values):
        """Update one request without removing another active goal's handle."""
        with self.lock:
            if request_id in self.requests:
                self.requests[request_id].update(values)

    def register(self, payload):
        """Reserve bounded command history before enqueueing a start request."""
        with self.lock:
            if self.closed:
                raise ValueError('Panel is shutting down')
            active = [item for item in self.requests.values()
                      if item['state'] not in TERMINAL]
            if len(active) >= 16:
                raise ValueError('Too many outstanding requests; cancel or wait')
            request_id = uuid.uuid4().hex
            self.requests[request_id] = {
                'id': request_id, 'capability': payload['capability'],
                'state': 'PENDING', 'route': '', 'feedback': {}, 'message': '',
            }
            finished = [key for key, item in self.requests.items()
                        if item['state'] in TERMINAL]
            for key in finished[:max(0, len(self.requests) - 32)]:
                del self.requests[key]
            return request_id

    def receive_frame(self, stream, message):
        """Replace a frame; do not perform inference or JPEG work in callbacks."""
        with self.lock:
            self.frames[stream] = (time.monotonic(), message)

    def receive_map(self, message):
        """Keep a new map without rendering it on the ROS executor."""
        with self.lock:
            self.map_cache.update(message)

    def map_snapshot(self):
        """Pair current map geometry with the latest valid robot transform."""
        with self.lock:
            return self.map_cache.snapshot(active=self.map_active, pose=self.robot_pose)

    def jpeg(self, stream):
        """Encode on demand once per latest raw frame, shared by all viewers."""
        with self.lock:
            frame = self.frames.get(stream)
        if frame is None or time.monotonic() - frame[0] > 5.0:
            raise ValueError('No recent image received')
        message = frame[1]
        if stream == 'debug':
            content = bytes(message.data)
            if not content.startswith(b'\xff\xd8'):
                raise ValueError('Debug topic is not JPEG compressed')
            return content
        with self.encode_lock:
            if self.encoded[0] != frame[0]:
                self.encoded = (frame[0], image_jpeg(message))
            return self.encoded[1]

    def snapshot(self):
        """Return a thread-safe status copy without exposing tokens or images."""
        with self.lock:
            return copy.deepcopy({
                'servers': self.servers, 'system': self.system,
                'tracking': self.tracking, 'requests': list(self.requests.values()),
                'runtime': self.runtime,
                'video_age_s': {key: round(time.monotonic() - frame[0], 1)
                                for key, frame in self.frames.items()},
            })


class RosBridge:
    """Own Action handles and execute all ROS commands on the ROS executor."""

    def __init__(self, data):
        """Subscribe to diagnostics/images and prepare nonblocking Action clients."""
        from malbut_interfaces.action import AutoSlam, ExecuteMission
        from malbut_interfaces.msg import SystemState
        from action_msgs.msg import GoalStatusArray
        from action_msgs.srv import CancelGoal
        from nav_msgs.msg import OccupancyGrid
        from rclpy.action import ActionClient
        from rclpy.node import Node
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from rosidl_runtime_py.convert import message_to_ordereddict
        from sensor_msgs.msg import CompressedImage, Image
        from std_msgs.msg import String
        from tf2_ros import Buffer, TransformListener

        self.node = Node('robot_web_panel')
        self.data = data
        self.commands = queue.Queue(maxsize=64)
        self.handles = {}
        self.cancel_pending = set()
        self.auto_goal = AutoSlam.Goal
        self.mission_goal = ExecuteMission.Goal
        self.to_dict = message_to_ordereddict
        self.clients = {
            'manager': ActionClient(self.node, ExecuteMission, '/malbut/mission/execute'),
            'autoslam': ActionClient(self.node, AutoSlam, '/autoslam'),
        }
        self.catalog = SavedMapCatalog(self.node.declare_parameter(
            'map_directory', str(Path.home() / '.ros/malbut/maps')).value)
        self.runtime = (RuntimeSupervisor(self.catalog) if self.node.declare_parameter(
            'manage_bringup', True).value else None)
        self.runtime_message = ''
        self.startup_status = {}
        self.stopping_runtime = None
        self.action_status = {}
        self.cancel_request = CancelGoal.Request
        self.cancel_clients = {
            name: self.node.create_client(CancelGoal, name + '/_action/cancel_goal')
            for name in set(sum(RUNTIME_ACTIONS.values(), ()))
        }
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self.node)
        self.robot_frame = self.node.declare_parameter('robot_frame', 'base_footprint').value
        sensor_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        topics = {
            'rgb_topic': '/depth_cam/rgb0/image_raw',
            'debug_topic': '/perception/person/debug_image/compressed',
            'map_topic': '/global_costmap/costmap',
        }
        self.topics = {key: self.node.declare_parameter(key, value).value
                       for key, value in topics.items()}
        self.subscriptions = [
            self.node.create_subscription(Image, self.topics['rgb_topic'],
                                          lambda msg: data.receive_frame('raw', msg),
                                          sensor_qos),
            self.node.create_subscription(CompressedImage, self.topics['debug_topic'],
                                          lambda msg: data.receive_frame('debug', msg),
                                          sensor_qos),
            self.node.create_subscription(String, '/tracking/person/status',
                                          self._tracking, 1),
            self.node.create_subscription(
                String, '/malbut/bringup/status', self._bringup_status,
                QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)),
            self.node.create_subscription(
                SystemState, '/malbut/state', self._system,
                QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)),
            self.node.create_subscription(
                OccupancyGrid, self.topics['map_topic'], self._map,
                QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)),
        ]
        self.subscriptions.extend(self.node.create_subscription(
            GoalStatusArray, name + '/_action/status',
            lambda msg, name=name: self._action_status(name, msg),
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
            for name in self.cancel_clients)
        self.guard = self.node.create_guard_condition(self._drain)
        self.timer = self.node.create_timer(1.0, self._refresh)
        self._refresh()

    def submit(self, payload):
        """Queue authenticated commands without blocking an HTTP worker on DDS."""
        payload = validate_command(payload)
        request_id = None
        if payload['command'] == 'start':
            request_id = self.data.register(payload)
        try:
            self.commands.put_nowait((request_id, payload))
        except queue.Full:
            if request_id:
                self.data.update(request_id, state='ERROR', message='Command queue full')
            raise ValueError('Command queue full')
        self.guard.trigger()
        return request_id

    def _refresh(self):
        with self.data.lock:
            self.data.servers = {name: client.server_is_ready()
                                 for name, client in self.clients.items()}
            self.data.map_active = bool(self.node.count_publishers(self.topics['map_topic']))
            self.data.robot_pose = self._robot_pose()
        self._finish_runtime_stop()
        status = (self.runtime.snapshot() if self.runtime else {
            'state': 'STOPPED', 'mode': None, 'map': None, 'log_path': None,
            'message': 'Embedded viewer: start a standalone web panel to control Bringup',
        })
        status['enabled'] = self.runtime is not None
        status['ready'] = bool(self.data.servers[
            'autoslam' if status['mode'] == 'mapping' else 'manager'])
        status['waiting'] = []
        if (self.runtime and status['state'] == 'RUNNING'
                and not status['ready']):
            status['waiting'] = self.startup_status.get('missing', [])
            status['message'] = ('필수 입력 준비 대기' if status['waiting']
                                 else 'Action 서버 준비 대기')
        if self.runtime_message:
            status['message'] = self.runtime_message
        if self.stopping_runtime is not None:
            status.update(state='STOPPING', message='Waiting for Action cancellation to finish')
        with self.data.lock:
            self.data.runtime = status

    def _map(self, message):
        try:
            self.data.receive_map(message)
        except ValueError as error:
            self.node.get_logger().warning(f'Ignoring invalid map: {error}')

    def _bringup_status(self, message):
        try:
            status = json.loads(message.data)
            if (isinstance(status, dict) and isinstance(status.get('missing'), list)
                    and all(isinstance(item, str) for item in status['missing'])):
                self.startup_status = status
        except (ValueError, TypeError):
            pass

    def _robot_pose(self):
        from rclpy.time import Time
        from tf2_ros import TransformException

        info = self.data.map_snapshot()
        if not info['available'] or not info['active']:
            return None
        try:
            transform = self.tf_buffer.lookup_transform(
                info['frame_id'], self.robot_frame, Time())
        except TransformException:
            return None
        stamp = Time.from_msg(transform.header.stamp).nanoseconds
        age = (self.node.get_clock().now().nanoseconds - stamp) / 1e9
        if not 0 <= age <= 2.0:
            return None  # Don't display the last known TF as a live robot position.
        translation = transform.transform.translation
        q = transform.transform.rotation
        return {'x': translation.x, 'y': translation.y,
                'yaw': math.atan2(2 * (q.w*q.z + q.x*q.y),
                                  1 - 2 * (q.y*q.y + q.z*q.z))}

    def _action_status(self, name, message):
        self.action_status[name] = {
            bytes(item.goal_info.goal_id.uuid): item.status for item in message.status_list}

    def _start_runtime(self, payload):
        if self.runtime is None:
            raise ValueError('Start a standalone robot_web_panel to control Bringup')
        if self.stopping_runtime is not None:
            raise ValueError('Wait for Bringup shutdown to finish')
        names = {name for name, _ in self.node.get_node_names_and_namespaces()}
        conflicts = names.intersection({
            'amcl', 'map_server', 'slam_toolbox', 'controller_server', 'planner_server',
            'bt_navigator', 'nav2_container', 'system_manager', 'autoslam',
            'person_follower', 'person_localizer', 'person_reidentifier', 'yolo_node',
        })
        if conflicts or self.node.count_publishers(self.topics['map_topic']):
            raise ValueError('Stop existing mapping/navigation/perception first: '
                             + ', '.join(sorted(conflicts)))
        scan = self.node.count_publishers('/scan_raw')
        odom = self.node.count_publishers('/odom')
        if scan > 1 or odom > 1 or bool(scan) != bool(odom):
            raise ValueError('Hardware is duplicated or only partly running; check scan/odom')
        if not scan and names.intersection({
                'controller', 'odom_publisher', 'ros_robot_controller',
                'robot_state_publisher', 'aurora930_node', 'LD19'}):
            raise ValueError('Existing hardware nodes are not ready; do not launch duplicates')
        self.runtime.start(payload['mode'], map_id=payload.get('map'),
                           start_hardware=not bool(scan))
        self.runtime_message = ''
        self.tf_buffer.clear()
        self.action_status.clear()
        self.startup_status = {}
        with self.data.lock:
            self.data.map_cache.clear()
            self.data.map_active = False
            self.data.robot_pose = None
            self.data.system = None
            self.data.tracking = None
            self.data.frames.clear()

    def _stop_runtime(self):
        if self.runtime is None or self.runtime.snapshot()['state'] == 'STOPPED':
            raise ValueError('This panel has no running Bringup to stop')
        if self.stopping_runtime is not None:
            return
        self.cancel_owned()
        mode = self.runtime.snapshot()['mode']
        names = RUNTIME_ACTIONS.get(mode, ())
        self.stopping_runtime = {
            'since': time.monotonic(), 'names': names,
            'futures': {name: self.cancel_clients[name].call_async(self.cancel_request())
                        for name in names if self.cancel_clients[name].service_is_ready()},
        }
        self.runtime_message = ''

    def _finish_runtime_stop(self):
        pending = self.stopping_runtime
        if pending is None:
            return
        if time.monotonic() - pending['since'] > 30.0:
            self.runtime_message = 'Action stop unconfirmed; Bringup kept running. Check robot.'
            self.stopping_runtime = None
            return
        try:
            for name, future in pending['futures'].items():
                if not future.done():
                    return
                response = future.result()
                if response.return_code == 1:
                    raise ValueError(f'{name} rejected cancellation')
                statuses = self.action_status.get(name, {})
                if any(statuses.get(bytes(goal.goal_id.uuid)) not in (4, 5, 6)
                       for goal in response.goals_canceling):
                    return
            if any(state in (1, 2, 3) for name in pending['names']
                   for state in self.action_status.get(name, {}).values()):
                return
            if any(item['state'] not in TERMINAL
                   for item in self.data.snapshot()['requests']):
                return
            self.runtime.stop()
            self.stopping_runtime = None
        except Exception as error:
            self.runtime_message = f'Stop unconfirmed; Bringup kept running: {error}'
            self.stopping_runtime = None

    def _system(self, message):
        with self.data.lock:
            self.data.system = self.to_dict(message)

    def _tracking(self, message):
        with self.data.lock:
            self.data.tracking = message.data[:8192]

    def _drain(self):
        while not self.commands.empty():
            request_id, payload = self.commands.get_nowait()
            if payload['command'] == 'cancel':
                self.cancel_owned()
            elif payload['command'] in ('bringup_start', 'bringup_stop'):
                try:
                    if payload['command'] == 'bringup_start':
                        self._start_runtime(payload)
                    else:
                        self._stop_runtime()
                except Exception as error:
                    self.runtime_message = str(error)
            else:
                try:
                    self._start(request_id, payload)
                except Exception as error:
                    self.data.update(request_id, state='ERROR', message=str(error))

    def _start(self, request_id, payload):
        if (self.stopping_runtime is not None
                or self.runtime and self.runtime.snapshot()['state'] == 'STOPPING'):
            raise ValueError('Bringup is stopping; no new mission can start')
        if self.data.closed or request_id in self.cancel_pending:
            self.data.update(request_id, state='CANCELED', message='Canceled before send')
            self.cancel_pending.discard(request_id)
            return
        capability = payload['capability']
        if capability == 'autoslam':
            if self.runtime and self.runtime.snapshot()['mode'] == 'navigation':
                raise ValueError('저장 지도 주행을 종료하고 지도 만들기 모드를 켜세요')
            if not self.clients['autoslam'].server_is_ready():
                raise ValueError('AutoSLAM 서버가 없습니다. 지도 만들기 모드를 먼저 켜세요')
        route = 'manager' if self.clients['manager'].server_is_ready() else 'autoslam'
        if route == 'autoslam' and capability != 'autoslam':
            raise ValueError('System manager is not available')
        if not self.clients[route].server_is_ready():
            raise ValueError(f'{route} Action server is not available')
        with self.data.lock:
            direct_active = any(
                item['route'] == 'autoslam' and item['state'] not in TERMINAL
                for key, item in self.data.requests.items() if key != request_id)
            other_active = any(
                item['state'] not in TERMINAL
                for key, item in self.data.requests.items() if key != request_id)
            system = self.data.system or {}
            manager_busy = any(system.get(key) for key in (
                'active_foreground_missions', 'pending_missions'))
        if direct_active:
            raise ValueError('Wait for this panel\'s direct AutoSLAM to finish or cancel it')
        if route == 'autoslam' and (other_active or manager_busy):
            raise ValueError('Cannot bypass an unavailable manager with unfinished missions')
        if route == 'manager':
            goal = self.mission_goal()
            goal.capability_id = capability
            goal.arguments_yaml = json.dumps(payload['arguments'])
        else:
            goal = self.auto_goal()
            goal.map_name = payload['arguments']['map_name']
        self.data.update(request_id, route=route)
        future = self.clients[route].send_goal_async(
            goal, feedback_callback=lambda msg: self._feedback(request_id, msg))
        future.add_done_callback(lambda result: self._accepted(request_id, result))

    def _feedback(self, request_id, message):
        self.data.update(request_id, feedback=self.to_dict(message.feedback))

    def _accepted(self, request_id, future):
        try:
            handle = future.result()
            if not handle.accepted:
                self.data.update(request_id, state='REJECTED', message='Goal rejected')
                self.cancel_pending.discard(request_id)
                return
            self.handles[request_id] = handle
            self.data.update(request_id, state='RUNNING')
            result = handle.get_result_async()
            result.add_done_callback(lambda value: self._finished(request_id, value))
            if request_id in self.cancel_pending:
                self._cancel(request_id, handle)
        except Exception as error:
            state = 'UNCONFIRMED' if request_id in self.handles else 'ERROR'
            self.data.update(request_id, state=state, message=str(error))

    def _finished(self, request_id, future):
        try:
            result = future.result()
            state = {4: 'SUCCEEDED', 5: 'CANCELED', 6: 'ABORTED'}.get(
                result.status, 'UNCONFIRMED')
            self.data.update(request_id, state=state, result=self.to_dict(result.result))
            if state == 'UNCONFIRMED':
                return
        except Exception as error:
            self.data.update(request_id, state='UNCONFIRMED', message=str(error))
            return
        self.handles.pop(request_id, None)
        self.cancel_pending.discard(request_id)

    def cancel_owned(self):
        """Cancel only this process's goals, including sends awaiting acceptance."""
        with self.data.lock:
            active = [key for key, item in self.data.requests.items()
                      if item['state'] not in TERMINAL]
        for request_id in active:
            self.cancel_pending.add(request_id)
            self.data.update(request_id, state='CANCELING')
            handle = self.handles.get(request_id)
            if handle is not None:
                self._cancel(request_id, handle)

    def _cancel(self, request_id, handle):
        self.data.update(request_id, state='CANCELING')
        try:
            future = handle.cancel_goal_async()
            future.add_done_callback(lambda value: self._cancel_reply(request_id, value))
        except Exception as error:
            self.data.update(request_id, message=f'Cancel send failed: {error}')

    def _cancel_reply(self, request_id, future):
        try:
            response = future.result()
            if not response.goals_canceling:
                self.data.update(request_id, message='Cancel not accepted; await result')
        except Exception as error:
            self.data.update(request_id, message=f'Cancel response failed: {error}')


class PanelServer(HTTPServer):
    """Bound HTTP workers and require a random bearer token for robot data/control."""

    def __init__(self, address, data, submit, token, catalog=None):
        """Bind the test port without granting anonymous control or data access."""
        super().__init__(address, PanelHandler)
        self.data = data
        self.submit = submit
        self.token = token
        self.catalog = catalog
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.slots = threading.BoundedSemaphore(8)

    def process_request(self, request, client_address):
        """Reject excess clients instead of allocating unbounded threads/queues."""
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        request.settimeout(5.0)
        self.pool.submit(self._process, request, client_address)

    def _process(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except (OSError, ValueError):
            pass
        finally:
            self.shutdown_request(request)
            self.slots.release()

    def server_close(self):
        """Release the listener and wait for the bounded HTTP workers."""
        super().server_close()
        self.pool.shutdown(wait=True)


class PanelHandler(BaseHTTPRequestHandler):
    """Serve the static page and a deliberately small same-origin API."""

    def log_message(self, format_string, *args):
        """Avoid logging credentials or potentially sensitive robot state."""

    def _reply(self, status, content, mime='application/json', headers=None):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; img-src 'self' blob:; "
                         "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def _authorized(self):
        provided = self.headers.get('Authorization', '')
        return hmac.compare_digest(provided.encode(), f'Bearer {self.server.token}'.encode())

    def do_GET(self):
        """GET can only display data; it never sends a robot command."""
        if self.path == '/':
            page = Path(__file__).with_name('web_panel.html').read_bytes()
            self._reply(200, page, 'text/html; charset=utf-8')
        elif not self._authorized():
            self._reply(401, {'error': 'Enter the token printed in the robot terminal'})
        elif self.path == '/api/status':
            self._reply(200, self.server.data.snapshot())
        elif self.path == '/api/maps':
            try:
                maps = self.server.catalog.list_maps() if self.server.catalog else []
                self._reply(200, {'maps': maps})
            except OSError as error:
                self._reply(503, {'error': str(error)})
        elif self.path == '/api/map':
            self._reply(200, self.server.data.map_snapshot())
        elif self.path == '/api/map/image':
            rendered = self.server.data.map_cache.png()
            if rendered is None:
                self._reply(503, {'error': 'Global Costmap has not been received'})
            else:
                self._reply(200, rendered[1], 'image/png', {
                    'X-Map-Metadata': json.dumps(rendered[0], ensure_ascii=True),
                })
        elif self.path in ('/api/image/raw', '/api/image/debug'):
            try:
                content = self.server.data.jpeg(self.path.rsplit('/', 1)[1])
                self._reply(200, content, 'image/jpeg')
            except (ValueError, RuntimeError) as error:
                self._reply(503, {'error': str(error)})
        else:
            self._reply(404, {'error': 'Not found'})

    def do_POST(self):
        """Require token, same-origin JSON and a bounded allowlisted command."""
        if not self._authorized():
            self._reply(401, {'error': 'Invalid access token'})
            return
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{self.headers.get("Host", "")}':
            self._reply(403, {'error': 'Cross-origin requests are not allowed'})
            return
        if urlsplit(self.path).path != '/api/command' or '?' in self.path:
            self._reply(404, {'error': 'Not found'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096 or self.headers.get('Content-Type') != 'application/json':
                raise ValueError('Expected bounded application/json request')
            payload = validate_command(json.loads(self.rfile.read(size)))
            request_id = self.server.submit(payload)
            self._reply(202, {'id': request_id, 'message': 'Request queued, not yet completed'})
        except (ValueError, UnicodeError) as error:
            self._reply(400, {'error': str(error)})


def main(args=None):
    """Wait for explicit web requests; never start Bringup or missions on page load."""
    import rclpy
    from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
    from rclpy.signals import SignalHandlerOptions

    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    previous_term = signal.signal(signal.SIGTERM, _terminate)
    data = PanelData()
    bridge = RosBridge(data)
    host = bridge.node.declare_parameter('host', '0.0.0.0').value
    port = bridge.node.declare_parameter('port', 8766).value
    token = secrets.token_urlsafe(24)
    server = PanelServer((host, port), data, bridge.submit, token, bridge.catalog)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    print(f'Robot test page: http://<robot-IP>:{server.server_port}', flush=True)
    print(f'Access token (enter in the page): {token}', flush=True)
    executor = SingleThreadedExecutor()
    executor.add_node(bridge.node)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        with data.lock:
            data.closed = True
        server.shutdown()
        worker.join(timeout=2.0)
        server.server_close()
        if rclpy.ok():
            bridge.cancel_owned()
            if bridge.runtime and bridge.runtime.snapshot()['state'] != 'STOPPED':
                bridge._stop_runtime()
            deadline = time.monotonic() + 31.0
            while ((bridge.stopping_runtime is not None
                    or any(item['state'] not in TERMINAL
                           for item in data.snapshot()['requests']))
                   and rclpy.ok() and time.monotonic() < deadline):
                executor.spin_once(timeout_sec=0.1)
        active = [item for item in data.snapshot()['requests'] if item['state'] not in TERMINAL]
        if active:
            print('WARNING: Action stop is unconfirmed. Check the robot and action server.',
                  flush=True)
        if bridge.runtime:
            try:
                # Graceful SIGINT still lets the owned servers clean up on exit.
                bridge.runtime.close()
            except Exception as error:
                print(f'WARNING: Owned Bringup shutdown failed: {error}', flush=True)
        executor.shutdown()
        bridge.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        signal.signal(signal.SIGTERM, previous_term)


def _terminate(signum, frame):
    raise KeyboardInterrupt
