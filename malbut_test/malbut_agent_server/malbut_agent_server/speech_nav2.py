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
        'Request one relative in-place rotation through Manager and Nav2 Spin. '
        'Use direction left/right and a positive angle_deg; one full turn is 360 degrees. '
        'Use the requested angle when specified; otherwise choose an appropriate '
        'approximate angle from the current request, RGB scene and robot/map context. '
        'Speed and timeout are server-owned. This is not navigation to a map heading.',
        {'direction': {'type': 'string', 'enum': ['left', 'right']},
         'angle_deg': {'type': 'number', 'minimum': 1, 'maximum': 360}},
    ),
    'request_backup': (
        'Request straight backward motion through Manager and Nav2 BackUp. '
        'distance_m is between 0.05 and 1 metre. Use the requested distance when specified; '
        'otherwise choose a modest distance from the current request and scene context. '
        'A front RGB image does not establish rear clearance, so keep estimates conservative. '
        'Speed and timeout are server-owned.',
        {'distance_m': {'type': 'number', 'minimum': 0.05, 'maximum': 1}},
    ),
    'request_navigation_point': (
        'Request one map-frame point through Manager NavigateToPose. '
        'Use map_revision from the current scene context, x/y in metres and optional '
        'yaw_deg (null uses the configured zero map heading). Use explicit coordinates '
        'when supplied; otherwise choose or calculate an approximate goal using the '
        'current pose, registered landmark coordinates and RGB cues together. '
        'Ground map goals in the supplied map coordinates, not image pixels as metres. '
        'Map bounds alone do not describe free space or paths. '
        'Use named navigation when going to a registered room itself.',
        {'map_revision': {'type': 'string', 'maxLength': 128},
         'x': {'type': 'number', 'minimum': -10000, 'maximum': 10000},
         'y': {'type': 'number', 'minimum': -10000, 'maximum': 10000},
         'yaw_deg': {'type': ['number', 'null'], 'minimum': -180, 'maximum': 180}},
    ),
    'request_waypoints': (
        'Visit an ordered list in ONE Manager mission using existing Nav2 FollowWaypoints. '
        'Copy map_revision from the current scene context. For each waypoint use either '
        'an exact registered location with x/y/yaw_deg null, OR location null with '
        'map x/y and optional yaw_deg. Coordinates may be specified by the user or '
        'chosen using the current pose, landmark coordinates and RGB cues together. '
        'Use reasonable approximate goals grounded in the supplied map coordinates. '
        'This tool is one action even when several places are requested. '
        'One traversal only, stop on the first failed waypoint; no loops or hidden tasks.',
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
