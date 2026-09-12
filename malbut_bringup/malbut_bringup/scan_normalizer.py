"""
Publish a stable angular LaserScan without changing the hardware driver.

The source header and scan_time are retained. Rebinning does not preserve exact
per-ray acquisition times, so time_increment is explicitly zero rather than
claiming the original timing still applies. This adapter does not deskew scans.
"""

from copy import deepcopy

from malbut_bringup.scan_geometry import AngularGrid


def normalize_scan(message, grid=None):
    """Return a newly normalized message and the fixed angular grid it uses."""
    selected = grid or AngularGrid.from_metadata(
        message.angle_min, message.angle_max, message.angle_increment)
    ranges, intensities = selected.project(
        message.angle_min, message.angle_max, message.angle_increment,
        message.ranges, message.range_min, message.range_max, message.intensities)
    output = deepcopy(message)
    output.angle_min = selected.angle_min
    output.angle_max = selected.angle_max
    output.angle_increment = selected.angle_increment
    output.ranges = ranges
    output.intensities = intensities
    output.time_increment = 0.0
    return output, selected


def main(args=None):
    """Run the callback-driven adapter on the robot's raw LiDAR topic."""
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import LaserScan

    class ScanNormalizer(Node):
        def __init__(self):
            super().__init__('scan_normalizer')
            self.declare_parameter('input_topic', '/scan_raw')
            self.declare_parameter('output_topic', '/scan_normalized')
            source = self.get_parameter('input_topic').value
            destination = self.get_parameter('output_topic').value
            if self.resolve_topic_name(source) == self.resolve_topic_name(destination):
                raise ValueError('scan normalizer input and output must differ')
            self._grid = None
            self._frame = None
            self._publisher = self.create_publisher(
                LaserScan, destination, qos_profile_sensor_data)
            self._subscription = self.create_subscription(
                LaserScan, source, self._receive, qos_profile_sensor_data)

        def _receive(self, message):
            try:
                if not message.header.frame_id:
                    raise ValueError('laser frame_id is empty')
                if self._frame and message.header.frame_id != self._frame:
                    raise ValueError('laser frame changed; check duplicate publishers')
                output, grid = normalize_scan(message, self._grid)
            except ValueError as error:
                self.get_logger().warning(
                    f'Cannot normalize LaserScan: {error}', throttle_duration_sec=5.0)
                return
            if self._grid is None:
                self.get_logger().info(
                    f'Fixed laser grid: {grid.count} rays, source frame '
                    f'{message.header.frame_id}; rebinning is not motion deskewing')
            self._grid, self._frame = grid, message.header.frame_id
            self._publisher.publish(output)

    rclpy.init(args=args)
    node = ScanNormalizer()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
