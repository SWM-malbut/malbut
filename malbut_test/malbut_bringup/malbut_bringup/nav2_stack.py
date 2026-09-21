"""
Compose the robot's Nav2 servers with Malbut-owned wiring.

The servers and parameter file are the same ones nav2_bringup's Humble launches
compose, and navigation and behaviors publish /cmd_vel as they do there. Malbut
adds two things: manual driving runs AssistedTeleop in its own behavior server
whose output passes the Collision Monitor, and saved-map Zones reach both
costmaps through the keepout filter servers.
"""

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


def nav2_actions(params_file, *, scan_topic, odom_topic):
    """Return the container, its components and the Zone mask loader."""
    remappings = [('/tf', 'tf'), ('/tf_static', 'tf_static'),
                  ('/scan_raw', scan_topic), ('/odom', odom_topic)]

    def component(name):
        package, plugin = COMPONENTS[name]
        return ComposableNode(
            package=package, plugin=plugin, name=name, parameters=[params_file],
            remappings=remappings + MOTION_REMAPPINGS.get(name, []))

    def lifecycle_manager(name, nodes, autostart):
        return ComposableNode(
            package='nav2_lifecycle_manager',
            plugin='nav2_lifecycle_manager::LifecycleManager', name=name,
            parameters=[{'use_sim_time': False, 'autostart': autostart,
                         'node_names': list(nodes)}])

    nodes = [component(name) for name in NAVIGATION_NODES]
    nodes.append(lifecycle_manager('lifecycle_manager_navigation', NAVIGATION_NODES, True))
    nodes.extend(component(name) for name in LOCALIZATION_NODES)
    nodes.append(lifecycle_manager(
        'lifecycle_manager_localization', LOCALIZATION_NODES, False))
    return [
        Node(
            package='rclcpp_components', executable='component_container_isolated',
            name=CONTAINER, output='screen',
            # The costmaps inside the servers read the same file through the
            # container's global arguments, as in nav2_bringup.
            parameters=[params_file, {'autostart': True, 'use_sim_time': False}],
            remappings=remappings),
        LoadComposableNodes(target_container='/' + CONTAINER,
                            composable_node_descriptions=nodes),
        Node(
            package='malbut_bringup', executable='zone_filter', name='zone_filter',
            output='screen', parameters=[{'use_sim_time': False}]),
    ]
