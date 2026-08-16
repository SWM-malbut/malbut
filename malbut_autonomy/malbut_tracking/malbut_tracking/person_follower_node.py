"""Follow one RGB-D person track safely by delegating motion to Nav2."""

import json
import math
import sys

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import Pose, PoseStamped
from malbut_interfaces.action import FollowPerson
from nav2_msgs.msg import Costmap, SpeedLimit
from nav_msgs.msg import OccupancyGrid
import rclpy
from rclpy.action import (
    ActionServer,
    CancelResponse,
    GoalResponse,
)
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.task import Future
from rclpy.time import Time
from std_msgs.msg import String
from tf2_geometry_msgs import do_transform_pose
from tf2_ros import Buffer, TransformException, TransformListener
from vision_msgs.msg import Detection3D, Detection3DArray
from visualization_msgs.msg import Marker, MarkerArray

from .costmap_tracking import (
    CostmapGrid,
    CostmapTargetTracker,
    LabeledObstacle,
)
from .follow_policy import (
    FollowCommand,
    FollowSettings,
    decide_follow_motion,
    should_update_goal,
    target_loss_timed_out,
)
from .geometry import (
    Point2D,
    distance,
    normalize_angle,
    quaternion_to_yaw,
    yaw_to_quaternion,
)
from .goal_safety import project_navigation_goal
from .motion_estimator import TargetMotionEstimator
from .navigation import MotionMode, Nav2MotionClient, Nav2PathClient
from .path_sampling import truncate_path
from .target_association import (
    TargetCandidate,
    select_target_candidate,
)


class FollowState:
    """Stable state names published through action feedback and status."""

    IDLE = 'IDLE'
    TRACKING = 'TRACKING'
    REACHING_WAYPOINT = 'REACHING_WAYPOINT'
    TURNING_TO_TARGET = 'TURNING_TO_TARGET'
    REACHING_LAST_POSITION = 'REACHING_LAST_POSITION'
    SEARCHING = 'SEARCHING'
    TARGET_LOST = 'TARGET_LOST'
    STOPPED = 'STOPPED'


def _duration_seconds(duration) -> float:
    return float(duration.sec) + float(duration.nanosec) * 1e-9


def _stamp_seconds(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


class PersonFollowerNode(Node):
    """Fuse RGB-D person positions with costmap refinement and follow."""

    def __init__(self) -> None:
        """Create sensor input, follow action, TF, status, and Nav2 clients."""
        super().__init__('person_follower')
        self._declare_parameters()
        self._validate_parameters()

        self._global_frame = str(self.get_parameter('global_frame').value)
        self._robot_frame = str(self.get_parameter('robot_frame').value)
        self._tf_timeout = float(
            self.get_parameter('transform_timeout_s').value
        )
        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)

        self._costmap_tracker = CostmapTargetTracker(
            float(self.get_parameter('cluster_radius_m').value),
            int(self.get_parameter('obstacle_cost_threshold').value),
            int(self.get_parameter('minimum_cluster_cells').value),
            int(self.get_parameter('maximum_cluster_cells').value),
            float(self.get_parameter('maximum_cluster_extent_m').value),
            int(self.get_parameter('static_occupied_threshold').value),
            float(self.get_parameter('static_exclusion_radius_m').value),
            float(self.get_parameter('tracker_process_variance').value),
            float(self.get_parameter('tracker_measurement_variance').value),
            float(self.get_parameter('mahalanobis_gate').value),
            int(self.get_parameter('track_confirmation_hits').value),
            int(self.get_parameter('maximum_missed_updates').value),
            float(self.get_parameter('maximum_coast_time_s').value),
            float(self.get_parameter('camera_label_gate_m').value),
            float(self.get_parameter('camera_rebind_margin_m').value),
        )
        self._camera_estimator = TargetMotionEstimator(
            float(self.get_parameter('camera_position_alpha').value),
            float(self.get_parameter('camera_velocity_alpha').value),
            float(self.get_parameter('maximum_person_speed_mps').value),
        )
        self._nav2 = Nav2MotionClient(
            self,
            str(self.get_parameter('follow_path_action').value),
            str(self.get_parameter('spin_action').value),
            self._on_nav2_result,
        )
        self._path_planner = Nav2PathClient(
            self,
            str(self.get_parameter('compute_path_action').value),
        )
        self._speed_publisher = self.create_publisher(
            SpeedLimit,
            str(self.get_parameter('speed_limit_topic').value),
            10,
        )
        self._status_publisher = self.create_publisher(
            String,
            str(self.get_parameter('status_topic').value),
            10,
        )
        self._target_pose_publisher = self.create_publisher(
            PoseStamped,
            str(self.get_parameter('target_pose_topic').value),
            10,
        )
        self._track_markers_publisher = self.create_publisher(
            MarkerArray,
            str(self.get_parameter('track_markers_topic').value),
            10,
        )
        self._detection_subscription = self.create_subscription(
            Detection3DArray,
            str(self.get_parameter('detections_topic').value),
            self._on_detections,
            10,
        )
        self._costmap_subscription = self.create_subscription(
            Costmap,
            str(self.get_parameter('global_costmap_topic').value),
            self._on_global_costmap,
            10,
        )
        map_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self._static_map_subscription = self.create_subscription(
            OccupancyGrid,
            str(self.get_parameter('static_map_topic').value),
            self._on_static_map,
            map_qos,
        )
        self._action_server = ActionServer(
            self,
            FollowPerson,
            str(self.get_parameter('action_name').value),
            execute_callback=self._execute_callback,
            goal_callback=self._goal_callback,
            cancel_callback=self._cancel_callback,
            handle_accepted_callback=self._handle_accepted,
        )

        self._active_goal = None
        self._result_future: Future | None = None
        self._settings: FollowSettings | None = None
        self._observed_track_id = ''
        self._detector_track_id = ''
        self._state = FollowState.IDLE
        self._last_seen_s: float | None = None
        self._last_camera_seen_s: float | None = None
        self._last_costmap_stamp_s: float | None = None
        self._latest_global_costmap: CostmapGrid | None = None
        self._latest_static_map: CostmapGrid | None = None
        self._last_goal_position: Point2D | None = None
        self._last_plan_target: Point2D | None = None
        self._last_plan_request_s = 0.0
        self._last_target_pose = PoseStamped()
        self._last_target_height = 0.0
        self._current_distance: float | None = None
        self._recovery_path_requested = False
        self._recovery_navigation_active = False
        self._recovery_scan_started = False
        self._recovery_scan_complete = False
        self._recovery_direction_target: Point2D | None = None
        self._last_observed_bearing_rad = 0.0
        self._recovery_turn_sign = 1.0
        self._last_motion_target: Point2D | None = None
        self._last_motion_precise = False
        self._last_motion_bearing_only = False
        self._goal_dispatch_count = 0
        self._navigation_retry_not_before_s = 0.0
        self._navigation_failure_count = 0
        self._tracking_source = 'none'
        self._last_speed_publish_s = -math.inf
        self._last_warning_s: dict[str, float] = {}
        self._timer = self.create_timer(0.1, self._tick)
        self._publish_status()
        self.get_logger().info(
            'Person follower ready; target motion is delegated to Nav2 only.'
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter('action_name', 'follow_person')
        self.declare_parameter(
            'detections_topic', '/perception/person/detections_3d'
        )
        self.declare_parameter(
            'global_costmap_topic', '/global_costmap/costmap_raw'
        )
        self.declare_parameter('static_map_topic', '/map')
        self.declare_parameter('status_topic', '/tracking/person/status')
        self.declare_parameter(
            'target_pose_topic', '/tracking/person/estimated_target_pose'
        )
        self.declare_parameter(
            'track_markers_topic', '/tracking/person/costmap_tracks'
        )
        self.declare_parameter('follow_path_action', 'follow_path')
        self.declare_parameter('compute_path_action', 'compute_path_to_pose')
        self.declare_parameter('planner_id', 'GridBased')
        self.declare_parameter('tracking_controller_id', 'FollowPath')
        self.declare_parameter('goal_checker_id', 'general_goal_checker')
        self.declare_parameter('spin_action', 'spin')
        self.declare_parameter('navigation_retry_delay_s', 0.75)
        self.declare_parameter('speed_limit_topic', 'speed_limit')
        self.declare_parameter('global_frame', 'map')
        self.declare_parameter('robot_frame', 'base_footprint')
        self.declare_parameter('minimum_confidence', 0.20)
        self.declare_parameter('association_max_distance_m', 1.50)
        self.declare_parameter('cluster_radius_m', 0.25)
        self.declare_parameter('obstacle_cost_threshold', 254)
        self.declare_parameter('minimum_cluster_cells', 1)
        self.declare_parameter('maximum_cluster_cells', 120)
        self.declare_parameter('maximum_cluster_extent_m', 1.00)
        self.declare_parameter('static_occupied_threshold', 65)
        self.declare_parameter('static_exclusion_radius_m', 0.10)
        self.declare_parameter('tracker_process_variance', 1.0)
        self.declare_parameter('tracker_measurement_variance', 0.04)
        self.declare_parameter('mahalanobis_gate', 9.21)
        self.declare_parameter('track_confirmation_hits', 3)
        self.declare_parameter('maximum_missed_updates', 4)
        self.declare_parameter('maximum_coast_time_s', 3.0)
        self.declare_parameter('camera_label_gate_m', 0.75)
        self.declare_parameter('camera_rebind_margin_m', 0.15)
        self.declare_parameter('camera_position_alpha', 0.55)
        self.declare_parameter('camera_velocity_alpha', 0.35)
        self.declare_parameter('maximum_person_speed_mps', 2.0)
        self.declare_parameter('costmap_refinement_max_camera_age_s', 1.0)
        self.declare_parameter('lidar_continuation_timeout_s', 3.0)
        self.declare_parameter('desired_distance_m', 1.20)
        self.declare_parameter('minimum_distance_m', 0.65)
        self.declare_parameter('distance_tolerance_m', 0.15)
        self.declare_parameter('maximum_linear_speed_mps', 0.30)
        self.declare_parameter('retreat_maximum_travel_m', 0.25)
        self.declare_parameter('goal_update_distance_m', 0.25)
        self.declare_parameter('goal_update_period_s', 0.75)
        self.declare_parameter('coarse_goal_update_distance_m', 0.50)
        self.declare_parameter('coarse_goal_update_period_s', 0.75)
        self.declare_parameter('bearing_goal_update_distance_m', 0.20)
        self.declare_parameter('bearing_goal_update_period_s', 0.50)
        self.declare_parameter('bearing_only_variance_threshold_m2', 1.0)
        self.declare_parameter('precise_maximum_travel_m', 0.50)
        self.declare_parameter('coarse_maximum_travel_m', 0.80)
        self.declare_parameter('bearing_maximum_travel_m', 1.20)
        # The global inflation layer already encodes the configured 0.55 m
        # wall margin. Only send goals in its low-cost exterior and prefer
        # room-side cells when the raw tracking point falls near geometry.
        self.declare_parameter('goal_maximum_cost', 80)
        self.declare_parameter('goal_safe_search_radius_m', 1.00)
        self.declare_parameter('goal_openness_radius_m', 0.60)
        self.declare_parameter('goal_openness_preference_m', 0.30)
        self.declare_parameter('heading_probe_distance_m', 0.90)
        self.declare_parameter('minimum_heading_clearance_m', 0.45)
        self.declare_parameter('require_global_costmap_for_goal', True)
        self.declare_parameter('temporary_lost_timeout_s', 0.75)
        self.declare_parameter('target_lost_timeout_s', 8.0)
        self.declare_parameter('recovery_direction_minimum_turn_rad', 0.70)
        self.declare_parameter('recovery_waypoint_tolerance_m', 0.08)
        self.declare_parameter('recovery_scan_angle_rad', 4.71238898)
        self.declare_parameter('prediction_horizon_s', 0.60)
        self.declare_parameter('recovery_spin_allowance_s', 12.0)
        self.declare_parameter('transform_timeout_s', 0.10)

    def _validate_parameters(self) -> None:
        detections_topic = str(
            self.get_parameter('detections_topic').value
        )
        if not detections_topic.startswith('/'):
            raise ValueError('detections_topic must be absolute')
        if not str(
            self.get_parameter('global_costmap_topic').value
        ).startswith('/'):
            raise ValueError('global_costmap_topic must be absolute')
        if not str(
            self.get_parameter('static_map_topic').value
        ).startswith('/'):
            raise ValueError('static_map_topic must be absolute')
        if float(self.get_parameter('minimum_confidence').value) < 0.0:
            raise ValueError('minimum_confidence must be non-negative')
        for parameter_name in (
            'navigation_retry_delay_s',
            'camera_position_alpha',
            'camera_velocity_alpha',
            'maximum_person_speed_mps',
            'costmap_refinement_max_camera_age_s',
            'lidar_continuation_timeout_s',
            'coarse_goal_update_distance_m',
            'coarse_goal_update_period_s',
            'bearing_goal_update_distance_m',
            'bearing_goal_update_period_s',
            'bearing_only_variance_threshold_m2',
            'precise_maximum_travel_m',
            'coarse_maximum_travel_m',
            'bearing_maximum_travel_m',
            'retreat_maximum_travel_m',
            'goal_safe_search_radius_m',
            'goal_openness_radius_m',
            'heading_probe_distance_m',
            'minimum_heading_clearance_m',
            'recovery_direction_minimum_turn_rad',
            'recovery_waypoint_tolerance_m',
            'recovery_scan_angle_rad',
            'recovery_spin_allowance_s',
        ):
            if float(self.get_parameter(parameter_name).value) <= 0.0:
                raise ValueError(f'{parameter_name} must be positive')
        if not 0 <= int(self.get_parameter('goal_maximum_cost').value) < 255:
            raise ValueError('goal_maximum_cost must be in [0, 254]')
        if float(
            self.get_parameter('goal_openness_preference_m').value
        ) < 0.0:
            raise ValueError('goal_openness_preference_m must be non-negative')
        if float(
            self.get_parameter('camera_rebind_margin_m').value
        ) < 0.0:
            raise ValueError('camera_rebind_margin_m must be non-negative')
        if float(
            self.get_parameter('association_max_distance_m').value
        ) <= 0.0:
            raise ValueError('association_max_distance_m must be positive')
        if float(self.get_parameter('prediction_horizon_s').value) < 0.0:
            raise ValueError('prediction_horizon_s must be non-negative')
        if float(
            self.get_parameter('recovery_direction_minimum_turn_rad').value
        ) > math.pi:
            raise ValueError(
                'recovery_direction_minimum_turn_rad must not exceed pi'
            )
        recovery_scan_angle = float(
            self.get_parameter('recovery_scan_angle_rad').value
        )
        if recovery_scan_angle > math.tau:
            raise ValueError('recovery_scan_angle_rad must not exceed tau')
        if float(
            self.get_parameter('lidar_continuation_timeout_s').value
        ) > float(self.get_parameter('maximum_coast_time_s').value):
            raise ValueError(
                'lidar_continuation_timeout_s must not exceed '
                'maximum_coast_time_s'
            )
        self._default_settings().validate()

    def _default_settings(self) -> FollowSettings:
        return FollowSettings(
            desired_distance_m=float(
                self.get_parameter('desired_distance_m').value
            ),
            minimum_distance_m=float(
                self.get_parameter('minimum_distance_m').value
            ),
            distance_tolerance_m=float(
                self.get_parameter('distance_tolerance_m').value
            ),
            goal_update_distance_m=float(
                self.get_parameter('goal_update_distance_m').value
            ),
            goal_update_period_s=float(
                self.get_parameter('goal_update_period_s').value
            ),
            maximum_linear_speed_mps=float(
                self.get_parameter('maximum_linear_speed_mps').value
            ),
            temporary_lost_timeout_s=float(
                self.get_parameter('temporary_lost_timeout_s').value
            ),
            target_lost_timeout_s=float(
                self.get_parameter('target_lost_timeout_s').value
            ),
        )

    def _settings_for_goal(self, request) -> FollowSettings:
        defaults = self._default_settings()
        lost_timeout = _duration_seconds(request.target_lost_timeout)
        settings = FollowSettings(
            desired_distance_m=(
                float(request.desired_distance_m)
                if request.desired_distance_m > 0.0
                else defaults.desired_distance_m
            ),
            minimum_distance_m=(
                float(request.minimum_distance_m)
                if request.minimum_distance_m > 0.0
                else defaults.minimum_distance_m
            ),
            distance_tolerance_m=defaults.distance_tolerance_m,
            goal_update_distance_m=defaults.goal_update_distance_m,
            goal_update_period_s=defaults.goal_update_period_s,
            maximum_linear_speed_mps=(
                float(request.maximum_linear_speed_mps)
                if request.maximum_linear_speed_mps > 0.0
                else defaults.maximum_linear_speed_mps
            ),
            temporary_lost_timeout_s=defaults.temporary_lost_timeout_s,
            target_lost_timeout_s=(
                lost_timeout if lost_timeout > 0.0
                else defaults.target_lost_timeout_s
            ),
        )
        settings.validate()
        return settings

    def _goal_callback(self, request) -> GoalResponse:
        if self._active_goal is not None:
            self.get_logger().warning('Rejecting concurrent follow action')
            return GoalResponse.REJECT
        try:
            self._settings_for_goal(request)
        except ValueError as error:
            self.get_logger().warning(
                f'Rejecting invalid follow goal: {error}'
            )
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _cancel_callback(self, _goal_handle) -> CancelResponse:
        return CancelResponse.ACCEPT

    def _handle_accepted(self, goal_handle) -> None:
        self._active_goal = goal_handle
        self._result_future = Future()
        self._settings = self._settings_for_goal(goal_handle.request)
        self._observed_track_id = ''
        self._detector_track_id = ''
        self._costmap_tracker.clear_selection()
        self._camera_estimator.reset()
        self._last_seen_s = None
        self._last_camera_seen_s = None
        self._last_costmap_stamp_s = None
        self._last_goal_position = None
        self._last_plan_target = None
        self._last_plan_request_s = 0.0
        self._last_target_pose = PoseStamped()
        self._last_target_height = 0.0
        self._last_observed_bearing_rad = 0.0
        self._recovery_turn_sign = 1.0
        self._current_distance = None
        self._reset_recovery()
        self._last_motion_target = None
        self._last_motion_precise = False
        self._last_motion_bearing_only = False
        self._goal_dispatch_count = 0
        self._navigation_retry_not_before_s = 0.0
        self._navigation_failure_count = 0
        self._tracking_source = 'none'
        self._set_state(FollowState.IDLE)
        self._publish_speed_limit(force=True)
        goal_handle.execute()
        self.get_logger().info(
            'Waiting to acquire the first observed person at '
            f'{self._settings.desired_distance_m:.2f} m'
        )

    async def _execute_callback(self, goal_handle):
        if goal_handle is not self._active_goal or self._result_future is None:
            result = FollowPerson.Result()
            result.success = False
            result.final_state = FollowState.STOPPED
            result.message = 'follow goal was superseded before execution'
            goal_handle.abort()
            return result
        return await self._result_future

    def _on_detections(self, message: Detection3DArray) -> None:
        if self._active_goal is None:
            return
        observation = self._select_target_observation(message)
        if observation is None:
            return
        detection, detected_pose = observation
        now_s = self._now_seconds()
        bearing_only = self._is_bearing_only(detection)
        camera_estimate = self._camera_estimator.update(
            Point2D(
                detected_pose.pose.position.x,
                detected_pose.pose.position.y,
            ),
            now_s,
        )
        camera_position = camera_estimate.position
        self._last_camera_seen_s = now_s
        self._last_seen_s = now_s
        self._detector_track_id = detection.id
        self._last_target_height = detected_pose.pose.position.z
        if not self._observed_track_id:
            self._observed_track_id = 'person-1'
            self.get_logger().info(
                'Acquired person-1 from sensor-backed RGB-D position'
            )
        try:
            robot_position, robot_yaw = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'target_tf', f'Target TF unavailable: {error}'
            )
            return
        observed_yaw = math.atan2(
            camera_position.y - robot_position.y,
            camera_position.x - robot_position.x,
        )
        observed_bearing = normalize_angle(observed_yaw - robot_yaw)
        if abs(observed_bearing) > 1e-3:
            self._last_observed_bearing_rad = observed_bearing
        self._reset_recovery()

        label = (
            self._costmap_tracker.target.label
            if self._costmap_tracker.target is not None
            else self._observed_track_id
        )
        labeled = None
        if (
            self._latest_global_costmap is not None
            and self._latest_static_map is not None
        ):
            previous_track_id = (
                self._costmap_tracker.target.track.track_id
                if self._costmap_tracker.target is not None
                else None
            )
            labeled = self._costmap_tracker.bind(
                label,
                camera_position,
                detection.id,
            )
            if (
                labeled is not None
                and labeled.track.track_id != previous_track_id
            ):
                self.get_logger().info(
                    f'Labeled global-costmap track '
                    f'{labeled.track.track_id} as {label}'
                )
        if (
            labeled is not None
            and labeled.track.confirmed
            and self._accept_costmap_observation(labeled)
        ):
            return
        if labeled is None:
            self._warn_periodically(
                'camera_only_tracking',
                'Person is outside costmap association; using '
                + (
                    'RGB bearing and depth lower bound'
                    if bearing_only
                    else 'coarse RGB-D tracking'
                ),
            )
        self._accept_camera_observation(
            camera_position,
            robot_position,
            now_s,
            bearing_only=bearing_only,
        )

    def _is_bearing_only(self, detection: Detection3D) -> bool:
        """Return whether range is a high-variance depth lower bound."""
        variance = max(
            (
                float(result.pose.covariance[14])
                for result in detection.results
                if result.hypothesis.class_id == 'person'
            ),
            default=0.0,
        )
        return variance >= float(
            self.get_parameter('bearing_only_variance_threshold_m2').value
        )

    def _on_global_costmap(self, message: Costmap) -> None:
        try:
            grid = self._costmap_grid(message)
        except ValueError as error:
            self._warn_periodically(
                'invalid_costmap', f'Ignoring invalid global costmap: {error}'
            )
            return
        self._latest_global_costmap = grid
        if self._latest_static_map is None:
            return
        labeled = self._costmap_tracker.update(
            grid,
            self._latest_static_map,
        )
        self._publish_track_markers()
        if (
            self._active_goal is not None
            and labeled is not None
            and labeled.track.confirmed
        ):
            self._accept_lidar_continuation(labeled)

    def _accept_lidar_continuation(
        self,
        labeled: LabeledObstacle,
    ) -> bool:
        """Continue a camera-labeled target from an observed LiDAR track."""
        now_s = self._now_seconds()
        if self._last_camera_seen_s is None or (
            now_s - self._last_camera_seen_s
            > float(
                self.get_parameter('lidar_continuation_timeout_s').value
            )
        ):
            return False
        if (
            self._last_costmap_stamp_s is not None
            and labeled.stamp_seconds <= self._last_costmap_stamp_s
        ):
            return False
        camera_age_s = now_s - self._last_camera_seen_s
        settings = self._settings
        if (
            settings is not None
            and camera_age_s <= settings.temporary_lost_timeout_s
        ):
            # The camera callback already uses a consistent costmap match as
            # an optional refinement. Do not let the asynchronous costmap
            # callback issue a second, alternating Nav2 goal while RGB-D is
            # still current. LiDAR owns continuation only after camera loss.
            return False
        try:
            robot_position, _ = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'lidar_target_tf',
                f'LiDAR target TF unavailable: {error}',
            )
            return False
        self._last_costmap_stamp_s = labeled.stamp_seconds
        self._last_seen_s = now_s
        self._last_target_pose = self._make_target_pose(
            labeled.track.position,
            self._last_target_height,
        )
        self._target_pose_publisher.publish(self._last_target_pose)
        self._reset_recovery()
        if self._nav2.mode == MotionMode.SPIN:
            self._nav2.cancel()
        self._set_state(FollowState.TRACKING)
        self._tracking_source = 'lidar'
        self._apply_tracking_motion(
            robot_position,
            labeled.track.position,
            now_s,
            precise=True,
        )
        self._publish_feedback()
        return True

    def _on_static_map(self, message: OccupancyGrid) -> None:
        try:
            self._latest_static_map = self._static_map_grid(message)
        except ValueError as error:
            self._warn_periodically(
                'invalid_static_map',
                f'Ignoring invalid static map: {error}',
            )

    def _costmap_grid(self, message: Costmap) -> CostmapGrid:
        if message.header.frame_id != self._global_frame:
            raise ValueError(
                f'expected frame {self._global_frame}, '
                f'got {message.header.frame_id or "<empty>"}'
            )
        stamp_s = _stamp_seconds(message.metadata.update_time)
        if stamp_s <= 0.0:
            stamp_s = _stamp_seconds(message.header.stamp)
        if stamp_s <= 0.0:
            stamp_s = self._now_seconds()
        orientation = message.metadata.origin.orientation
        grid = CostmapGrid(
            frame_id=message.header.frame_id,
            stamp_seconds=stamp_s,
            resolution=float(message.metadata.resolution),
            width=int(message.metadata.size_x),
            height=int(message.metadata.size_y),
            origin=Point2D(
                float(message.metadata.origin.position.x),
                float(message.metadata.origin.position.y),
            ),
            origin_yaw=quaternion_to_yaw(
                orientation.x,
                orientation.y,
                orientation.z,
                orientation.w,
            ),
            costs=message.data,
        )
        grid.validate()
        return grid

    def _static_map_grid(self, message: OccupancyGrid) -> CostmapGrid:
        if message.header.frame_id != self._global_frame:
            raise ValueError(
                f'expected frame {self._global_frame}, '
                f'got {message.header.frame_id or "<empty>"}'
            )
        stamp_s = _stamp_seconds(message.header.stamp)
        if stamp_s <= 0.0:
            stamp_s = self._now_seconds()
        orientation = message.info.origin.orientation
        grid = CostmapGrid(
            frame_id=message.header.frame_id,
            stamp_seconds=stamp_s,
            resolution=float(message.info.resolution),
            width=int(message.info.width),
            height=int(message.info.height),
            origin=Point2D(
                float(message.info.origin.position.x),
                float(message.info.origin.position.y),
            ),
            origin_yaw=quaternion_to_yaw(
                orientation.x,
                orientation.y,
                orientation.z,
                orientation.w,
            ),
            costs=message.data,
        )
        grid.validate()
        return grid

    def _accept_costmap_observation(
        self,
        labeled: LabeledObstacle,
    ) -> bool:
        now_s = self._now_seconds()
        maximum_camera_age = float(
            self.get_parameter(
                'costmap_refinement_max_camera_age_s'
            ).value
        )
        if (
            self._last_camera_seen_s is None
            or now_s - self._last_camera_seen_s > maximum_camera_age
        ):
            return False
        camera_position = self._camera_estimator.predict(now_s, 0.0)
        if camera_position is None or distance(
            camera_position,
            labeled.track.position,
        ) > float(self.get_parameter('camera_label_gate_m').value):
            # This camera callback refines only its current visible person.
            # Bounded LiDAR-only continuation is handled by the costmap
            # callback after RGB-D has already established the label.
            return False
        if (
            self._last_costmap_stamp_s is not None
            and labeled.stamp_seconds <= self._last_costmap_stamp_s
        ):
            # Never replace a newer RGB-D position with the same stale grid
            # sample. The global-costmap callback consumes newer observations.
            return False
        try:
            robot_position, _ = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'target_tf', f'Target TF unavailable: {error}'
            )
            return False
        self._last_costmap_stamp_s = labeled.stamp_seconds
        self._last_target_pose = self._make_target_pose(
            labeled.track.position,
            self._last_target_height,
        )
        self._target_pose_publisher.publish(self._last_target_pose)
        self._reset_recovery()
        if self._nav2.mode == MotionMode.SPIN:
            self._nav2.cancel()
        self._set_state(FollowState.TRACKING)
        self._tracking_source = 'costmap'
        self._apply_tracking_motion(
            robot_position,
            labeled.track.position,
            now_s,
            precise=True,
        )
        self._publish_feedback()
        return True

    def _accept_camera_observation(
        self,
        camera_position: Point2D,
        robot_position: Point2D,
        now_s: float,
        bearing_only: bool = False,
    ) -> None:
        """Follow a visible RGB-D person even without a costmap cluster."""
        self._last_target_pose = self._make_target_pose(
            camera_position,
            self._last_target_height,
        )
        self._target_pose_publisher.publish(self._last_target_pose)
        self._reset_recovery()
        if self._nav2.mode == MotionMode.SPIN:
            self._nav2.cancel()
        self._set_state(FollowState.TRACKING)
        self._tracking_source = 'bearing' if bearing_only else 'camera'
        self._apply_tracking_motion(
            robot_position,
            camera_position,
            now_s,
            precise=False,
            bearing_only=bearing_only,
        )
        self._publish_feedback()

    def _select_target_observation(
        self,
        message: Detection3DArray,
    ) -> tuple[Detection3D, PoseStamped] | None:
        minimum_confidence = float(
            self.get_parameter('minimum_confidence').value
        )
        candidates = []
        poses = {}
        for index, detection in enumerate(message.detections):
            score = max(
                (
                    float(result.hypothesis.score)
                    for result in detection.results
                    if result.hypothesis.class_id == 'person'
                ),
                default=0.0,
            )
            if score < minimum_confidence:
                continue
            try:
                pose = self._target_in_global_frame(message, detection)
            except TransformException as error:
                self._warn_periodically(
                    'target_tf',
                    f'Target TF unavailable: {error}',
                )
                continue
            candidates.append(
                TargetCandidate(
                    source_index=index,
                    position=Point2D(
                        pose.pose.position.x,
                        pose.pose.position.y,
                    ),
                    confidence=score,
                    observed_track_id=detection.id,
                )
            )
            poses[index] = pose

        now_s = self._now_seconds()
        predicted = self._camera_estimator.predict(
            now_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )
        if predicted is None:
            predicted = self._costmap_tracker.predict_target(
                now_s,
                float(self.get_parameter('prediction_horizon_s').value),
            )
        selected = select_target_candidate(
            candidates,
            predicted,
            float(
                self.get_parameter('association_max_distance_m').value
            ),
            preferred_track_id=self._detector_track_id,
        )
        if selected is None:
            return None

        previous_id = self._detector_track_id
        if previous_id and previous_id != selected.observed_track_id:
            self.get_logger().info(
                f'Detector track changed {previous_id} -> '
                f'{selected.observed_track_id or "unknown"}; preserving '
                'the existing costmap obstacle label'
            )
        return (
            message.detections[selected.source_index],
            poses[selected.source_index],
        )

    def _target_in_global_frame(
        self,
        message: Detection3DArray,
        detection: Detection3D,
    ) -> PoseStamped:
        source_frame = detection.header.frame_id or message.header.frame_id
        if not source_frame:
            raise TransformException('3D detection has no frame_id')
        stamp = detection.header.stamp
        if stamp.sec == 0 and stamp.nanosec == 0:
            stamp = message.header.stamp
        pose = Pose()
        pose.position = detection.bbox.center.position
        pose.orientation.w = 1.0
        if source_frame == self._global_frame:
            output = PoseStamped()
            output.header.stamp = stamp
            output.header.frame_id = self._global_frame
            output.pose = pose
            return output
        try:
            transform = self._tf_buffer.lookup_transform(
                self._global_frame,
                source_frame,
                Time.from_msg(stamp),
                timeout=Duration(seconds=self._tf_timeout),
            )
        except TransformException as exact_error:
            # Some simulators publish a dynamic transform only when a static
            # robot pose changes. Use the newest localization transform rather
            # than discarding a current RGB-D observation in that case.
            try:
                transform = self._tf_buffer.lookup_transform(
                    self._global_frame,
                    source_frame,
                    Time(),
                    timeout=Duration(seconds=self._tf_timeout),
                )
            except TransformException:
                raise exact_error
            self._warn_periodically(
                'latest_target_tf',
                'Exact detection-time TF unavailable; using latest '
                'localization TF',
            )
        output = PoseStamped()
        output.header.stamp = stamp
        output.header.frame_id = self._global_frame
        output.pose = do_transform_pose(pose, transform)
        return output

    def _robot_pose(self) -> tuple[Point2D, float]:
        transform = self._tf_buffer.lookup_transform(
            self._global_frame,
            self._robot_frame,
            Time(),
            timeout=Duration(seconds=self._tf_timeout),
        )
        translation = transform.transform.translation
        rotation = transform.transform.rotation
        return (
            Point2D(float(translation.x), float(translation.y)),
            quaternion_to_yaw(
                rotation.x,
                rotation.y,
                rotation.z,
                rotation.w,
            ),
        )

    def _apply_tracking_motion(
        self,
        robot_position: Point2D,
        target_position: Point2D,
        now_s: float,
        precise: bool,
        bearing_only: bool = False,
        recovery: bool = False,
    ) -> None:
        settings = self._settings
        if settings is None:
            return
        self._last_motion_target = target_position
        self._last_motion_precise = precise
        self._last_motion_bearing_only = bearing_only
        travel_parameter = (
            'precise_maximum_travel_m'
            if precise
            else (
                'bearing_maximum_travel_m'
                if bearing_only
                else 'coarse_maximum_travel_m'
            )
        )
        maximum_travel_m = (
            None
            if recovery
            else float(self.get_parameter(travel_parameter).value)
        )
        decision = decide_follow_motion(
            robot_position,
            target_position,
            settings,
            # Keep the existing distance-band decision. For forward tracking,
            # only the planner destination changes to the observed person.
            maximum_travel_m=None,
        )
        self._current_distance = decision.goal.target_distance
        if bearing_only and decision.command == FollowCommand.RETREAT:
            # An RGB-only observation proves direction and a lower-bound
            # range, not that the person is close. Never infer reverse motion
            # from that uncertain depth, and stop an existing retreat until
            # metric depth returns.
            self._path_planner.cancel()
            self._last_goal_position = None
            self._last_plan_target = None
            self._publish_track_markers()
            if recovery:
                self._start_recovery_scan()
            return
        if decision.command in {FollowCommand.HOLD, FollowCommand.ALIGN}:
            # Stop as soon as the requested distance band is met. Nav2 owns the
            # complete velocity command, including path-following body yaw.
            if self._nav2.mode == MotionMode.NAVIGATE:
                self._nav2.cancel()
            elif self._nav2.mode == MotionMode.SPIN:
                self._nav2.cancel()
            self._path_planner.cancel()
            self._last_goal_position = None
            self._last_plan_target = None
            self._publish_track_markers()
            if recovery:
                self._start_recovery_scan()
            return
        if now_s < self._navigation_retry_not_before_s:
            return
        if decision.command == FollowCommand.RETREAT:
            # Use the same collision-checked omnidirectional controller for a
            # short retreat instead of bypassing Nav2 with a velocity command.
            maximum_travel_m = min(
                maximum_travel_m
                if maximum_travel_m is not None
                else float(
                    self.get_parameter('retreat_maximum_travel_m').value
                ),
                float(
                    self.get_parameter('retreat_maximum_travel_m').value
                ),
            )
            self._tracking_source = 'retreat'
        if self._nav2.mode == MotionMode.SPIN:
            self._nav2.cancel()
        planning_to_target = decision.command == FollowCommand.NAVIGATE
        requested_position = (
            target_position if planning_to_target else decision.goal.position
        )
        final_pose = PoseStamped()
        grid = self._latest_global_costmap
        if grid is None:
            if bool(
                self.get_parameter('require_global_costmap_for_goal').value
            ):
                self._warn_periodically(
                    'goal_costmap_unavailable',
                    'Waiting for global costmap before dispatching a '
                    'tracking goal',
                )
                return
            safe_goal_position = requested_position
            safe_goal_yaw = decision.goal.yaw
        else:
            safe_goal = project_navigation_goal(
                grid,
                requested_position,
                decision.goal.yaw,
                int(self.get_parameter('goal_maximum_cost').value),
                float(
                    self.get_parameter('goal_safe_search_radius_m').value
                ),
                float(self.get_parameter('goal_openness_radius_m').value),
                0.0
                if planning_to_target
                else float(
                    self.get_parameter('goal_openness_preference_m').value
                ),
                float(
                    self.get_parameter('heading_probe_distance_m').value
                ),
                float(
                    self.get_parameter('minimum_heading_clearance_m').value
                ),
            )
            if safe_goal is None:
                if self._nav2.mode == MotionMode.NAVIGATE:
                    self._nav2.cancel()
                self._path_planner.cancel()
                self._last_goal_position = None
                self._last_plan_target = None
                self._warn_periodically(
                    'no_safe_tracking_goal',
                    'No global-costmap goal with the configured margin; '
                    'holding instead of entering obstacle inflation',
                )
                if recovery:
                    self._start_recovery_scan()
                return
            safe_goal_position = safe_goal.position
            safe_goal_yaw = safe_goal.yaw
            if safe_goal.position_adjusted or safe_goal.heading_adjusted:
                self.get_logger().info(
                    'Adjusted follow goal toward open space: '
                    f'position={safe_goal.position_adjusted}, '
                    f'heading={safe_goal.heading_adjusted}, '
                    f'openness={safe_goal.openness:.2f}'
                )
        final_pose.header.frame_id = self._global_frame
        final_pose.header.stamp = self.get_clock().now().to_msg()
        final_pose.pose.position.x = safe_goal_position.x
        final_pose.pose.position.y = safe_goal_position.y
        quaternion = yaw_to_quaternion(safe_goal_yaw)
        final_pose.pose.orientation.x = quaternion[0]
        final_pose.pose.orientation.y = quaternion[1]
        final_pose.pose.orientation.z = quaternion[2]
        final_pose.pose.orientation.w = quaternion[3]
        if precise:
            update_distance_m = settings.goal_update_distance_m
            update_period_s = settings.goal_update_period_s
        elif bearing_only:
            update_distance_m = float(
                self.get_parameter('bearing_goal_update_distance_m').value
            )
            update_period_s = float(
                self.get_parameter('bearing_goal_update_period_s').value
            )
        else:
            update_distance_m = float(
                self.get_parameter('coarse_goal_update_distance_m').value
            )
            update_period_s = float(
                self.get_parameter('coarse_goal_update_period_s').value
            )
        plan_reference_position = (
            target_position if planning_to_target else safe_goal_position
        )
        if not recovery and not should_update_goal(
            self._last_plan_target,
            plan_reference_position,
            max(0.0, now_s - self._last_plan_request_s),
            settings,
            update_distance_m=update_distance_m,
            update_period_s=update_period_s,
        ):
            return
        if self._path_planner.compute(
            final_pose,
            str(self.get_parameter('planner_id').value),
            lambda path, detail: self._on_tracking_path(
                path,
                detail,
                maximum_travel_m,
                self._tracking_source,
                recovery,
            ),
        ):
            self._last_plan_target = plan_reference_position
            self._last_plan_request_s = now_s
            if recovery:
                self._recovery_path_requested = True
        else:
            self._warn_periodically(
                'planner_unavailable',
                'Nav2 ComputePathToPose action is not ready',
            )

    def _on_tracking_path(
        self,
        path,
        detail: str,
        lookahead_m: float | None,
        tracking_source: str,
        recovery: bool,
    ) -> None:
        """Dispatch a bounded tracking path or full last-seen recovery path."""
        expected_state = (
            FollowState.REACHING_LAST_POSITION
            if recovery
            else FollowState.TRACKING
        )
        if self._active_goal is None or self._state != expected_state:
            return
        if path is None:
            self._last_plan_target = None
            self._navigation_failure_count += 1
            self._warn_periodically('tracking_path_failed', detail)
            if recovery:
                self._start_recovery_scan()
            return
        if not path.poses:
            self._last_plan_target = None
            self._warn_periodically(
                'empty_tracking_path',
                'Nav2 returned an empty tracking path',
            )
            if recovery:
                self._start_recovery_scan()
            return
        if recovery:
            selected_path = path
            endpoint = path.poses[-1].pose.position
            waypoint_position = Point2D(float(endpoint.x), float(endpoint.y))
            travel_description = 'full recovery path'
        else:
            bounded = truncate_path(path, lookahead_m)
            if bounded is None:
                return
            selected_path, waypoint = bounded
            waypoint_position = waypoint.position
            travel_description = f'lookahead={waypoint.travelled_m:.2f}m'
        # The unmodified planner path owns translation and body rotation.
        if self._nav2.follow_path(
            selected_path,
            str(self.get_parameter('tracking_controller_id').value),
            str(self.get_parameter('goal_checker_id').value),
        ):
            self._last_goal_position = waypoint_position
            self._navigation_retry_not_before_s = 0.0
            self._navigation_failure_count = 0
            self._goal_dispatch_count += 1
            if recovery:
                self._recovery_navigation_active = True
            self.get_logger().info(
                f'Updated path waypoint from {tracking_source}: '
                f'({waypoint_position.x:.2f}, '
                f'{waypoint_position.y:.2f}), '
                f'{travel_description}'
            )
            self._publish_track_markers()
        else:
            self._warn_periodically(
                'follow_path_unavailable',
                'Nav2 FollowPath action is not ready',
            )
            if recovery:
                self._start_recovery_scan()

    def _tick(self) -> None:
        if self._active_goal is None:
            return
        if self._active_goal.is_cancel_requested:
            self._finish_action(
                success=False,
                final_state=FollowState.STOPPED,
                message='follow action canceled',
                canceled=True,
            )
            return
        settings = self._settings
        if settings is None:
            return
        now_s = self._now_seconds()
        self._publish_speed_limit()
        if self._last_seen_s is None:
            # A follow request waits stationary for sensor-backed acquisition.
            # Search rotation is valid only after a real target was acquired.
            self._publish_feedback()
            return
        lost_for = max(0.0, now_s - self._last_seen_s)
        if lost_for >= settings.temporary_lost_timeout_s:
            if (
                self._state == FollowState.REACHING_WAYPOINT
                and self._last_goal_position is not None
            ):
                try:
                    robot_position, _ = self._robot_pose()
                except TransformException as error:
                    self._warn_periodically(
                        'recovery_waypoint_tf',
                        f'Recovery waypoint TF unavailable: {error}',
                    )
                else:
                    tolerance_m = float(
                        self.get_parameter(
                            'recovery_waypoint_tolerance_m'
                        ).value
                    )
                    if distance(
                        robot_position,
                        self._last_goal_position,
                    ) <= tolerance_m:
                        self.get_logger().info(
                            'Recovery waypoint reached within '
                            f'{tolerance_m:.2f} m tolerance'
                        )
                        self._start_direction_turn()
                        self._publish_predicted_target(now_s)
                        self._publish_feedback()
                        return
            if self._state == FollowState.TRACKING:
                self._begin_loss_recovery(now_s)
            elif (
                self._state == FollowState.TURNING_TO_TARGET
                and self._nav2.mode is None
            ):
                self._start_direction_turn()
            elif (
                self._state == FollowState.REACHING_LAST_POSITION
                and not self._recovery_path_requested
                and not self._recovery_navigation_active
            ):
                self._request_last_seen_recovery(now_s)
            elif (
                self._state == FollowState.SEARCHING
                and self._recovery_scan_complete
                and target_loss_timed_out(
                    self._last_seen_s,
                    now_s,
                    settings.target_lost_timeout_s,
                )
            ):
                self._finish_action(
                    success=False,
                    final_state=FollowState.TARGET_LOST,
                    message='the selected person was not reacquired',
                )
                return
        elif (
            self._state == FollowState.TRACKING
            and self._nav2.mode is None
            and self._last_motion_target is not None
            and now_s >= self._navigation_retry_not_before_s
        ):
            try:
                robot_position, _ = self._robot_pose()
            except TransformException as error:
                self._warn_periodically(
                    'target_tf', f'Target TF unavailable: {error}'
                )
            else:
                self._apply_tracking_motion(
                    robot_position,
                    self._last_motion_target,
                    now_s,
                    precise=self._last_motion_precise,
                    bearing_only=self._last_motion_bearing_only,
                )
        self._publish_predicted_target(now_s)
        self._publish_feedback()

    def _reset_recovery(self) -> None:
        """Forget loss recovery as soon as a sensor target is visible."""
        self._recovery_path_requested = False
        self._recovery_navigation_active = False
        self._recovery_scan_started = False
        self._recovery_scan_complete = False
        self._recovery_direction_target = None

    def _begin_loss_recovery(self, now_s: float) -> None:
        """Freeze the latest waypoint before escalating target recovery."""
        self._path_planner.cancel()
        if abs(self._last_observed_bearing_rad) > 1e-3:
            self._recovery_turn_sign = math.copysign(
                1.0,
                self._last_observed_bearing_rad,
            )
        self._recovery_direction_target = None
        if self._tracking_source != 'lidar':
            self._recovery_direction_target = self._camera_estimator.predict(
                now_s,
                float(self.get_parameter('prediction_horizon_s').value),
            )
        if self._recovery_direction_target is None:
            self._recovery_direction_target = self._last_motion_target
        if self._nav2.mode == MotionMode.NAVIGATE:
            self._set_state(FollowState.REACHING_WAYPOINT)
            self.get_logger().info(
                'Person lost; keeping the current Nav2 waypoint unchanged'
            )
            return
        self._start_direction_turn()

    def _start_direction_turn(self) -> None:
        """Turn once toward the predicted exit direction after the waypoint."""
        if self._active_goal is None:
            return
        self._path_planner.cancel()
        self._set_state(FollowState.TURNING_TO_TARGET)
        target = self._recovery_direction_target
        if target is None:
            self._request_last_seen_recovery(self._now_seconds())
            return
        try:
            robot_position, robot_yaw = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'recovery_turn_tf',
                f'Recovery turn TF unavailable: {error}',
            )
            return
        target_yaw = math.atan2(
            target.y - robot_position.y,
            target.x - robot_position.x,
        )
        raw_turn_angle = normalize_angle(target_yaw - robot_yaw)
        if abs(self._last_observed_bearing_rad) <= 1e-3 and (
            abs(raw_turn_angle) > 1e-3
        ):
            self._recovery_turn_sign = math.copysign(1.0, raw_turn_angle)
        minimum_turn = float(
            self.get_parameter(
                'recovery_direction_minimum_turn_rad'
            ).value
        )
        turn_angle = math.copysign(
            max(abs(raw_turn_angle), minimum_turn),
            self._recovery_turn_sign,
        )
        full_scan_allowance = float(
            self.get_parameter('recovery_spin_allowance_s').value
        )
        allowance = max(
            4.0,
            full_scan_allowance
            * abs(turn_angle)
            / float(self.get_parameter('recovery_scan_angle_rad').value)
            + 2.0,
        )
        if self._nav2.spin(turn_angle, allowance):
            self.get_logger().info(
                'Current waypoint reached; turning toward the predicted '
                f'target direction ({math.degrees(turn_angle):.1f} deg)'
            )
            return
        self._warn_periodically(
            'direction_spin_unavailable',
            'Nav2 Spin action is not ready; continuing to last position',
        )
        self._request_last_seen_recovery(self._now_seconds())

    def _request_last_seen_recovery(self, now_s: float) -> None:
        """Follow one complete Nav2 path to the last safe target standoff."""
        self._set_state(FollowState.REACHING_LAST_POSITION)
        if self._last_motion_target is None:
            self._start_recovery_scan()
            return
        try:
            robot_position, _ = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'recovery_tf', f'Recovery TF unavailable: {error}'
            )
            return
        self._tracking_source = 'last_seen_recovery'
        self._apply_tracking_motion(
            robot_position,
            self._last_motion_target,
            now_s,
            precise=self._last_motion_precise,
            bearing_only=self._last_motion_bearing_only,
            recovery=True,
        )

    def _start_recovery_scan(self) -> None:
        """Scan 270 degrees toward the side where the target disappeared."""
        if self._active_goal is None or self._recovery_scan_started:
            return
        self._path_planner.cancel()
        self._recovery_path_requested = False
        self._recovery_navigation_active = False
        self._recovery_scan_started = True
        self._set_state(FollowState.SEARCHING)
        scan_angle = math.copysign(
            float(self.get_parameter('recovery_scan_angle_rad').value),
            self._recovery_turn_sign,
        )
        if self._nav2.spin(
            scan_angle,
            float(self.get_parameter('recovery_spin_allowance_s').value),
        ):
            self.get_logger().info(
                'Last-seen goal reached; scanning 270 degrees toward the '
                f'last exit side ({math.degrees(scan_angle):.0f} deg)'
            )
            return
        self._recovery_scan_complete = True
        self._warn_periodically(
            'spin_unavailable',
            'Nav2 Spin action is not ready; waiting safely in place',
        )

    def _publish_predicted_target(self, now_s: float) -> None:
        predicted = self._camera_estimator.predict(
            now_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )
        if predicted is None:
            predicted = self._costmap_tracker.predict_target(
                now_s,
                float(self.get_parameter('prediction_horizon_s').value),
            )
        if predicted is None:
            return
        self._last_target_pose = self._make_target_pose(
            predicted,
            self._last_target_height,
        )
        self._target_pose_publisher.publish(self._last_target_pose)

    def _make_target_pose(self, point: Point2D, z: float) -> PoseStamped:
        pose = PoseStamped()
        pose.header.frame_id = self._global_frame
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.pose.position.x = point.x
        pose.pose.position.y = point.y
        pose.pose.position.z = z
        pose.pose.orientation.w = 1.0
        return pose

    def _on_nav2_result(
        self,
        mode: MotionMode,
        status: int,
        detail: str,
    ) -> None:
        if mode == MotionMode.NAVIGATE:
            if self._state == FollowState.REACHING_WAYPOINT:
                if status != GoalStatus.STATUS_SUCCEEDED:
                    self._warn_periodically('waypoint_recovery_failed', detail)
                self._start_direction_turn()
                return
            if (
                self._state == FollowState.REACHING_LAST_POSITION
                and self._recovery_navigation_active
            ):
                self._recovery_navigation_active = False
                if status != GoalStatus.STATUS_SUCCEEDED:
                    self._warn_periodically(
                        'last_seen_navigation_failed', detail
                    )
                self._start_recovery_scan()
                return
            if status not in {
                GoalStatus.STATUS_SUCCEEDED,
                GoalStatus.STATUS_CANCELED,
            }:
                # A failed path is disposable. Forget its dispatch point so
                # the next visible-person observation retries immediately,
                # while the outer FollowPerson action remains active.
                self._last_goal_position = None
                self._navigation_failure_count += 1
                self._navigation_retry_not_before_s = (
                    self._now_seconds()
                    + float(
                        self.get_parameter('navigation_retry_delay_s').value
                    )
                )
                self._warn_periodically('navigate_failed', detail)
            else:
                self._navigation_failure_count = 0
        elif mode == MotionMode.SPIN:
            if self._state == FollowState.TURNING_TO_TARGET:
                if status != GoalStatus.STATUS_SUCCEEDED:
                    self._warn_periodically('direction_spin_failed', detail)
                self._request_last_seen_recovery(self._now_seconds())
                return
            if self._recovery_scan_started:
                self._recovery_scan_complete = True
                if status != GoalStatus.STATUS_SUCCEEDED:
                    self._warn_periodically('spin_failed', detail)

    def _publish_feedback(self) -> None:
        if self._active_goal is None:
            return
        visibility_timeout_s = (
            self._settings.temporary_lost_timeout_s
            if self._settings is not None
            else float(
                self.get_parameter('temporary_lost_timeout_s').value
            )
        )
        feedback = FollowPerson.Feedback()
        feedback.state = self._state
        feedback.target_visible = (
            self._last_camera_seen_s is not None
            and self._now_seconds() - self._last_camera_seen_s
            < visibility_timeout_s
        )
        feedback.observed_track_id = self._observed_track_id
        feedback.current_distance_m = float(self._current_distance or 0.0)
        feedback.estimated_target_pose = self._last_target_pose
        self._active_goal.publish_feedback(feedback)
        self._publish_status(feedback.target_visible)

    def _publish_status(self, target_visible: bool = False) -> None:
        message = String()
        message.data = json.dumps(
            {
                'state': self._state,
                'observed_track_id': self._observed_track_id or None,
                'detector_track_id': self._detector_track_id or None,
                'costmap_obstacle_labeled': (
                    self._costmap_tracker.target is not None
                ),
                'costmap_track_id': (
                    self._costmap_tracker.target.track.track_id
                    if self._costmap_tracker.target is not None
                    else None
                ),
                'costmap_track_confirmed': (
                    self._costmap_tracker.target.track.confirmed
                    if self._costmap_tracker.target is not None
                    else False
                ),
                'costmap_track_count': len(self._costmap_tracker.tracks),
                'static_map_available': self._latest_static_map is not None,
                'tracking_source': self._tracking_source,
                'navigation_failure_count': self._navigation_failure_count,
                'target_visible': target_visible,
                'current_distance_m': self._current_distance,
                'recovery_phase': (
                    self._state if self._state != FollowState.TRACKING else None
                ),
                'recovery_path_requested': self._recovery_path_requested,
                'recovery_scan_started': self._recovery_scan_started,
            },
            separators=(',', ':'),
        )
        self._status_publisher.publish(message)

    def _publish_track_markers(self) -> None:
        """Visualize costmap identities and the selected person in RViz."""
        message = MarkerArray()
        clear = Marker()
        clear.action = Marker.DELETEALL
        message.markers.append(clear)
        selected = self._costmap_tracker.target
        selected_id = selected.track.track_id if selected is not None else None
        stamp = self.get_clock().now().to_msg()
        for track in self._costmap_tracker.tracks:
            body = Marker()
            body.header.frame_id = self._global_frame
            body.header.stamp = stamp
            body.ns = 'costmap_tracks'
            body.id = track.track_id * 2
            body.type = Marker.CYLINDER
            body.action = Marker.ADD
            body.pose.position.x = track.position.x
            body.pose.position.y = track.position.y
            body.pose.position.z = 0.15
            body.pose.orientation.w = 1.0
            body.scale.x = max(0.20, min(0.80, track.extent_m))
            body.scale.y = body.scale.x
            body.scale.z = 0.30
            if track.track_id == selected_id:
                body.color.r = 1.0
                body.color.g = 0.2
            elif track.confirmed:
                body.color.g = 0.8
                body.color.b = 1.0
            else:
                body.color.r = 0.6
                body.color.g = 0.6
                body.color.b = 0.6
            body.color.a = 0.75 if track.misses == 0 else 0.30
            message.markers.append(body)

            text_marker = Marker()
            text_marker.header = body.header
            text_marker.ns = 'costmap_track_labels'
            text_marker.id = body.id + 1
            text_marker.type = Marker.TEXT_VIEW_FACING
            text_marker.action = Marker.ADD
            text_marker.pose.position.x = track.position.x
            text_marker.pose.position.y = track.position.y
            text_marker.pose.position.z = 0.55
            text_marker.pose.orientation.w = 1.0
            text_marker.scale.z = 0.22
            text_marker.color.r = 1.0
            text_marker.color.g = 1.0
            text_marker.color.b = 1.0
            text_marker.color.a = 1.0
            prefix = 'person-1 / ' if track.track_id == selected_id else ''
            suffix = ' coast' if track.misses > 0 else ''
            text_marker.text = f'{prefix}T{track.track_id}{suffix}'
            message.markers.append(text_marker)
        if self._last_goal_position is not None:
            goal = Marker()
            goal.header.frame_id = self._global_frame
            goal.header.stamp = stamp
            goal.ns = 'follow_navigation_goal'
            goal.id = 1
            goal.type = Marker.SPHERE
            goal.action = Marker.ADD
            goal.pose.position.x = self._last_goal_position.x
            goal.pose.position.y = self._last_goal_position.y
            goal.pose.position.z = 0.12
            goal.pose.orientation.w = 1.0
            goal.scale.x = 0.28
            goal.scale.y = 0.28
            goal.scale.z = 0.28
            goal.color.r = 1.0
            goal.color.g = 0.85
            goal.color.a = 0.95
            message.markers.append(goal)

            goal_label = Marker()
            goal_label.header = goal.header
            goal_label.ns = 'follow_navigation_goal_label'
            goal_label.id = 2
            goal_label.type = Marker.TEXT_VIEW_FACING
            goal_label.action = Marker.ADD
            goal_label.pose.position.x = self._last_goal_position.x
            goal_label.pose.position.y = self._last_goal_position.y
            goal_label.pose.position.z = 0.48
            goal_label.pose.orientation.w = 1.0
            goal_label.scale.z = 0.24
            goal_label.color.r = 1.0
            goal_label.color.g = 1.0
            goal_label.color.b = 1.0
            goal_label.color.a = 1.0
            goal_label.text = f'FOLLOW GOAL #{self._goal_dispatch_count}'
            message.markers.append(goal_label)
        self._track_markers_publisher.publish(message)

    def _publish_speed_limit(self, force: bool = False) -> None:
        if self._settings is None:
            return
        now_s = self._now_seconds()
        if not force and now_s - self._last_speed_publish_s < 1.0:
            return
        message = SpeedLimit()
        message.header.stamp = self.get_clock().now().to_msg()
        message.percentage = False
        message.speed_limit = self._settings.maximum_linear_speed_mps
        self._speed_publisher.publish(message)
        self._last_speed_publish_s = now_s

    def _reset_speed_limit(self) -> None:
        message = SpeedLimit()
        message.header.stamp = self.get_clock().now().to_msg()
        message.percentage = False
        message.speed_limit = 0.0
        self._speed_publisher.publish(message)

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.get_logger().info(f'Follow state: {state}')
        self._publish_status()

    def _finish_action(
        self,
        success: bool,
        final_state: str,
        message: str,
        canceled: bool = False,
    ) -> None:
        goal_handle = self._active_goal
        result_future = self._result_future
        if goal_handle is None or result_future is None:
            return
        self._nav2.cancel()
        self._path_planner.cancel()
        self._reset_speed_limit()
        self._set_state(final_state)
        result = FollowPerson.Result()
        result.success = success
        result.final_state = final_state
        result.message = message
        if canceled:
            goal_handle.canceled()
        elif success:
            goal_handle.succeed()
        else:
            goal_handle.abort()
        self._active_goal = None
        self._result_future = None
        self._settings = None
        self._observed_track_id = ''
        self._detector_track_id = ''
        self._costmap_tracker.clear_selection()
        self._camera_estimator.reset()
        self._last_seen_s = None
        self._last_camera_seen_s = None
        self._last_costmap_stamp_s = None
        self._last_goal_position = None
        self._last_plan_target = None
        self._last_motion_target = None
        self._last_motion_bearing_only = False
        self._last_observed_bearing_rad = 0.0
        self._recovery_turn_sign = 1.0
        self._reset_recovery()
        self._tracking_source = 'none'
        result_future.set_result(result)
        self.get_logger().info(message)

    def _warn_periodically(self, key: str, message: str) -> None:
        now_s = self._now_seconds()
        if now_s - self._last_warning_s.get(key, -math.inf) >= 5.0:
            self.get_logger().warning(message)
            self._last_warning_s[key] = now_s

    def _now_seconds(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9

    def destroy_node(self):
        """Cancel owned actions before destroying ROS entities."""
        self._nav2.destroy()
        self._path_planner.destroy()
        self._action_server.destroy()
        return super().destroy_node()


def main(args=None) -> int:
    """Run the sensor-driven person follower."""
    rclpy.init(args=args)
    node = None
    try:
        node = PersonFollowerNode()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        return 0
    except (RuntimeError, ValueError) as error:
        print(f'person_follower startup failed: {error}', file=sys.stderr)
        return 2
    finally:
        if node is not None:
            try:
                node.destroy_node()
            except KeyboardInterrupt:
                pass
        if rclpy.ok():
            rclpy.shutdown()
    return 0
