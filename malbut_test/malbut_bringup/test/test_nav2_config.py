"""Check the robot Nav2 copy without starting navigation or changing tuning."""

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
    assert amcl['scan_topic'] == 'scan_raw'
    assert config['map_server']['ros__parameters']['yaml_filename'] == ''
    planner = config['planner_server']['ros__parameters']
    assert planner['planner_plugins'] == ['GridBased']
    assert planner['GridBased']['plugin'] == 'nav2_navfn_planner/NavfnPlanner'
    assert planner['GridBased']['tolerance'] == 0.5


def test_vendor_costmaps_and_final_velocity_limits_are_preserved(config):
    """Integration fixes must not silently expand real-robot motion limits."""
    for scope, frame, resolution, inflation in (
            ('local_costmap', 'odom', 0.03, 0.15),
            ('global_costmap', 'map', 0.05, 0.2)):
        costmap = config[scope][scope]['ros__parameters']
        assert costmap['global_frame'] == frame
        assert costmap['robot_base_frame'] == 'base_footprint'
        assert costmap['use_sim_time'] is False
        assert costmap['robot_radius'] == 0.08
        assert costmap['resolution'] == resolution
        assert costmap['inflation_layer']['inflation_radius'] == inflation
        layer = 'voxel_layer' if scope == 'local_costmap' else 'obstacle_layer'
        scan = costmap[layer]['scan']
        assert scan['topic'] == '/scan_raw'
        assert scan['marking'] is True
        assert scan['clearing'] is True
    smoother = config['velocity_smoother']['ros__parameters']
    assert smoother['max_velocity'] == [0.26, 0.0, 1.0]
    assert smoother['min_velocity'] == [-0.26, 0.0, -1.0]
    assert smoother['max_accel'] == [2.5, 0.0, 3.2]
    assert smoother['max_decel'] == [-2.5, 0.0, -3.2]
