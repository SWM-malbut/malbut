"""Contract tests for target identity, sensor input, and motion ownership."""

import ast
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
AUTONOMY_ROOT = PACKAGE_ROOT.parent
LIDAR_ROOT = AUTONOMY_ROOT / 'malbut_lidar_preprocessor'


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


def test_follow_action_exposes_only_target_selection_and_follow_distance():
    """Action clients choose whom to follow, not deployment safety policy."""
    action = (
        AUTONOMY_ROOT
        / 'malbut_interfaces'
        / 'action'
        / 'FollowPerson.action'
    ).read_text(encoding='utf-8')
    goal, _, feedback = action.split('---')
    assert 'uint8 VISIBLE_PERSON=0' in goal
    assert 'uint8 REGISTERED_PERSON=1' in goal
    assert 'uint8 target_mode' in goal
    assert 'string target_person_id' in goal
    assert 'float32 desired_distance_m' in goal
    assert 'minimum_distance_m' not in goal
    assert 'maximum_linear_speed_mps' not in goal
    assert 'target_lost_timeout' not in goal
    assert 'string state' in feedback
    assert 'bool target_visible' in feedback
    assert 'observed_track_id' not in feedback
    assert 'estimated_target_pose' not in feedback


def test_command_trace_measures_sensor_to_follow_path_dispatch():
    """Latency trace must be diagnostic-only and timestamped at dispatch."""
    interface = (
        AUTONOMY_ROOT
        / 'malbut_interfaces'
        / 'msg'
        / 'TrackingCommandTrace.msg'
    ).read_text(encoding='utf-8')
    source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    assert 'builtin_interfaces/Time source_stamp' in interface
    assert 'builtin_interfaces/Time dispatch_stamp' in interface
    assert 'uint64 planning_started_steady_time_ns' in interface
    assert 'uint64 planning_finished_steady_time_ns' in interface
    assert 'uint64 dispatch_steady_time_ns' in interface
    dispatch_source = source.split(
        '    def _dispatch_tracking_path(', 1
    )[1].split('    def _turn_allowance(', 1)[0]
    assert '_publish_command_trace' in dispatch_source
    assert 'self._nav2.follow_path(' in dispatch_source
    assert dispatch_source.index(
        'self._nav2.follow_path('
    ) < dispatch_source.index(
        'self._publish_command_trace('
    )


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
    assert raw['minimum_follow_speed_mps'] == 0.10
    assert raw['maximum_linear_speed_mps'] == 0.40
    assert raw['full_speed_travel_distance_m'] == 1.50
    assert raw['approach_prediction_horizon_s'] > 0.0
    assert raw['approach_speed_threshold_mps'] > 0.0
    assert 'goal_update_distance_m' not in raw
    assert 'goal_update_minimum_period_s' not in raw
    assert 'goal_update_period_s' not in raw
    assert raw['observation_loss_debounce_s'] < 1.0
    assert raw['lidar_proximity_control_distance_m'] > raw[
        'desired_distance_m'
    ]
    assert 0.0 < raw['lidar_proximity_camera_guard_s'] < 0.5
    assert raw['global_costmap_topic'] == '/global_costmap/costmap_raw'
    assert raw['static_map_topic'] == '/map'
    assert raw['static_occupied_threshold'] == 65
    assert raw['static_padding_radius_m'] == pytest.approx(0.35)
    assert 'target_path_history_duration_s' not in raw
    assert raw['lidar_clusters_topic'] == (
        '/perception/lidar/foreground_clusters'
    )
    assert raw['odometry_frame'] == 'odom'
    assert raw['tracker_process_variance'] > 0.0
    assert raw['tracker_measurement_variance'] > 0.0
    assert raw['mahalanobis_gate'] == 9.21
    assert raw['track_confirmation_hits'] >= 2
    assert raw['maximum_coast_time_s'] > 0.0
    assert 0.0 <= raw['camera_rebind_margin_m'] < raw[
        'camera_label_gate_m'
    ]
    assert raw['camera_label_gate_m'] == pytest.approx(0.40)
    assert raw['lidar_candidate_gate_m'] > raw['camera_label_gate_m']
    assert 0.0 < raw['camera_lidar_fusion_freshness_s'] < raw[
        'observation_loss_debounce_s'
    ]
    assert raw['camera_lidar_range_gate_m'] > raw['camera_label_gate_m']
    assert 0.0 <= raw['camera_lidar_extent_padding_m'] < raw[
        'camera_label_gate_m'
    ]
    assert raw['lidar_reassociation_max_distance_m'] > raw[
        'lidar_candidate_gate_m'
    ]
    assert raw['dynamic_rebind_minimum_speed_mps'] > 0.0
    assert raw['camera_negative_evidence_frames'] >= 2
    assert 0.0 < raw['camera_horizontal_fov_rad'] < 3.14159265
    assert 'lidar_continuation_timeout_s' not in raw
    assert 'lidar_continuation_max_distance_m' not in raw
    assert 'coarse_goal_update_distance_m' not in raw
    assert 'coarse_goal_update_period_s' not in raw
    assert 'bearing_goal_update_distance_m' not in raw
    assert 'bearing_goal_update_period_s' not in raw
    assert 'target_lost_timeout_s' not in raw
    assert raw['recovery_direction_minimum_turn_rad'] >= 0.65
    assert 0.0 < raw['recovery_waypoint_tolerance_m'] <= 0.10
    assert raw['recovery_scan_angle_rad'] == pytest.approx(1.5 * 3.14159265)
    assert raw['recovery_spin_allowance_s'] >= 10.0


def test_target_selection_separates_visible_and_registered_identity_modes():
    """Visible mode stays spatial; registered mode filters upstream IDs."""
    node_source = (
        PACKAGE_ROOT
        / 'malbut_tracking'
        / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    assert 'select_target_candidate' in node_source
    selection_source = node_source.split(
        '    def _select_target_observation(', 1
    )[1].split('    def _target_in_global_frame(', 1)[0]
    assert 'lidar_continuation_max_distance_m' not in selection_source
    assert '_obstacle_tracker.predict_target' not in selection_source
    assert 'Detection3DArray' in node_source
    assert 'FollowPerson.Goal.VISIBLE_PERSON' in node_source
    assert 'FollowPerson.Goal.REGISTERED_PERSON' in node_source
    assert 'detection.id != self._target_person_id' in selection_source
    assert 'preferred_track_id' not in selection_source
    assert 'ObstacleTargetTracker' in node_source
    assert 'TargetMotionEstimator' in node_source
    assert '_on_global_costmap' in node_source
    assert '_on_static_map' in node_source
    assert 'plan_static_path' in node_source
    assert '_on_lidar_clusters' in node_source
    assert 'LidarClusterArray' in node_source
    assert 'LaserScan' not in node_source
    assert 'Continue a camera-labeled target' in node_source
    assert 'labeled.track.position' in node_source
    assert '_accept_map_target' in node_source
    assert '_accept_costmap_observation' not in node_source
    assert "else 'camera_lidar'" in node_source
    assert '_request_last_seen_recovery' in node_source
    recovery_source = node_source.split(
        '    def _request_last_seen_recovery(', 1
    )[1].split('    def _publish_command_trace(', 1)[0]
    assert "'last_seen_recovery'" in recovery_source
    assert 'self._path_planner.compute(' in recovery_source
    assert 'self._recovery_last_position' in recovery_source
    assert '_accept_lidar_continuation' in node_source
    assert '_accept_lidar_proximity' in node_source
    assert "self._tracking_source = 'lidar_proximity'" in node_source
    assert 'or labeled.track.misses != 0' in node_source
    assert 'fuse_camera_bearing_with_lidar_range' in node_source
    assert 'self._last_seen_s = now_s' in node_source
    assert 'camera_age_s <= settings.observation_loss_debounce_s' in (
        node_source
    )
    continuation_source = node_source.split(
        '    def _accept_lidar_continuation(', 1
    )[1].split('    def _accept_lidar_proximity(', 1)[0]
    assert 'labeled.track.misses != 0' in continuation_source
    assert 'lidar_continuation_timeout_s' not in continuation_source
    assert '_camera_estimator.predict(' not in continuation_source
    assert '_start_recovery_scan' in node_source
    assert "'recovery_scan_angle_rad'" in node_source
    assert 'directed_search_offsets' not in node_source
    assert 'should_update_goal' not in node_source
    assert 'self._path_planner.busy' in node_source
    assert '_plan_latest_observation_if_pending' in node_source
    assert 'sensor_transform_queue_timeout_s' in node_source
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
    )[1].split('    def _on_lidar_clusters(', 1)[0]
    lidar_callback = node_source.split(
        '    def _on_lidar_clusters(', 1
    )[1].split('    def _fresh_precise_camera_position(', 1)[0]

    assert '_latest_global_costmap = grid' in costmap_callback
    assert '_obstacle_tracker' not in costmap_callback
    assert 'extract_foreground_clusters' not in costmap_callback
    assert 'distance(cluster.position, gate_center)' in lidar_callback
    assert '_obstacle_tracker.update(clusters' in lidar_callback
    assert 'if labeled is None:' in lidar_callback
    assert lidar_callback.index('if labeled is None:') < lidar_callback.index(
        '_rebind_unique_moving_lidar_target'
    )


def test_lidar_front_end_uses_humble_projection_and_cached_static_map():
    """Raw scan geometry stays in C++ and publishes only compact clusters."""
    source = (
        LIDAR_ROOT / 'src' / 'lidar_foreground_preprocessor.cpp'
    ).read_text(encoding='utf-8')
    config = yaml.safe_load(
        (LIDAR_ROOT / 'config' / 'lidar_foreground.yaml').read_text(
            encoding='utf-8'
        )
    )['lidar_foreground_preprocessor']['ros__parameters']

    assert 'transformLaserScanToPointCloud' in source
    assert 'pending_scan_' in source
    assert 'process_pending_scan' in source
    assert 'std::chrono::milliseconds(20)' in source
    assert 'sensor_transform_queue_timeout_s' in source
    assert 'laser_geometry::channel_option::Index' in source
    assert 'cv::distanceTransform' in source
    assert 'static_clearance' in source
    assert 'LidarClusterArray' in source
    assert 'Costmap' not in source
    assert config['sensor_transform_queue_timeout_s'] == pytest.approx(0.30)
    assert config['scan_topic'] == '/scan'
    assert config['static_map_topic'] == '/map'
    assert config['clusters_topic'] == (
        '/perception/lidar/foreground_clusters'
    )
    assert config['minimum_cluster_points'] >= 3


def test_nav2_owns_path_geometry_and_body_rotation():
    """No downstream mixer may replace the controller's angular command."""
    node_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    sampling_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'path_sampling.py'
    ).read_text(encoding='utf-8')
    assert 'self._path_planner.compute(' in node_source
    assert 'if self._path_planner.busy:' in node_source
    assert 'truncate_path_at_target_distance(' not in node_source
    assert "travel_description = 'selected safe tracking goal'" in node_source
    assert 'requested_position = (' in node_source
    assert 'target_position if planning_to_target' in node_source
    assert 'static_path=static_path' in node_source
    assert 'recent_target_path' not in node_source
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
    loss_handler = node_source.split(
        '    def _on_loss_timer(', 1
    )[1].split('    def _plan_latest_observation_if_pending(', 1)[0]
    assert 'or self._last_seen_s is None' in loss_handler
    assert 'self._begin_loss_recovery(now_s)' in loss_handler
    assert 'self._loss_timer.reset()' in node_source


def test_loss_recovery_is_a_small_ordered_nav2_state_machine():
    """Recovery keeps the waypoint, turns once, then escalates by distance."""
    node_source = (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
    result_handler = node_source[node_source.index('def _on_nav2_result'):]
    assert "STOPPED = 'STOPPED'" in node_source
    assert "IDLE = 'IDLE'" in node_source
    assert "TRACKING = 'TRACKING'" in node_source
    assert "RECOVERING = 'RECOVERING'" in node_source
    assert "TARGET_LOST = 'TARGET_LOST'" not in node_source
    assert '_ALLOWED_FOLLOW_TRANSITIONS' in node_source
    assert 'Rejecting invalid follow-state transition' in node_source
    public_state_source = node_source.split('class FollowState:', 1)[1].split(
        'class RecoveryPhase:', 1
    )[0]
    assert 'REACHING_WAYPOINT' not in public_state_source
    assert 'SEARCHING' not in public_state_source
    assert "FINISHING_WAYPOINT = 'FINISHING_WAYPOINT'" in node_source
    assert "TURNING_TO_TARGET = 'TURNING_TO_TARGET'" in node_source
    assert "REACHING_LAST_POSITION = 'REACHING_LAST_POSITION'" in node_source
    assert "SCANNING = 'SCANNING'" in node_source
    begin_recovery = node_source[
        node_source.index('def _begin_loss_recovery'):
        node_source.index('def _start_direction_turn')
    ]
    assert 'self._path_planner.cancel()' in begin_recovery
    assert 'self._nav2.cancel()' not in begin_recovery
    assert 'self._last_target_pose.pose.position.x' in begin_recovery
    assert 'self._last_target_pose.pose.position.y' in begin_recovery
    assert 'self._camera_estimator.predict(' not in begin_recovery
    assert 'self._last_motion_target' not in begin_recovery
    assert "self._tracking_source = 'last_seen_recovery'" in begin_recovery
    assert 'RecoveryPhase.FINISHING_WAYPOINT' in begin_recovery
    assert 'FollowState.RECOVERING' in begin_recovery
    assert 'self._request_last_seen_recovery(now_s)' in begin_recovery
    assert 'self._start_direction_turn()' not in begin_recovery
    assert 'def _tick' not in node_source
    assert 'create_timer(0.05' not in node_source
    cancel_guard = node_source.split(
        '    def _on_cancel_guard(', 1
    )[1].split('    def _handle_accepted(', 1)[0]
    assert "self._cancel_follow_action('follow action canceled')" in (
        cancel_guard
    )
    nav_feedback = node_source.split(
        '    def _on_nav2_feedback(', 1
    )[1].split('    def _on_nav2_result(', 1)[0]
    assert "'recovery_waypoint_tolerance_m'" in nav_feedback
    assert 'self._start_direction_turn()' in nav_feedback
    assert 'continuing directed search' in result_handler
    assert 'FollowState.TARGET_LOST' not in node_source
    assert 'self._start_direction_turn()' in result_handler
    assert 'status == GoalStatus.STATUS_SUCCEEDED' in result_handler
    assert 'normalize_angle(target_yaw - robot_yaw)' in node_source
    assert "'recovery_direction_minimum_turn_rad'" in node_source
    assert 'math.copysign(' in node_source
    assert 'self._request_last_seen_recovery(' in result_handler
    assert 'self._start_recovery_scan()' in result_handler
    recovery_request = node_source[
        node_source.index('def _request_last_seen_recovery'):
        node_source.index('def _publish_command_trace')
    ]
    assert 'self._start_recovery_scan()' not in recovery_request
    assert 'self._recovery_turn_sign' in node_source
    assert 'self._last_camera_bearing_rad' in node_source
    assert 'directed_recovery_turn(' in begin_recovery
    direction_turn = node_source[
        node_source.index('def _start_direction_turn'):
        node_source.index('def _request_last_seen_recovery')
    ]
    assert 'turn_angle = raw_turn_angle' in direction_turn
    assert 'self._recovery_initial_turn_rad' not in node_source
    assert 'def _publish_predicted_target' not in node_source
    assert 'self._publish_predicted_target(' not in node_source
    accept_target = node_source[
        node_source.index('def _accept_map_target'):
        node_source.index('def _select_target_observation')
    ]
    assert 'self._path_planner.cancel()' in accept_target
    assert 'search_step' not in node_source
    assert 'search_offsets' not in node_source
