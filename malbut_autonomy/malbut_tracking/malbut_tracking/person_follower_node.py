"""Follow one RGB-D person track safely by delegating motion to Nav2."""

import json
import math
import sys

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import Pose, PoseStamped
from malbut_interfaces.action import FollowPerson
from nav2_msgs.msg import Costmap, SpeedLimit
from nav_msgs.msg import OccupancyGrid
from nav_msgs.srv import GetMap
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
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from tf2_geometry_msgs import do_transform_pose
from tf2_ros import Buffer, TransformException, TransformListener
from vision_msgs.msg import Detection3D, Detection3DArray
from visualization_msgs.msg import Marker, MarkerArray

from .background_memory import (
    BackgroundMemory,
    select_acquisition_turn,
)
from .costmap_tracking import (
    CostmapGrid,
    LabeledObstacle,
    ObstacleTargetTracker,
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
from .goal_safety import (
    first_admissible_point_on_ray,
    project_navigation_goal,
)
from .lidar_foreground import (
    ScanTransform2D,
    StaticDistanceField,
    camera_consistent_clusters,
    extract_foreground_clusters,
)
from .motion_estimator import TargetMotionEstimator
from .navigation import MotionMode, Nav2MotionClient, Nav2PathClient
from .path_sampling import truncate_path
from .target_association import (
    CameraObservationGate,
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


# 카메라만으로 사람을 되찾으려 애쓰는 구간. 이 동안에도 라이다는 사람을
# 보고 있으므로, 후보가 있으면 남은 절차보다 그쪽을 먼저 본다.
_RECOVERY_STATES = (
    FollowState.REACHING_WAYPOINT,
    FollowState.TURNING_TO_TARGET,
    FollowState.REACHING_LAST_POSITION,
    FollowState.SEARCHING,
)


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
        self._camera_observation_gate = CameraObservationGate(
            int(self.get_parameter('camera_jump_confirmation_hits').value),
            float(
                self.get_parameter(
                    'camera_jump_pending_consistency_m'
                ).value
            ),
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
        self._scan_subscription = self.create_subscription(
            LaserScan,
            str(self.get_parameter('scan_topic').value),
            self._on_scan,
            qos_profile_sensor_data,
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
        # 래치된 지도는 늦게 붙은 구독자에게 항상 다시 오지는 않는다.
        # 그러면 _on_scan 이 매번 조기 반환해 라이다 융합이 그 세션 내내
        # 조용히 죽는다. 안 오면 직접 물어본다.
        self._static_map_client = self.create_client(
            GetMap,
            str(self.get_parameter('static_map_service').value),
        )
        self._static_map_request_pending = False
        self._static_map_retry_timer = self.create_timer(
            2.0, self._request_static_map
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
        self._background = BackgroundMemory(
            settle_seconds=float(
                self.get_parameter('background_settle_seconds').value
            ),
            settle_radius_m=float(
                self.get_parameter('background_settle_radius_m').value
            ),
            forget_seconds=float(
                self.get_parameter('background_forget_seconds').value
            ),
        )
        self._acquisition_candidates: list = []
        self._raw_foreground_count = 0
        self._last_acquisition_turn_s: float | None = None
        self._last_seen_s: float | None = None
        self._last_camera_seen_s: float | None = None
        self._last_camera_frame_s: float | None = None
        self._camera_miss_count = 0
        self._last_precise_camera_position: Point2D | None = None
        self._last_lidar_stamp_s: float | None = None
        self._lidar_proximity_guard_until_s = 0.0
        self._latest_global_costmap: CostmapGrid | None = None
        self._latest_static_map: CostmapGrid | None = None
        self._static_distance_field: StaticDistanceField | None = None
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
        self._last_motion_velocity: Point2D | None = None
        self._last_motion_precise = False
        self._last_motion_bearing_only = False
        self._goal_dispatch_count = 0
        self._navigation_retry_not_before_s = 0.0
        self._navigation_failure_count = 0
        self._tracking_source = 'none'
        self._last_speed_publish_s = -math.inf
        self._last_warning_s: dict[str, float] = {}
        self._pending_scan: LaserScan | None = None
        self._latest_clusters: list = []
        self._latest_clusters_s = 0.0
        self._bearing_range_from_lidar = 0
        self._pending_scan_queued_s = 0.0
        self._scan_transform_drops = 0
        self._scan_processed_count = 0
        self._timer = self.create_timer(0.1, self._tick)
        self._scan_transform_timer = self.create_timer(
            0.02,
            self._process_pending_scan,
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
        self.declare_parameter('static_map_service',
                               '/map_server/map')
        self.declare_parameter('scan_topic', '/scan')
        self.declare_parameter('status_topic', '/tracking/person/status')
        self.declare_parameter(
            'target_pose_topic', '/tracking/person/estimated_target_pose'
        )
        self.declare_parameter(
            'track_markers_topic', '/tracking/person/lidar_tracks'
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
        self.declare_parameter('lidar_continuation_max_distance_m', 1.50)
        self.declare_parameter('cluster_gap_m', 0.20)
        self.declare_parameter('minimum_cluster_points', 3)
        self.declare_parameter(
            'minimum_cluster_density_points_per_m', 5.0
        )
        self.declare_parameter('maximum_cluster_points', 120)
        self.declare_parameter('maximum_cluster_extent_m', 0.80)
        self.declare_parameter('static_occupied_threshold', 65)
        self.declare_parameter('static_exclusion_radius_m', 0.20)
        self.declare_parameter('tracker_process_variance', 1.0)
        self.declare_parameter('tracker_measurement_variance', 0.04)
        self.declare_parameter('mahalanobis_gate', 9.21)
        self.declare_parameter('track_confirmation_hits', 3)
        self.declare_parameter('maximum_missed_updates', 4)
        self.declare_parameter('maximum_coast_time_s', 3.0)
        self.declare_parameter('camera_label_gate_m', 0.40)
        self.declare_parameter('lidar_candidate_gate_m', 0.70)
        self.declare_parameter('camera_lidar_fusion_freshness_s', 0.40)
        self.declare_parameter('camera_lidar_extent_padding_m', 0.15)
        self.declare_parameter('camera_jump_base_gate_m', 0.75)
        self.declare_parameter('camera_jump_confirmation_hits', 2)
        self.declare_parameter('camera_jump_pending_consistency_m', 0.50)
        self.declare_parameter('camera_rebind_margin_m', 0.15)
        self.declare_parameter('camera_position_alpha', 0.55)
        self.declare_parameter('camera_velocity_alpha', 0.35)
        self.declare_parameter('maximum_person_speed_mps', 2.0)
        self.declare_parameter('lidar_continuation_timeout_s', 3.0)
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
        self.declare_parameter('maximum_linear_speed_mps', 0.30)
        self.declare_parameter('retreat_maximum_travel_m', 0.12)
        self.declare_parameter('approach_prediction_horizon_s', 0.75)
        self.declare_parameter('approach_speed_threshold_mps', 0.10)
        self.declare_parameter('retreat_goal_update_distance_m', 0.04)
        self.declare_parameter('retreat_goal_update_period_s', 0.15)
        self.declare_parameter('goal_update_distance_m', 0.25)
        self.declare_parameter('goal_update_period_s', 0.75)
        self.declare_parameter('coarse_goal_update_distance_m', 0.50)
        self.declare_parameter('coarse_goal_update_period_s', 0.75)
        self.declare_parameter('bearing_goal_update_distance_m', 0.20)
        self.declare_parameter('bearing_goal_update_period_s', 0.50)
        self.declare_parameter('bearing_only_variance_threshold_m2', 1.0)
        # 깊이가 못 미치는 근거리에서 라이다가 대신 거리를 준다. 카메라는
        # 방위를, 라이다는 거리를 맡는다. 방위선에서 이 각도 안에 있는
        # 가장 가까운 전경 군집을 쓴다.
        self.declare_parameter('bearing_lidar_gate_rad', 0.20)
        self.declare_parameter('bearing_lidar_max_age_s', 0.50)
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
        # 지도를 만든 뒤 들어온 정적 물체를 후보에서 제외하기 위한 값들이다.
        self.declare_parameter('background_settle_seconds', 8.0)
        self.declare_parameter('background_settle_radius_m', 0.25)
        self.declare_parameter('background_forget_seconds', 20.0)
        # 카메라가 사람을 확인하지 못한 동안, 라이다 후보 쪽으로 돌아본다.
        self.declare_parameter('lidar_acquisition_enabled', True)
        # 회전 한 번이 대략 3초다. 그보다 짧게 두면 이전 회전이 끝나기도
        # 전에 다음 회전을 걸어 제자리에서 흔들린다. 사람이 스쳐 지나가는
        # 2~3초를 놓치지 않으려면 그 정도까지만 줄인다.
        self.declare_parameter('lidar_acquisition_interval_s', 3.0)
        self.declare_parameter('lidar_acquisition_max_distance_m', 6.0)
        self.declare_parameter('lidar_acquisition_spin_allowance_s', 12.0)
        self.declare_parameter('prediction_horizon_s', 0.60)
        # 사람이 유지 거리 안에 들어오면 정책이 ALIGN 을 내고, 그러면 제자리
        # 회전만 하다가 대상이 시야를 벗어난다. 갈 곳을 앞질러 목표로 삼으면
        # 목표가 계속 움직여 회전과 병진이 함께 일어난다. 실제품(Astro)도
        # 멈춰서 돌지 않고 통합 경로로 움직인다.
        self.declare_parameter('target_lead_time_s', 1.00)
        self.declare_parameter('target_lead_minimum_speed_mps', 0.15)
        # 앞지른 목표가 사람의 방위에서 이 각도 이상 벗어나면
        # Nav2 가 경로 쪽을 향해 카메라가 사람을 벗어난다.
        self.declare_parameter('target_lead_max_offset_rad', 0.25)
        # 놓친 뒤 이 시간까지는 마지막 속도로 외삽한 지점을 쫓는다.
        # 그보다 오래되면 추정이 근거를 잃어 마지막 관측으로 돌아간다.
        self.declare_parameter('predicted_pursuit_timeout_s', 3.00)
        self.declare_parameter('recovery_spin_allowance_s', 12.0)
        self.declare_parameter('transform_timeout_s', 0.10)
        self.declare_parameter('sensor_transform_queue_timeout_s', 0.30)
        # TF 가 스캔보다 뒤처지면 정확 시각 변환이 존재하지 않아 모든 스캔이
        # 버려진다. 이 값보다 지연이 작으면 최신 변환으로 대체한다. 자기 운동
        # 보정을 포기하는 절충이므로 기본은 0(사용 안 함)이다.
        self.declare_parameter('scan_transform_max_tf_lag_s', 0.0)

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
        if not str(self.get_parameter('scan_topic').value).startswith('/'):
            raise ValueError('scan_topic must be absolute')
        if not str(self.get_parameter('odometry_frame').value):
            raise ValueError('odometry_frame must not be empty')
        if float(self.get_parameter('minimum_confidence').value) < 0.0:
            raise ValueError('minimum_confidence must be non-negative')
        if float(self.get_parameter('cluster_gap_m').value) <= 0.0:
            raise ValueError('cluster_gap_m must be positive')
        minimum_cluster_points = int(
            self.get_parameter('minimum_cluster_points').value
        )
        maximum_cluster_points = int(
            self.get_parameter('maximum_cluster_points').value
        )
        if minimum_cluster_points <= 0:
            raise ValueError('minimum_cluster_points must be positive')
        if float(
            self.get_parameter(
                'minimum_cluster_density_points_per_m'
            ).value
        ) <= 0.0:
            raise ValueError(
                'minimum_cluster_density_points_per_m must be positive'
            )
        if maximum_cluster_points < minimum_cluster_points:
            raise ValueError(
                'maximum_cluster_points must cover minimum_cluster_points'
            )
        if float(
            self.get_parameter('maximum_cluster_extent_m').value
        ) <= 0.0:
            raise ValueError('maximum_cluster_extent_m must be positive')
        static_threshold = int(
            self.get_parameter('static_occupied_threshold').value
        )
        if not 0 <= static_threshold <= 100:
            raise ValueError('static_occupied_threshold must be in [0, 100]')
        if float(
            self.get_parameter('static_exclusion_radius_m').value
        ) < 0.0:
            raise ValueError('static_exclusion_radius_m must be non-negative')
        for parameter_name in (
            'navigation_retry_delay_s',
            'camera_position_alpha',
            'camera_velocity_alpha',
            'maximum_person_speed_mps',
            'lidar_continuation_timeout_s',
            'lidar_continuation_max_distance_m',
            'lidar_candidate_gate_m',
            'camera_lidar_fusion_freshness_s',
            'camera_jump_base_gate_m',
            'camera_jump_pending_consistency_m',
            'lidar_proximity_control_distance_m',
            'lidar_proximity_camera_guard_s',
            'lidar_reassociation_max_distance_m',
            'dynamic_rebind_minimum_speed_mps',
            'camera_horizontal_fov_rad',
            'coarse_goal_update_distance_m',
            'coarse_goal_update_period_s',
            'bearing_goal_update_distance_m',
            'bearing_goal_update_period_s',
            'bearing_only_variance_threshold_m2',
            'precise_maximum_travel_m',
            'coarse_maximum_travel_m',
            'bearing_maximum_travel_m',
            'retreat_maximum_travel_m',
            'approach_prediction_horizon_s',
            'approach_speed_threshold_mps',
            'retreat_goal_update_distance_m',
            'retreat_goal_update_period_s',
            'goal_safe_search_radius_m',
            'goal_openness_radius_m',
            'heading_probe_distance_m',
            'minimum_heading_clearance_m',
            'alignment_angle_tolerance_rad',
            'sensor_transform_queue_timeout_s',
            'recovery_direction_minimum_turn_rad',
            'recovery_waypoint_tolerance_m',
            'recovery_scan_angle_rad',
            'recovery_spin_allowance_s',
        ):
            if float(self.get_parameter(parameter_name).value) <= 0.0:
                raise ValueError(f'{parameter_name} must be positive')
        if float(
            self.get_parameter('camera_lidar_extent_padding_m').value
        ) < 0.0:
            raise ValueError(
                'camera_lidar_extent_padding_m must be non-negative'
            )
        if int(self.get_parameter('camera_jump_confirmation_hits').value) < 2:
            raise ValueError(
                'camera_jump_confirmation_hits must be at least 2'
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
        self._obstacle_tracker.clear_selection()
        self._camera_estimator.reset()
        self._camera_observation_gate.reset()
        self._last_seen_s = None
        self._last_camera_seen_s = None
        self._last_camera_frame_s = None
        self._camera_miss_count = 0
        self._last_precise_camera_position = None
        self._last_lidar_stamp_s = None
        self._lidar_proximity_guard_until_s = 0.0
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
        self._last_motion_velocity = None
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
        now_s = self._now_seconds()
        self._last_camera_frame_s = now_s
        observation = self._select_target_observation(message)
        if observation is None:
            self._record_camera_miss()
            return
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
            observed_yaw = math.atan2(
                camera_position.y - robot_position.y,
                camera_position.x - robot_position.x,
            )
            measured = self._lidar_range_on_bearing(
                robot_position, observed_yaw, now_s
            )
            if measured is not None:
                self._bearing_range_from_lidar += 1
                camera_position = measured
            else:
                # 라이다에 방위선 위 군집이 없을 때만 지도로 추측한다.
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
                        'No free global-costmap point exists on the camera '
                        'ray at or beyond the depth range',
                    )
                    return
                camera_position = projected
        if not self._camera_observation_is_acceptable(
            camera_position,
            now_s,
        ):
            self._record_camera_miss(reset_jump_candidate=False)
            self._warn_periodically(
                'camera_jump_pending',
                'Ignoring one discontinuous camera observation until it '
                'repeats or receives LiDAR support',
            )
            return
        camera_estimate = self._camera_estimator.update(
            camera_position,
            now_s,
        )
        self._last_camera_seen_s = now_s
        self._camera_miss_count = 0
        self._last_precise_camera_position = (
            None if bearing_only else camera_position
        )
        self._last_seen_s = now_s
        self._detector_track_id = detection.id
        self._last_target_height = detected_pose.pose.position.z
        if not self._observed_track_id:
            self._observed_track_id = 'person-1'
            self.get_logger().info(
                'Acquired person-1 from sensor-backed RGB-D position'
            )
        observed_yaw = math.atan2(
            camera_position.y - robot_position.y,
            camera_position.x - robot_position.x,
        )
        observed_bearing = normalize_angle(observed_yaw - robot_yaw)
        if abs(observed_bearing) > 1e-3:
            self._last_observed_bearing_rad = observed_bearing
        self._reset_recovery()

        label = (
            self._obstacle_tracker.target.label
            if self._obstacle_tracker.target is not None
            else self._observed_track_id
        )
        labeled = None
        if (
            not bearing_only
            and self._static_distance_field is not None
        ):
            previous_track_id = (
                self._obstacle_tracker.target.track.track_id
                if self._obstacle_tracker.target is not None
                else None
            )
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
            source='bearing' if bearing_only else 'camera',
            precise=False,
            bearing_only=bearing_only,
            target_velocity=(
                None if bearing_only else camera_estimate.velocity
            ),
        )

    def _lidar_range_on_bearing(
        self,
        robot_position: Point2D,
        observed_yaw: float,
        now_s: float,
    ) -> Point2D | None:
        """
        Take the range from LiDAR when the camera can only give a bearing.

        The two sensors fail in opposite places. A person close enough to
        fill the frame has no usable depth, which is exactly the range LiDAR
        measures best. Guessing the range from the map instead puts the gate
        centre past the person, so the cluster that does measure them is
        discarded. Keep the camera's bearing and take the range from the
        nearest foreground cluster along it.
        """
        if not self._latest_clusters:
            return None
        if now_s - self._latest_clusters_s > float(
            self.get_parameter('bearing_lidar_max_age_s').value
        ):
            return None
        gate_rad = float(
            self.get_parameter('bearing_lidar_gate_rad').value
        )
        # 지도를 만든 뒤 들어온 가구도 전경으로 잡힌다. 로봇과 사람 사이에
        # 의자가 있으면 그쪽이 더 가까워 사람 대신 집힌다. 그래서 자리를
        # 지키지 않는 군집을 먼저 본다.
        moving = self._background.filter_moving(
            self._latest_clusters, self._latest_clusters_s
        )
        for candidates in (moving, self._latest_clusters):
            nearest = None
            nearest_range = math.inf
            for cluster in candidates:
                offset_x = cluster.position.x - robot_position.x
                offset_y = cluster.position.y - robot_position.y
                range_m = math.hypot(offset_x, offset_y)
                if range_m <= 1e-6:
                    continue
                bearing = math.atan2(offset_y, offset_x)
                if abs(normalize_angle(bearing - observed_yaw)) > gate_rad:
                    continue
                if range_m < nearest_range:
                    nearest_range = range_m
                    nearest = cluster.position
            if nearest is not None:
                return nearest
        return None

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

    def _on_scan(self, message: LaserScan) -> None:
        """Queue only the newest scan until its exact TF becomes available."""
        if self._static_distance_field is None:
            # 정적 지도가 없으면 전경을 뺄 기준이 없어 스캔을 통째로 버린다.
            # 예전에는 말없이 버려서 라이다 융합이 죽은 줄도 몰랐다.
            self._warn_periodically(
                'static_map_missing',
                'Dropping LiDAR scans: the static map has not arrived, so '
                'LiDAR person tracking is disabled',
            )
            return
        if not message.header.frame_id:
            return
        if self._pending_scan is None:
            self._pending_scan_queued_s = self._now_seconds()
        self._pending_scan = message
        self._process_pending_scan()

    def _process_pending_scan(self) -> None:
        """Process a queued scan without blocking camera or action callbacks."""
        message = self._pending_scan
        if message is None:
            return
        try:
            transform = self._scan_transform(message)
        except TransformException as error:
            # 스캔 스탬프가 TF 최신값보다 앞서면 스탬프로 잰 경과 시간이
            # 음수라 아래 조건이 영원히 성립하지 않는다. 그러면 모든 스캔이
            # 경고 하나 없이 버려지고 LiDAR 추적이 통째로 멈춘 채 지나간다.
            # 그래서 대기 시간은 스탬프가 아니라 큐에 들어온 시점으로 잰다.
            waited_s = self._now_seconds() - self._pending_scan_queued_s
            if waited_s > float(
                self.get_parameter(
                    'sensor_transform_queue_timeout_s'
                ).value
            ):
                fallback = self._degraded_scan_transform(message)
                if fallback is not None:
                    self._pending_scan = None
                    self._warn_periodically(
                        'lidar_transform_degraded',
                        'Using the newest TF for a scan whose exact-time TF '
                        'never arrived; ego-motion compensation is off',
                    )
                    self._process_scan(message, fallback)
                    return
                self._pending_scan = None
                self._scan_transform_drops += 1
                self._warn_periodically(
                    'lidar_transform_timeout',
                    'Dropping LiDAR scan after waiting '
                    f'{waited_s:.2f}s for its exact-time TF '
                    f'({self._scan_transform_drops} dropped so far): {error}',
                )
            return
        self._pending_scan = None
        self._process_scan(message, transform)

    def _process_scan(
        self,
        message: LaserScan,
        transform: ScanTransform2D,
    ) -> None:
        """Extract and track camera-gated, map-subtracted LiDAR clusters."""
        static_field = self._static_distance_field
        if static_field is None:
            return
        self._scan_processed_count += 1
        try:
            clusters = extract_foreground_clusters(
                message.ranges,
                float(message.angle_min),
                float(message.angle_increment),
                float(message.range_min),
                float(message.range_max),
                transform,
                static_field,
                float(
                    self.get_parameter('static_exclusion_radius_m').value
                ),
                float(self.get_parameter('cluster_gap_m').value),
                int(self.get_parameter('minimum_cluster_points').value),
                float(
                    self.get_parameter(
                        'minimum_cluster_density_points_per_m'
                    ).value
                ),
                int(self.get_parameter('maximum_cluster_points').value),
                float(
                    self.get_parameter('maximum_cluster_extent_m').value
                ),
            )
        except ValueError as error:
            self._warn_periodically(
                'lidar_foreground', f'Ignoring LiDAR scan: {error}'
            )
            return
        stamp_s = _stamp_seconds(message.header.stamp)
        if stamp_s <= 0.0:
            stamp_s = self._now_seconds()
        fresh_camera_position = self._fresh_precise_camera_position()
        gate_center = (
            fresh_camera_position
            if fresh_camera_position is not None
            else self._lidar_candidate_center(stamp_s)
        )
        # 확인 여부와 무관하게 자리를 학습한다. 지도를 만든 뒤 들어온
        # 가구가 계속 후보로 떠오르는 것을 막기 위해서다.
        self._background.observe(clusters, stamp_s)
        # 깊이가 없을 때 방위선 위의 거리를 여기서 받아 간다. 게이트로
        # 걸러지기 전 원본이어야 한다.
        self._latest_clusters = list(clusters)
        self._latest_clusters_s = stamp_s
        if gate_center is None:
            # 카메라가 아직 사람을 확인하지 못한 구간이다. 트래커에는 넣지
            # 않되, 어디를 돌아볼지 정하는 단서로만 남긴다. 확인은 여전히
            # 카메라가 한다.
            self._raw_foreground_count = len(clusters)
            self._acquisition_candidates = self._background.filter_moving(
                clusters, stamp_s
            )
            clusters = []
        elif fresh_camera_position is not None:
            self._raw_foreground_count = len(clusters)
            self._acquisition_candidates = []
            # A current RGB-D detection is positive evidence for its own
            # region and negative evidence for neighboring LiDAR geometry.
            # Keep a small bounded extent allowance for torso/leg centroid
            # differences, rather than retaining every obstacle in the wider
            # prediction gate.
            clusters = camera_consistent_clusters(
                clusters,
                fresh_camera_position,
                float(self.get_parameter('camera_label_gate_m').value),
                float(
                    self.get_parameter(
                        'camera_lidar_extent_padding_m'
                    ).value
                ),
            )
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
        reassociated = self._rebind_unique_moving_lidar_target(stamp_s)
        if reassociated is not None:
            labeled = reassociated
        elif labeled is None:
            labeled = self._bind_predicted_lidar_target()
        self._publish_track_markers()
        if (
            self._active_goal is not None
            and labeled is not None
            and labeled.track.confirmed
        ):
            if not self._accept_lidar_proximity(labeled):
                self._accept_lidar_continuation(
                    labeled,
                    allow_camera_negative_rebind=reassociated is not None,
                )

    def _degraded_scan_transform(
        self, message: LaserScan
    ) -> ScanTransform2D | None:
        """
        Fall back to the newest TF when the exact-time one never arrives.

        Simulation and loaded hardware can publish odometry TF seconds
        behind the scans, and an exact-time lookup can then never succeed.
        Dropping every scan silently disables LiDAR tracking, so allow a
        bounded substitution instead. It is off by default because it
        trades away the ego-motion compensation the exact lookup provides.
        """
        allowance_s = float(
            self.get_parameter('scan_transform_max_tf_lag_s').value
        )
        if allowance_s <= 0.0:
            return None
        try:
            latest = self._tf_buffer.lookup_transform(
                self._global_frame, message.header.frame_id, Time()
            )
        except TransformException:
            return None
        lag_s = self._now_seconds() - _stamp_seconds(latest.header.stamp)
        if lag_s > allowance_s:
            return None
        pose = latest.transform
        return ScanTransform2D(
            translation=Point2D(pose.translation.x, pose.translation.y),
            yaw=quaternion_to_yaw(
                pose.rotation.x,
                pose.rotation.y,
                pose.rotation.z,
                pose.rotation.w,
            ),
        )

    def _scan_transform(self, message: LaserScan) -> ScanTransform2D:
        """Resolve exact start/end transforms for ego-motion compensation."""
        start_time = Time.from_msg(message.header.stamp)
        duration_s = max(
            0.0,
            max(0, len(message.ranges) - 1) * float(message.time_increment),
        )
        start = self._tf_buffer.lookup_transform_full(
            self._global_frame,
            Time(),
            message.header.frame_id,
            start_time,
            self._odometry_frame,
            timeout=Duration(),
        )
        end = start
        if duration_s > 0.0:
            end = self._tf_buffer.lookup_transform_full(
                self._global_frame,
                Time(),
                message.header.frame_id,
                start_time + Duration(seconds=duration_s),
                self._odometry_frame,
                timeout=Duration(),
            )
        translation = start.transform.translation
        rotation = start.transform.rotation
        end_translation = end.transform.translation
        end_rotation = end.transform.rotation
        return ScanTransform2D(
            Point2D(float(translation.x), float(translation.y)),
            quaternion_to_yaw(
                rotation.x,
                rotation.y,
                rotation.z,
                rotation.w,
            ),
            Point2D(
                float(end_translation.x),
                float(end_translation.y),
            ),
            quaternion_to_yaw(
                end_rotation.x,
                end_rotation.y,
                end_rotation.z,
                end_rotation.w,
            ),
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

    def _record_camera_miss(
        self,
        *,
        reset_jump_candidate: bool = True,
    ) -> None:
        """Record one camera frame without an accepted target observation."""
        self._camera_miss_count += 1
        self._last_precise_camera_position = None
        if reset_jump_candidate:
            self._camera_observation_gate.reset()

    def _camera_observation_is_acceptable(
        self,
        camera_position: Point2D,
        now_s: float,
    ) -> bool:
        """Reject an unsupported one-frame jump without blocking reacquisition."""
        prediction = self._camera_estimator.predict(
            now_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )
        elapsed_s = (
            0.0
            if self._last_camera_seen_s is None
            else max(0.0, now_s - self._last_camera_seen_s)
        )
        elapsed_s = min(
            elapsed_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )
        continuity_radius_m = float(
            self.get_parameter('camera_jump_base_gate_m').value
        ) + float(
            self.get_parameter('maximum_person_speed_mps').value
        ) * elapsed_s
        return self._camera_observation_gate.accept(
            camera_position,
            prediction,
            continuity_radius_m,
            self._camera_position_has_lidar_support(camera_position),
        )

    def _camera_position_has_lidar_support(
        self,
        camera_position: Point2D,
    ) -> bool:
        """Return whether a current confirmed LiDAR track supports RGB-D."""
        base_gate_m = float(
            self.get_parameter('camera_label_gate_m').value
        )
        maximum_padding_m = float(
            self.get_parameter('camera_lidar_extent_padding_m').value
        )
        return any(
            track.confirmed
            and track.misses == 0
            and distance(track.position, camera_position)
            <= base_gate_m
            + min(maximum_padding_m, 0.5 * track.extent_m)
            for track in self._obstacle_tracker.tracks
        )

    def _lidar_candidate_center(self, stamp_s: float) -> Point2D | None:
        """Return the only region where LiDAR may support the camera target."""
        if self._active_goal is None or self._last_camera_seen_s is None:
            return None
        now_s = self._now_seconds()
        if now_s - self._last_camera_seen_s > float(
            self.get_parameter('lidar_continuation_timeout_s').value
        ):
            return None
        settings = self._settings
        if (
            settings is not None
            and now_s - self._last_camera_seen_s
            > settings.temporary_lost_timeout_s
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
        """Use a gated LiDAR candidate only while recovering a camera target."""
        if self._active_goal is None or self._last_camera_seen_s is None:
            return None
        now_s = self._now_seconds()
        camera_age_s = now_s - self._last_camera_seen_s
        settings = self._settings
        if settings is None or not (
            settings.temporary_lost_timeout_s
            < camera_age_s
            <= float(
                self.get_parameter('lidar_continuation_timeout_s').value
            )
        ):
            return None
        prediction = self._camera_estimator.predict(
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
        *,
        allow_camera_negative_rebind: bool = False,
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
            self._last_lidar_stamp_s is not None
            and labeled.stamp_seconds <= self._last_lidar_stamp_s
        ):
            return False
        camera_age_s = now_s - self._last_camera_seen_s
        settings = self._settings
        if (
            settings is not None
            and camera_age_s <= settings.temporary_lost_timeout_s
            and not allow_camera_negative_rebind
        ):
            # RGB-D owns the map target while it is current. Do not let an
            # asynchronous LiDAR update replace that authoritative point.
            # The labeled dynamic track owns only brief camera-loss recovery.
            return False
        camera_prediction = self._camera_estimator.predict(
            now_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )
        if camera_prediction is None or distance(
            labeled.track.position,
            camera_prediction,
        ) > float(
            self.get_parameter('lidar_continuation_max_distance_m').value
        ):
            # The distance gate belongs only to LiDAR continuation. A fresh
            # camera detection is never rejected by this stale-position test.
            return False
        try:
            robot_position, _ = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'lidar_target_tf',
                f'LiDAR target TF unavailable: {error}',
            )
            return False
        self._last_lidar_stamp_s = labeled.stamp_seconds
        self._last_seen_s = now_s
        self._accept_map_target(
            labeled.track.position,
            robot_position,
            now_s,
            source='lidar',
            precise=True,
            target_velocity=labeled.track.velocity,
        )
        return True

    def _accept_lidar_proximity(
        self,
        labeled: LabeledObstacle,
    ) -> bool:
        """Apply fast LiDAR ALIGN/RETREAT only to the camera-labeled person."""
        settings = self._settings
        if settings is None or self._last_camera_seen_s is None:
            return False
        now_s = self._now_seconds()
        camera_age_s = now_s - self._last_camera_seen_s
        if camera_age_s > float(
            self.get_parameter('lidar_continuation_timeout_s').value
        ):
            return False
        if (
            self._last_lidar_stamp_s is not None
            and labeled.stamp_seconds <= self._last_lidar_stamp_s
        ):
            return False
        camera_prediction = self._camera_estimator.predict(
            now_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )
        if camera_prediction is None or distance(
            labeled.track.position,
            camera_prediction,
        ) > float(
            self.get_parameter('lidar_continuation_max_distance_m').value
        ):
            return False
        try:
            robot_position, _ = self._robot_pose()
        except TransformException as error:
            self._warn_periodically(
                'lidar_proximity_tf',
                f'LiDAR proximity TF unavailable: {error}',
            )
            return False
        target_distance = distance(robot_position, labeled.track.position)
        if target_distance > float(
            self.get_parameter('lidar_proximity_control_distance_m').value
        ):
            return False
        decision = decide_follow_motion(
            robot_position,
            labeled.track.position,
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
        self._lidar_proximity_guard_until_s = now_s + float(
            self.get_parameter('lidar_proximity_camera_guard_s').value
        )
        if camera_age_s > settings.temporary_lost_timeout_s:
            self._last_target_pose = self._make_target_pose(
                labeled.track.position,
                self._last_target_height,
            )
            self._target_pose_publisher.publish(self._last_target_pose)
        self._apply_tracking_motion(
            robot_position,
            labeled.track.position,
            now_s,
            precise=True,
            target_velocity=labeled.track.velocity,
        )
        self._publish_track_markers()
        self._publish_feedback()
        return True

    def _request_static_map(self) -> None:
        """Ask the map server directly when the latched map never arrived."""
        if self._static_distance_field is not None:
            self._static_map_retry_timer.cancel()
            return
        if self._static_map_request_pending:
            return
        if not self._static_map_client.service_is_ready():
            return
        self._static_map_request_pending = True
        future = self._static_map_client.call_async(GetMap.Request())
        future.add_done_callback(self._on_static_map_response)

    def _on_static_map_response(self, future) -> None:
        self._static_map_request_pending = False
        try:
            response = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._warn_periodically(
                'static_map_request',
                f'Static map request failed: {error}',
            )
            return
        self.get_logger().info(
            'Static map was requested directly; the latched publication never '
            'reached this node'
        )
        self._on_static_map(response.map)

    def _on_static_map(self, message: OccupancyGrid) -> None:
        try:
            static_map = self._static_map_grid(message)
            static_field = StaticDistanceField.build(
                static_map,
                int(self.get_parameter('static_occupied_threshold').value),
            )
        except ValueError as error:
            self._warn_periodically(
                'invalid_static_map',
                f'Ignoring invalid static map: {error}',
            )
            return
        self._latest_static_map = static_map
        self._static_distance_field = static_field

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

    def _accept_map_target(
        self,
        target_position: Point2D,
        robot_position: Point2D,
        now_s: float,
        *,
        source: str,
        precise: bool,
        bearing_only: bool = False,
        target_velocity: Point2D | None = None,
    ) -> None:
        """Publish and follow the single resolved person point in `map`."""
        self._last_target_pose = self._make_target_pose(
            target_position,
            self._last_target_height,
        )
        self._target_pose_publisher.publish(self._last_target_pose)
        recovering = self._state in {
            FollowState.REACHING_WAYPOINT,
            FollowState.TURNING_TO_TARGET,
            FollowState.REACHING_LAST_POSITION,
            FollowState.SEARCHING,
            FollowState.TARGET_LOST,
        }
        self._reset_recovery()
        if recovering and self._nav2.mode == MotionMode.SPIN:
            self._nav2.cancel()
        self._set_state(FollowState.TRACKING)
        self._tracking_source = source
        self._apply_tracking_motion(
            robot_position,
            target_position,
            now_s,
            precise=precise,
            bearing_only=bearing_only,
            target_velocity=target_velocity,
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
        selected = select_target_candidate(
            candidates,
            predicted,
            preferred_track_id=self._detector_track_id,
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
        precise: bool,
        bearing_only: bool = False,
        recovery: bool = False,
        target_velocity: Point2D | None = None,
    ) -> None:
        settings = self._settings
        if settings is None:
            return
        self._last_motion_target = target_position
        self._last_motion_velocity = target_velocity
        self._last_motion_precise = precise
        self._last_motion_bearing_only = bearing_only
        # 거리 판정은 실제 위치로 한다. 안전 거리는 지금 어디 있느냐의
        # 문제이지 어디로 갈 것이냐의 문제가 아니다. 접근 목표만 앞지른다.
        planned_target = self._led_target(target_position, target_velocity)
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
            self._last_plan_target = None
            self._publish_track_markers()
            if recovery:
                self._start_recovery_scan()
            return
        if decision.command == FollowCommand.HOLD:
            if self._nav2.mode is not None:
                self._nav2.cancel()
            self._path_planner.cancel()
            self._last_goal_position = None
            self._last_plan_target = None
            self._publish_track_markers()
            if recovery:
                self._start_recovery_scan()
            return
        if decision.command == FollowCommand.ALIGN:
            lead_decision = (
                decide_follow_motion(
                    robot_position,
                    planned_target,
                    settings,
                    maximum_travel_m=None,
                )
                if planned_target is not target_position
                else None
            )
            if (
                lead_decision is not None
                and lead_decision.command == FollowCommand.NAVIGATE
            ):
                # 사람이 걷고 있으면 멈춰서 돌지 않는다. 갈 곳으로 주행하면
                # Nav2 가 방향과 이동을 같이 맡아 카메라도 따라 돈다.
                decision = lead_decision
            else:
                self._align_with_target(decision.goal.yaw)
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
                approach_origin=(
                    robot_position if planning_to_target else None
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
        if decision.command == FollowCommand.RETREAT:
            update_distance_m = float(
                self.get_parameter('retreat_goal_update_distance_m').value
            )
            update_period_s = float(
                self.get_parameter('retreat_goal_update_period_s').value
            )
        elif precise:
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

    def _led_target(
        self,
        target_position: Point2D,
        target_velocity: Point2D | None,
    ) -> Point2D:
        """
        Aim where the person is going, not where they were.

        Standing still to rotate loses a walking person: the camera wedge is
        narrow and the aim takes longer than the person stays inside it. A
        goal that leads the target keeps translating, so Nav2 turns and moves
        at once instead of handing the two to separate controllers.
        """
        if target_velocity is None:
            return target_position
        lead_s = float(self.get_parameter('target_lead_time_s').value)
        if lead_s <= 0.0:
            return target_position
        speed = math.hypot(target_velocity.x, target_velocity.y)
        if speed < float(
            self.get_parameter('target_lead_minimum_speed_mps').value
        ):
            return target_position
        # 속도 추정이 튀어도 사람 걸음 이상으로는 앞지르지 않는다.
        lead_m = min(
            speed * lead_s,
            float(self.get_parameter('maximum_person_speed_mps').value)
            * lead_s,
        )
        return self._bounded_lead(target_position, target_velocity, speed,
                                  lead_m)

    def _bounded_lead(
        self,
        target_position: Point2D,
        target_velocity: Point2D,
        speed: float,
        lead_m: float,
    ) -> Point2D:
        """
        Keep the led goal inside the camera's view of the actual person.

        Nav2 aims along the path, so a goal far off the person's bearing
        turns the camera away from them. Leading in time alone does that:
        the same lead is a wide angle up close and a narrow one far away.
        Bound the offset by angle instead, well inside the camera half-wedge.
        """
        try:
            robot_position, _ = self._robot_pose()
        except TransformException:
            return target_position
        unit_x = target_velocity.x / speed
        unit_y = target_velocity.y / speed
        range_m = math.hypot(
            target_position.x - robot_position.x,
            target_position.y - robot_position.y,
        )
        if range_m > 1e-6:
            maximum_offset_rad = float(
                self.get_parameter('target_lead_max_offset_rad').value
            )
            lead_m = min(lead_m, range_m * math.tan(maximum_offset_rad))
        return Point2D(
            target_position.x + unit_x * lead_m,
            target_position.y + unit_y * lead_m,
        )

    def _align_with_target(self, target_yaw: float) -> None:
        """Use Nav2 Spin only after translational standoff is satisfied."""
        self._path_planner.cancel()
        if self._nav2.mode == MotionMode.NAVIGATE:
            self._nav2.cancel()
        self._last_goal_position = None
        self._last_plan_target = None
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
        full_scan_angle = float(
            self.get_parameter('recovery_scan_angle_rad').value
        )
        full_scan_allowance = float(
            self.get_parameter('recovery_spin_allowance_s').value
        )
        allowance = max(
            3.0,
            full_scan_allowance * abs(turn_angle) / full_scan_angle + 1.0,
        )
        if not self._nav2.spin(turn_angle, allowance):
            self._warn_periodically(
                'alignment_spin_unavailable',
                'Nav2 Spin action is not ready for target alignment',
            )

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
            # Waiting stationary means a person crossing behind the robot is
            # never acquired: LiDAR spans every bearing while the camera sees
            # only its own wedge. Turn toward one unconfirmed LiDAR candidate
            # so the detector gets a look; the camera still decides.
            self._try_lidar_acquisition_turn(now_s)
            self._publish_feedback()
            return
        if self._state == FollowState.TARGET_LOST:
            # 놓친 뒤에도 사각은 그대로다. 카메라 감지만 서서 기다리면
            # 로봇 뒤로 지나가는 사람을 다시 잡을 길이 없다. 여기서도
            # 라이다 후보 쪽으로 돌아보되, 확인은 카메라가 한다.
            self._try_lidar_acquisition_turn(now_s)
            self._publish_feedback()
            return
        if (
            self._state in _RECOVERY_STATES
            and self._try_lidar_acquisition_turn(now_s)
        ):
            # 복구 절차는 마지막으로 본 자리로 가서 270도를 훑는, 카메라만
            # 있을 때 설계된 순서다. 그 사이 라이다에는 사람이 보이는데도
            # 엉뚱한 데를 본다. 후보가 있으면 그쪽이 더 나은 근거이므로
            # 남은 절차보다 먼저 돌아본다. 확인은 그대로 카메라가 한다.
            self._path_planner.cancel()
            self._reset_recovery()
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
                # Target loss stops motion, but the long-running follow action
                # stays active until its client explicitly cancels it. A later
                # RGB-D detection can therefore resume TRACKING immediately.
                self._path_planner.cancel()
                if self._nav2.mode is not None:
                    self._nav2.cancel()
                self._last_goal_position = None
                self._last_plan_target = None
                self._set_state(FollowState.TARGET_LOST)
                self.get_logger().info(
                    'Person not reacquired; waiting stationary for a new '
                    'camera detection'
                )
                self._publish_track_markers()
                self._publish_feedback()
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
                    target_velocity=self._last_motion_velocity,
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

    def _predicted_pursuit_target(self, now_s: float) -> Point2D | None:
        """
        Move to where the person went, not to where they were last seen.

        Driving to the last observation and sweeping the camera there only
        finds someone who stopped. A person who kept walking is already
        metres away by the time the sweep runs. Carry the last observed
        velocity forward over the time actually lost, which is the same
        estimate the tracker already trusts for prediction, and let the
        normal goal projection move it to open space.
        """
        if self._last_motion_target is None or self._last_seen_s is None:
            return None
        velocity = self._last_motion_velocity
        if velocity is None:
            return None
        elapsed_s = now_s - self._last_seen_s
        timeout_s = float(
            self.get_parameter('predicted_pursuit_timeout_s').value
        )
        if not 0.0 < elapsed_s <= timeout_s:
            return None
        speed = math.hypot(velocity.x, velocity.y)
        if speed < float(
            self.get_parameter('target_lead_minimum_speed_mps').value
        ):
            return None
        travel_m = min(
            speed * elapsed_s,
            float(self.get_parameter('maximum_person_speed_mps').value)
            * timeout_s,
        )
        return Point2D(
            self._last_motion_target.x + velocity.x / speed * travel_m,
            self._last_motion_target.y + velocity.y / speed * travel_m,
        )

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
        predicted = self._predicted_pursuit_target(now_s)
        if predicted is not None:
            self._tracking_source = 'predicted_pursuit'
            self.get_logger().info(
                'Person left the camera; moving to where they should be '
                f'({predicted.x:.2f}, {predicted.y:.2f}) instead of the '
                'last observation'
            )
        else:
            self._tracking_source = 'last_seen_recovery'
        self._apply_tracking_motion(
            robot_position,
            predicted if predicted is not None else self._last_motion_target,
            now_s,
            precise=self._last_motion_precise,
            bearing_only=self._last_motion_bearing_only,
            recovery=True,
            target_velocity=self._last_motion_velocity,
        )

    def _try_lidar_acquisition_turn(self, now_s: float) -> bool:
        """
        Point the camera at one LiDAR candidate the detector cannot see.

        Nothing here claims the candidate is a person. It only chooses where
        to look, because a target outside the camera wedge can never be
        confirmed while the robot waits facing elsewhere.
        """
        if not bool(
            self.get_parameter('lidar_acquisition_enabled').value
        ):
            return False
        if not self._acquisition_candidates:
            return False
        interval_s = float(
            self.get_parameter('lidar_acquisition_interval_s').value
        )
        if (
            self._last_acquisition_turn_s is not None
            and now_s - self._last_acquisition_turn_s < interval_s
        ):
            return False
        try:
            robot_position, robot_yaw = self._robot_pose()
        except TransformException:
            return False
        turn = select_acquisition_turn(
            self._acquisition_candidates,
            robot_position,
            robot_yaw,
            camera_half_fov_rad=0.5 * float(
                self.get_parameter('camera_horizontal_fov_rad').value
            ),
            maximum_distance_m=float(
                self.get_parameter('lidar_acquisition_max_distance_m').value
            ),
        )
        if turn is None:
            return False
        if not self._nav2.spin(
            turn,
            float(
                self.get_parameter(
                    'lidar_acquisition_spin_allowance_s'
                ).value
            ),
        ):
            self._warn_periodically(
                'acquisition_spin_unavailable',
                'Nav2 Spin action is not ready; cannot look at the LiDAR '
                'candidate',
            )
            return False
        self._last_acquisition_turn_s = now_s
        self.get_logger().info(
            'Turning %.0f deg toward an unconfirmed LiDAR candidate so the '
            'camera can check it' % math.degrees(turn)
        )
        return True

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
        if (
            self._last_camera_seen_s is not None
            and self._settings is not None
            and now_s - self._last_camera_seen_s
            <= self._settings.temporary_lost_timeout_s
        ):
            # A current camera observation owns the visible green target.
            # Prediction is only for the short period after camera loss.
            return
        predicted = self._camera_estimator.predict(
            now_s,
            float(self.get_parameter('prediction_horizon_s').value),
        )
        if predicted is None:
            predicted = self._obstacle_tracker.predict_target(
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
        self._publish_track_markers()

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
                'acquisition_candidate_count': len(
                    self._acquisition_candidates
                ),
                'raw_foreground_count': self._raw_foreground_count,
                'scan_processed_count': self._scan_processed_count,
                'bearing_range_from_lidar': self._bearing_range_from_lidar,
                'scan_transform_drops': self._scan_transform_drops,
                'static_distance_field_available': (
                    self._static_distance_field is not None
                ),
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
        self._obstacle_tracker.clear_selection()
        self._camera_estimator.reset()
        self._camera_observation_gate.reset()
        self._last_seen_s = None
        self._last_camera_seen_s = None
        self._last_camera_frame_s = None
        self._camera_miss_count = 0
        self._last_precise_camera_position = None
        self._last_lidar_stamp_s = None
        self._last_goal_position = None
        self._last_plan_target = None
        self._last_motion_target = None
        self._last_motion_velocity = None
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
