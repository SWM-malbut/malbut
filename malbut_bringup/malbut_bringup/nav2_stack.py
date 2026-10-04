"""
Compose the robot's Nav2 servers with Malbut-owned wiring.

The servers and parameter file are the same ones nav2_bringup's Humble launches
compose, and navigation and behaviors publish /cmd_vel as they do there. Malbut
adds two things: manual driving runs AssistedTeleop in its own behavior server
whose output passes the Collision Monitor, and saved-map Zones reach both
costmaps through the keepout filter servers.
"""

import math
import json
from pathlib import Path

import yaml

from ament_index_python.packages import get_package_share_directory, PackageNotFoundError
from launch_ros.actions import LoadComposableNodes, Node
from launch_ros.descriptions import ComposableNode

CONTAINER = 'nav2_container'
# AssistedTeleop output; the Collision Monitor passes it on to /cmd_vel.
PRE_COLLISION_TOPIC = 'cmd_vel_pre_collision'
# Lifecycle order: the Zone mask servers start before the costmaps that read
# them. The global costmap in planner_server waits for map->base_footprint, so
# everything that works without a map pose comes first: manual driving and the
# relocalization spin need behaviors, the smoother and the Collision Monitor.
NAVIGATION_NODES = (
    'zone_filter_mask_server', 'zone_filter_info_server',
    'controller_server', 'smoother_server', 'behavior_server',
    'teleop_behavior_server', 'velocity_smoother', 'collision_monitor',
    'planner_server', 'bt_navigator', 'waypoint_follower',
)
# Unconfigured until the system manager selects a saved map.
LOCALIZATION_NODES = ('map_server', 'amcl')
PLANNING_NODES = ('planner_server', 'bt_navigator', 'waypoint_follower')
MOTION_NODES = tuple(name for name in NAVIGATION_NODES if name not in PLANNING_NODES)
COMPONENTS = {
    'zone_filter_mask_server': ('nav2_map_server', 'nav2_map_server::MapServer'),
    'zone_filter_info_server': (
        'nav2_map_server', 'nav2_map_server::CostmapFilterInfoServer'),
    'controller_server': ('nav2_controller', 'nav2_controller::ControllerServer'),
    'smoother_server': ('nav2_smoother', 'nav2_smoother::SmootherServer'),
    'planner_server': ('nav2_planner', 'nav2_planner::PlannerServer'),
    'behavior_server': ('nav2_behaviors', 'behavior_server::BehaviorServer'),
    'teleop_behavior_server': ('nav2_behaviors', 'behavior_server::BehaviorServer'),
    'bt_navigator': ('nav2_bt_navigator', 'nav2_bt_navigator::BtNavigator'),
    'waypoint_follower': (
        'nav2_waypoint_follower', 'nav2_waypoint_follower::WaypointFollower'),
    'velocity_smoother': (
        'nav2_velocity_smoother', 'nav2_velocity_smoother::VelocitySmoother'),
    'collision_monitor': (
        'nav2_collision_monitor', 'nav2_collision_monitor::CollisionMonitor'),
    'map_server': ('nav2_map_server', 'nav2_map_server::MapServer'),
    'amcl': ('nav2_amcl', 'nav2_amcl::AmclNode'),
}
MOTION_REMAPPINGS = {
    'controller_server': [('cmd_vel', 'cmd_vel_nav')],
    'velocity_smoother': [('cmd_vel', 'cmd_vel_nav'), ('cmd_vel_smoothed', 'cmd_vel')],
    # Only manual driving passes the Collision Monitor. Spin, BackUp and Wait
    # in behavior_server publish /cmd_vel directly, as in Humble's launch.
    'teleop_behavior_server': [('cmd_vel', PRE_COLLISION_TOPIC)],
}


def missing_packages(names=None):
    """Return the composed Nav2 packages that are not installed."""
    if names is None:
        names = {package for package, _plugin in COMPONENTS.values()}
        names |= {'rclcpp_components', 'nav2_lifecycle_manager'}
    absent = []
    for name in sorted(names):
        try:
            get_package_share_directory(name)
        except PackageNotFoundError:
            absent.append(name)
    return absent


def navigation_profiles(params_file):
    """Expose only the parameters that differ between map and sensor navigation."""
    config = yaml.safe_load(Path(params_file).read_text())
    path = Path(get_package_share_directory('malbut_bringup')) / 'config/nav2_mapless.yaml'
    overrides = yaml.safe_load(path.read_text())
    mapped, mapless = {}, {}
    for name in ('global_costmap', 'local_costmap', 'bt_navigator'):
        original = config[name][name] if name.endswith('costmap') else config[name]
        local = overrides[name][name] if name.endswith('costmap') else overrides[name]
        values = (local['ros__parameters'] if name != 'local_costmap'
                  else {'keepout_filter.enabled': False})
        node = f'/{name}/{name}' if name.endswith('costmap') else f'/{name}'
        mapless[node] = values
        defaults = {'rolling_window': False, 'width': 5, 'height': 5, 'filters': [],
                    'global_frame': 'map',
                    'plugins': ['static_layer', 'obstacle_layer', 'inflation_layer']}
        original_values = original['ros__parameters']
        if name == 'local_costmap':
            if 'keepout_filter' not in original_values.get('filters', []):
                del mapless[node]
                continue
            original_values = {'keepout_filter.enabled':
                               original_values.get('keepout_filter', {}).get('enabled', True)}
        mapped[node] = {key: original_values.get(key, defaults.get(key))
                        for key in values}
    return json.dumps({'mapped': mapped, 'mapless': mapless})


def nav2_actions(params_file, *, scan_topic, odom_topic, mapless=False):
    """Return the container, its components and the Zone mask loader."""
    absent = missing_packages()
    if absent:
        # The lifecycle manager would otherwise wait forever for the component.
        raise RuntimeError(
            'Nav2 packages are not installed: ' + ', '.join(absent)
            + ' (sudo apt install ' + ' '.join(
                'ros-humble-' + name.replace('_', '-') for name in absent) + ')')
    remappings = [('/tf', 'tf'), ('/tf_static', 'tf_static'),
                  ('/scan_raw', scan_topic), ('/odom', odom_topic)]
    parameter_files = [params_file]
    if mapless:
        parameter_files.append(str(Path(get_package_share_directory('malbut_bringup'))
                                   / 'config/nav2_mapless.yaml'))

    def component(name, overrides=None):
        package, plugin = COMPONENTS[name]
        return ComposableNode(
            package=package, plugin=plugin, name=name,
            parameters=parameter_files + ([overrides] if overrides else []),
            remappings=remappings + MOTION_REMAPPINGS.get(name, []))

    def lifecycle_manager(name, nodes, autostart):
        return ComposableNode(
            package='nav2_lifecycle_manager',
            plugin='nav2_lifecycle_manager::LifecycleManager', name=name,
            parameters=[{'use_sim_time': False, 'autostart': autostart,
                         # Keep bond failure detection, but only the explicit
                         # manual recovery may reactivate a failed group.
                         'attempt_respawn_reconnection': False,
                         'node_names': list(nodes)}])

    nodes = [component(name) for name in NAVIGATION_NODES]
    nodes.append(lifecycle_manager('lifecycle_manager_motion', MOTION_NODES, True))
    nodes.append(lifecycle_manager('lifecycle_manager_navigation', PLANNING_NODES, True))
    nodes.extend(component(name) for name in LOCALIZATION_NODES)
    nodes.append(lifecycle_manager(
        'lifecycle_manager_localization', LOCALIZATION_NODES, False))
    container = Node(
            package='rclcpp_components', executable='component_container_isolated',
            name=CONTAINER, output='screen',
            # Only this process uses the growable DDS send-buffer pool. Manual
            # recovery preserves its expanded environment when restarting it.
            additional_env={
                'RMW_IMPLEMENTATION': 'rmw_fastrtps_cpp',
                'FASTRTPS_DEFAULT_PROFILES_FILE': str(Path(
                    get_package_share_directory('malbut_bringup')) / 'config/fastdds_nav2.xml'),
            },
            # The costmaps inside the servers read the same file through the
            # container's global arguments, as in nav2_bringup.
            parameters=parameter_files + [{'autostart': True, 'use_sim_time': False}],
            remappings=remappings)

    def recovery_parameters(localization, pose):
        """Restore maps cleared by cleanup, not just by process exit."""
        selected = localization.get('map') if localization.get('mode') == 'LOCALIZATION' else None
        mask = Path.home() / '.ros/malbut/zones/zone_mask.yaml'
        initial_pose = {}
        if selected and pose is not None and pose.header.frame_id == 'map':
            position, q = pose.pose.pose.position, pose.pose.pose.orientation
            initial_pose = {'set_initial_pose': True, 'initial_pose.x': position.x,
                            'initial_pose.y': position.y, 'initial_pose.z': position.z,
                            'initial_pose.yaw': math.atan2(
                                2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y*q.y + q.z*q.z))}
        return {
            'zone_filter_mask_server': {'yaml_filename': str(mask)} if mask.is_file() else {},
            'map_server': {'yaml_filename': selected or ''}, 'amcl': initial_pose,
        }

    def recovery_lifecycle(localization, pose):
        parameters = recovery_parameters(localization, pose)
        groups = []
        # Restore map/AMCL before the planner waits for map TF. In mapping mode
        # these nodes are intentionally unconfigured; SLAM remains the owner.
        if localization.get('mode') == 'LOCALIZATION' and localization.get('map'):
            groups.append(dict(manager='lifecycle_manager_localization',
                               nodes=LOCALIZATION_NODES, parameters=parameters))
        groups.append(dict(manager='lifecycle_manager_motion',
                           nodes=MOTION_NODES, parameters=parameters))
        groups.append(dict(manager='lifecycle_manager_navigation',
                           nodes=PLANNING_NODES, parameters=parameters))
        return groups

    def recovery_components(localization, pose):
        # A respawned container is empty. The recovery owner activates these
        # groups explicitly after loading; no competing autostart transition.
        parameters = recovery_parameters(localization, pose)
        restored = [component(name, parameters.get(name)) for name in NAVIGATION_NODES]
        restored.append(lifecycle_manager(
            'lifecycle_manager_motion', MOTION_NODES, False))
        restored.append(lifecycle_manager(
            'lifecycle_manager_navigation', PLANNING_NODES, False))
        restored.extend(component(name, parameters[name]) for name in LOCALIZATION_NODES)
        restored.append(lifecycle_manager(
            'lifecycle_manager_localization', LOCALIZATION_NODES, False))
        return [LoadComposableNodes(target_container='/' + CONTAINER,
                                    composable_node_descriptions=restored)]

    container._malbut_recovery_followup = recovery_components
    container._malbut_recovery_lifecycle = recovery_lifecycle
    container._malbut_restart_unresponsive = True
    return [
        container,
        LoadComposableNodes(target_container='/' + CONTAINER,
                            composable_node_descriptions=nodes),
        Node(
            package='malbut_bringup', executable='zone_filter', name='zone_filter',
            output='screen', parameters=[{'use_sim_time': False}]),
    ]
