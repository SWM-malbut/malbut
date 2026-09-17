"""Cache native OccupancyGrid previews for the LAN costmap and cloud map."""

import math
import operator
import threading


def world_to_grid(x, y, resolution, origin):
    """Return continuous grid coordinates, measured from the lower-left corner."""
    dx, dy = x - origin['x'], y - origin['y']
    cosine, sine = math.cos(origin['yaw']), math.sin(origin['yaw'])
    return ((cosine * dx + sine * dy) / resolution,
            (-sine * dx + cosine * dy) / resolution)


def grid_to_world(column, row, resolution, origin):
    """Transform continuous grid coordinates, including cell centers, to world XY."""
    x, y = column * resolution, row * resolution
    cosine, sine = math.cos(origin['yaw']), math.sin(origin['yaw'])
    return (origin['x'] + cosine * x - sine * y,
            origin['y'] + sine * x + cosine * y)


def _metadata(message):
    import numpy as np

    info = message.info
    try:
        if isinstance(info.width, bool) or isinstance(info.height, bool):
            raise TypeError
        width, height = operator.index(info.width), operator.index(info.height)
    except TypeError as exc:
        raise ValueError('Map dimensions must be integers') from exc
    if width <= 0 or height <= 0 or len(message.data) != width * height:
        raise ValueError('Invalid map dimensions or data length')
    resolution = float(info.resolution)
    if not math.isfinite(resolution) or resolution <= 0:
        raise ValueError('Map resolution must be positive and finite')
    position, orientation = info.origin.position, info.origin.orientation
    xyz = tuple(float(getattr(position, axis)) for axis in ('x', 'y', 'z'))
    quaternion = tuple(float(getattr(orientation, axis)) for axis in ('x', 'y', 'z', 'w'))
    if not all(math.isfinite(value) for value in (*xyz, *quaternion)):
        raise ValueError('Map origin must be finite')
    qx, qy, qz, qw = quaternion
    # Numerical tolerance only: the browser does not represent roll or pitch.
    if (not math.isclose(sum(value * value for value in quaternion), 1.0,
                         rel_tol=1e-6, abs_tol=1e-6)
            or abs(qx) > 1e-6 or abs(qy) > 1e-6):
        raise ValueError('Map origin must have a unit, planar quaternion')
    if not isinstance(message.header.frame_id, str) or not message.header.frame_id.strip():
        raise ValueError('Map frame_id is required')
    cells = np.asarray(message.data)
    if (cells.ndim != 1 or cells.dtype.kind not in 'iu'
            or np.any(cells < -1) or np.any(cells > 100)):
        raise ValueError('Occupancy values must be integers from -1 to 100')
    return {
        'width': width, 'height': height, 'resolution': resolution,
        'origin': {'x': xyz[0], 'y': xyz[1],
                   'yaw': math.atan2(2 * qw * qz, 1 - 2 * qz * qz)},
        'frame_id': message.header.frame_id,
    }


def _encode_png(message, palette='costmap'):
    import cv2
    import numpy as np

    cells = np.asarray(message.data).reshape(message.info.height, message.info.width)
    if palette == 'map':
        # Match the existing user-facing live map (map_lifecycle.render_map_png)
        # without importing Gazebo code or altering native grid geometry/cells.
        pixels = np.full((*cells.shape, 3), (247, 242, 247), dtype=np.uint8)
        pixels[(cells >= 0) & (cells <= 19)] = (255, 255, 255)
        pixels[cells >= 65] = (39, 31, 25)
        pixels[(cells > 19) & (cells < 65)] = (205, 205, 205)
    else:
        # Match RViz Humble's makeCostmapPalette; OpenCV encodes BGRA, not RGBA.
        # ros2/rviz: rviz_default_plugins/displays/map/palette_builder.cpp
        colors = np.zeros((102, 4), dtype=np.uint8)
        red = np.arange(1, 99) * 255 // 100
        colors[1:99, 0] = 255 - red
        colors[1:99, 2] = red
        colors[1:99, 3] = 255
        colors[99] = (255, 255, 0, 255)  # Inscribed obstacle: cyan.
        colors[100] = (255, 0, 255, 255)  # Lethal obstacle: magenta.
        colors[101] = (134, 137, 112, 255)  # Unknown; free cells stay transparent.
        pixels = colors[np.where(cells == -1, 101, cells)]
    # OccupancyGrid row zero is at the bottom; PNG row zero is at the top.
    success, encoded = cv2.imencode('.png', np.ascontiguousarray(pixels[::-1]))
    if not success:
        raise ValueError('Map PNG encoding failed')
    return encoded.tobytes()


def _pose(pose):
    if pose is None:
        return None
    try:
        result = {axis: float(pose[axis]) for axis in ('x', 'y', 'yaw')}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Map pose requires finite x, y and yaw') from exc
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError('Map pose requires finite x, y and yaw')
    return result


class MapCache:
    """Keep the latest map and one PNG, without expiring a static map by age."""

    def __init__(self, *, palette='costmap'):
        """Choose display colors without changing the source grid or its scale."""
        if palette not in ('costmap', 'map'):
            raise ValueError('Map palette must be costmap or map')
        self._palette = palette
        self._lock = threading.Lock()
        self._encode_lock = threading.Lock()
        self._version = 0
        self._message = None
        self._metadata = None
        self._png = None

    def update(self, message):
        """Validate and retain a message; callers must not mutate it afterward."""
        metadata = _metadata(message)
        with self._lock:
            self._version += 1
            self._message = message
            self._metadata = metadata
            self._png = None
            return self._version

    def clear(self):
        """Forget a previous runtime's map without ever reusing its version."""
        with self._lock:
            self._version += 1
            self._message = None
            self._metadata = None
            self._png = None

    def snapshot(self, *, active, pose=None):
        """Report publisher presence separately from cached map availability."""
        pose = _pose(pose)
        with self._lock:
            available = self._message is not None
            result = {'available': available, 'active': bool(active),
                      'version': self._version, 'pose': pose if available else None,
                      'pose_available': available and pose is not None}
            if available:
                result.update(self._metadata)
                result['origin'] = dict(self._metadata['origin'])
            return result

    def png(self):
        """Return one consistent geometry/PNG pair, even if a new map arrives."""
        with self._encode_lock:
            with self._lock:
                current = self._version
                if self._message is None:
                    return None
                if self._png is not None:
                    return self._png
                message = self._message
                metadata = {**self._metadata, 'version': current}
                metadata['origin'] = dict(metadata['origin'])
            # Slow encoding must not block the ROS callback or metadata readers.
            result = (metadata, _encode_png(message, self._palette))
            with self._lock:
                if self._version == current:
                    self._png = result
            return result
