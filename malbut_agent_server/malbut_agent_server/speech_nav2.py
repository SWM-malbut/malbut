"""Bound speech arguments to existing Nav2 Actions, never publish velocity."""

import math

POINT_SCHEMA = {
    'type': 'object',
    'properties': {
        'location': {'type': ['string', 'null'], 'maxLength': 128},
        'x': {'type': ['number', 'null'], 'minimum': -10000, 'maximum': 10000},
        'y': {'type': ['number', 'null'], 'minimum': -10000, 'maximum': 10000},
        'yaw_deg': {'type': ['number', 'null'], 'minimum': -180, 'maximum': 180},
    },
    'required': ['location', 'x', 'y', 'yaw_deg'],
    'additionalProperties': False,
}
NAV2_TOOL_DEFINITIONS = {
    'request_rotation': (
        'Request relative in-place rotation via Manager Nav2 Spin (left/right, angle_deg 1-360; '
        'use requested angle, otherwise choose an appropriate angle from request/RGB/map context; '
        'server owns speed/timeout; not absolute map heading).',
        {'direction': {'type': 'string', 'enum': ['left', 'right']},
         'angle_deg': {'type': 'number', 'minimum': 1, 'maximum': 360}},
    ),
    'request_backup': (
        'Request straight backward motion via Manager Nav2 BackUp (distance_m 0.05-1.0; '
        'use requested distance, otherwise choose a modest distance conservatively since front RGB '
        'does not show rear clearance; server owns speed/timeout).',
        {'distance_m': {'type': 'number', 'minimum': 0.05, 'maximum': 1}},
    ),
    'request_navigation_point': (
        'Request one map-frame point via Manager NavigateToPose. Copy map_revision from scene context; '
        'x/y in metres, optional yaw_deg (null=zero heading). Use explicit coords or choose or calculate '
        'an approximate goal from pose, landmarks, and RGB cues grounded in map coords (never pixels as metres). '
        'Prefer named navigation for registered rooms.',
        {'map_revision': {'type': 'string', 'maxLength': 128},
         'x': {'type': 'number', 'minimum': -10000, 'maximum': 10000},
         'y': {'type': 'number', 'minimum': -10000, 'maximum': 10000},
         'yaw_deg': {'type': ['number', 'null'], 'minimum': -180, 'maximum': 180}},
    ),
    'request_waypoints': (
        'Visit ordered waypoints in ONE Manager Nav2 FollowWaypoints mission. Copy map_revision from scene context. '
        'Each item: either exact registered location with x/y/yaw_deg null, OR location null with map x/y '
        'and optional yaw_deg (explicit or chosen using the current pose, landmarks, and RGB cues). '
        'Single traversal, stops on first failure; no loops or hidden tasks.',
        {'map_revision': {'type': 'string', 'maxLength': 128},
         'waypoints': {'type': 'array', 'minItems': 1, 'maxItems': 16,
                       'items': POINT_SCHEMA}},
    ),
}
NAV2_CAPABILITIES = {
    'request_rotation': 'spin',
    'request_backup': 'backup',
    'request_navigation_point': 'navigate_to_pose',
    'request_waypoints': 'follow_waypoints',
}
MAP_TOOLS = frozenset({'request_navigation_point', 'request_waypoints'})


def duration(seconds):
    """Build a finite server-owned builtin Duration."""
    return {'sec': math.ceil(seconds), 'nanosec': 0}


def motion_arguments(tool, arguments):
    """Convert bounded human units without giving the model ROS transport knobs."""
    if tool == 'request_rotation':
        angle = math.radians(arguments['angle_deg'])
        if arguments['direction'] == 'right':
            angle = -angle
        return {'target_yaw': angle, 'time_allowance': duration(abs(angle) / 0.4 * 2 + 3)}
    if tool == 'request_backup':
        distance = float(arguments['distance_m'])
        return {'target': {'x': distance, 'y': 0.0, 'z': 0.0}, 'speed': 0.1,
                'time_allowance': duration(distance / 0.1 * 2 + 3)}
    raise ValueError('unsupported relative motion')


def map_arguments(tool, arguments, targets, active_map):
    """Resolve coordinates against the same saved-map revision used by the model."""
    from malbut_agent_server.speech_navigation import NavigationTargetError

    scene = targets.scene(active_map)
    if arguments['map_revision'] != scene['revision']:
        raise NavigationTargetError('map_changed')
    if tool == 'request_navigation_point':
        target = targets.resolve_point(
            arguments['x'], arguments['y'], arguments['yaw_deg'], active_map)
        return target.arguments, scene['revision']
    poses = []
    for point in arguments['waypoints']:
        if point['location'] is not None:
            if any(point[name] is not None for name in ('x', 'y', 'yaw_deg')):
                raise ValueError('named waypoint must not contain coordinates')
            target = targets.resolve(point['location'], active_map)
        else:
            if point['x'] is None or point['y'] is None:
                raise ValueError('coordinate waypoint requires x and y')
            target = targets.resolve_point(point['x'], point['y'], point['yaw_deg'], active_map)
        poses.append(target.arguments['pose'])
    # Detect file/map edits while the list was being resolved as well.
    if targets.scene(active_map)['revision'] != scene['revision']:
        raise NavigationTargetError('map_changed')
    return {'poses': poses}, scene['revision']
