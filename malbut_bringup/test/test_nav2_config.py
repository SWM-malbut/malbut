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
    assert amcl['scan_topic'] == '/scan_normalized'
    assert config['map_server']['ros__parameters']['yaml_filename'] == ''
    planner = config['planner_server']['ros__parameters']
    assert planner['planner_plugins'] == ['GridBased']
    assert planner['GridBased']['plugin'] == 'nav2_navfn_planner/NavfnPlanner'
    assert planner['GridBased']['tolerance'] == 0.5


def test_robot_costmap_radii_and_vendor_matched_velocity_limits(config):
    """Use robot-tested radii and match the manufacturer's DWB limits."""
    for scope, frame, resolution, inflation in (
            ('local_costmap', 'odom', 0.03, 0.20),
            ('global_costmap', 'map', 0.05, 0.2)):
        costmap = config[scope][scope]['ros__parameters']
        assert costmap['global_frame'] == frame
        assert costmap['robot_base_frame'] == 'base_footprint'
        assert costmap['use_sim_time'] is False
        assert costmap['robot_radius'] == 0.18
        assert costmap['resolution'] == resolution
        assert costmap['inflation_layer']['inflation_radius'] == inflation
        scan = costmap['obstacle_layer']['scan']
        assert scan['topic'] == '/scan_normalized'
        assert scan['marking'] is True
        assert scan['clearing'] is True
    smoother = config['velocity_smoother']['ros__parameters']
    assert smoother['max_velocity'] == [0.4, 0.0, 1.0]
    assert smoother['min_velocity'] == [-0.4, 0.0, -1.0]
    assert smoother['max_accel'] == [2.5, 0.0, 3.2]
    assert smoother['max_decel'] == [-2.5, 0.0, -3.2]


def test_depth_obstacles_use_separate_3d_layers_and_real_cloud(config):
    """A laser clearing ray must not erase obstacles below the laser plane."""
    for scope in ('local_costmap', 'global_costmap'):
        costmap = config[scope][scope]['ros__parameters']
        assert 'depth_voxel_layer' in costmap['plugins']
        assert costmap['plugins'][-1] == 'inflation_layer'
        layer = costmap['depth_voxel_layer']
        assert layer['plugin'] == 'nav2_costmap_2d::VoxelLayer'
        assert layer['mark_threshold'] == 0
        assert layer['z_resolution'] == 0.03
        assert layer['z_voxels'] <= 16
        assert layer['max_obstacle_height'] == 0.20
        assert layer['origin_z'] + layer['z_resolution'] * layer['z_voxels'] >= (
            layer['max_obstacle_height'])
        cloud = layer['depth']
        assert cloud['topic'] == '/depth_cam/depth0/points'
        assert cloud['data_type'] == 'PointCloud2'
        assert cloud['min_obstacle_height'] == 0.05
        assert cloud['max_obstacle_height'] == layer['max_obstacle_height']
        assert cloud['min_obstacle_height'] < cloud['max_obstacle_height']
        assert 'sensor_frame' not in cloud  # Resolve the actual message frame by TF.
        assert cloud['marking'] and not cloud['clearing']
        assert cloud['raytrace_max_range'] >= cloud['obstacle_max_range']
        clearing = layer['depth_clear']
        assert clearing['topic'] == cloud['topic']
        assert clearing['clearing'] and not clearing['marking']
        assert clearing['min_obstacle_height'] < 0.0
        # Higher points can establish free space without becoming obstacles.
        assert clearing['max_obstacle_height'] == 0.48
        assert clearing['max_obstacle_height'] > cloud['max_obstacle_height']
        assert layer['observation_sources'].split() == ['depth', 'depth_clear']


def test_planar_lidar_uses_2d_layers_with_unchanged_observation_ranges(config):
    """Keep planar scans separate from the depth camera's 3D occupancy."""
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
