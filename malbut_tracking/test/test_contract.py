"""Contract tests for target identity, sensor input, and motion ownership."""

import ast
from pathlib import Path
from xml.etree import ElementTree

import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parent


def test_runtime_config_matches_declared_node_defaults():
    """Keep the installed YAML and code fallback values synchronized."""
    source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    tree = ast.parse(source)
    declarations = {}
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        if not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr != 'declare_parameter' or len(call.args) < 2:
            continue
        try:
            name = ast.literal_eval(call.args[0])
            default = ast.literal_eval(call.args[1])
        except (ValueError, SyntaxError):
            continue
        declarations[name] = default

    configured = yaml.safe_load(
        (PACKAGE_ROOT / 'config' / 'person_following.yaml').read_text(
            encoding='utf-8'
        )
    )['person_follower']['ros__parameters']
    configured.pop('use_sim_time')
    assert configured == declarations


def test_tracking_is_robot_reusable_and_never_publishes_velocity():
    """Follower stays at Nav2 action level and never owns robot velocity."""
    package = ElementTree.parse(PACKAGE_ROOT / 'package.xml').getroot()
    dependencies = {
        element.text
        for tag in ('depend', 'exec_depend')
        for element in package.findall(tag)
    }
    follower_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    assert 'malbut_gazebo' not in dependencies
    assert 'spatio_temporal_voxel_layer' not in dependencies
    assert '/cmd_vel' not in follower_source
    assert 'FollowPath' in follower_source
    assert 'Spin' not in follower_source

    config = yaml.safe_load(
        (PACKAGE_ROOT / 'config' / 'person_following.yaml').read_text(
            encoding='utf-8'
        )
    )['person_follower']['ros__parameters']
    assert config['use_sim_time'] is False


def test_follow_action_auto_selects_and_exposes_continuous_feedback():
    """Clients start auto-follow and observe the selected sensor track."""
    action = (
        REPOSITORY_ROOT
        / 'malbut_interfaces'
        / 'action'
        / 'FollowPerson.action'
    ).read_text(encoding='utf-8')
    goal, _, feedback = action.split('---')
    assert 'target_id' not in goal
    assert 'string state' in feedback
    assert 'bool target_visible' in feedback
    assert 'string observed_track_id' in feedback
    assert 'geometry_msgs/PoseStamped estimated_target_pose' in feedback


def test_default_config_has_ordered_loss_timeouts_and_safe_distance():
    """Default loss handling must recover, then terminate in order."""
    raw = yaml.safe_load(
        (PACKAGE_ROOT / 'config' / 'person_following.yaml').read_text(
            encoding='utf-8'
        )
    )['person_follower']['ros__parameters']
    assert raw['minimum_distance_m'] < raw['desired_distance_m']
    assert raw['maximum_linear_speed_mps'] == 0.30
    assert raw['retreat_maximum_travel_m'] > 0.0
    assert raw['temporary_lost_timeout_s'] < 1.0
    assert raw['association_max_distance_m'] > 0.0
    assert raw['global_costmap_topic'] == '/global_costmap/costmap_raw'
    assert raw['static_map_topic'] == '/map'
    assert raw['obstacle_cost_threshold'] == 254
    assert raw['static_occupied_threshold'] == 65
    assert raw['cluster_radius_m'] > 0.0
    assert raw['tracker_process_variance'] > 0.0
    assert raw['tracker_measurement_variance'] > 0.0
    assert raw['mahalanobis_gate'] == 9.21
    assert raw['track_confirmation_hits'] >= 2
    assert raw['maximum_coast_time_s'] < raw['target_lost_timeout_s']
    assert raw['coarse_goal_update_distance_m'] > raw[
        'goal_update_distance_m'
    ]
    assert raw['coarse_goal_update_period_s'] >= raw[
        'goal_update_period_s'
    ]
    assert raw['precise_maximum_travel_m'] > 0.0
    assert raw['coarse_maximum_travel_m'] > raw[
        'precise_maximum_travel_m'
    ]
    assert (
        raw['temporary_lost_timeout_s']
        < raw['recovery_start_timeout_s']
        < raw['target_lost_timeout_s']
    )
    assert 0.0 < raw['recovery_observation_fraction'] < 1.0
    assert raw['tracking_controller_id'] == 'FollowPath'


def test_target_selection_uses_sensor_position_not_exact_detection_id():
    """Changing detector IDs must not break a spatially continuous target."""
    node_source = (
        PACKAGE_ROOT
        / 'malbut_tracking'
        / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    assert 'detection.id !=' not in node_source
    assert 'select_target_candidate' in node_source
    assert 'Detection3DArray' in node_source
    assert 'CostmapTargetTracker' in node_source
    assert 'TargetMotionEstimator' in node_source
    assert '_on_global_costmap' in node_source
    assert 'Never create motion from a costmap callback alone' in node_source
    assert 'labeled.track.position' in node_source
    assert '_accept_camera_observation' in node_source
    assert '_begin_recovery' in node_source
    assert '_on_recovery_path' in node_source
    assert 'coarse_maximum_travel_m' in node_source
    assert 'should_update_goal' in node_source
    assert 'self._nav2.follow_path(' in node_source
    assert 'Exact detection-time TF unavailable' in node_source
    assert 'model_pose' not in node_source


def test_planner_path_owns_translation_and_heading():
    """Perception must not replace planner geometry or controller heading."""
    node_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    sampling_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'path_sampling.py'
    ).read_text(encoding='utf-8')
    assert 'self._path_planner.compute(' in node_source
    assert 'truncate_path(' in node_source
    assert 'tracking_controller_id' in node_source
    assert (
        'without changing its heading'
        in sampling_source
    )
    assert 'No separate camera-yaw command competes' in node_source


def test_waiting_for_first_person_does_not_start_blind_recovery():
    """Recovery motion is allowed only after a sensor target was acquired."""
    node_source = (
        PACKAGE_ROOT
        / 'malbut_tracking'
        / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    waiting_guard = node_source.index('if self._last_seen_s is None:')
    recovery_call = node_source.index('self._begin_recovery(', waiting_guard)
    assert waiting_guard < recovery_call
