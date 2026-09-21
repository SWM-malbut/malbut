"""Show robot data and supervise explicitly requested Bringup on a trusted LAN."""

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
import copy
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
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
from .zones import COSTS, MAX_POINTS, MAX_ZONES, read_zones, write_zones, zone_feature, ZoneError


TERMINAL = {'SUCCEEDED', 'CANCELED', 'ABORTED', 'REJECTED', 'ERROR'}
# One Bringup runs every server; stopping it cancels all of their goals.
RUNTIME_ACTIONS = ('/malbut/mission/execute', '/autoslam', '/follow_person', '/patrol',
                   '/navigate_to_pose', '/follow_path', '/spin', '/backup', '/wait',
                   '/assisted_teleop', '/relocalize')
LOAD_MAP_SERVICE = '/malbut/localization/load_map'
START_MAPPING_SERVICE = '/malbut/localization/start_mapping'
LOCALIZATION_MODES = {'MAPPING': 'mapping', 'LOCALIZATION': 'navigation'}
# manual_drive input: bounded by the vendor driver's /cmd_vel limits (m/s, m/s, rad/s).
TELEOP_TOPIC = '/cmd_vel_teleop'
TELEOP_LIMITS = {'linear_x': 0.2, 'linear_y': 0.2, 'angular_z': 0.5}
# The page repeats a held command; if it stops (closed page, lost Wi-Fi), stop once.
TELEOP_TIMEOUT_S = 0.5
ZONES_STATE_TOPIC = '/malbut/zones/state'
MAX_ZONE_REQUEST_BYTES = 64 * 1024
# One remote manual step (about 12 cm or 23 degrees), inside TELEOP_LIMITS.
NUDGES = {
    'forward': (0.15, 0.0, 0.0), 'backward': (-0.15, 0.0, 0.0),
    'left': (0.0, 0.15, 0.0), 'right': (0.0, -0.15, 0.0),
    'turn_left': (0.0, 0.0, 0.5), 'turn_right': (0.0, 0.0, -0.5),
}
NUDGE_S = 0.8
# manual_control requests manual_drive on the first input; the step starts once
# /malbut/state reports MANUAL, so a slow start does not shorten it.
NUDGE_START_TIMEOUT_S = 3.0
# RViz's 2D Pose Estimate spread: 0.5 m and about 15 degrees.
GIVEN_POSE_COVARIANCE = [0.0] * 36
GIVEN_POSE_COVARIANCE[0] = GIVEN_POSE_COVARIANCE[7] = 0.25
GIVEN_POSE_COVARIANCE[35] = 0.0685
DEBUG_ARGUMENT_BYTES = 8 * 1024
DIAGNOSTIC_TOPICS = (
    '/scan_raw', '/odom', '/tf', '/map', '/cmd_vel', '/cmd_vel_pre_collision',
    '/cmd_vel_teleop', '/amcl_pose', '/initialpose', '/malbut/state',
    '/malbut/localization/state', '/malbut/zones/state', '/malbut/bringup/status',
    '/depth_cam/rgb0/image_raw', '/perception/person/debug_image/compressed',
)


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
    if payload.get('command') == 'nudge':
        if set(payload) != {'command', 'direction'} or payload['direction'] not in (
                *NUDGES, 'stop'):
            raise ValueError('Manual step needs one of: ' + ', '.join((*NUDGES, 'stop')))
        return payload
    if payload.get('command') == 'debug_start':
        # Any registered capability; the manager validates fields against its manifest.
        if (set(payload) != {'command', 'capability', 'arguments'}
                or not isinstance(payload['capability'], str)
                or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', payload['capability'])
                or not isinstance(payload['arguments'], dict)):
            raise ValueError('Debug mission needs a capability ID and an arguments object')
        try:
            size = len(json.dumps(payload['arguments'], allow_nan=False).encode('utf-8'))
        except (TypeError, ValueError) as error:
            raise ValueError('Debug arguments must be finite JSON') from error
        if size > DEBUG_ARGUMENT_BYTES:
            raise ValueError(f'Debug arguments exceed {DEBUG_ARGUMENT_BYTES} bytes')
        return payload
    if payload.get('command') == 'teleop':
        if set(payload) != {'command', *TELEOP_LIMITS}:
            raise ValueError('Teleop requires linear_x, linear_y and angular_z')
        for key, limit in TELEOP_LIMITS.items():
            value = payload[key]
            if (type(value) not in (float, int) or not math.isfinite(value)
                    or abs(value) > limit):
                raise ValueError(f'{key} must be finite and within ±{limit}')
        return payload
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
                or distance < 0.2):
            raise ValueError('desired_distance_m must be finite and at least 0.2 m')
    elif capability == 'patrol':
        if (set(args) != {'thoroughness'} or type(args['thoroughness']) is not int
                or args['thoroughness'] not in (0, 1, 2)):
            raise ValueError('thoroughness must be 0, 1 or 2')
    elif capability == 'navigate_to_pose':
        if (set(args) != {'x', 'y', 'yaw'}
                or any(type(args[key]) not in (float, int)
                       or not math.isfinite(args[key]) for key in args)):
            raise ValueError('Navigation requires finite x, y and yaw in map coordinates')
    elif capability == 'manual_drive':
        if args:
            raise ValueError('manual_drive uses the registered time limit; send no arguments')
    elif capability == 'relocalize':
        method = args.get('method')
        if type(method) is not int or method not in (0, 1, 2):
            raise ValueError('method must be 0 (saved pose first), 1 (given pose) or 2 (search)')
        pose = {'x', 'y', 'yaw'} if method == 1 else set()
        if (set(args) != {'method', *pose}
                or any(type(args[key]) not in (float, int)
                       or not math.isfinite(args[key]) for key in pose)):
            raise ValueError('A given pose needs finite x, y and yaw in map coordinates')
    else:
        raise ValueError('Unknown capability')
    return payload


def live_zone_map(runtime, catalog):
    """Return the saved map in use when this panel may edit its Zones."""
    localization = runtime.get('localization') or {}
    if localization.get('mode') != 'LOCALIZATION' or not localization.get('map'):
        raise ValueError('Zones belong to a saved map; drive on a saved map first')
    if catalog is None:
        raise ValueError('This panel has no map directory')
    path = Path(localization['map'])
    if catalog.resolve(path.name) != path.resolve():
        raise ValueError('The saved map in use is outside this panel\'s map directory')
    return path.resolve()


def zone_view(runtime, catalog):
    """Describe the Zones of the saved map in use as editable polygons."""
    try:
        path = live_zone_map(runtime, catalog)
    except ValueError as error:
        return {'map': None, 'editable': False, 'zones': [], 'message': str(error)}
    try:
        features, message = read_zones(path), ''
    except ZoneError as error:
        features, message = [], f'{error}; applying replaces it'
    zones = [{'behavior': item['properties']['behavior'],
              'name': item['properties'].get('name', ''),
              'points': [point[:2] for point in item['geometry']['coordinates'][0][:-1]]}
             for item in features]
    return {'map': path.name, 'editable': True, 'zones': zones, 'message': message}


def save_zones(runtime, catalog, payload):
    """Validate the editor's polygons and replace the map's Zone file."""
    if not isinstance(payload, dict) or set(payload) != {'map', 'zones'}:
        raise ValueError('Expected map and zones')
    path = live_zone_map(runtime, catalog)
    if payload['map'] != path.name:
        raise ValueError('The saved map in use changed; reload the Zones')
    zones = payload['zones']
    if not isinstance(zones, list) or len(zones) > MAX_ZONES:
        raise ValueError(f'Use at most {MAX_ZONES} Zones')
    features = []
    for zone in zones:
        if (not isinstance(zone, dict) or not {'behavior', 'points'} <= set(zone)
                or set(zone) - {'behavior', 'points', 'name'}
                or zone['behavior'] not in COSTS):
            raise ValueError('Each Zone needs a behavior and points')
        points, name = zone['points'], zone.get('name', '')
        if (not isinstance(points, list) or not 3 <= len(points) <= MAX_POINTS
                or not all(isinstance(point, list) and len(point) == 2
                           and all(type(value) in (int, float) and math.isfinite(value)
                                   for value in point) for point in points)):
            raise ValueError(f'Zone corners must be 3 to {MAX_POINTS} finite [x, y] points')
        if not isinstance(name, str) or len(name) > 64:
            raise ValueError('Zone names are at most 64 characters')
        features.append(zone_feature(zone['behavior'], points, name))
    write_zones(path, features)
    return len(features)


def _map_pose(arguments):
    return {
        'position': {'x': float(arguments['x']), 'y': float(arguments['y']), 'z': 0.0},
        'orientation': {'x': 0.0, 'y': 0.0,
                        'z': math.sin(arguments['yaw'] / 2.0),
                        'w': math.cos(arguments['yaw'] / 2.0)},
    }


def mission_arguments(capability, arguments):
    """Translate a bounded map-coordinate request to the public Goal fields."""
    if capability == 'navigate_to_pose':
        return {
            'pose': {'header': {'frame_id': 'map'}, 'pose': _map_pose(arguments)},
            'behavior_tree': '',
        }
    if capability == 'relocalize' and arguments['method'] == 1:
        return {'method': 1, 'initial_pose': {
            'header': {'frame_id': 'map'},
            'pose': {'pose': _map_pose(arguments), 'covariance': GIVEN_POSE_COVARIANCE},
        }}
    return arguments


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

    def __init__(self, *, map_palette='costmap'):
        """Initialize bounded in-memory state without subscriptions or commands."""
        self.lock = threading.RLock()
        self.encode_lock = threading.Lock()
        self.requests = OrderedDict()
        self.servers = {'manager': False, 'autoslam': False}
        self.system = None
        self.tracking = None
        self.zones = None
        self.manual = {'state': 'IDLE', 'direction': None, 'message': ''}
        self.frames = {}
        self.encoded = (None, b'')
        self.map_cache = MapCache(palette=map_palette)
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
                'runtime': self.runtime, 'zones': self.zones, 'manual': self.manual,
                'video_age_s': {key: round(time.monotonic() - frame[0], 1)
                                for key, frame in self.frames.items()},
            })


class RosBridge:
    """Own Action handles and execute all ROS commands on the ROS executor."""

    def __init__(self, data, *, node_name='robot_web_panel',
                 map_topic='/global_costmap/costmap'):
        """Subscribe to diagnostics/images and prepare nonblocking Action clients."""
        from malbut_interfaces.action import AutoSlam, ExecuteMission
        from malbut_interfaces.msg import SystemState
        from action_msgs.msg import GoalStatusArray
        from action_msgs.srv import CancelGoal
        from geometry_msgs.msg import Twist
        from nav2_msgs.srv import LoadMap
        from std_srvs.srv import Trigger
        from nav_msgs.msg import OccupancyGrid
        from rclpy.action import ActionClient
        from rclpy.node import Node
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from rosidl_runtime_py.convert import message_to_ordereddict
        from sensor_msgs.msg import CompressedImage, Image
        from std_msgs.msg import String
        from tf2_ros import Buffer, TransformListener

        self.node = Node(node_name)
        self.data = data
        self.commands = queue.Queue(maxsize=64)
        self.handles = {}
        self.cancel_pending = set()
        self.auto_goal = AutoSlam.Goal
        self.mission_goal = ExecuteMission.Goal
        self.to_dict = message_to_ordereddict
        self.twist = Twist
        self.teleop_publisher = self.node.create_publisher(Twist, TELEOP_TOPIC, 10)
        self.teleop_received = None
        self.nudge = None
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
        self.speech_ready = False
        self.stopping_runtime = None
        self.action_status = {}
        self.cancel_request = CancelGoal.Request
        self.cancel_clients = {
            name: self.node.create_client(CancelGoal, name + '/_action/cancel_goal')
            for name in RUNTIME_ACTIONS
        }
        self.load_map = self.node.create_client(LoadMap, LOAD_MAP_SERVICE)
        self.load_map_request = LoadMap.Request
        self.start_mapping = self.node.create_client(Trigger, START_MAPPING_SERVICE)
        self.start_mapping_request = Trigger.Request
        self.localization = {}
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self.node)
        self.robot_frame = self.node.declare_parameter('robot_frame', 'base_footprint').value
        sensor_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        topics = {
            'rgb_topic': '/depth_cam/rgb0/image_raw',
            'debug_topic': '/perception/person/debug_image/compressed',
            'map_topic': map_topic,
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
                String, '/malbut/speech/status', self._speech_status,
                QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)),
            self.node.create_subscription(
                SystemState, '/malbut/state', self._system,
                QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)),
            self.node.create_subscription(
                String, '/malbut/localization/state', self._localization,
                QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)),
            self.node.create_subscription(
                String, ZONES_STATE_TOPIC, self._zones,
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
        self.teleop_timer = self.node.create_timer(0.1, self._teleop_watchdog)
        self._refresh()

    def submit(self, payload):
        """Queue authenticated commands without blocking an HTTP worker on DDS."""
        payload = validate_command(payload)
        request_id = None
        if payload['command'] in ('start', 'debug_start'):
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
        # The running mode is the manager's localization, not the start request.
        mode = LOCALIZATION_MODES.get(self.localization.get('mode'))
        if mode and status['state'] in ('STARTING', 'RUNNING') or not self.runtime:
            status['mode'] = mode
            status['map'] = (Path(self.localization['map']).name
                             if mode == 'navigation' and self.localization.get('map') else None)
        status['localization'] = dict(self.localization)
        with self.data.lock:
            booting = (self.data.system or {}).get('system_state', 0) == 0
        server_ready = bool(self.data.servers['manager']) and not booting
        if self.runtime:
            if status['state'] not in ('STARTING', 'RUNNING'):
                self.speech_ready = False
            status['ready'] = (status['state'] == 'RUNNING'
                               and server_ready and self.speech_ready
                               and bool(self.node.count_publishers('/malbut/speech/status')))
        else:
            status['ready'] = server_ready
        status['waiting'] = []
        if (self.runtime and status['state'] == 'RUNNING'
                and not status['ready']):
            if server_ready:
                status['waiting'] = ['speech: microphone startup']
                status['message'] = '음성 모델·마이크 준비 대기'
            else:
                status['waiting'] = self.startup_status.get('missing', [])
                status['message'] = ('필수 입력 준비 대기' if status['waiting']
                                     else 'Action 서버 준비 대기')
        if self.runtime_message:
            status['message'] = self.runtime_message
        if self.stopping_runtime is not None:
            status.update(state='STOPPING', ready=False,
                          message='Waiting for Action cancellation to finish')
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

    def _speech_status(self, message):
        if (self.runtime and self.stopping_runtime is None
                and self.runtime.snapshot()['state'] in ('STARTING', 'RUNNING')):
            self.speech_ready = message.data == 'ready'

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

    def _localization(self, message):
        try:
            state = json.loads(message.data)
        except ValueError:
            return
        if isinstance(state, dict):
            self.localization = state

    def _zones(self, message):
        try:
            state = json.loads(message.data)
        except ValueError:
            return
        if isinstance(state, dict):
            with self.data.lock:
                self.data.zones = state

    def _start_runtime(self, payload):
        if self.stopping_runtime is not None:
            raise ValueError('Wait for Bringup shutdown to finish')
        if self.load_map.service_is_ready() and self.start_mapping.service_is_ready():
            # Bringup is already running: switch localization, never relaunch.
            self._switch_localization(payload)
            return
        if self.runtime is None:
            raise ValueError('Start a standalone robot_web_panel to control Bringup')
        names = {name for name, _ in self.node.get_node_names_and_namespaces()}
        conflicts = names.intersection({
            'amcl', 'map_server', 'slam_toolbox', 'controller_server', 'planner_server',
            'bt_navigator', 'nav2_container', 'system_manager', 'autoslam',
            'person_follower', 'person_localizer', 'person_reidentifier', 'yolo_node',
            'malbut_stt', 'malbut_tts', 'malbut_agent_communication',
        })
        if os.environ.get('HOMECAM_BACKEND_URL', '').strip():
            conflicts.update(names.intersection({'homecam_media_agent'}))
        if conflicts or self.node.count_publishers(self.topics['map_topic']):
            raise ValueError('Stop existing mapping/navigation/perception/media/speech first: '
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
        self.speech_ready = False
        self.localization = {}
        with self.data.lock:
            self.data.map_cache.clear()
            self.data.map_active = False
            self.data.robot_pose = None
            self.data.system = None
            self.data.tracking = None
            self.data.frames.clear()

    def _switch_localization(self, payload):
        if payload['mode'] == 'mapping':
            future = self.start_mapping.call_async(self.start_mapping_request())
        else:
            request = self.load_map_request()
            request.map_url = str(self.catalog.resolve(payload['map']))
            future = self.load_map.call_async(request)
        self.runtime_message = 'Switching localization; missions using the base must be stopped'
        future.add_done_callback(lambda done: self._switched(payload, done))

    def _switched(self, payload, future):
        try:
            response = future.result()
        except Exception as error:
            self.runtime_message = f'Localization switch failed: {error}'
            return
        if payload['mode'] == 'mapping':
            ok, message = response.success, response.message
        else:
            # The manager's localization message tells whether the pose was found.
            ok = response.result == 0
            message = ('Saved map loaded' if ok else
                       f'Map was not loaded (result {response.result}); '
                       'cancel base missions or check the map')
        self.runtime_message = message if ok else f'Localization switch failed: {message}'

    def _stop_runtime(self):
        if self.runtime is None or self.runtime.snapshot()['state'] == 'STOPPED':
            raise ValueError('This panel has no running Bringup to stop')
        if self.stopping_runtime is not None:
            return
        self.speech_ready = False
        self.cancel_owned()
        names = RUNTIME_ACTIONS
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

    def call(self, function, timeout=3.0):
        """Run a read-only query on the ROS executor and wait for its result."""
        future = Future()
        try:
            self.commands.put_nowait((None, {'command': 'call', 'function': function,
                                             'future': future}))
        except queue.Full:
            raise ValueError('Command queue full') from None
        self.guard.trigger()
        return future.result(timeout=timeout)

    def diagnostics(self):
        """Describe the ROS graph and runtime state for remote debugging."""
        node = self.node
        with self.data.lock:
            data = copy.deepcopy({
                'servers': self.data.servers, 'system': self.data.system,
                'zones': self.data.zones, 'manual': self.data.manual,
                # Local log files stay on the robot, as in the state upload.
                'runtime': {key: value for key, value in self.data.runtime.items()
                            if key not in ('log_path', 'log_tail')}})
        return {
            'nodes': sorted(namespace.rstrip('/') + '/' + name
                            for name, namespace in node.get_node_names_and_namespaces())[:256],
            'topics': {topic: {'publishers': node.count_publishers(topic),
                               'subscribers': node.count_subscribers(topic)}
                       for topic in DIAGNOSTIC_TOPICS},
            'actions': {name: client.service_is_ready()
                        for name, client in self.cancel_clients.items()},
            'localization': dict(self.localization),
            'startup': dict(self.startup_status), 'speech_ready': self.speech_ready,
            'pose': self._robot_pose(),
            'map': {key: value for key, value in self.data.map_snapshot().items()
                    if key != 'pose'},
            **data,
        }

    def _drain(self):
        while not self.commands.empty():
            request_id, payload = self.commands.get_nowait()
            if payload['command'] == 'call':
                try:
                    payload['future'].set_result(payload['function']())
                except Exception as error:
                    payload['future'].set_exception(error)
            elif payload['command'] == 'cancel':
                self.cancel_owned()
            elif payload['command'] == 'teleop':
                self._teleop(payload)
            elif payload['command'] == 'nudge':
                self._nudge(payload['direction'])
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

    def _teleop(self, payload):
        """Forward one web command; AssistedTeleop keeps the latest input."""
        self.nudge = None
        message = self.twist()
        message.linear.x = float(payload['linear_x'])
        message.linear.y = float(payload['linear_y'])
        message.angular.z = float(payload['angular_z'])
        moving = any(payload[key] for key in TELEOP_LIMITS)
        self.teleop_received = time.monotonic() if moving else None
        self.teleop_publisher.publish(message)

    def _nudge(self, direction):
        """Start one bounded step, or stop the current one."""
        if direction == 'stop':
            self.stop_teleop()
            self._manual_state('IDLE', None, 'Stopped')
            return
        self.nudge = {'direction': direction, 'requested': time.monotonic(), 'moving': None}
        self._manual_state('STARTING', direction, 'Waiting for manual control')

    def _manual_state(self, state, direction, message):
        with self.data.lock:
            self.data.manual = {'state': state, 'direction': direction, 'message': message}

    def _step_nudge(self):
        nudge, now = self.nudge, time.monotonic()
        with self.data.lock:
            manual = (self.data.system or {}).get('control_mode') == 1  # SystemState.MANUAL
        if nudge['moving'] is None and manual:
            nudge['moving'] = now
            self._manual_state('MOVING', nudge['direction'], 'Moving one step')
        if nudge['moving'] is None and now - nudge['requested'] > NUDGE_START_TIMEOUT_S:
            self.stop_teleop()
            self._manual_state('IDLE', None, 'Manual control did not start; '
                               'the manager may be switching localization')
            return
        if nudge['moving'] is not None and now - nudge['moving'] >= NUDGE_S:
            self.stop_teleop()
            self._manual_state('IDLE', None, 'Step finished')
            return
        # Before MANUAL this only wakes manual_control; AssistedTeleop is not running.
        message = self.twist()
        (message.linear.x, message.linear.y,
         message.angular.z) = NUDGES[nudge['direction']]
        self.teleop_publisher.publish(message)

    def _teleop_watchdog(self):
        """Stop once when a held web command is no longer repeated."""
        if self.nudge is not None:
            self._step_nudge()
        elif (self.teleop_received is not None
                and time.monotonic() - self.teleop_received > TELEOP_TIMEOUT_S):
            self.stop_teleop()

    def stop_teleop(self):
        """Publish zero if this panel last commanded motion."""
        if self.teleop_received is not None or self.nudge is not None:
            self.teleop_received = None
            self.nudge = None
            self.teleop_publisher.publish(self.twist())

    def _start(self, request_id, payload):
        if (self.stopping_runtime is not None
                or self.runtime and self.runtime.snapshot()['state'] == 'STOPPING'):
            raise ValueError('Bringup is stopping; no new mission can start')
        if self.data.closed or request_id in self.cancel_pending:
            self.data.update(request_id, state='CANCELED', message='Canceled before send')
            self.cancel_pending.discard(request_id)
            return
        capability = payload['capability']
        if capability == 'navigate_to_pose':
            info = self.data.map_snapshot()
            if (not info.get('active') or info.get('frame_id') != 'map'
                    or self._robot_pose() is None):
                raise ValueError('Navigation requires a live map and current robot pose')
        if capability == 'autoslam' and not self.clients['autoslam'].server_is_ready():
            raise ValueError('AutoSLAM 서버가 없습니다. Bringup 준비를 확인하세요')
        route = 'manager' if self.clients['manager'].server_is_ready() else 'autoslam'
        raw = payload['command'] == 'debug_start'
        if route == 'autoslam' and (raw or capability != 'autoslam'):
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
            goal.arguments_yaml = json.dumps(payload['arguments'] if raw else mission_arguments(
                capability, payload['arguments']))
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
            # Losing the acceptance reply does not prove the goal was rejected.
            self.data.update(request_id, state='UNCONFIRMED', message=str(error))

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
        elif self.path == '/api/zones':
            self._reply(200, zone_view(self.server.data.snapshot()['runtime'],
                                       self.server.catalog))
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
        path = urlsplit(self.path).path
        limits = {'/api/command': 4096, '/api/zones': MAX_ZONE_REQUEST_BYTES}
        if path not in limits or '?' in self.path:
            self._reply(404, {'error': 'Not found'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if (not 0 < size <= limits[path]
                    or self.headers.get('Content-Type') != 'application/json'):
                raise ValueError('Expected bounded application/json request')
            payload = json.loads(self.rfile.read(size))
            if path == '/api/zones':
                # A file edit, not a robot command; zone_filter applies it to Nav2.
                count = save_zones(self.server.data.snapshot()['runtime'],
                                   self.server.catalog, payload)
                self._reply(200, {'saved': count, 'message': 'Zones saved; applying to Nav2'})
                return
            request_id = self.server.submit(validate_command(payload))
            self._reply(202, {'id': request_id, 'message': 'Request queued, not yet completed'})
        except (ValueError, UnicodeError) as error:
            self._reply(400, {'error': str(error)})
        except OSError as error:
            self._reply(503, {'error': f'Cannot save Zones: {error}'})


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
            bridge.stop_teleop()
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
