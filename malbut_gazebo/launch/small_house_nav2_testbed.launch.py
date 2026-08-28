"""Start the Small House Nav2 foundation with motion owners off by default."""

import json
import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    GroupAction,
    IncludeLaunchDescription,
    OpaqueFunction,
    RegisterEventHandler,
)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import IfElseSubstitution, LaunchConfiguration
from launch_ros.actions import Node

from malbut_gazebo.map_lifecycle import load_active_revision
from malbut_gazebo.nav2_startup_gate import (
    StartupGateError,
    _require_isolated_ros_context,
)
from malbut_gazebo.user_map_builder import load_slam_map


USER_MAP_FORMAT = 'malbut-user-map-v1'
MAX_USER_MAP_BYTES = 2 * 1024 * 1024


def _validate_isolated_ros_context(_context):
    """Fail before starting any simulation or ROS process."""
    try:
        _require_isolated_ros_context()
    except StartupGateError as error:
        raise RuntimeError(error.code) from error
    return []


def _named_navigation_error(code):
    """Raise one path-free preflight error before any child starts."""
    raise RuntimeError(code)


def _required_path(raw_value, *, kind):
    """Resolve one absolute, readable opt-in path without logging it."""
    candidate = Path(raw_value).expanduser()
    if not raw_value or not candidate.is_absolute() or candidate.is_symlink():
        _named_navigation_error(f'named_navigation_{kind}_invalid')
    try:
        path = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        _named_navigation_error(f'named_navigation_{kind}_invalid')
    expected = path.is_dir() if kind == 'map_store' else path.is_file()
    if not expected or not os.access(path, os.R_OK):
        _named_navigation_error(f'named_navigation_{kind}_invalid')
    return path


def _unique_json_object(pairs):
    """Reject ambiguous duplicate members in one preflight object."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON object member')
        value[key] = item
    return value


def _reject_json_constant(_value):
    """Reject non-standard NaN and Infinity constants."""
    raise ValueError('non-finite JSON number')


def _validate_named_navigation_configuration(context):
    """Validate the complete opt-in binding before creating a process."""
    enabled_value = LaunchConfiguration(
        'enable_named_navigation'
    ).perform(context).strip().lower()
    if enabled_value not in {'true', 'false'}:
        _named_navigation_error('named_navigation_enable_invalid')

    user_map_value = LaunchConfiguration(
        'named_navigation_user_map'
    ).perform(context).strip()
    map_store_value = LaunchConfiguration(
        'named_navigation_map_store'
    ).perform(context).strip()
    if enabled_value == 'false':
        if user_map_value or map_store_value:
            _named_navigation_error(
                'named_navigation_disabled_configuration'
            )
        return []

    port_value = LaunchConfiguration(
        'named_navigation_port'
    ).perform(context).strip()
    if not port_value.isascii() or not port_value.isdecimal():
        _named_navigation_error('named_navigation_port_invalid')
    port = int(port_value)
    if not 0 < port < 65536:
        _named_navigation_error('named_navigation_port_invalid')

    user_map_path = _required_path(user_map_value, kind='user_map')
    map_store = _required_path(map_store_value, kind='map_store')
    if not os.access(map_store, os.W_OK | os.X_OK):
        _named_navigation_error('named_navigation_map_store_not_writable')

    active = load_active_revision(map_store)
    if active is None:
        _named_navigation_error('named_navigation_active_map_invalid')
    try:
        active_user_map = (
            map_store / active['user_map']
        ).resolve(strict=True)
        active_map_yaml = (
            map_store / active['map_yaml']
        ).resolve(strict=True)
    except (KeyError, OSError, RuntimeError, ValueError):
        _named_navigation_error('named_navigation_active_map_invalid')
    if active_user_map != user_map_path:
        _named_navigation_error('named_navigation_user_map_mismatch')

    bundled_map = Path(
        get_package_share_directory('malbut_gazebo')
    ) / 'maps' / 'small_house.yaml'
    try:
        expected_map = load_slam_map(bundled_map)
        selected_map = load_slam_map(active_map_yaml)
    except (OSError, UnicodeDecodeError, ValueError):
        _named_navigation_error('named_navigation_active_map_invalid')
    if (
        selected_map.map_id != expected_map.map_id
        or selected_map.map_revision != expected_map.map_revision
        or active.get('map_id') != expected_map.map_id
        or active.get('map_revision') != expected_map.map_revision
    ):
        _named_navigation_error('named_navigation_map_mismatch')

    try:
        with user_map_path.open('rb') as stream:
            payload = stream.read(MAX_USER_MAP_BYTES + 1)
        if not 0 < len(payload) <= MAX_USER_MAP_BYTES:
            raise ValueError('User Map size is outside the bound')
        user_map = json.loads(
            payload.decode('utf-8', errors='strict'),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        _named_navigation_error('named_navigation_user_map_invalid')
    if (
        not isinstance(user_map, dict)
        or user_map.get('format') != USER_MAP_FORMAT
        or user_map.get('map_id') != expected_map.map_id
        or user_map.get('map_revision') != expected_map.map_revision
    ):
        _named_navigation_error('named_navigation_user_map_mismatch')
    return []


def _shutdown_on_nav2_startup_failure(event, _context):
    """Stop the testbed unless every required lifecycle node became active."""
    if event.returncode == 0:
        return []
    return [EmitEvent(event=Shutdown(
        reason=f'Nav2 startup gate exited with code {event.returncode}',
    ))]


def generate_launch_description():
    """Start no-goal Nav2, optionally exposing one named-goal test path."""
    gazebo_share = Path(get_package_share_directory('malbut_gazebo'))
    use_sim_time = LaunchConfiguration('use_sim_time')
    spawn_x = LaunchConfiguration('x')
    spawn_y = LaunchConfiguration('y')
    spawn_yaw = LaunchConfiguration('yaw')
    enable_named_navigation = LaunchConfiguration(
        'enable_named_navigation'
    )
    named_user_map = LaunchConfiguration('named_navigation_user_map')
    named_map_store = LaunchConfiguration('named_navigation_map_store')
    named_port = LaunchConfiguration('named_navigation_port')
    small_house_map_path = gazebo_share / 'maps' / 'small_house.yaml'
    small_house_map = load_slam_map(small_house_map_path)
    enabled_user_map = IfElseSubstitution(
        enable_named_navigation,
        if_value=named_user_map,
        else_value='',
    )
    enabled_map_store = IfElseSubstitution(
        enable_named_navigation,
        if_value=named_map_store,
        else_value='',
    )
    enabled_map_id = IfElseSubstitution(
        enable_named_navigation,
        if_value=small_house_map.map_id,
        else_value='',
    )
    enabled_map_revision = IfElseSubstitution(
        enable_named_navigation,
        if_value=small_house_map.map_revision,
        else_value='',
    )

    # Scope the simulation-only arguments so they cannot leak into the Nav2
    # include below.
    simulation = GroupAction(
        scoped=True,
        actions=[IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                str(gazebo_share / 'launch' / 'worlds.launch.py')
            ),
            launch_arguments={
                'world_name': 'small_house',
                'x': spawn_x,
                'y': spawn_y,
                'z': '0.002',
                'yaw': spawn_yaw,
                'gui': LaunchConfiguration('gui'),
                'headless': LaunchConfiguration('headless'),
                'rviz': 'false',
                'use_sim_time': use_sim_time,
                # Named Nav2 needs LiDAR/TF only.  Keep the unrelated image
                # bridge out of this minimal testbed and its dependency set.
                'depth_camera_enabled': 'false',
                'lidar_enabled': 'true',
                'imu_enabled': 'true',
                'spawn_robot': 'true',
                'bridge': 'true',
            }.items(),
        )],
    )
    navigation = GroupAction(
        scoped=True,
        actions=[IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                str(gazebo_share / 'launch' / 'navigation.launch.py')
            ),
            launch_arguments={
                'namespace': '',
                'use_namespace': 'false',
                'map': str(small_house_map_path),
                'depth_costmap_enabled': 'false',
                'use_sim_time': use_sim_time,
                'rviz': LaunchConfiguration('rviz'),
                'restore_localization': 'false',
                'set_initial_pose': 'true',
                'initial_pose_x': spawn_x,
                'initial_pose_y': spawn_y,
                'initial_pose_yaw': spawn_yaw,
                'localization_source': 'static',
                'autostart': 'false',
                'use_composition': 'False',
                'use_respawn': 'False',
                'zone_mask': '',
                'robot_web': enable_named_navigation,
                'robot_web_port': named_port,
                'robot_web_navigation_action': '/navigate_to_pose',
                'robot_web_device_id': 'malbut-sim-01',
                'robot_web_simulation': 'true',
                'user_map': enabled_user_map,
                'pose_checkpoint_store': enabled_map_store,
                'pose_checkpoint_map_id': enabled_map_id,
                'pose_checkpoint_map_revision': enabled_map_revision,
                'boot_pose_trusted': enable_named_navigation,
                'autonomous_modes': 'false',
                'patrol_route_file': '',
                'person_following': 'false',
                'person_projection_frame': '',
                'inscribed_escape_enabled': 'false',
            }.items(),
        )],
    )
    startup_gate = Node(
        package='malbut_gazebo',
        executable='nav2_startup_gate',
        name='small_house_nav2_testbed_startup_gate',
        output='screen',
        arguments=[
            '--service-timeout-seconds',
            LaunchConfiguration('nav2_startup_service_timeout'),
            '--discovery-stability-seconds',
            LaunchConfiguration('nav2_discovery_stability'),
            '--quiet-period-seconds',
            LaunchConfiguration('nav2_startup_quiet_period'),
            '--response-timeout-seconds',
            LaunchConfiguration('nav2_startup_response_timeout'),
        ],
    )
    startup_guard = RegisterEventHandler(OnProcessExit(
        target_action=startup_gate,
        on_exit=_shutdown_on_nav2_startup_failure,
    ))
    isolation_guard = OpaqueFunction(
        function=_validate_isolated_ros_context,
    )
    named_navigation_guard = OpaqueFunction(
        function=_validate_named_navigation_configuration,
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='true'),
        DeclareLaunchArgument('gui', default_value='false'),
        DeclareLaunchArgument('headless', default_value='true'),
        DeclareLaunchArgument('rviz', default_value='false'),
        DeclareLaunchArgument(
            'enable_named_navigation',
            default_value='false',
            description=(
                'Explicitly enable the local Robot Web named-goal test path.'
            ),
        ),
        DeclareLaunchArgument(
            'named_navigation_user_map',
            default_value='',
            description=(
                'Absolute User Map path inside the selected runtime map store.'
            ),
        ),
        DeclareLaunchArgument(
            'named_navigation_map_store',
            default_value='',
            description=(
                'Writable runtime map store used for pose revalidation.'
            ),
        ),
        DeclareLaunchArgument(
            'named_navigation_port',
            default_value='8765',
            description='Loopback Robot Web port for the explicit opt-in.',
        ),
        DeclareLaunchArgument(
            'nav2_discovery_stability',
            default_value='1.0',
        ),
        DeclareLaunchArgument(
            'nav2_startup_quiet_period',
            default_value='2.0',
        ),
        DeclareLaunchArgument(
            'nav2_startup_service_timeout',
            default_value='30.0',
        ),
        DeclareLaunchArgument(
            'nav2_startup_response_timeout',
            default_value='60.0',
        ),
        DeclareLaunchArgument('x', default_value='-3.665503'),
        DeclareLaunchArgument('y', default_value='-0.4874'),
        DeclareLaunchArgument('yaw', default_value='0.0'),
        # Launch processes are visited in order. Keep this guard before every
        # action that can create a simulation or ROS child process.
        isolation_guard,
        named_navigation_guard,
        simulation,
        navigation,
        startup_guard,
        startup_gate,
    ])
