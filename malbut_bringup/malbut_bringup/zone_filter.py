"""
Load the selected saved map's Zone mask into Nav2's keepout filter.

The same node keeps the selected map's rooms where patrol reads them
(``active.user-map.geojson``), so a patrol uses the rooms edited on the web (SWM25-237).
"""

import json
import math
import os
from pathlib import Path
import tempfile
import time

from nav2_msgs.srv import LoadMap
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from .user_map import load_slam_map, read_user_map, user_map_path
from .zones import (
    build_mask, COSTS, empty_mask, read_zones, RESTRICTED_MARGIN_M, write_mask, zones_path,
    ZoneError,
)


STATE_TOPIC = '/malbut/zones/state'
LOCALIZATION_STATE_TOPIC = '/malbut/localization/state'
# Patrol's room_map_file (launch_support) names this file in the default cache directory.
ACTIVE_ROOMS_FILE = 'active.user-map.geojson'
RESPONSE_TIMEOUT_S = 10.0
RETRY_DELAY_S = 5.0
# map_server answers this before its lifecycle reaches ACTIVE; the service
# exists from configure, so a slow container makes the first load land early.
ACTIVATION_RETRY_S = 1.0
RESULT_NAMES = {
    LoadMap.Response.RESULT_MAP_DOES_NOT_EXIST: 'mask file not found',
    LoadMap.Response.RESULT_INVALID_MAP_DATA: 'invalid mask image',
    LoadMap.Response.RESULT_INVALID_MAP_METADATA: 'invalid mask metadata',
}


class ZoneFilter(Node):
    """Keep the keepout mask in step with the map the system manager selected."""

    def __init__(self, **kwargs):
        super().__init__('zone_filter', **kwargs)
        self.directory = Path(self.declare_parameter(
            'cache_directory', str(Path.home() / '.ros/malbut/zones')).value).expanduser()
        self.buffer_m = float(self.declare_parameter(
            'restricted_buffer_m', RESTRICTED_MARGIN_M).value)
        if not math.isfinite(self.buffer_m) or self.buffer_m < 0:
            raise ValueError('restricted_buffer_m must be finite and not negative')
        self.client = self.create_client(LoadMap, self.declare_parameter(
            'mask_load_service', '/zone_filter_mask_server/load_map').value)
        latched = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.status = self.create_publisher(String, STATE_TOPIC, latched)
        self.create_subscription(String, LOCALIZATION_STATE_TOPIC, self._localization, latched)
        self.map_file = None
        # () means nothing loaded yet, so the first tick clears any old mask.
        self.applied = ()
        self.pending = None
        self.retry_at = 0.0
        self.last_report = None
        # () means nothing written yet, so the first tick removes any old room file.
        self.rooms_applied = ()
        self.create_timer(1.0, self._tick)

    def _localization(self, message):
        try:
            state = json.loads(message.data)
            map_file = state.get('map') if state.get('mode') == 'LOCALIZATION' else None
        except (AttributeError, ValueError):
            return
        if map_file != self.map_file:
            # Clear right away on SWITCHING so no mask outlives its map.
            self.map_file = map_file
            self._tick()

    def _target(self):
        """Identify the wanted mask: the map plus its Zone file's version."""
        if not self.map_file:
            return None
        try:
            stat = zones_path(self.map_file).stat()
            return self.map_file, stat.st_mtime_ns, stat.st_size
        except OSError:
            return self.map_file, None, None

    def _tick(self):
        self._sync_rooms()
        if self.pending is not None:
            self._finish_load()
            return
        target = self._target()
        if target == self.applied or time.monotonic() < self.retry_at:
            return
        if not self.client.service_is_ready():
            self._report('WAITING', target, 0, 'waiting for the zone mask server')
            return
        try:
            mask, report = self._write_mask(target)
        except OSError as error:
            self._report('ERROR', target, 0, f'cannot write the zone mask: {error}')
            self.retry_at = time.monotonic() + RETRY_DELAY_S
            return
        request = LoadMap.Request()
        request.map_url = str(mask)
        self.pending = (target, self.client.call_async(request), report, time.monotonic())

    def _rooms_target(self):
        """Identify the wanted room file: the map and its file versions."""
        if not self.map_file:
            return None
        stamps = []
        for path in (Path(self.map_file), user_map_path(self.map_file)):
            try:
                stat = path.stat()
                stamps.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                stamps.append(None)
        return (self.map_file, *stamps)

    def _sync_rooms(self):
        """Give patrol the selected map's rooms; none for another or a remade map."""
        target = self._rooms_target()
        if target == self.rooms_applied:
            return
        document = None
        if target is not None and target[2] is not None:
            try:
                stored = read_user_map(self.map_file)
                slam_map = load_slam_map(Path(self.map_file))
                # Rooms drawn before the map was made again do not fit its walls.
                if stored is not None and stored.get('map_revision') == slam_map.map_revision:
                    transform = slam_map.transform
                    document = {**stored, 'grid': {
                        'width': int(slam_map.image.shape[1]),
                        'height': int(slam_map.image.shape[0]),
                        'resolution': transform.resolution,
                        'origin': [transform.origin_x, transform.origin_y,
                                   transform.origin_yaw]}}
            except (OSError, KeyError, TypeError, ValueError) as error:
                self.get_logger().warning(f'Rooms: cannot read the map\'s rooms: {error}')
        output = self.directory / ACTIVE_ROOMS_FILE
        try:
            if document is None:
                output.unlink(missing_ok=True)
            else:
                _write_json(output, document)
        except OSError as error:
            self.get_logger().warning(f'Rooms: cannot update the patrol room file: {error}')
            return  # Try again on the next tick.
        self.rooms_applied = target
        if document is not None:
            count = sum((feature.get('properties') or {}).get('role') == 'room'
                        for feature in document.get('features', []))
            self.get_logger().info(f'Rooms: {count} rooms ready for patrol ({self.map_file})')

    def _write_mask(self, target):
        output = self.directory / 'zone_mask.yaml'
        map_file = target[0] if target else None
        if map_file is None:
            return empty_mask(output), ('CLEARED', None, 0, 'no saved map selected')
        try:
            features = read_zones(map_file)
            count = sum(COSTS[item['properties']['behavior']] > 0 for item in features)
            if not count:
                return empty_mask(output), ('CLEARED', target, 0, 'no zones for this map')
            mask, geometry = build_mask(map_file, features, self.buffer_m)
            return (write_mask(output, mask, geometry),
                    ('APPLIED', target, count, f'{count} zones applied'))
        except (ZoneError, KeyError, TypeError, ValueError) as error:
            # A mask from another map or a broken file must not stay active.
            return empty_mask(output), ('ERROR', target, 0, f'zones not applied: {error}')

    def _finish_load(self):
        target, future, report, started = self.pending
        if not future.done():
            if time.monotonic() - started < RESPONSE_TIMEOUT_S:
                return
            self.client.remove_pending_request(future)
            future.cancel()
            self.pending = None
            self.retry_at = time.monotonic() + RETRY_DELAY_S
            self._report('ERROR', target, 0, 'zone mask server did not respond')
            return
        self.pending = None
        try:
            result = future.result().result
        except Exception as error:  # noqa: B902 - rclpy future boundary
            result = f'no reply: {error}'
        if result == LoadMap.Response.RESULT_UNDEFINED_FAILURE:
            self.retry_at = time.monotonic() + ACTIVATION_RETRY_S
            self._report('WAITING', target, 0, 'waiting for the zone mask server to activate')
            return
        if result != LoadMap.Response.RESULT_SUCCESS:
            self.retry_at = time.monotonic() + RETRY_DELAY_S
            reason = RESULT_NAMES.get(result, f'result {result}')
            self._report('ERROR', target, 0, f'zone mask server rejected the mask: {reason}')
            return
        self.applied = target
        self._report(*report)

    def _report(self, state, target, count, message):
        report = {'state': state, 'map': target[0] if target else None,
                  'zones': count, 'message': message}
        if report == self.last_report:
            return
        self.last_report = report
        text = f'Zones: {message}' + (f' ({report["map"]})' if report['map'] else '')
        # rclpy binds a severity to each call site; one line must not switch levels.
        if state == 'ERROR':
            self.get_logger().warning(text)
        else:
            self.get_logger().info(text)
        self.status.publish(String(data=json.dumps(report)))


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=path.parent, prefix=f'.{path.name}-',
                delete=False) as stream:
            temporary = stream.name
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def main(args=None):
    """Load Zone masks; never publish velocity or change localization."""
    rclpy.init(args=args)
    node = None
    try:
        node = ZoneFilter()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
