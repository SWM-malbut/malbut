"""Check the robot Nav2 deployment settings without starting navigation."""

import math
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def config():
    """Load the deployment parameters, independent of robot-installed files."""
    path = Path(__file__).parents[1] / 'config/nav2_params.yaml'
    return yaml.safe_load(path.read_text())


def test_default_navigation_recovery_behaviors_are_available(config):
    """Patrol's BT needs the recovery servers; manual_drive has its own AssistedTeleop."""
    behavior = config['behavior_server']['ros__parameters']
    assert behavior['behavior_plugins'] == ['spin', 'wait', 'backup']
    for name, plugin in (('spin', 'Spin'), ('wait', 'Wait'), ('backup', 'BackUp')):
        assert behavior[name]['plugin'] == f'nav2_behaviors/{plugin}'
    teleop = config['teleop_behavior_server']['ros__parameters']
    assert teleop['behavior_plugins'] == ['assisted_teleop']
    assert teleop['assisted_teleop']['plugin'] == 'nav2_behaviors/AssistedTeleop'
    for key in ('costmap_topic', 'footprint_topic', 'global_frame', 'robot_base_frame'):
        assert teleop[key] == behavior[key]
    assert type(behavior['rotational_acc_lim']) is float
    assert behavior['rotational_acc_lim'] == 3.2
    # The vendor driver clamps /cmd_vel rotation to 0.5 rad/s.
    assert behavior['max_rotational_vel'] == 0.5
    assert behavior['min_rotational_vel'] == 0.4


def test_localization_uses_actual_initial_pose_and_vendor_frames(config):
    """Do not initialize a real robot at an assumed map origin."""
    amcl = config['amcl']['ros__parameters']
    assert amcl['set_initial_pose'] is False
    assert amcl['use_sim_time'] is False
    assert amcl['base_frame_id'] == 'base_footprint'
    assert amcl['odom_frame_id'] == 'odom'
    assert amcl['global_frame_id'] == 'map'
    assert amcl['scan_topic'] == '/scan_raw'
    assert amcl['robot_model_type'] == 'nav2_amcl::OmniMotionModel'
    assert config['map_server']['ros__parameters']['yaml_filename'] == ''
    planner = config['planner_server']['ros__parameters']
    assert planner['planner_plugins'] == ['GridBased']
    assert planner['GridBased']['plugin'] == 'nav2_navfn_planner/NavfnPlanner'
    # The person follower plans to the person's own cells and needs a search
    # radius wider than two legs plus the 0.106 m inscribed inflation.
    assert planner['GridBased']['tolerance'] == 0.5


def test_robot_costmap_footprint_and_driver_velocity_limits(config):
    """Use the official mecanum outline and the vendor driver's /cmd_vel clamp."""
    for scope, frame, resolution in (
            ('local_costmap', 'odom', 0.03),
            ('global_costmap', 'map', 0.05)):
        costmap = config[scope][scope]['ros__parameters']
        assert costmap['global_frame'] == frame
        assert costmap['robot_base_frame'] == 'base_footprint'
        assert costmap['use_sim_time'] is False
        assert costmap['footprint_padding'] == 0.01
        corners = yaml.safe_load(costmap['footprint'])
        assert corners == [[0.1385, 0.106], [0.1385, -0.106],
                           [-0.1385, -0.106], [-0.1385, 0.106]]
        assert costmap['resolution'] == resolution
        inflation = costmap['inflation_layer']['inflation_radius']
        assert inflation == 0.30
        # A sub-cell soft band cannot provide a useful wall-clearance gradient.
        corner = max(math.hypot(x, y) for x, y in corners)
        assert inflation - corner >= 2 * resolution
        scan = costmap['obstacle_layer']['scan']
        assert scan['topic'] == '/scan_raw'
        assert scan['marking'] is True
        assert scan['clearing'] is True
    smoother = config['velocity_smoother']['ros__parameters']
    assert smoother['max_velocity'] == [0.2, 0.0, 0.5]
    assert smoother['min_velocity'] == [-0.2, 0.0, -0.5]
    assert smoother['max_accel'] == [2.5, 0.0, 3.2]
    assert smoother['max_decel'] == [-2.5, 0.0, -3.2]


def test_deployed_nav2_parameters_match_source(config):
    """The copy used by the real robot must retain the same safety settings."""
    path = Path(__file__).parents[2] / 'malbut_test/malbut_bringup/config/nav2_params.yaml'
    assert yaml.safe_load(path.read_text()) == config


def test_saved_map_zones_reach_both_costmaps_through_keepout_filter(config):
    """zone_filter loads the mask; both costmaps read it through filter info."""
    mask = config['zone_filter_mask_server']['ros__parameters']
    info = config['zone_filter_info_server']['ros__parameters']
    assert mask['yaml_filename'] == '' and mask['frame_id'] == 'map'
    assert info['type'] == 0  # keepout
    assert info['mask_topic'] == mask['topic_name']
    assert info['base'] == 0.0 and info['multiplier'] == 1.0
    for scope in ('local_costmap', 'global_costmap'):
        costmap = config[scope][scope]['ros__parameters']
        assert costmap['filters'] == ['keepout_filter']
        keepout = costmap['keepout_filter']
        assert keepout['plugin'] == 'nav2_costmap_2d::KeepoutFilter'
        assert keepout['enabled'] is True
        assert keepout['filter_info_topic'] == info['filter_info_topic']


def test_dwb_rejects_footprint_contact_and_keeps_the_vendor_distance_score(config):
    """The centre cell alone would protect only the 0.106 m inscribed circle."""
    follow = config['controller_server']['ros__parameters']['FollowPath']
    assert follow['critics'] == ['RotateToGoal', 'Oscillation', 'BaseObstacle',
                                 'ObstacleFootprint', 'GoalAlign', 'PathAlign',
                                 'PathDist', 'GoalDist', 'PreferForward']
    assert follow['BaseObstacle.scale'] == 0.02
    resolution = config['local_costmap']['local_costmap']['ros__parameters']['resolution']
    # DWB skips a critic at scale 0; at 254 the outline score must still stay
    # far below one cell of path distance (32 * resolution / 2).
    assert follow['ObstacleFootprint.scale'] > 0
    assert follow['ObstacleFootprint.scale'] * resolution * 254 < 0.1


def test_only_manual_driving_passes_the_collision_monitor(config):
    """Navigation and Spin/BackUp publish /cmd_vel; AssistedTeleop goes through it."""
    from malbut_bringup.nav2_stack import MOTION_REMAPPINGS, PRE_COLLISION_TOPIC

    monitor = config['collision_monitor']['ros__parameters']
    assert monitor['cmd_vel_in_topic'] == PRE_COLLISION_TOPIC
    assert monitor['cmd_vel_out_topic'] == 'cmd_vel'
    assert (monitor['base_frame_id'], monitor['odom_frame_id']) == ('base_footprint', 'odom')
    assert monitor['polygons'] == ['FootprintApproach']
    polygon = monitor['FootprintApproach']
    # Approach follows the mecanum base's direction of travel; moving away is free.
    assert polygon['action_type'] == 'approach' and polygon['type'] == 'polygon'
    assert polygon['footprint_topic'] == '/local_costmap/published_footprint'
    assert 0 < polygon['time_before_collision'] <= 2.0
    assert monitor['observation_sources'] == ['scan']
    assert monitor['scan'] == {'type': 'scan', 'topic': '/scan_raw'}
    outputs = {name: dict(MOTION_REMAPPINGS.get(name, [])).get(topic, topic)
               for name, topic in (('controller_server', 'cmd_vel'),
                                   ('velocity_smoother', 'cmd_vel_smoothed'),
                                   ('behavior_server', 'cmd_vel'),
                                   ('teleop_behavior_server', 'cmd_vel'))}
    assert outputs == {'controller_server': 'cmd_vel_nav',
                       'velocity_smoother': 'cmd_vel',
                       'behavior_server': 'cmd_vel',
                       'teleop_behavior_server': PRE_COLLISION_TOPIC}


def test_planar_lidar_uses_2d_layers_with_unchanged_observation_ranges(config):
    """Retain LiDAR marking/clearing while depth obstacle processing is disabled."""
    for scope in ('local_costmap', 'global_costmap'):
        costmap = config[scope][scope]['ros__parameters']
        assert 'obstacle_layer' in costmap['plugins']
        layer = costmap['obstacle_layer']
        assert layer['plugin'] == 'nav2_costmap_2d::ObstacleLayer'
        assert layer['observation_sources'] == 'scan'
        scan = layer['scan']
        assert scan['data_type'] == 'LaserScan'
        assert scan['max_obstacle_height'] == 2.0
        assert scan['obstacle_min_range'] == scan['raytrace_min_range'] == 0.0
        assert scan['obstacle_max_range'] == 2.5
        assert scan['raytrace_max_range'] == 3.0


def test_goals_stop_within_the_follower_distance_band(config):
    """0.25 m left a web goal a body length short and outside 0.90-1.10 m."""
    controller = config['controller_server']['ros__parameters']
    checker = controller['general_goal_checker']
    assert checker['xy_goal_tolerance'] == 0.12
    assert checker['stateful'] is True
    assert controller['FollowPath']['xy_goal_tolerance'] == checker['xy_goal_tolerance']


def test_autonomous_driving_prefers_forward_without_forbidding_reverse(config):
    """The person follower retreats with BackUp, so one DWB instance is enough."""
    controller = config['controller_server']['ros__parameters']
    assert controller['controller_plugins'] == ['FollowPath']
    follow = controller['FollowPath']
    assert follow['min_vel_x'] < 0  # Reverse stays possible when forward is blocked.
    assert follow['PreferForward.penalty'] == 1.0
    assert follow['PreferForward.strafe_x'] == 0.0
    assert follow['PreferForward.theta_scale'] == 0.0
    # A soft preference in the range public DWB configs use (1-40 next to
    # PathDist 32 / GoalDist 24); reverse stays available, not banned.
    assert follow['PreferForward.scale'] == 40.0
    assert follow['PreferForward.scale'] <= follow['PathDist.scale'] + follow['GoalDist.scale']
    behavior = config['behavior_server']['ros__parameters']
    assert behavior['backup']['plugin'] == 'nav2_behaviors/BackUp'
