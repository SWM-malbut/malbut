"""Start the no-goal Small House foundation for a future Agent executor."""

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
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_gazebo.nav2_startup_gate import (
    StartupGateError,
    _require_isolated_ros_context,
)


def _validate_isolated_ros_context(_context):
    """Fail before starting any simulation or ROS process."""
    try:
        _require_isolated_ros_context()
    except StartupGateError as error:
        raise RuntimeError(error.code) from error
    return []


def _shutdown_on_nav2_startup_failure(event, _context):
    """Stop the testbed unless every required lifecycle node became active."""
    if event.returncode == 0:
        return []
    return [EmitEvent(event=Shutdown(
        reason=f'Nav2 startup gate exited with code {event.returncode}',
    ))]


def generate_launch_description():
    """Start Small House, static localization, and Nav2 with no goal source."""
    gazebo_share = Path(get_package_share_directory('malbut_gazebo'))
    use_sim_time = LaunchConfiguration('use_sim_time')
    spawn_x = LaunchConfiguration('x')
    spawn_y = LaunchConfiguration('y')
    spawn_yaw = LaunchConfiguration('yaw')

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
                'depth_camera_enabled': 'true',
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
                'map': str(gazebo_share / 'maps' / 'small_house.yaml'),
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
                'robot_web': 'false',
                'user_map': '',
                'pose_checkpoint_store': '',
                'pose_checkpoint_map_id': '',
                'pose_checkpoint_map_revision': '',
                'boot_pose_trusted': 'false',
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

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='true'),
        DeclareLaunchArgument('gui', default_value='false'),
        DeclareLaunchArgument('headless', default_value='true'),
        DeclareLaunchArgument('rviz', default_value='false'),
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
        simulation,
        navigation,
        startup_guard,
        startup_gate,
    ])
