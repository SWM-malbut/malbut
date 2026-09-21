"""Load the selected saved map's Zone mask into Nav2's keepout filter."""

import json
import math
from pathlib import Path
import time

from nav2_msgs.srv import LoadMap
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from .zones import build_mask, COSTS, empty_mask, read_zones, write_mask, zones_path, ZoneError


STATE_TOPIC = '/malbut/zones/state'
LOCALIZATION_STATE_TOPIC = '/malbut/localization/state'
RESPONSE_TIMEOUT_S = 10.0
RETRY_DELAY_S = 5.0


class ZoneFilter(Node):
    """Keep the keepout mask in step with the map the system manager selected."""

    def __init__(self, **kwargs):
        super().__init__('zone_filter', **kwargs)
        self.directory = Path(self.declare_parameter(
            'cache_directory', str(Path.home() / '.ros/malbut/zones')).value).expanduser()
        self.buffer_m = float(self.declare_parameter('restricted_buffer_m', 0.20).value)
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
            loaded = future.result().result == LoadMap.Response.RESULT_SUCCESS
        except Exception:  # noqa: B902 - rclpy future boundary
            loaded = False
        if not loaded:
            self.retry_at = time.monotonic() + RETRY_DELAY_S
            self._report('ERROR', target, 0, 'zone mask server rejected the mask')
            return
        self.applied = target
        self._report(*report)

    def _report(self, state, target, count, message):
        report = {'state': state, 'map': target[0] if target else None,
                  'zones': count, 'message': message}
        if report == self.last_report:
            return
        self.last_report = report
        (self.get_logger().warning if state == 'ERROR' else self.get_logger().info)(
            f'Zones: {message}' + (f' ({report["map"]})' if report['map'] else ''))
        self.status.publish(String(data=json.dumps(report)))


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
