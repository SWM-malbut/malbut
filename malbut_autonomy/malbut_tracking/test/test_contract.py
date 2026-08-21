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
    assert raw['desired_distance_m'] == pytest.approx(1.0)
    assert raw['distance_tolerance_m'] == pytest.approx(0.10)
    assert raw['alignment_angle_tolerance_rad'] > 0.0
    assert raw['maximum_linear_speed_mps'] == 0.30
    assert 0.0 < raw['retreat_maximum_travel_m'] <= 0.15
    assert raw['approach_prediction_horizon_s'] > 0.0
    assert raw['approach_speed_threshold_mps'] > 0.0
    assert raw['retreat_goal_update_distance_m'] < raw[
        'goal_update_distance_m'
    ]
    assert raw['retreat_goal_update_period_s'] < raw[
        'goal_update_period_s'
    ]
    assert raw['retreat_goal_update_period_s'] <= 0.20
    assert raw['temporary_lost_timeout_s'] < 1.0
    assert raw['lidar_continuation_max_distance_m'] > 0.0
    assert raw['lidar_proximity_control_distance_m'] > raw[
        'desired_distance_m'
    ]
    assert 0.0 < raw['lidar_proximity_camera_guard_s'] < 0.5
    assert raw['global_costmap_topic'] == '/global_costmap/costmap_raw'
    assert raw['static_map_topic'] == '/map'
    assert raw['scan_topic'] == '/scan'
    assert raw['odometry_frame'] == 'odom'
    assert raw['static_occupied_threshold'] == 65
    assert raw['static_exclusion_radius_m'] == pytest.approx(0.20)
    assert raw['cluster_gap_m'] > 0.0
    assert raw['minimum_cluster_points'] >= 3
    assert raw['minimum_cluster_density_points_per_m'] > 0.0
    assert raw['tracker_process_variance'] > 0.0
    assert raw['tracker_measurement_variance'] > 0.0
    assert raw['mahalanobis_gate'] == 9.21
    assert raw['track_confirmation_hits'] >= 2
    assert raw['maximum_coast_time_s'] < raw['target_lost_timeout_s']
    assert 0.0 <= raw['camera_rebind_margin_m'] < raw[
        'camera_label_gate_m'
    ]
    assert raw['camera_label_gate_m'] == pytest.approx(0.40)
    assert raw['lidar_candidate_gate_m'] > raw['camera_label_gate_m']
    assert 0.0 < raw['camera_lidar_fusion_freshness_s'] < raw[
        'temporary_lost_timeout_s'
    ]
    assert 0.0 <= raw['camera_lidar_extent_padding_m'] < raw[
        'camera_label_gate_m'
    ]
    assert raw['camera_jump_confirmation_hits'] >= 2
    assert raw['camera_jump_base_gate_m'] > raw['camera_label_gate_m']
    assert raw['lidar_reassociation_max_distance_m'] > raw[
        'lidar_candidate_gate_m'
    ]
    assert raw['dynamic_rebind_minimum_speed_mps'] > 0.0
    assert raw['camera_negative_evidence_frames'] >= 2
    assert 0.0 < raw['camera_horizontal_fov_rad'] < 3.14159265
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
    assert '_obstacle_tracker.predict_target' not in selection_source
    assert 'Detection3DArray' in node_source
    assert 'ObstacleTargetTracker' in node_source
    assert 'TargetMotionEstimator' in node_source
    assert '_on_global_costmap' in node_source
    assert '_on_scan' in node_source
    assert 'extract_foreground_clusters' in node_source
    assert 'StaticDistanceField.build' in node_source
    assert 'Continue a camera-labeled target' in node_source
    assert 'labeled.track.position' in node_source
    assert '_accept_map_target' in node_source
    assert '_accept_costmap_observation' not in node_source
    assert "source='bearing' if bearing_only else 'camera'" in node_source
    assert '_request_last_seen_recovery' in node_source
    assert 'recovery=True' in node_source
    assert '_accept_lidar_continuation' in node_source
    assert '_accept_lidar_proximity' in node_source
    assert "self._tracking_source = 'lidar_proximity'" in node_source
    assert 'self._last_seen_s = now_s' in node_source
    assert 'camera_age_s <= settings.temporary_lost_timeout_s' in node_source
    assert '_start_recovery_scan' in node_source
    assert "'recovery_scan_angle_rad'" in node_source
    assert 'directed_search_offsets' not in node_source
    assert 'coarse_maximum_travel_m' in node_source
    assert 'should_update_goal' in node_source
    assert 'self._nav2.follow_path(' in node_source
    assert 'def _align_with_target' in node_source
    assert 'self._nav2.spin(turn_angle, allowance)' in node_source
    assert 'using latest localization TF' not in node_source
    assert 'spin_thread=True' in node_source
    assert 'lookup_transform_full' in node_source
    assert 'model_pose' not in node_source


def test_master_costmap_is_not_used_as_dynamic_object_measurement():
    """Merged static, keepout, and inflation costs stay out of tracking."""
    node_source = (
        PACKAGE_ROOT
        / 'malbut_tracking'
        / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    costmap_callback = node_source.split(
        '    def _on_global_costmap(', 1
    )[1].split('    def _on_scan(', 1)[0]
    scan_callback = node_source.split(
        '    def _on_scan(', 1
    )[1].split('    def _scan_transform(', 1)[0]

    assert '_latest_global_costmap = grid' in costmap_callback
    assert '_obstacle_tracker' not in costmap_callback
    assert 'extract_foreground_clusters' not in costmap_callback
    assert 'extract_foreground_clusters' in scan_callback
    assert 'distance(cluster.position, gate_center)' in scan_callback
    assert '_obstacle_tracker.update(clusters' in scan_callback


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


def test_dropped_lidar_scans_are_never_silent():
    """
    Discard a scan only after a warning that says how long it waited.

    The exact-time lookup fails whenever the scan stamp runs ahead of TF, and
    the elapsed time is then measured from the queue, not from the stamp: a
    future stamp makes the stamp-based age negative, so a stamp-based guard
    would discard every scan without ever warning.
    """
    node_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    handler = node_source[
        node_source.index('def _process_pending_scan'):
        node_source.index('def _process_scan')
    ]
    assert 'self._now_seconds() - self._pending_scan_queued_s' in handler
    assert '_stamp_seconds(message' not in handler
    drop = handler[handler.index('self._scan_transform_drops += 1'):]
    assert "'lidar_transform_timeout'" in drop
    assert 'self._warn_periodically(' in drop
    assert 'self._pending_scan_queued_s = self._now_seconds()' in node_source


def test_degraded_scan_transform_is_bounded_and_off_by_default():
    """
    Bound the newest-TF substitution, which drops ego-motion compensation.

    It stays available for a runtime whose TF lags behind its scans, but only
    within an explicit lag allowance, and never without the operator asking.
    """
    node_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    assert (
        "self.declare_parameter('scan_transform_max_tf_lag_s', 0.0)"
        in node_source
    )
    fallback = node_source[
        node_source.index('def _degraded_scan_transform'):
        node_source.index('def _scan_transform')
    ]
    assert 'if allowance_s <= 0.0:' in fallback
    assert 'return None' in fallback
    assert 'if lag_s > allowance_s:' in fallback
    handler = node_source[
        node_source.index('def _process_pending_scan'):
        node_source.index('def _process_scan')
    ]
    assert "'lidar_transform_degraded'" in handler


def test_declare_parameters_only_declares_parameters():
    """
    Keep runtime state out of the parameter-declaration pass.

    ``__init__`` calls ``_declare_parameters`` first and then assigns its
    attributes, so an object built inside that pass is silently overwritten
    by the later assignment. That is invisible at import time and disables
    whatever depends on the object for the entire run.
    """
    node_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    tree = ast.parse(node_source)
    node_class = next(
        item for item in ast.walk(tree)
        if isinstance(item, ast.ClassDef) and item.name == 'PersonFollowerNode'
    )
    declare = next(
        item for item in node_class.body
        if isinstance(item, ast.FunctionDef)
        and item.name == '_declare_parameters'
    )
    for statement in ast.walk(declare):
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        for target in targets:
            assert not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == 'self'
            ), (
                f'_declare_parameters assigns self.{target.attr}; '
                '__init__ overwrites it afterwards'
            )


def test_lidar_acquisition_turn_covers_both_blind_waits():
    """
    Try the acquisition turn wherever the follower waits on the camera.

    The camera wedge is narrower than the LiDAR, so a person outside it can
    never be confirmed while the robot faces elsewhere. That blind wait
    happens twice: before the first acquisition, and again after a target is
    lost. Handling only the first leaves the same defect in the second.
    """
    node_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    tick = node_source[
        node_source.index('def _tick'):
        node_source.index('def _reset_recovery')
    ]
    waiting_first = tick.index('if self._last_seen_s is None:')
    assert (
        tick.index('self._try_lidar_acquisition_turn(now_s)', waiting_first)
        > waiting_first
    )
    lost = tick.index('if self._state == FollowState.TARGET_LOST:')
    assert (
        tick.index('self._try_lidar_acquisition_turn(now_s)', lost) > lost
    )
    assert tick.count('self._try_lidar_acquisition_turn(now_s)') == 2
