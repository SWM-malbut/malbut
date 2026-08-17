"""Contract tests for target identity, sensor input, and motion ownership."""

import ast
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
AUTONOMY_ROOT = PACKAGE_ROOT.parent


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
    """Follower stays at action level and never overrides Nav2 velocity."""
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
    assert not (
        PACKAGE_ROOT / 'malbut_tracking' / 'tracking_twist_mixer_node.py'
    ).exists()
    assert 'FollowPath' in follower_source
    assert 'Spin' in follower_source

    config = yaml.safe_load(
        (PACKAGE_ROOT / 'config' / 'person_following.yaml').read_text(
            encoding='utf-8'
        )
    )['person_follower']['ros__parameters']
    assert config['use_sim_time'] is False


def test_follow_action_auto_selects_and_exposes_continuous_feedback():
    """Clients start auto-follow and observe the selected sensor track."""
    action = (
        AUTONOMY_ROOT
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


def test_default_config_has_safe_loss_recovery_and_distance():
    """Loss recovery must allow a full path and one collision-checked scan."""
    raw = yaml.safe_load(
        (PACKAGE_ROOT / 'config' / 'person_following.yaml').read_text(
            encoding='utf-8'
        )
    )['person_follower']['ros__parameters']
    assert raw['minimum_distance_m'] < raw['desired_distance_m']
    assert raw['maximum_linear_speed_mps'] == 0.30
    assert raw['retreat_maximum_travel_m'] > 0.0
    assert raw['approach_prediction_horizon_s'] > 0.0
    assert raw['approach_speed_threshold_mps'] > 0.0
    assert raw['retreat_goal_update_distance_m'] < raw[
        'goal_update_distance_m'
    ]
    assert raw['retreat_goal_update_period_s'] < raw[
        'goal_update_period_s'
    ]
    assert raw['temporary_lost_timeout_s'] < 1.0
    assert raw['lidar_continuation_max_distance_m'] > 0.0
    assert raw['global_costmap_topic'] == '/global_costmap/costmap_raw'
    assert raw['static_map_topic'] == '/map'
    assert raw['obstacle_cost_threshold'] == 254
    assert raw['static_occupied_threshold'] == 65
    assert raw['static_exclusion_radius_m'] == pytest.approx(0.20)
    assert raw['cluster_radius_m'] > 0.0
    assert raw['tracker_process_variance'] > 0.0
    assert raw['tracker_measurement_variance'] > 0.0
    assert raw['mahalanobis_gate'] == 9.21
    assert raw['track_confirmation_hits'] >= 2
    assert raw['maximum_coast_time_s'] < raw['target_lost_timeout_s']
    assert 0.0 <= raw['camera_rebind_margin_m'] < raw[
        'camera_label_gate_m'
    ]
    assert raw['camera_label_gate_m'] == pytest.approx(0.40)
    assert raw['lidar_continuation_timeout_s'] <= raw[
        'maximum_coast_time_s'
    ]
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
    assert raw['temporary_lost_timeout_s'] < raw['target_lost_timeout_s']
    assert raw['recovery_direction_minimum_turn_rad'] >= 0.65
    assert 0.0 < raw['recovery_waypoint_tolerance_m'] <= 0.10
    assert raw['recovery_scan_angle_rad'] == pytest.approx(1.5 * 3.14159265)
    assert raw['recovery_spin_allowance_s'] >= 10.0


def test_target_selection_uses_sensor_position_not_exact_detection_id():
    """Changing detector IDs must not break a spatially continuous target."""
    node_source = (
        PACKAGE_ROOT
        / 'malbut_tracking'
        / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    assert 'detection.id !=' not in node_source
    assert 'select_target_candidate' in node_source
    selection_source = node_source.split(
        '    def _select_target_observation(', 1
    )[1].split('    def _target_in_global_frame(', 1)[0]
    assert 'lidar_continuation_max_distance_m' not in selection_source
    assert '_costmap_tracker.predict_target' not in selection_source
    assert 'Detection3DArray' in node_source
    assert 'CostmapTargetTracker' in node_source
    assert 'TargetMotionEstimator' in node_source
    assert '_on_global_costmap' in node_source
    assert 'Continue a camera-labeled target' in node_source
    assert 'labeled.track.position' in node_source
    assert '_accept_map_target' in node_source
    assert '_accept_costmap_observation' not in node_source
    assert "source='bearing' if bearing_only else 'camera'" in node_source
    assert '_request_last_seen_recovery' in node_source
    assert 'recovery=True' in node_source
    assert '_accept_lidar_continuation' in node_source
    assert 'self._last_seen_s = now_s' in node_source
    assert 'camera_age_s <= settings.temporary_lost_timeout_s' in node_source
    assert '_start_recovery_scan' in node_source
    assert "'recovery_scan_angle_rad'" in node_source
    assert 'directed_search_offsets' not in node_source
    assert 'coarse_maximum_travel_m' in node_source
    assert 'should_update_goal' in node_source
    assert 'self._nav2.follow_path(' in node_source
    assert 'Exact detection-time TF unavailable' in node_source
    assert 'model_pose' not in node_source


def test_nav2_owns_path_geometry_and_body_rotation():
    """No downstream mixer may replace the controller's angular command."""
    node_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    sampling_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'path_sampling.py'
    ).read_text(encoding='utf-8')
    assert 'self._path_planner.compute(' in node_source
    assert 'truncate_path(' in node_source
    assert 'requested_position = (' in node_source
    assert 'target_position if planning_to_target' in node_source
    assert '0.0\n                if planning_to_target' in node_source
    assert 'tracking_controller_id' in node_source
    assert 'Camera control is deliberately handled downstream' not in (
        sampling_source
    )
    assert 'tracking_twist_mixer' not in node_source
    assert "tracking_controller_id', 'FollowPath'" in node_source


def test_waiting_for_first_person_does_not_start_blind_search():
    """Recovery is allowed only after a sensor target was acquired."""
    node_source = (
        PACKAGE_ROOT
        / 'malbut_tracking'
        / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    waiting_guard = node_source.index('if self._last_seen_s is None:')
    recovery_call = node_source.index(
        'self._request_last_seen_recovery(now_s)', waiting_guard
    )
    assert waiting_guard < recovery_call


def test_loss_recovery_is_a_small_ordered_nav2_state_machine():
    """Recovery keeps the waypoint, turns once, then escalates by distance."""
    node_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    result_handler = node_source[node_source.index('def _on_nav2_result'):]
    assert "REACHING_WAYPOINT = 'REACHING_WAYPOINT'" in node_source
    assert "TURNING_TO_TARGET = 'TURNING_TO_TARGET'" in node_source
    assert (
        "REACHING_LAST_POSITION = 'REACHING_LAST_POSITION'"
        in node_source
    )
    begin_recovery = node_source[
        node_source.index('def _begin_loss_recovery'):
        node_source.index('def _start_direction_turn')
    ]
    assert 'self._path_planner.cancel()' in begin_recovery
    assert 'self._nav2.cancel()' not in begin_recovery
    assert 'self._camera_estimator.predict(' in begin_recovery
    assert 'FollowState.REACHING_WAYPOINT' in begin_recovery
    tick = node_source[
        node_source.index('def _tick'):
        node_source.index('def _reset_recovery')
    ]
    assert "'recovery_waypoint_tolerance_m'" in tick
    assert 'self._last_goal_position' in tick
    assert 'self._start_direction_turn()' in tick
    assert 'self._start_direction_turn()' in result_handler
    assert 'normalize_angle(target_yaw - robot_yaw)' in node_source
    assert "'recovery_direction_minimum_turn_rad'" in node_source
    assert 'math.copysign(' in node_source
    assert 'self._request_last_seen_recovery(' in result_handler
    assert 'self._start_recovery_scan()' in result_handler
    assert 'self._recovery_turn_sign' in node_source
    assert 'search_step' not in node_source
    assert 'search_offsets' not in node_source
