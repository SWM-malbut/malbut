"""Check the robot Nav2 deployment settings without starting navigation."""

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def config():
    """Load the deployment parameters, independent of robot-installed files."""
    path = Path(__file__).parents[1] / 'config/nav2_params.yaml'
    return yaml.safe_load(path.read_text())


def test_default_navigation_recovery_behaviors_are_available(config):
    """Patrol's default BT requires all three servers during tree creation."""
    behavior = config['behavior_server']['ros__parameters']
    assert behavior['behavior_plugins'] == ['spin', 'wait', 'backup']
    for name, plugin in (('spin', 'Spin'), ('wait', 'Wait'), ('backup', 'BackUp')):
        assert behavior[name]['plugin'] == f'nav2_behaviors/{plugin}'
    assert type(behavior['rotational_acc_lim']) is float
    assert behavior['rotational_acc_lim'] == 3.2
    assert behavior['max_rotational_vel'] == 1.0
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
    assert config['map_server']['ros__parameters']['yaml_filename'] == ''
    planner = config['planner_server']['ros__parameters']
    assert planner['planner_plugins'] == ['GridBased']
    assert planner['GridBased']['plugin'] == 'nav2_navfn_planner/NavfnPlanner'
    assert planner['GridBased']['tolerance'] == 0.5


def test_robot_costmap_radii_and_vendor_matched_velocity_limits(config):
    """Use robot-tested radii and match the manufacturer's DWB limits."""
    for scope, frame, resolution in (
            ('local_costmap', 'odom', 0.03),
            ('global_costmap', 'map', 0.05)):
        costmap = config[scope][scope]['ros__parameters']
        assert costmap['global_frame'] == frame
        assert costmap['robot_base_frame'] == 'base_footprint'
        assert costmap['use_sim_time'] is False
        assert costmap['robot_radius'] == 0.18
        assert costmap['resolution'] == resolution
        inflation = costmap['inflation_layer']['inflation_radius']
        assert inflation == 0.30
        # A sub-cell soft band cannot provide a useful wall-clearance gradient.
        assert inflation - costmap['robot_radius'] >= 2 * resolution
        scan = costmap['obstacle_layer']['scan']
        assert scan['topic'] == '/scan_raw'
        assert scan['marking'] is True
        assert scan['clearing'] is True
    smoother = config['velocity_smoother']['ros__parameters']
    assert smoother['max_velocity'] == [0.4, 0.0, 1.0]
    assert smoother['min_velocity'] == [-0.4, 0.0, -1.0]
    assert smoother['max_accel'] == [2.5, 0.0, 3.2]
    assert smoother['max_decel'] == [-2.5, 0.0, -3.2]


def test_deployed_nav2_parameters_match_source(config):
    """The copy used by the real robot must retain the same safety settings."""
    path = Path(__file__).parents[2] / 'malbut_test/malbut_bringup/config/nav2_params.yaml'
    assert yaml.safe_load(path.read_text()) == config


def test_nav2_projects_depth_locally_without_cloud_subscriptions(config):
    """Restore camera obstacles without receiving the measured 8 MB cloud."""
    for scope in ('local_costmap', 'global_costmap'):
        costmap = config[scope][scope]['ros__parameters']
        expected = ['obstacle_layer', 'depth_voxel_layer', 'inflation_layer']
        if scope == 'global_costmap':
            expected.insert(0, 'static_layer')
        assert costmap['plugins'] == expected
        depth = costmap['depth_voxel_layer']
        assert depth['plugin'] == 'malbut_depth_costmap::DepthVoxelLayer'
        assert depth['observation_sources'] == ''
        assert depth['depth_topic'] == '/depth_cam/depth0/image_raw'
        assert depth['camera_info_topic'] == '/depth_cam/depth0/camera_info'
        assert depth['depth_is_rectified'] is False
        assert depth['publish_voxel_map'] is False
        assert depth['expected_update_rate'] == 0.5
        assert depth['max_obstacle_height'] == 0.20
        assert depth['origin_z'] == 0.0
        assert depth['z_resolution'] == 0.03 and depth['z_voxels'] == 16
        assert depth['mark_threshold'] == 0
        assert depth['marking'] == {
            'min_obstacle_height': 0.05, 'max_obstacle_height': 0.20,
            'obstacle_min_range': 0.0, 'obstacle_max_range': 2.5,
        }
        assert depth['clearing'] == {
            'min_obstacle_height': -0.05, 'max_obstacle_height': 0.48,
            'raytrace_min_range': 0.0, 'raytrace_max_range': 3.0,
        }
        observations = [
            layer[source]
            for layer in costmap.values() if isinstance(layer, dict)
            for source in layer.get('observation_sources', '').split()
        ]
        assert len(observations) == 1
        assert observations[0]['data_type'] == 'LaserScan'
        assert observations[0]['topic'] == '/scan_raw'
    assert '/depth_cam/depth0/points' not in yaml.safe_dump(config)


def test_planar_lidar_uses_2d_layers_with_unchanged_observation_ranges(config):
    """Retain independent LiDAR marking/clearing when depth is projected locally."""
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
