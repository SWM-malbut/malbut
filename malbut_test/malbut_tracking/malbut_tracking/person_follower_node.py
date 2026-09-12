"""Follow one RGB-D person track safely by delegating motion to Nav2."""

import json
import math
import sys
import time

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import Pose, PoseStamped
from malbut_interfaces.action import FollowPerson
from malbut_interfaces.msg import LidarClusterArray, TrackingCommandTrace
from nav_msgs.msg import OccupancyGrid, Path
from nav2_msgs.msg import Costmap, SpeedLimit
import rclpy
from rclpy.action import (
    ActionServer,
    CancelResponse,
    GoalResponse,
)
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from rclpy.task import Future
from rclpy.time import Time
from std_msgs.msg import String
from tf2_geometry_msgs import do_transform_pose
from tf2_ros import Buffer, TransformException, TransformListener
from vision_msgs.msg import Detection3D, Detection3DArray
from visualization_msgs.msg import Marker, MarkerArray

from .costmap_tracking import (
    CostmapGrid,
    LabeledObstacle,
    ObstacleCluster,
    ObstacleTargetTracker,
    TrackedObstacle,
)
from .follow_policy import (
    directed_recovery_turn,
    FollowCommand,
    FollowSettings,
    decide_follow_motion,
    speed_limit_for_travel_distance,
)
from .geometry import (
    Point2D,
    distance,
    normalize_angle,
    quaternion_to_yaw,
    yaw_to_quaternion,
)
from .goal_safety import (
    first_admissible_point_on_ray,
    pad_static_map,
    plan_static_path,
    project_navigation_goal,
)
from .motion_estimator import TargetMotionEstimator
from .navigation import MotionMode, Nav2MotionClient, Nav2PathClient
from .path_sampling import path_length_m
from .target_association import (
    TargetCandidate,
    fuse_camera_bearing_with_lidar_range,
    select_target_candidate,
)


class FollowState:
    """Stable state names published through action feedback and status."""

    STOPPED = 'STOPPED'
    IDLE = 'IDLE'
    TRACKING = 'TRACKING'
    RECOVERING = 'RECOVERING'


class RecoveryPhase:
    """Internal recovery detail hidden behind the public RECOVERING state."""

    NONE = 'NONE'
    FINISHING_WAYPOINT = 'FINISHING_WAYPOINT'
    TURNING_TO_TARGET = 'TURNING_TO_TARGET'
    REACHING_LAST_POSITION = 'REACHING_LAST_POSITION'
    SCANNING = 'SCANNING'


_ALLOWED_FOLLOW_TRANSITIONS = {
    FollowState.STOPPED: {FollowState.IDLE},
    FollowState.IDLE: {FollowState.TRACKING, FollowState.STOPPED},
    FollowState.TRACKING: {FollowState.RECOVERING, FollowState.STOPPED},
    FollowState.RECOVERING: {
        FollowState.TRACKING,
        FollowState.STOPPED,
    },
}


def _stamp_seconds(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


def _stamp_nanoseconds(stamp) -> int:
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def _monotonic_nanoseconds() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


class PersonFollowerNode(Node):
    """Resolve one map-frame person position and follow it through Nav2."""

    def __init__(self) -> None:
        """Create sensor input, follow action, TF, status, and Nav2 clients."""
        super().__init__('person_follower')
        self._declare_parameters()
        self._validate_parameters()

        self._global_frame = str(self.get_parameter('global_frame').value)
        self._odometry_frame = str(
            self.get_parameter('odometry_frame').value
        )
        self._robot_frame = str(self.get_parameter('robot_frame').value)
        self._tf_timeout = float(
            self.get_parameter('transform_timeout_s').value
        )
        self._tf_buffer = Buffer()
        # TF must continue filling while callbacks wait for measurement-time
        # odometry. The slower map correction is composed through fixed-frame
        # lookup instead of replacing fast ego motion with one latest pose.
        self._tf_listener = TransformListener(
            self._tf_buffer,
            self,
            spin_thread=True,
        )

        self._obstacle_tracker = ObstacleTargetTracker(
            process_variance=float(
                self.get_parameter('tracker_process_variance').value
            ),
            measurement_variance=float(
                self.get_parameter('tracker_measurement_variance').value
            ),
            mahalanobis_gate=float(
                self.get_parameter('mahalanobis_gate').value
            ),
            confirmation_hits=int(
                self.get_parameter('track_confirmation_hits').value
            ),
            maximum_missed_updates=int(
                self.get_parameter('maximum_missed_updates').value
            ),
            maximum_coast_time_s=float(
                self.get_parameter('maximum_coast_time_s').value
            ),
            camera_label_gate_m=float(
                self.get_parameter('camera_label_gate_m').value
            ),
            camera_rebind_margin_m=float(
                self.get_parameter('camera_rebind_margin_m').value
            ),
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
            self._on_nav2_feedback,
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
        self._command_trace_publisher = self.create_publisher(
            TrackingCommandTrace,
            str(self.get_parameter('command_trace_topic').value),
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
        self._lidar_clusters_subscription = self.create_subscription(
            LidarClusterArray,
            str(self.get_parameter('lidar_clusters_topic').value),
            self._on_lidar_clusters,
            qos_profile_sensor_data,
        )
        self._costmap_subscription = self.create_subscription(
            Costmap,
            str(self.get_parameter('global_costmap_topic').value),
            self._on_global_costmap,
            10,
        )
        static_map_qos = QoSProfile(depth=1)
        static_map_qos.reliability = ReliabilityPolicy.RELIABLE
        static_map_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self._static_map_subscription = self.create_subscription(
            OccupancyGrid,
            str(self.get_parameter('static_map_topic').value),
            self._on_static_map,
            static_map_qos,
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
        self._target_mode = FollowPerson.Goal.VISIBLE_PERSON
        self._target_person_id = ''
        self._observed_track_id = ''
        self._detector_track_id = ''
        self._state = FollowState.STOPPED
        self._recovery_phase = RecoveryPhase.NONE
        self._last_seen_s: float | None = None
        self._last_camera_seen_s: float | None = None
        self._last_camera_frame_s: float | None = None
        self._pending_detection: Detection3DArray | None = None
        self._pending_detection_received_s: float | None = None
        self._detection_transform_pending = False
        self._camera_miss_count = 0
        self._last_precise_camera_position: Point2D | None = None
        self._last_lidar_stamp_s: float | None = None
        self._lidar_proximity_guard_until_s = 0.0
        self._latest_global_costmap: CostmapGrid | None = None
        self._latest_static_map: CostmapGrid | None = None
        self._lidar_clusters_received = False
        self._last_goal_position: Point2D | None = None
        self._last_target_pose = PoseStamped()
        self._last_target_height = 0.0
        self._current_distance: float | None = None
        self._remaining_travel_distance_m = 0.0
        self._recovery_path_requested = False
        self._recovery_navigation_active = False
        self._recovery_scan_started = False
        self._recovery_direction_target: Point2D | None = None
        self._recovery_last_position: Point2D | None = None
        self._last_observed_bearing_rad = 0.0
        self._last_camera_bearing_rad = 0.0
        self._recovery_turn_sign = 1.0
        self._last_motion_target: Point2D | None = None
        self._last_motion_velocity: Point2D | None = None
        self._last_motion_bearing_only = False
        self._last_motion_source_stamp_ns: int | None = None
        self._motion_generation = 0
        self._goal_dispatch_count = 0
        self._tracking_retry_pending = False
        self._navigation_failure_count = 0
        self._tracking_source = 'none'
        self._last_warning_s: dict[str, float] = {}
        self._cancel_requested_goal = None

        # These timers are canceled while idle and are reset only by the event
        # that needs them. There is no permanent polling loop in the follower.
        self._loss_timer = self.create_timer(
            max(
                1e-3,
                float(
                    self.get_parameter('observation_loss_debounce_s').value
                ),
            ),
            self._on_loss_timer,
        )
        self._loss_timer.cancel()
        self._pending_detection_timer = self.create_timer(
            0.02,
            self._on_pending_detection_timer,
        )
        self._pending_detection_timer.cancel()
        retry_period_s = float(
            self.get_parameter('navigation_retry_delay_s').value
        )
        self._tracking_retry_timer = self.create_timer(
            retry_period_s,
            self._on_tracking_retry_timer,
        )
        self._tracking_retry_timer.cancel()
        self._recovery_retry_timer = self.create_timer(
            retry_period_s,
            self._on_recovery_retry_timer,
        )
        self._recovery_retry_timer.cancel()
        self._cancel_guard = self.create_guard_condition(
            self._on_cancel_guard
        )
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
        self.declare_parameter(
            'lidar_clusters_topic',
            '/perception/lidar/foreground_clusters',
        )
        self.declare_parameter('status_topic', '/tracking/person/status')
        self.declare_parameter(
            'target_pose_topic', '/tracking/person/estimated_target_pose'
        )
        self.declare_parameter(
            'track_markers_topic', '/tracking/person/lidar_tracks'
        )
        self.declare_parameter(
            'command_trace_topic', '/tracking/person/command_trace'
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
        self.declare_parameter('odometry_frame', 'odom')
        self.declare_parameter('robot_frame', 'base_footprint')
        self.declare_parameter('minimum_confidence', 0.20)
        self.declare_parameter('tracker_process_variance', 1.0)
        self.declare_parameter('tracker_measurement_variance', 0.04)
        self.declare_parameter('mahalanobis_gate', 9.21)
        self.declare_parameter('track_confirmation_hits', 3)
        self.declare_parameter('maximum_missed_updates', 4)
        self.declare_parameter('maximum_coast_time_s', 3.0)
        self.declare_parameter('camera_label_gate_m', 0.40)
        self.declare_parameter('camera_lidar_range_gate_m', 1.00)
        self.declare_parameter('lidar_candidate_gate_m', 0.70)
        self.declare_parameter('camera_lidar_fusion_freshness_s', 0.40)
        self.declare_parameter('camera_lidar_extent_padding_m', 0.15)
        self.declare_parameter('camera_rebind_margin_m', 0.15)
        self.declare_parameter('camera_position_alpha', 0.55)
        self.declare_parameter('camera_velocity_alpha', 0.35)
        self.declare_parameter('maximum_person_speed_mps', 2.0)
        self.declare_parameter('lidar_reassociation_max_distance_m', 1.50)
        self.declare_parameter('dynamic_rebind_minimum_speed_mps', 0.10)
        self.declare_parameter('camera_negative_evidence_frames', 2)
        self.declare_parameter('camera_horizontal_fov_rad', 1.291543646)
        self.declare_parameter(
            'lidar_proximity_control_distance_m', 1.50
        )
        self.declare_parameter('lidar_proximity_camera_guard_s', 0.30)
        self.declare_parameter('desired_distance_m', 1.00)
        self.declare_parameter('minimum_distance_m', 0.65)
        self.declare_parameter('distance_tolerance_m', 0.10)
        self.declare_parameter('alignment_angle_tolerance_rad', 0.10)
        self.declare_parameter('minimum_follow_speed_mps', 0.10)
        self.declare_parameter('maximum_linear_speed_mps', 0.40)
        self.declare_parameter('full_speed_travel_distance_m', 1.50)
        self.declare_parameter('approach_prediction_horizon_s', 0.75)
        self.declare_parameter('approach_speed_threshold_mps', 0.10)
        self.declare_parameter('bearing_only_variance_threshold_m2', 1.0)
        # The global inflation layer already encodes the configured 0.55 m
        # wall margin. Only send goals in its low-cost exterior and prefer
        # room-side cells when the raw tracking point falls near geometry.
        self.declare_parameter('goal_maximum_cost', 80)
        self.declare_parameter('static_occupied_threshold', 65)
        self.declare_parameter('static_padding_radius_m', 0.35)
        self.declare_parameter('goal_safe_search_radius_m', 1.00)
        self.declare_parameter('goal_openness_radius_m', 0.60)
        self.declare_parameter('goal_openness_preference_m', 0.30)
        self.declare_parameter('heading_probe_distance_m', 0.90)
        self.declare_parameter('minimum_heading_clearance_m', 0.45)
        self.declare_parameter('require_global_costmap_for_goal', True)
        self.declare_parameter('observation_loss_debounce_s', 0.75)
        self.declare_parameter('recovery_direction_minimum_turn_rad', 0.70)
        self.declare_parameter('recovery_waypoint_tolerance_m', 0.08)
        self.declare_parameter('recovery_scan_angle_rad', 4.71238898)
        self.declare_parameter('prediction_horizon_s', 0.60)
        self.declare_parameter('recovery_spin_allowance_s', 12.0)
        self.declare_parameter('transform_timeout_s', 0.10)
        self.declare_parameter('sensor_transform_queue_timeout_s', 0.30)

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
        if not str(self.get_parameter('static_map_topic').value).startswith('/'):
            raise ValueError('static_map_topic must be absolute')
        if not str(
            self.get_parameter('lidar_clusters_topic').value
        ).startswith('/'):
            raise ValueError('lidar_clusters_topic must be absolute')
        if not str(self.get_parameter('odometry_frame').value):
            raise ValueError('odometry_frame must not be empty')
        if float(self.get_parameter('minimum_confidence').value) < 0.0:
            raise ValueError('minimum_confidence must be non-negative')
        for parameter_name in (
            'navigation_retry_delay_s',
            'camera_position_alpha',
            'camera_velocity_alpha',
            'maximum_person_speed_mps',
            'lidar_candidate_gate_m',
            'camera_lidar_fusion_freshness_s',
            'camera_lidar_range_gate_m',
            'lidar_proximity_control_distance_m',
            'lidar_proximity_camera_guard_s',
            'lidar_reassociation_max_distance_m',
            'dynamic_rebind_minimum_speed_mps',
            'camera_horizontal_fov_rad',
            'bearing_only_variance_threshold_m2',
            'approach_prediction_horizon_s',
            'approach_speed_threshold_mps',
            'goal_safe_search_radius_m',
            'goal_openness_radius_m',
            'static_padding_radius_m',
            'heading_probe_distance_m',
            'minimum_heading_clearance_m',
            'alignment_angle_tolerance_rad',
            'recovery_direction_minimum_turn_rad',
            'recovery_waypoint_tolerance_m',
            'recovery_scan_angle_rad',
            'recovery_spin_allowance_s',
            'sensor_transform_queue_timeout_s',
        ):
            if float(self.get_parameter(parameter_name).value) <= 0.0:
                raise ValueError(f'{parameter_name} must be positive')
        static_occupied_threshold = int(
            self.get_parameter('static_occupied_threshold').value
        )
        if not 0 <= static_occupied_threshold <= 100:
            raise ValueError('static_occupied_threshold must be in [0, 100]')
        if float(
            self.get_parameter('camera_lidar_extent_padding_m').value
        ) < 0.0:
            raise ValueError(
                'camera_lidar_extent_padding_m must be non-negative'
            )
        if int(self.get_parameter('camera_negative_evidence_frames').value) < 1:
            raise ValueError(
                'camera_negative_evidence_frames must be positive'
            )
        if float(
            self.get_parameter('camera_horizontal_fov_rad').value
        ) >= math.pi:
            raise ValueError('camera_horizontal_fov_rad must be below pi')
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
        if float(self.get_parameter('prediction_horizon_s').value) < 0.0:
            raise ValueError('prediction_horizon_s must be non-negative')
        if float(
            self.get_parameter('recovery_direction_minimum_turn_rad').value
        ) > math.pi:
            raise ValueError(
                'recovery_direction_minimum_turn_rad must not exceed pi'
            )
        if float(
            self.get_parameter('alignment_angle_tolerance_rad').value
        ) > math.pi:
            raise ValueError(
                'alignment_angle_tolerance_rad must not exceed pi'
            )
        recovery_scan_angle = float(
            self.get_parameter('recovery_scan_angle_rad').value
        )
        if recovery_scan_angle > math.tau:
            raise ValueError('recovery_scan_angle_rad must not exceed tau')
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
            minimum_follow_speed_mps=float(
                self.get_parameter('minimum_follow_speed_mps').value
            ),
            maximum_linear_speed_mps=float(
                self.get_parameter('maximum_linear_speed_mps').value
            ),
            full_speed_travel_distance_m=float(
                self.get_parameter('full_speed_travel_distance_m').value
            ),
            observation_loss_debounce_s=float(
                self.get_parameter('observation_loss_debounce_s').value
            ),
        )

    def _settings_for_goal(self, request) -> FollowSettings:
        defaults = self._default_settings()
        settings = FollowSettings(
            desired_distance_m=(
                float(request.desired_distance_m)
                if request.desired_distance_m > 0.0
                else defaults.desired_distance_m
            ),
            minimum_distance_m=defaults.minimum_distance_m,
            distance_tolerance_m=defaults.distance_tolerance_m,
            minimum_follow_speed_mps=defaults.minimum_follow_speed_mps,
            maximum_linear_speed_mps=defaults.maximum_linear_speed_mps,
            full_speed_travel_distance_m=(
                defaults.full_speed_travel_distance_m
            ),
            observation_loss_debounce_s=(
                defaults.observation_loss_debounce_s
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
            self._validate_target_request(request)
        except ValueError as error:
            self.get_logger().warning(
                f'Rejecting invalid follow goal: {error}'
            )
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _validate_target_request(self, request) -> None:
        valid_modes = {
            FollowPerson.Goal.VISIBLE_PERSON,
            FollowPerson.Goal.REGISTERED_PERSON,
        }
        if int(request.target_mode) not in valid_modes:
            raise ValueError('target_mode is not supported')
        if (
            int(request.target_mode) == FollowPerson.Goal.REGISTERED_PERSON
            and not str(request.target_person_id).strip()
        ):
            raise ValueError(
                'target_person_id is required for REGISTERED_PERSON'
            )

    def _cancel_callback(self, goal_handle) -> CancelResponse:
        """Defer cancellation until the ActionServer enters canceling state."""
        self._cancel_requested_goal = goal_handle
        self._cancel_guard.trigger()
        return CancelResponse.ACCEPT

    def _on_cancel_guard(self) -> None:
        """Finish an accepted cancel request without a polling timer."""
        goal_handle = self._cancel_requested_goal
        self._cancel_requested_goal = None
        if goal_handle is self._active_goal:
            self._cancel_follow_action('follow action canceled')

    def _handle_accepted(self, goal_handle) -> None:
        self._active_goal = goal_handle
        self._result_future = Future()
        self._settings = self._settings_for_goal(goal_handle.request)
        self._target_mode = int(goal_handle.request.target_mode)
        self._target_person_id = str(
            goal_handle.request.target_person_id
        ).strip()
        self._observed_track_id = ''
        self._detector_track_id = ''
        self._obstacle_tracker.clear_selection()
        self._camera_estimator.reset()
        self._last_seen_s = None
        self._last_camera_seen_s = None
        self._last_camera_frame_s = None
        self._pending_detection = None
        self._pending_detection_received_s = None
        self._detection_transform_pending = False
        self._camera_miss_count = 0
        self._last_precise_camera_position = None
        self._last_lidar_stamp_s = None
        self._lidar_proximity_guard_until_s = 0.0
        self._last_goal_position = None
        self._last_target_pose = PoseStamped()
        self._last_target_height = 0.0
        self._last_observed_bearing_rad = 0.0
        self._last_camera_bearing_rad = 0.0
        self._recovery_turn_sign = 1.0
        self._current_distance = None
        self._remaining_travel_distance_m = 0.0
        self._reset_recovery()
        self._last_motion_target = None
        self._last_motion_velocity = None
        self._last_motion_bearing_only = False
        self._last_motion_source_stamp_ns = None
        self._motion_generation = 0
        self._goal_dispatch_count = 0
        self._tracking_retry_pending = False
        self._tracking_retry_timer.cancel()
        self._loss_timer.cancel()
        self._pending_detection_timer.cancel()
        self._recovery_retry_timer.cancel()
        self._navigation_failure_count = 0
        self._tracking_source = 'none'
        self._set_state(FollowState.IDLE)
        self._publish_speed_limit()
        goal_handle.execute()
        self.get_logger().info(
            'Waiting to acquire '
            + (
                f'registered person {self._target_person_id!r}'
                if self._target_mode == FollowPerson.Goal.REGISTERED_PERSON
                else 'the first visible person'
            )
            + f' at {self._settings.desired_distance_m:.2f} m'
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

    def _on_detections(
        self,
        message: Detection3DArray,
        *,
        retrying_transform: bool = False,
    ) -> None:
        if self._active_goal is None:
            return
        now_s = self._now_seconds()
        if not retrying_transform:
            self._last_camera_frame_s = now_s
        self._detection_transform_pending = False
        observation = self._select_target_observation(message)
        if observation is None:
            if self._detection_transform_pending:
                # ros_gz can deliver the stamped image result a few
                # milliseconds before the matching odom/TF sample. Keep the
                # oldest pending frame until that transform arrives. Replacing
                # it with every newer camera frame can starve the queue when
                # TF consistently trails the camera stream.
                if self._pending_detection is None:
                    self._pending_detection = message
                    self._pending_detection_received_s = now_s
                    self._pending_detection_timer.reset()
                return
            self._pending_detection = None
            self._pending_detection_received_s = None
            self._pending_detection_timer.cancel()
            self._record_camera_miss()
            return
        self._pending_detection = None
        self._pending_detection_received_s = None
        self._pending_detection_timer.cancel()
        detection, detected_pose = observation
        bearing_only = self._is_bearing_only(detection)
        camera_position = Point2D(
            detected_pose.pose.position.x,
            detected_pose.pose.position.y,
        )
        try:
            robot_position, robot_yaw = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'target_tf', f'Target TF unavailable: {error}'
            )
            return
        if bearing_only:
            grid = self._latest_global_costmap
            projected = (
                first_admissible_point_on_ray(
                    grid,
                    robot_position,
                    camera_position,
                    int(self.get_parameter('goal_maximum_cost').value),
                )
                if grid is not None
                else None
            )
            if projected is None:
                self._warn_periodically(
                    'bearing_goal_unavailable',
                    'No free global-costmap point exists on the camera ray '
                    'at or beyond the depth range',
                )
                return
            camera_position = projected
        raw_camera_position = camera_position
        fused_lidar = (
            None
            if bearing_only
            else self._camera_lidar_fusion(
                robot_position,
                raw_camera_position,
            )
        )
        if fused_lidar is not None:
            _, camera_position = fused_lidar
        camera_estimate = self._camera_estimator.update(camera_position, now_s)
        self._last_camera_seen_s = now_s
        self._camera_miss_count = 0
        self._last_precise_camera_position = (
            None if bearing_only else raw_camera_position
        )
        self._last_seen_s = now_s
        self._detector_track_id = detection.id
        self._last_target_height = detected_pose.pose.position.z
        if not self._observed_track_id:
            self._observed_track_id = (
                self._target_person_id
                if self._target_mode
                == FollowPerson.Goal.REGISTERED_PERSON
                else 'person-1'
            )
            self.get_logger().info(
                f'Acquired {self._observed_track_id} from sensor-backed '
                'RGB-D position'
            )
        observed_yaw = math.atan2(
            camera_position.y - robot_position.y,
            camera_position.x - robot_position.x,
        )
        observed_bearing = normalize_angle(observed_yaw - robot_yaw)
        if abs(observed_bearing) > 1e-3:
            self._last_camera_bearing_rad = observed_bearing
            self._last_observed_bearing_rad = observed_bearing
        label = (
            self._obstacle_tracker.target.label
            if self._obstacle_tracker.target is not None
            else self._observed_track_id
        )
        labeled = None
        if not bearing_only:
            previous_track_id = (
                self._obstacle_tracker.target.track.track_id
                if self._obstacle_tracker.target is not None
                else None
            )
            if fused_lidar is not None:
                lidar_track, _ = fused_lidar
                labeled = self._obstacle_tracker.bind_observed_track(
                    label,
                    lidar_track.track_id,
                    detection.id,
                )
            else:
                labeled = self._obstacle_tracker.bind(
                    label,
                    camera_position,
                    detection.id,
                )
            if (
                labeled is not None
                and labeled.track.track_id != previous_track_id
            ):
                self.get_logger().info(
                    f'Labeled LiDAR foreground track '
                    f'{labeled.track.track_id} as {label}'
                )
        if labeled is None:
            self._warn_periodically(
                'camera_only_tracking',
                'Person is outside LiDAR association; using '
                + (
                    'the first free point beyond the RGB depth bound'
                    if bearing_only
                    else 'coarse RGB-D tracking'
                ),
            )
        # The RGB-D observation has already been transformed into `map`.
        # LiDAR binding above records which foreground obstacle may continue
        # the person after camera loss, but it must never replace the current
        # camera position with a nearby wall or furniture centroid.
        self._accept_map_target(
            camera_position,
            robot_position,
            now_s,
            source=(
                'bearing'
                if bearing_only
                else 'camera_lidar'
                if fused_lidar is not None
                else 'camera'
            ),
            bearing_only=bearing_only,
            target_velocity=(
                None if bearing_only else camera_estimate.velocity
            ),
            source_stamp_ns=self._message_stamp_nanoseconds(
                message,
                detection,
            ),
        )

    def _retry_pending_detection(self, now_s: float) -> None:
        """Retry the oldest pending detection until matching TF is ready."""
        message = self._pending_detection
        received_s = self._pending_detection_received_s
        if message is None or received_s is None:
            return
        timeout_s = float(
            self.get_parameter('sensor_transform_queue_timeout_s').value
        )
        if now_s - received_s > timeout_s:
            self._pending_detection = None
            self._pending_detection_received_s = None
            self._pending_detection_timer.cancel()
            self._record_camera_miss()
            self._warn_periodically(
                'target_tf_expired',
                'Dropping a detection because its matching TF did not arrive '
                f'within {timeout_s:.2f} s',
            )
            return
        self._on_detections(message, retrying_transform=True)

    def _on_pending_detection_timer(self) -> None:
        """Retry TF only while one detection is actually waiting for it."""
        self._pending_detection_timer.cancel()
        self._retry_pending_detection(self._now_seconds())
        if self._pending_detection is not None:
            self._pending_detection_timer.reset()

    def _message_stamp_nanoseconds(
        self,
        message: Detection3DArray,
        detection: Detection3D,
    ) -> int:
        """Return the exact sensor timestamp used by one accepted detection."""
        stamp = detection.header.stamp
        if stamp.sec == 0 and stamp.nanosec == 0:
            stamp = message.header.stamp
        nanoseconds = _stamp_nanoseconds(stamp)
        if nanoseconds > 0:
            return nanoseconds
        return self.get_clock().now().nanoseconds

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
        """Cache the merged Nav2 grid only for collision-safe goal selection."""
        try:
            grid = self._costmap_grid(message)
        except ValueError as error:
            self._warn_periodically(
                'invalid_costmap', f'Ignoring invalid global costmap: {error}'
            )
            return
        self._latest_global_costmap = grid

    def _on_static_map(self, message: OccupancyGrid) -> None:
        """Pad and cache the already-built SLAM map exactly once."""
        if self._latest_static_map is not None:
            return
        try:
            grid = self._occupancy_grid(message)
        except ValueError as error:
            self._warn_periodically(
                'invalid_static_map', f'Ignoring invalid static map: {error}'
            )
            return
        padding_radius_m = float(
            self.get_parameter('static_padding_radius_m').value
        )
        self._latest_static_map = pad_static_map(
            grid,
            int(self.get_parameter('static_occupied_threshold').value),
            padding_radius_m,
        )
        self.get_logger().info(
            'Cached padded static navigation map '
            f'(radius={padding_radius_m:.2f} m)'
        )

    def _on_lidar_clusters(self, message: LidarClusterArray) -> None:
        """Track compact map-frame clusters produced by the C++ front end."""
        if message.header.frame_id != self._global_frame:
            self._warn_periodically(
                'lidar_cluster_frame',
                'Ignoring LiDAR clusters outside the global frame: '
                f'{message.header.frame_id or "<empty>"}',
            )
            return
        self._lidar_clusters_received = True
        clusters = [
            ObstacleCluster(
                Point2D(cluster.position.x, cluster.position.y),
                int(cluster.point_count),
                float(cluster.extent_m),
            )
            for cluster in message.clusters
        ]
        stamp_s = _stamp_seconds(message.header.stamp)
        if stamp_s <= 0.0:
            stamp_s = self._now_seconds()
        source_stamp_ns = _stamp_nanoseconds(message.header.stamp)
        if source_stamp_ns <= 0:
            source_stamp_ns = self.get_clock().now().nanoseconds
        fresh_camera_position = self._fresh_precise_camera_position()
        gate_center = (
            fresh_camera_position
            if fresh_camera_position is not None
            else self._lidar_candidate_center(stamp_s)
        )
        if gate_center is None:
            clusters = []
        elif fresh_camera_position is not None:
            # A current RGB-D detection is positive evidence for its own
            # region and negative evidence for neighboring LiDAR geometry.
            # Keep a small bounded extent allowance for torso/leg centroid
            # differences, rather than retaining every obstacle in the wider
            # prediction gate.
            extent_padding_m = float(
                self.get_parameter('camera_lidar_extent_padding_m').value
            )
            try:
                robot_position, _ = self._robot_pose()
            except TransformException as error:
                self._warn_periodically(
                    'lidar_fusion_tf',
                    f'Camera-LiDAR fusion TF unavailable: {error}',
                )
                clusters = []
            else:
                clusters = [
                    cluster
                    for cluster in clusters
                    if fuse_camera_bearing_with_lidar_range(
                        robot_position,
                        fresh_camera_position,
                        cluster.position,
                        float(
                            self.get_parameter('camera_label_gate_m').value
                        )
                        + min(extent_padding_m, 0.5 * cluster.extent_m),
                        float(
                            self.get_parameter(
                                'camera_lidar_range_gate_m'
                            ).value
                        ),
                    )
                    is not None
                ]
        else:
            camera_age_s = (
                0.0
                if self._last_camera_seen_s is None
                else max(
                    0.0,
                    self._now_seconds() - self._last_camera_seen_s,
                )
            )
            gate_m = min(
                float(
                    self.get_parameter(
                        'lidar_reassociation_max_distance_m'
                    ).value
                ),
                float(self.get_parameter('lidar_candidate_gate_m').value)
                + float(
                    self.get_parameter('maximum_person_speed_mps').value
                )
                * min(
                    camera_age_s,
                    float(self.get_parameter('prediction_horizon_s').value),
                ),
            )
            clusters = [
                cluster
                for cluster in clusters
                if distance(cluster.position, gate_center) <= gate_m
            ]
        labeled = self._obstacle_tracker.update(clusters, stamp_s)
        reassociated = None
        if labeled is None:
            # A track measured in this scan already carries the camera-backed
            # person label. Never replace that valid observation merely
            # because another moving cluster also exists nearby.
            reassociated = self._rebind_unique_moving_lidar_target(stamp_s)
            if reassociated is not None:
                labeled = reassociated
            else:
                labeled = self._bind_predicted_lidar_target()
        self._publish_track_markers()
        if (
            self._active_goal is not None
            and labeled is not None
            and labeled.track.confirmed
        ):
            if not self._accept_lidar_proximity(
                labeled,
                source_stamp_ns,
            ):
                self._accept_lidar_continuation(
                    labeled,
                    source_stamp_ns,
                    allow_camera_negative_rebind=reassociated is not None,
                )

    def _fresh_precise_camera_position(self) -> Point2D | None:
        """Return an unfiltered RGB-D point only while it is current."""
        if (
            self._last_precise_camera_position is None
            or self._last_camera_seen_s is None
        ):
            return None
        if self._now_seconds() - self._last_camera_seen_s > float(
            self.get_parameter('camera_lidar_fusion_freshness_s').value
        ):
            return None
        return self._last_precise_camera_position

    def _record_camera_miss(self) -> None:
        """Record one camera frame without an accepted target observation."""
        self._camera_miss_count += 1
        self._last_precise_camera_position = None

    def _camera_lidar_fusion(
        self,
        robot_position: Point2D,
        camera_position: Point2D,
    ) -> tuple[TrackedObstacle, Point2D] | None:
        """Select one current person cluster and fuse its range with RGB."""
        maximum_padding_m = float(
            self.get_parameter('camera_lidar_extent_padding_m').value
        )
        candidates = []
        for track in self._obstacle_tracker.tracks:
            if not track.confirmed or track.misses != 0:
                continue
            fused = fuse_camera_bearing_with_lidar_range(
                robot_position,
                camera_position,
                track.position,
                float(self.get_parameter('camera_label_gate_m').value)
                + min(maximum_padding_m, 0.5 * track.extent_m),
                float(
                    self.get_parameter('camera_lidar_range_gate_m').value
                ),
            )
            if fused is None:
                continue
            candidates.append(
                (
                    distance(track.position, fused),
                    distance(camera_position, fused),
                    track.track_id,
                    track,
                    fused,
                )
            )
        if not candidates:
            return None
        _, _, _, track, fused = min(candidates)
        return track, fused

    def _lidar_candidate_center(self, stamp_s: float) -> Point2D | None:
        """Return the only region where LiDAR may support the camera target."""
        if self._active_goal is None or self._last_camera_seen_s is None:
            return None
        now_s = self._now_seconds()
        settings = self._settings
        if (
            settings is not None
            and now_s - self._last_camera_seen_s
            > settings.observation_loss_debounce_s
        ):
            tracked = self._obstacle_tracker.predict_target(
                stamp_s,
                float(self.get_parameter('prediction_horizon_s').value),
            )
            if tracked is not None:
                return tracked
        return self._camera_estimator.predict(
            stamp_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )

    def _rebind_unique_moving_lidar_target(
        self,
        stamp_s: float,
    ) -> LabeledObstacle | None:
        """Use camera negative evidence to recover one nearby moving track."""
        if (
            self._camera_miss_count
            < int(
                self.get_parameter('camera_negative_evidence_frames').value
            )
            or self._last_camera_frame_s is None
            or self._now_seconds() - self._last_camera_frame_s
            > float(
                self.get_parameter(
                    'camera_lidar_fusion_freshness_s'
                ).value
            )
        ):
            return None
        reference = self._camera_estimator.predict(
            stamp_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )
        if reference is None:
            return None
        try:
            robot_position, robot_yaw = self._robot_pose()
        except TransformException:
            return None
        reference_bearing = normalize_angle(
            math.atan2(
                reference.y - robot_position.y,
                reference.x - robot_position.x,
            )
            - robot_yaw
        )
        if abs(reference_bearing) > 0.5 * float(
            self.get_parameter('camera_horizontal_fov_rad').value
        ):
            # No detection outside the image says nothing about identity.
            return None
        selected = self._obstacle_tracker.target
        selected_track_id = (
            selected.track.track_id if selected is not None else None
        )
        maximum_distance_m = float(
            self.get_parameter('lidar_reassociation_max_distance_m').value
        )
        minimum_speed_mps = float(
            self.get_parameter('dynamic_rebind_minimum_speed_mps').value
        )
        candidates = [
            track
            for track in self._obstacle_tracker.tracks
            if track.track_id != selected_track_id
            and track.confirmed
            and track.misses == 0
            and math.hypot(track.velocity.x, track.velocity.y)
            >= minimum_speed_mps
            and distance(track.position, reference) <= maximum_distance_m
        ]
        if len(candidates) != 1:
            # Zero candidates means no evidence; multiple candidates remain
            # ambiguous. In either case keep prediction rather than guessing.
            return None
        candidate = candidates[0]
        rebound = self._obstacle_tracker.bind_observed_track(
            self._observed_track_id or 'person-1',
            candidate.track_id,
            self._detector_track_id,
        )
        if rebound is not None:
            self.get_logger().info(
                'Camera found no person at the predicted point; rebound to '
                f'unique moving LiDAR track {candidate.track_id}'
            )
        return rebound

    def _bind_predicted_lidar_target(self) -> LabeledObstacle | None:
        """Rebind only while the previously labeled LiDAR track is alive."""
        if self._active_goal is None or self._last_camera_seen_s is None:
            return None
        now_s = self._now_seconds()
        camera_age_s = now_s - self._last_camera_seen_s
        settings = self._settings
        if (
            settings is None
            or camera_age_s <= settings.observation_loss_debounce_s
            or self._obstacle_tracker.target is None
        ):
            return None
        prediction = self._obstacle_tracker.predict_target(
            now_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )
        if prediction is None:
            return None
        return self._obstacle_tracker.bind(
            self._observed_track_id or 'person-1',
            prediction,
            self._detector_track_id,
        )

    def _accept_lidar_continuation(
        self,
        labeled: LabeledObstacle,
        source_stamp_ns: int,
        *,
        allow_camera_negative_rebind: bool = False,
    ) -> bool:
        """Continue a camera-labeled target from an observed LiDAR track."""
        now_s = self._now_seconds()
        if (
            self._last_camera_seen_s is None
            or not labeled.track.confirmed
            or labeled.track.misses != 0
        ):
            return False
        if (
            self._last_lidar_stamp_s is not None
            and labeled.stamp_seconds <= self._last_lidar_stamp_s
        ):
            return False
        camera_age_s = now_s - self._last_camera_seen_s
        settings = self._settings
        if (
            settings is not None
            and camera_age_s <= settings.observation_loss_debounce_s
            and not allow_camera_negative_rebind
        ):
            # RGB-D owns the map target while it is current. Do not let an
            # asynchronous LiDAR update replace that authoritative point.
            # The labeled dynamic track owns camera-loss continuation.
            return False
        try:
            robot_position, robot_yaw = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'lidar_target_tf',
                f'LiDAR target TF unavailable: {error}',
            )
            return False
        self._last_lidar_stamp_s = labeled.stamp_seconds
        self._last_seen_s = now_s
        self._record_observed_bearing(
            robot_position,
            robot_yaw,
            labeled.track.position,
        )
        self._accept_map_target(
            labeled.track.position,
            robot_position,
            now_s,
            source='lidar',
            target_velocity=labeled.track.velocity,
            source_stamp_ns=source_stamp_ns,
        )
        return True

    def _accept_lidar_proximity(
        self,
        labeled: LabeledObstacle,
        source_stamp_ns: int,
    ) -> bool:
        """Apply fast LiDAR ALIGN/RETREAT only to the camera-labeled person."""
        settings = self._settings
        if (
            settings is None
            or self._last_camera_seen_s is None
            or labeled.track.misses != 0
        ):
            # A coasted prediction is not a current range measurement and
            # must not drive close-range ALIGN/RETREAT control.
            return False
        now_s = self._now_seconds()
        if (
            self._last_lidar_stamp_s is not None
            and labeled.stamp_seconds <= self._last_lidar_stamp_s
        ):
            return False
        try:
            robot_position, robot_yaw = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'lidar_proximity_tf',
                f'LiDAR proximity TF unavailable: {error}',
            )
            return False
        control_position = labeled.track.position
        fresh_camera_position = self._fresh_precise_camera_position()
        if fresh_camera_position is not None:
            fused = fuse_camera_bearing_with_lidar_range(
                robot_position,
                fresh_camera_position,
                labeled.track.position,
                float(self.get_parameter('camera_label_gate_m').value)
                + min(
                    float(
                        self.get_parameter(
                            'camera_lidar_extent_padding_m'
                        ).value
                    ),
                    0.5 * labeled.track.extent_m,
                ),
                float(
                    self.get_parameter('camera_lidar_range_gate_m').value
                ),
            )
            if fused is not None:
                control_position = fused
        target_distance = distance(robot_position, control_position)
        if target_distance > float(
            self.get_parameter('lidar_proximity_control_distance_m').value
        ):
            return False
        decision = decide_follow_motion(
            robot_position,
            control_position,
            settings,
            target_velocity=labeled.track.velocity,
            approach_prediction_horizon_s=float(
                self.get_parameter('approach_prediction_horizon_s').value
            ),
            approach_speed_threshold_mps=float(
                self.get_parameter('approach_speed_threshold_mps').value
            ),
        )
        if decision.command == FollowCommand.NAVIGATE:
            # LiDAR improves close-range response but never initiates forward
            # pursuit while a camera observation remains authoritative.
            return False
        recovering = self._state != FollowState.TRACKING
        self._reset_recovery()
        if recovering and self._nav2.mode == MotionMode.SPIN:
            self._nav2.cancel()
        self._set_state(FollowState.TRACKING)
        self._tracking_source = 'lidar_proximity'
        self._last_lidar_stamp_s = labeled.stamp_seconds
        self._last_seen_s = now_s
        self._arm_loss_timer()
        self._record_observed_bearing(
            robot_position,
            robot_yaw,
            control_position,
        )
        self._lidar_proximity_guard_until_s = now_s + float(
            self.get_parameter('lidar_proximity_camera_guard_s').value
        )
        self._last_target_pose = self._make_target_pose(
            control_position,
            self._last_target_height,
        )
        self._target_pose_publisher.publish(self._last_target_pose)
        self._apply_tracking_motion(
            robot_position,
            control_position,
            now_s,
            target_velocity=labeled.track.velocity,
            source_stamp_ns=source_stamp_ns,
        )
        self._publish_track_markers()
        self._publish_feedback()
        return True

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

    def _occupancy_grid(self, message: OccupancyGrid) -> CostmapGrid:
        if message.header.frame_id != self._global_frame:
            raise ValueError(
                f'expected frame {self._global_frame}, '
                f'got {message.header.frame_id or "<empty>"}'
            )
        orientation = message.info.origin.orientation
        grid = CostmapGrid(
            frame_id=message.header.frame_id,
            stamp_seconds=_stamp_seconds(message.header.stamp),
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
            costs=tuple(int(value) for value in message.data),
        )
        grid.validate()
        return grid

    def _record_observed_bearing(
        self,
        robot_position: Point2D,
        robot_yaw: float,
        target_position: Point2D,
    ) -> None:
        """Remember the side of the latest sensor-backed person position."""
        target_yaw = math.atan2(
            target_position.y - robot_position.y,
            target_position.x - robot_position.x,
        )
        observed_bearing = normalize_angle(target_yaw - robot_yaw)
        if abs(observed_bearing) > 1e-3:
            self._last_observed_bearing_rad = observed_bearing

    def _accept_map_target(
        self,
        target_position: Point2D,
        robot_position: Point2D,
        now_s: float,
        *,
        source: str,
        bearing_only: bool = False,
        target_velocity: Point2D | None = None,
        source_stamp_ns: int | None = None,
    ) -> None:
        """Publish and follow the single resolved person point in `map`."""
        self._last_target_pose = self._make_target_pose(
            target_position,
            self._last_target_height,
        )
        self._target_pose_publisher.publish(self._last_target_pose)
        recovering = self._state == FollowState.RECOVERING
        if recovering:
            # Invalidate any outstanding recovery-plan callback before the
            # public state returns to TRACKING. A fresh camera/LiDAR target is
            # the only event allowed to end recovery.
            self._path_planner.cancel()
        self._reset_recovery()
        if recovering and self._nav2.mode == MotionMode.SPIN:
            self._nav2.cancel()
        self._set_state(FollowState.TRACKING)
        self._tracking_source = source
        self._last_seen_s = now_s
        self._arm_loss_timer()
        self._apply_tracking_motion(
            robot_position,
            target_position,
            now_s,
            bearing_only=bearing_only,
            target_velocity=target_velocity,
            source_stamp_ns=source_stamp_ns,
        )
        self._publish_track_markers()
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
            if (
                self._target_mode == FollowPerson.Goal.REGISTERED_PERSON
                and detection.id != self._target_person_id
            ):
                continue
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
                self._detection_transform_pending = True
                self._warn_periodically(
                    'target_tf',
                    f'Target TF not ready; queuing the newest detection: '
                    f'{error}',
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
        selected = select_target_candidate(
            candidates,
            predicted,
        )
        if selected is None:
            return None

        previous_id = self._detector_track_id
        if previous_id and previous_id != selected.observed_track_id:
            self.get_logger().info(
                f'Detector track changed {previous_id} -> '
                f'{selected.observed_track_id or "unknown"}; continuing '
                'with the camera-observed person'
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
        transform = self._tf_buffer.lookup_transform_full(
            self._global_frame,
            Time(),
            source_frame,
            Time.from_msg(stamp),
            self._odometry_frame,
            timeout=Duration(seconds=self._tf_timeout),
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
        bearing_only: bool = False,
        recovery: bool = False,
        target_velocity: Point2D | None = None,
        source_stamp_ns: int | None = None,
        new_observation: bool = True,
    ) -> None:
        settings = self._settings
        if settings is None:
            return
        self._last_motion_target = target_position
        self._last_motion_velocity = target_velocity
        self._last_motion_bearing_only = bearing_only
        if source_stamp_ns is not None:
            self._last_motion_source_stamp_ns = source_stamp_ns
        if new_observation:
            self._motion_generation += 1
        decision = decide_follow_motion(
            robot_position,
            target_position,
            settings,
            # Keep the existing distance-band decision. For forward tracking,
            # only the planner destination changes to the observed person.
            maximum_travel_m=None,
            target_velocity=target_velocity,
            approach_prediction_horizon_s=float(
                self.get_parameter('approach_prediction_horizon_s').value
            ),
            approach_speed_threshold_mps=float(
                self.get_parameter('approach_speed_threshold_mps').value
            ),
        )
        self._current_distance = decision.goal.target_distance
        if (
            self._tracking_source in {'camera', 'bearing'}
            and now_s < self._lidar_proximity_guard_until_s
            and decision.command == FollowCommand.NAVIGATE
        ):
            # Do not let a slower camera range immediately undo a fresh
            # near-range LiDAR safety decision. The guard is deliberately
            # shorter than one normal camera update period.
            return
        if bearing_only and decision.command == FollowCommand.RETREAT:
            # An RGB-only observation proves direction and a lower-bound
            # range, not that the person is close. Never infer reverse motion
            # from that uncertain depth, and stop an existing retreat until
            # metric depth returns.
            self._path_planner.cancel()
            self._last_goal_position = None
            self._cancel_tracking_retry()
            self._remaining_travel_distance_m = 0.0
            self._publish_speed_limit()
            self._publish_track_markers()
            if recovery:
                self._schedule_recovery_navigation_retry()
            return
        if decision.command == FollowCommand.HOLD:
            if self._nav2.mode is not None:
                self._nav2.cancel()
            self._path_planner.cancel()
            self._last_goal_position = None
            self._cancel_tracking_retry()
            self._remaining_travel_distance_m = 0.0
            self._publish_speed_limit()
            self._publish_track_markers()
            if recovery:
                self._schedule_recovery_navigation_retry()
            return
        if decision.command == FollowCommand.ALIGN:
            self._cancel_tracking_retry()
            self._remaining_travel_distance_m = 0.0
            self._publish_speed_limit()
            self._align_with_target(decision.goal.yaw)
            self._publish_track_markers()
            if recovery:
                self._schedule_recovery_navigation_retry()
            return
        if self._tracking_retry_pending:
            return
        if self._nav2.mode == MotionMode.SPIN:
            return
        planning_to_target = decision.command == FollowCommand.NAVIGATE
        requested_position = (
            target_position
            if planning_to_target
            else decision.goal.position
        )
        if self._path_planner.busy:
            # Sensor callbacks keep replacing `_last_motion_*`. Once the
            # in-flight plan completes, exactly one newest observation is
            # planned next instead of polling stale sensor data.
            return
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
            static_path = None
            if planning_to_target:
                static_map = self._latest_static_map
                if static_map is None:
                    self._warn_periodically(
                        'static_map_unavailable',
                        'Waiting for the cached static SLAM map before '
                        'selecting a tracking goal',
                    )
                    return
                static_path = plan_static_path(
                    static_map,
                    robot_position,
                    target_position,
                    int(
                        self.get_parameter(
                            'static_occupied_threshold'
                        ).value
                    ),
                )
                if static_path is None:
                    self._warn_periodically(
                        'static_path_unavailable',
                        'No fixed-map route exists from the robot to the '
                        'current person position',
                    )
                    return
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
                approach_origin=(
                    robot_position if planning_to_target else None
                ),
                static_path=static_path,
            )
            if safe_goal is None:
                if self._nav2.mode == MotionMode.NAVIGATE:
                    self._nav2.cancel()
                self._path_planner.cancel()
                self._last_goal_position = None
                self._warn_periodically(
                    'no_safe_tracking_goal',
                    'No global-costmap goal with the configured margin; '
                    'holding instead of entering obstacle inflation',
                )
                if recovery:
                    self._schedule_recovery_navigation_retry()
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
        plan_source = self._tracking_source
        plan_source_stamp_ns = self._last_motion_source_stamp_ns
        plan_generation = self._motion_generation
        planning_started_ns = _monotonic_nanoseconds()
        if self._path_planner.compute(
            final_pose,
            str(self.get_parameter('planner_id').value),
            lambda path, detail, source=plan_source,
            source_stamp_ns=plan_source_stamp_ns,
            started_ns=planning_started_ns,
            generation=plan_generation: self._on_tracking_path(
                path,
                detail,
                target_position if planning_to_target else None,
                source,
                recovery,
                source_stamp_ns,
                started_ns,
                generation,
            ),
        ):
            if recovery:
                self._recovery_path_requested = True
        else:
            self._warn_periodically(
                'planner_unavailable',
                'Nav2 ComputePathToPose action is not ready',
            )
            if recovery:
                self._schedule_recovery_navigation_retry()
            else:
                self._schedule_tracking_navigation_retry()

    def _on_tracking_path(
        self,
        path,
        detail: str,
        target_position: Point2D | None,
        tracking_source: str,
        recovery: bool,
        source_stamp_ns: int | None,
        planning_started_ns: int,
        planning_generation: int,
    ) -> None:
        """Dispatch a bounded tracking path or full last-seen recovery path."""
        planning_finished_ns = _monotonic_nanoseconds()
        expected_state = (
            FollowState.RECOVERING if recovery else FollowState.TRACKING
        )
        if self._active_goal is None or self._state != expected_state:
            return
        if (
            recovery
            and self._recovery_phase
            != RecoveryPhase.REACHING_LAST_POSITION
        ):
            return
        if path is None:
            self._navigation_failure_count += 1
            self._warn_periodically('tracking_path_failed', detail)
            if recovery:
                self._schedule_recovery_navigation_retry()
            else:
                self._schedule_tracking_navigation_retry()
            return
        if not path.poses:
            self._warn_periodically(
                'empty_tracking_path',
                'Nav2 returned an empty tracking path',
            )
            if recovery:
                self._schedule_recovery_navigation_retry()
            else:
                self._schedule_tracking_navigation_retry()
            return
        if recovery:
            selected_path = path
            endpoint = path.poses[-1].pose.position
            waypoint_position = Point2D(float(endpoint.x), float(endpoint.y))
            travel_description = 'full recovery path'
            travel_distance_m = path_length_m(selected_path)
        elif target_position is not None:
            selected_path = path
            endpoint = path.poses[-1].pose.position
            waypoint_position = Point2D(float(endpoint.x), float(endpoint.y))
            travel_distance_m = path_length_m(selected_path)
            travel_description = 'selected safe tracking goal'
        else:
            selected_path = path
            endpoint = path.poses[-1].pose.position
            waypoint_position = Point2D(float(endpoint.x), float(endpoint.y))
            travel_description = 'full retreat path'
            travel_distance_m = path_length_m(selected_path)
        if self._dispatch_tracking_path(
            selected_path,
            waypoint_position,
            travel_distance_m,
            travel_description,
            tracking_source,
            source_stamp_ns,
            planning_started_ns,
            planning_finished_ns,
            recovery,
        ):
            if not recovery:
                self._plan_latest_observation_if_pending(
                    planning_generation
                )
            return
        self._warn_periodically(
            'follow_path_unavailable',
            'Nav2 FollowPath action is not ready',
        )
        if recovery:
            self._schedule_recovery_navigation_retry()
        else:
            self._schedule_tracking_navigation_retry()

    def _dispatch_tracking_path(
        self,
        path: Path,
        waypoint_position: Point2D,
        travel_distance_m: float,
        travel_description: str,
        tracking_source: str,
        source_stamp_ns: int | None,
        planning_started_ns: int,
        planning_finished_ns: int,
        recovery: bool,
    ) -> bool:
        """Send one already computed route to Nav2 FollowPath."""
        if not self._nav2.follow_path(
            path,
            str(self.get_parameter('tracking_controller_id').value),
            str(self.get_parameter('goal_checker_id').value),
        ):
            return False
        dispatch_ns = _monotonic_nanoseconds()
        self._cancel_tracking_retry()
        self._remaining_travel_distance_m = travel_distance_m
        self._publish_speed_limit()
        self._last_goal_position = waypoint_position
        self._navigation_failure_count = 0
        self._goal_dispatch_count += 1
        self._publish_command_trace(
            source_stamp_ns,
            tracking_source,
            self._goal_dispatch_count,
            planning_started_ns,
            planning_finished_ns,
            dispatch_ns,
        )
        if recovery:
            self._recovery_navigation_active = True
        self.get_logger().info(
            f'Updated path waypoint from {tracking_source}: '
            f'({waypoint_position.x:.2f}, '
            f'{waypoint_position.y:.2f}), '
            f'{travel_description}'
        )
        self._publish_track_markers()
        return True

    def _turn_allowance(self, turn_angle: float) -> float:
        """Scale the existing Nav2 spin allowance to one relative turn."""
        full_scan_angle = float(
            self.get_parameter('recovery_scan_angle_rad').value
        )
        full_scan_allowance = float(
            self.get_parameter('recovery_spin_allowance_s').value
        )
        return max(
            3.0,
            full_scan_allowance * abs(turn_angle) / full_scan_angle + 1.0,
        )

    def _align_with_target(self, target_yaw: float) -> None:
        """Use Nav2 Spin only after translational standoff is satisfied."""
        self._path_planner.cancel()
        if self._nav2.mode == MotionMode.NAVIGATE:
            self._nav2.cancel()
        self._last_goal_position = None
        try:
            _, robot_yaw = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'alignment_tf', f'Alignment TF unavailable: {error}'
            )
            return
        turn_angle = normalize_angle(target_yaw - robot_yaw)
        tolerance = float(
            self.get_parameter('alignment_angle_tolerance_rad').value
        )
        if abs(turn_angle) <= tolerance:
            if self._nav2.mode == MotionMode.SPIN:
                self._nav2.cancel()
            return
        if self._nav2.mode == MotionMode.SPIN:
            return
        if not self._nav2.spin(
            turn_angle,
            self._turn_allowance(turn_angle),
        ):
            self._warn_periodically(
                'alignment_spin_unavailable',
                'Nav2 Spin action is not ready for target alignment',
            )

    def _arm_loss_timer(self) -> None:
        """Restart the one-shot loss deadline for a real sensor observation."""
        if self._active_goal is not None and self._last_seen_s is not None:
            self._loss_timer.reset()

    def _on_loss_timer(self) -> None:
        """Enter recovery once after the latest observation deadline."""
        self._loss_timer.cancel()
        settings = self._settings
        if (
            self._active_goal is None
            or settings is None
            or self._last_seen_s is None
        ):
            return
        now_s = self._now_seconds()
        if now_s - self._last_seen_s < settings.observation_loss_debounce_s:
            self._loss_timer.reset()
            return
        if self._state == FollowState.TRACKING:
            self._begin_loss_recovery(now_s)
            self._publish_feedback()

    def _plan_latest_observation_if_pending(
        self,
        completed_generation: int,
    ) -> None:
        """Coalesce observations received while Nav2 planned the last one."""
        if (
            self._active_goal is None
            or self._state != FollowState.TRACKING
            or self._tracking_retry_pending
            or self._motion_generation <= completed_generation
            or self._last_motion_target is None
        ):
            return
        try:
            robot_position, _ = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'target_tf', f'Target TF unavailable: {error}'
            )
            self._schedule_tracking_navigation_retry()
            return
        self._apply_tracking_motion(
            robot_position,
            self._last_motion_target,
            self._now_seconds(),
            bearing_only=self._last_motion_bearing_only,
            target_velocity=self._last_motion_velocity,
            source_stamp_ns=self._last_motion_source_stamp_ns,
            new_observation=False,
        )

    def _schedule_tracking_navigation_retry(self) -> None:
        """Retry one latest observation after an actual Nav2 failure."""
        self._tracking_retry_pending = True
        self._tracking_retry_timer.reset()

    def _cancel_tracking_retry(self) -> None:
        self._tracking_retry_pending = False
        self._tracking_retry_timer.cancel()

    def _on_tracking_retry_timer(self) -> None:
        """Retry failed planning without continuously replaying sensor data."""
        self._tracking_retry_timer.cancel()
        if (
            self._active_goal is None
            or self._state != FollowState.TRACKING
            or self._last_motion_target is None
        ):
            self._tracking_retry_pending = False
            return
        self._tracking_retry_pending = False
        try:
            robot_position, _ = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'target_tf', f'Target TF unavailable: {error}'
            )
            self._schedule_tracking_navigation_retry()
            return
        self._apply_tracking_motion(
            robot_position,
            self._last_motion_target,
            self._now_seconds(),
            bearing_only=self._last_motion_bearing_only,
            target_velocity=self._last_motion_velocity,
            source_stamp_ns=self._last_motion_source_stamp_ns,
            new_observation=False,
        )

    def _on_recovery_retry_timer(self) -> None:
        """Resume only the recovery phase that requested a retry."""
        self._recovery_retry_timer.cancel()
        if self._active_goal is None or self._state != FollowState.RECOVERING:
            return
        if self._recovery_phase == RecoveryPhase.TURNING_TO_TARGET:
            self._start_direction_turn()
        elif self._recovery_phase == RecoveryPhase.REACHING_LAST_POSITION:
            self._request_last_seen_recovery(self._now_seconds())
        elif self._recovery_phase == RecoveryPhase.SCANNING:
            self._recovery_scan_started = False
            self._start_recovery_scan()

    def _reset_recovery(self) -> None:
        """Forget loss recovery as soon as a sensor target is visible."""
        self._recovery_retry_timer.cancel()
        self._recovery_path_requested = False
        self._recovery_navigation_active = False
        self._recovery_scan_started = False
        self._recovery_direction_target = None
        self._recovery_last_position = None
        self._recovery_phase = RecoveryPhase.NONE

    def _begin_loss_recovery(self, now_s: float) -> None:
        """Freeze the last green target before escalating target recovery."""
        self._cancel_tracking_retry()
        self._path_planner.cancel()
        initial_turn = directed_recovery_turn(
            self._last_camera_bearing_rad,
            self._last_observed_bearing_rad,
            float(
                self.get_parameter(
                    'recovery_direction_minimum_turn_rad'
                ).value
            ),
        )
        if initial_turn is not None:
            self._recovery_turn_sign = math.copysign(
                1.0,
                initial_turn,
            )
        if self._last_target_pose.header.frame_id:
            # `last_seen` has one literal meaning: the final green PERSON
            # marker that was visible before recovery started. Do not replace
            # it with a planner input or another extrapolated sensor point.
            self._recovery_last_position = Point2D(
                self._last_target_pose.pose.position.x,
                self._last_target_pose.pose.position.y,
            )
        else:
            self._recovery_last_position = None
        self._recovery_direction_target = self._recovery_last_position
        self._tracking_source = 'last_seen_recovery'
        # Only the label changes here. The green point itself remains the
        # exact sensor-resolved point already stored in `_last_target_pose`.
        self._publish_track_markers()
        if self._nav2.mode == MotionMode.NAVIGATE:
            self._recovery_phase = RecoveryPhase.FINISHING_WAYPOINT
            self._set_state(FollowState.RECOVERING)
            self.get_logger().info(
                'Person lost; keeping the current Nav2 waypoint unchanged'
            )
            return
        # A visible green goal may still be between path planning and
        # FollowPath dispatch. Never interpret that gap as permission to
        # search; explicitly travel to the frozen last position first.
        self._request_last_seen_recovery(now_s)

    def _start_direction_turn(self) -> None:
        """Turn once toward the frozen final green target."""
        if self._active_goal is None:
            return
        self._path_planner.cancel()
        self._recovery_phase = RecoveryPhase.TURNING_TO_TARGET
        self._set_state(FollowState.RECOVERING)
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
            self._recovery_retry_timer.reset()
            return
        target_yaw = math.atan2(
            target.y - robot_position.y,
            target.x - robot_position.x,
        )
        raw_turn_angle = normalize_angle(target_yaw - robot_yaw)
        if abs(raw_turn_angle) > 1e-3:
            self._recovery_turn_sign = math.copysign(1.0, raw_turn_angle)
        else:
            self.get_logger().info(
                'Current waypoint already faces the final green target'
            )
            self._request_last_seen_recovery(self._now_seconds())
            return
        turn_angle = raw_turn_angle
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
                'Current waypoint reached; turning toward the final green '
                f'target ({math.degrees(turn_angle):.1f} deg)'
            )
            return
        self._warn_periodically(
            'direction_spin_unavailable',
            'Nav2 Spin action is not ready; continuing to the last position',
        )
        self._request_last_seen_recovery(self._now_seconds())

    def _request_last_seen_recovery(self, now_s: float) -> None:
        """Follow one complete Nav2 path to the frozen last target position."""
        self._recovery_phase = RecoveryPhase.REACHING_LAST_POSITION
        self._set_state(FollowState.RECOVERING)
        target_position = self._recovery_last_position
        if target_position is None:
            self._warn_periodically(
                'last_seen_position_unavailable',
                'Last person position is unavailable; holding recovery '
                'instead of starting search before reaching a goal',
            )
            return
        try:
            robot_position, _ = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'recovery_tf', f'Recovery TF unavailable: {error}'
            )
            self._schedule_recovery_navigation_retry()
            return
        target_yaw = math.atan2(
            target_position.y - robot_position.y,
            target_position.x - robot_position.x,
        )
        safe_position = target_position
        safe_yaw = target_yaw
        grid = self._latest_global_costmap
        if grid is not None:
            safe_goal = project_navigation_goal(
                grid,
                target_position,
                target_yaw,
                int(self.get_parameter('goal_maximum_cost').value),
                float(
                    self.get_parameter('goal_safe_search_radius_m').value
                ),
                float(self.get_parameter('goal_openness_radius_m').value),
                0.0,
                float(
                    self.get_parameter('heading_probe_distance_m').value
                ),
                float(
                    self.get_parameter('minimum_heading_clearance_m').value
                ),
                approach_origin=robot_position,
            )
            if safe_goal is None:
                self._warn_periodically(
                    'last_seen_goal_unavailable',
                    'No safe costmap cell exists near the last person '
                    'position; retrying the frozen recovery goal',
                )
                self._schedule_recovery_navigation_retry()
                return
            safe_position = safe_goal.position
            safe_yaw = safe_goal.yaw
        recovery_pose = PoseStamped()
        recovery_pose.header.frame_id = self._global_frame
        recovery_pose.header.stamp = self.get_clock().now().to_msg()
        recovery_pose.pose.position.x = safe_position.x
        recovery_pose.pose.position.y = safe_position.y
        quaternion = yaw_to_quaternion(safe_yaw)
        recovery_pose.pose.orientation.x = quaternion[0]
        recovery_pose.pose.orientation.y = quaternion[1]
        recovery_pose.pose.orientation.z = quaternion[2]
        recovery_pose.pose.orientation.w = quaternion[3]
        self._tracking_source = 'last_seen_recovery'
        planning_started_ns = _monotonic_nanoseconds()
        if self._path_planner.compute(
            recovery_pose,
            str(self.get_parameter('planner_id').value),
            lambda path, detail: self._on_tracking_path(
                path,
                detail,
                None,
                'last_seen_recovery',
                True,
                None,
                planning_started_ns,
                self._motion_generation,
            ),
        ):
            self._recovery_path_requested = True
            return
        self._warn_periodically(
            'recovery_planner_unavailable',
            'Nav2 ComputePathToPose is not ready for last-position recovery',
        )
        self._schedule_recovery_navigation_retry()

    def _schedule_recovery_navigation_retry(self) -> None:
        """Retry the frozen goal later without entering search prematurely."""
        self._recovery_path_requested = False
        self._recovery_navigation_active = False
        self._recovery_retry_timer.reset()

    def _publish_command_trace(
        self,
        source_stamp_ns: int | None,
        source: str,
        sequence: int,
        planning_started_ns: int,
        planning_finished_ns: int,
        dispatch_ns: int,
    ) -> None:
        """Publish ROS correlation and actual same-host monotonic timings."""
        if source_stamp_ns is None or source_stamp_ns <= 0:
            return
        trace = TrackingCommandTrace()
        trace.source_stamp.sec = source_stamp_ns // 1_000_000_000
        trace.source_stamp.nanosec = source_stamp_ns % 1_000_000_000
        trace.dispatch_stamp = self.get_clock().now().to_msg()
        trace.planning_started_steady_time_ns = planning_started_ns
        trace.planning_finished_steady_time_ns = planning_finished_ns
        trace.dispatch_steady_time_ns = dispatch_ns
        trace.sequence = sequence
        trace.source = source
        self._command_trace_publisher.publish(trace)

    def _start_recovery_scan(self) -> None:
        """Scan 270 degrees toward the side where the target disappeared."""
        if self._active_goal is None or self._recovery_scan_started:
            return
        self._path_planner.cancel()
        self._recovery_path_requested = False
        self._recovery_navigation_active = False
        self._recovery_scan_started = True
        self._recovery_phase = RecoveryPhase.SCANNING
        self._set_state(FollowState.RECOVERING)
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
        self._warn_periodically(
            'spin_unavailable',
            'Nav2 Spin action is not ready; waiting safely in place',
        )
        self._recovery_retry_timer.reset()

    def _make_target_pose(self, point: Point2D, z: float) -> PoseStamped:
        pose = PoseStamped()
        pose.header.frame_id = self._global_frame
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.pose.position.x = point.x
        pose.pose.position.y = point.y
        pose.pose.position.z = z
        pose.pose.orientation.w = 1.0
        return pose

    def _on_nav2_feedback(self, mode: MotionMode, feedback) -> None:
        """Advance recovery from Nav2 distance feedback, not TF polling."""
        if mode != MotionMode.NAVIGATE:
            return
        self._remaining_travel_distance_m = max(
            0.0,
            float(feedback.distance_to_goal),
        )
        self._publish_speed_limit()
        if (
            self._state == FollowState.RECOVERING
            and self._recovery_phase == RecoveryPhase.FINISHING_WAYPOINT
            and self._remaining_travel_distance_m
            <= float(
                self.get_parameter('recovery_waypoint_tolerance_m').value
            )
        ):
            self.get_logger().info(
                'Recovery waypoint reached within Nav2 feedback tolerance'
            )
            self._start_direction_turn()
            self._publish_feedback()

    def _on_nav2_result(
        self,
        mode: MotionMode,
        status: int,
        detail: str,
    ) -> None:
        if mode == MotionMode.NAVIGATE:
            if (
                self._state == FollowState.RECOVERING
                and self._recovery_phase
                == RecoveryPhase.FINISHING_WAYPOINT
            ):
                if status == GoalStatus.STATUS_SUCCEEDED:
                    self._start_direction_turn()
                else:
                    self._warn_periodically('waypoint_recovery_failed', detail)
                    self._request_last_seen_recovery(self._now_seconds())
                return
            if (
                self._state == FollowState.RECOVERING
                and self._recovery_phase
                == RecoveryPhase.REACHING_LAST_POSITION
                and self._recovery_navigation_active
            ):
                self._recovery_navigation_active = False
                self._recovery_path_requested = False
                if status == GoalStatus.STATUS_SUCCEEDED:
                    self._start_recovery_scan()
                else:
                    self._warn_periodically(
                        'last_seen_navigation_failed', detail
                    )
                    self._schedule_recovery_navigation_retry()
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
                self._schedule_tracking_navigation_retry()
                self._warn_periodically('navigate_failed', detail)
            else:
                self._navigation_failure_count = 0
        elif mode == MotionMode.SPIN:
            if (
                self._state == FollowState.RECOVERING
                and self._recovery_phase == RecoveryPhase.TURNING_TO_TARGET
            ):
                if status != GoalStatus.STATUS_SUCCEEDED:
                    self._warn_periodically('direction_spin_failed', detail)
                self._request_last_seen_recovery(self._now_seconds())
                return
            if self._recovery_phase == RecoveryPhase.SCANNING:
                if status != GoalStatus.STATUS_SUCCEEDED:
                    self._warn_periodically('spin_failed', detail)
                    self._recovery_scan_started = False
                    self._recovery_retry_timer.reset()
                    return
                self._recovery_scan_started = False
                self.get_logger().info(
                    'Person not reacquired; continuing directed search'
                )
                self._start_recovery_scan()

    def _publish_feedback(self) -> None:
        if self._active_goal is None:
            return
        visibility_timeout_s = (
            self._settings.observation_loss_debounce_s
            if self._settings is not None
            else float(
                self.get_parameter('observation_loss_debounce_s').value
            )
        )
        feedback = FollowPerson.Feedback()
        feedback.state = self._state
        feedback.target_visible = (
            self._last_camera_seen_s is not None
            and self._now_seconds() - self._last_camera_seen_s
            < visibility_timeout_s
        )
        self._active_goal.publish_feedback(feedback)
        self._publish_status(feedback.target_visible)

    def _publish_status(self, target_visible: bool = False) -> None:
        message = String()
        message.data = json.dumps(
            {
                'state': self._state,
                'target_mode': (
                    'REGISTERED_PERSON'
                    if self._target_mode
                    == FollowPerson.Goal.REGISTERED_PERSON
                    else 'VISIBLE_PERSON'
                ),
                'target_person_id': self._target_person_id or None,
                'observed_track_id': self._observed_track_id or None,
                'detector_track_id': self._detector_track_id or None,
                'lidar_obstacle_labeled': (
                    self._obstacle_tracker.target is not None
                ),
                'lidar_track_id': (
                    self._obstacle_tracker.target.track.track_id
                    if self._obstacle_tracker.target is not None
                    else None
                ),
                'lidar_track_confirmed': (
                    self._obstacle_tracker.target.track.confirmed
                    if self._obstacle_tracker.target is not None
                    else False
                ),
                'lidar_track_count': len(self._obstacle_tracker.tracks),
                'lidar_preprocessor_receiving': (
                    self._lidar_clusters_received
                ),
                'tracking_source': self._tracking_source,
                'navigation_failure_count': self._navigation_failure_count,
                'target_visible': target_visible,
                'current_distance_m': self._current_distance,
                'recovery_phase': self._recovery_phase,
                'recovery_path_requested': self._recovery_path_requested,
                'recovery_scan_started': self._recovery_scan_started,
            },
            separators=(',', ':'),
        )
        self._status_publisher.publish(message)

    def _publish_track_markers(self) -> None:
        """Visualize LiDAR tracks, resolved person, and the Nav2 goal."""
        message = MarkerArray()
        clear = Marker()
        clear.action = Marker.DELETEALL
        message.markers.append(clear)
        selected = self._obstacle_tracker.target
        stamp = self.get_clock().now().to_msg()
        # Normal operation exposes only the camera-associated person support.
        # Scene-wide foreground candidates are deliberately not presented as
        # dynamic objects; they are merely intermediate sensor residuals.
        visible_tracks = [selected.track] if selected is not None else []
        for track in visible_tracks:
            body = Marker()
            body.header.frame_id = self._global_frame
            body.header.stamp = stamp
            body.ns = 'lidar_tracks'
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
            body.color.r = 1.0
            body.color.g = 0.2
            body.color.a = 0.75 if track.misses == 0 else 0.30
            message.markers.append(body)

            text_marker = Marker()
            text_marker.header = body.header
            text_marker.ns = 'lidar_track_labels'
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
            suffix = ' coast' if track.misses > 0 else ''
            text_marker.text = f'person-1 / lidar T{track.track_id}{suffix}'
            message.markers.append(text_marker)
        if self._last_target_pose.header.frame_id:
            target = Marker()
            target.header.frame_id = self._global_frame
            target.header.stamp = stamp
            target.ns = 'person_map_target'
            target.id = 1
            target.type = Marker.SPHERE
            target.action = Marker.ADD
            target.pose.position.x = self._last_target_pose.pose.position.x
            target.pose.position.y = self._last_target_pose.pose.position.y
            target.pose.position.z = 0.20
            target.pose.orientation.w = 1.0
            target.scale.x = 0.34
            target.scale.y = 0.34
            target.scale.z = 0.34
            target.color.g = 1.0
            target.color.a = 0.95
            message.markers.append(target)

            target_label = Marker()
            target_label.header = target.header
            target_label.ns = 'person_map_target_label'
            target_label.id = 2
            target_label.type = Marker.TEXT_VIEW_FACING
            target_label.action = Marker.ADD
            target_label.pose.position.x = target.pose.position.x
            target_label.pose.position.y = target.pose.position.y
            target_label.pose.position.z = 0.62
            target_label.pose.orientation.w = 1.0
            target_label.scale.z = 0.24
            target_label.color.r = 1.0
            target_label.color.g = 1.0
            target_label.color.b = 1.0
            target_label.color.a = 1.0
            target_label.text = f'PERSON ({self._tracking_source})'
            message.markers.append(target_label)
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

    def _publish_speed_limit(self) -> None:
        if self._settings is None:
            return
        message = SpeedLimit()
        message.header.stamp = self.get_clock().now().to_msg()
        message.percentage = False
        message.speed_limit = speed_limit_for_travel_distance(
            self._remaining_travel_distance_m,
            self._settings,
        )
        self._speed_publisher.publish(message)

    def _reset_speed_limit(self) -> None:
        message = SpeedLimit()
        message.header.stamp = self.get_clock().now().to_msg()
        message.percentage = False
        message.speed_limit = 0.0
        self._speed_publisher.publish(message)

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        if state not in _ALLOWED_FOLLOW_TRANSITIONS.get(self._state, set()):
            self.get_logger().error(
                f'Rejecting invalid follow-state transition '
                f'{self._state} -> {state}'
            )
            return
        self._state = state
        self.get_logger().info(f'Follow state: {state}')
        self._publish_status()

    def _cancel_follow_action(self, message: str) -> None:
        """Stop the continuous follow action only on an explicit cancel."""
        goal_handle = self._active_goal
        result_future = self._result_future
        if goal_handle is None or result_future is None:
            return
        self._nav2.cancel()
        self._path_planner.cancel()
        self._reset_speed_limit()
        self._set_state(FollowState.STOPPED)
        result = FollowPerson.Result()
        result.success = False
        result.final_state = FollowState.STOPPED
        result.message = message
        goal_handle.canceled()
        self._active_goal = None
        self._result_future = None
        self._settings = None
        self._target_mode = FollowPerson.Goal.VISIBLE_PERSON
        self._target_person_id = ''
        self._observed_track_id = ''
        self._detector_track_id = ''
        self._obstacle_tracker.clear_selection()
        self._camera_estimator.reset()
        self._last_seen_s = None
        self._last_camera_seen_s = None
        self._last_camera_frame_s = None
        self._pending_detection = None
        self._pending_detection_received_s = None
        self._detection_transform_pending = False
        self._camera_miss_count = 0
        self._last_precise_camera_position = None
        self._last_lidar_stamp_s = None
        self._last_goal_position = None
        self._last_motion_target = None
        self._last_motion_velocity = None
        self._last_motion_bearing_only = False
        self._last_motion_source_stamp_ns = None
        self._motion_generation = 0
        self._last_observed_bearing_rad = 0.0
        self._last_camera_bearing_rad = 0.0
        self._recovery_turn_sign = 1.0
        self._loss_timer.cancel()
        self._pending_detection_timer.cancel()
        self._cancel_tracking_retry()
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
