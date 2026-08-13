"""ROS node for map-aware autonomous roaming over standard Nav2 actions."""

from enum import Enum
import json
import math
import random

from geometry_msgs.msg import PoseStamped
from lifecycle_msgs.srv import GetState
from nav_msgs.msg import OccupancyGrid
import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from rclpy.time import Time
from std_msgs.msg import String
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformException, TransformListener

from malbut_roaming.geometry import Point2D, distance, standoff_point
from malbut_roaming.grid_map import Candidate, GridMap
from malbut_roaming.navigation_client import (
    NavigationClient,
    NavigationOutcome,
    NavigationRequest,
    make_pose,
)
from malbut_roaming.policy import PolicyConfig, RoamingPolicy
from malbut_roaming.target_interest import TargetInterest


class RoamingState(Enum):
    """Observable high-level mode of the roaming manager."""

    IDLE = 'idle'
    WAITING = 'waiting'
    SELECTING = 'selecting'
    PLANNING = 'planning'
    NAVIGATING = 'navigating'
    DWELLING = 'dwelling'
    FOLLOWING = 'following'
    EXTERNAL = 'external_navigation'
    PAUSED = 'paused'


class RoamingManager(Node):
    """Select varied map goals while Nav2 owns all robot motion."""

    def __init__(self) -> None:
        """Load parameters, connect Nav2, and expose the mode interface."""
        super().__init__('roaming_manager')
        self._declare_parameters()
        self._validate_parameters()

        self._map_frame = self._string_parameter('map_frame')
        self._base_frame = self._string_parameter('base_frame')
        seed_value = self._integer_parameter('random_seed')
        seed = None if seed_value < 0 else seed_value
        self._random = random.Random(seed)
        self._policy = RoamingPolicy(self._policy_config(), seed)
        self._target = TargetInterest(
            timeout_seconds=self._float_parameter('target_timeout_seconds'),
            minimum_speed=self._float_parameter('target_minimum_speed'),
        )

        self._state = RoamingState.IDLE
        self._enabled = False
        self._detail = 'roaming manager ready'
        self._grid = None
        self._candidates: tuple[Candidate, ...] = ()
        self._current_pose = None
        self._current_candidate = None
        self._selection_mode = None
        self._deadline_seconds = None
        self._last_map_refresh_seconds = float('-inf')
        self._follow_started_seconds = None
        self._follow_cooldown_until = 0.0
        self._last_target_replan_seconds = float('-inf')
        self._last_target_goal = None
        self._pending_external_pose = None
        self._nav2_active = False
        self._nav2_state_future = None

        transient_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._status_publisher = self.create_publisher(
            String,
            'roaming/status',
            transient_qos,
        )
        self._goal_publisher = self.create_publisher(
            PoseStamped,
            'roaming/selected_goal',
            transient_qos,
        )
        self.create_subscription(
            OccupancyGrid,
            self._string_parameter('map_topic'),
            self._map_callback,
            transient_qos,
        )
        self.create_subscription(
            PoseStamped,
            self._string_parameter('interest_target_topic'),
            self._target_callback,
            10,
        )
        self.create_subscription(
            PoseStamped,
            self._string_parameter('external_goal_topic'),
            self._external_goal_callback,
            10,
        )
        self.create_service(Trigger, 'roaming/start', self._start_callback)
        self.create_service(Trigger, 'roaming/pause', self._pause_callback)
        self.create_service(Trigger, 'roaming/resume', self._resume_callback)
        self.create_service(Trigger, 'roaming/stop', self._stop_callback)

        self._tf_buffer = Buffer(cache_time=Duration(seconds=10.0))
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._navigator = NavigationClient(
            self,
            self._string_parameter('planner_action_name'),
            self._string_parameter('navigation_action_name'),
            self._navigation_started,
            self._navigation_finished,
        )
        lifecycle_service = (
            self._string_parameter('navigator_lifecycle_node')
            + '/get_state'
        )
        self._nav2_state_client = self.create_client(
            GetState,
            lifecycle_service,
        )
        self._timer = self.create_timer(
            self._float_parameter('control_period_seconds'),
            self._tick,
        )
        if self._boolean_parameter('autostart'):
            self._start()
        self._publish_status()

    def destroy_node(self):
        """Release Nav2 action clients before destroying the node."""
        self._navigator.cancel()
        self._navigator.destroy()
        return super().destroy_node()

    def _declare_parameters(self) -> None:
        defaults = {
            'autostart': False,
            'map_frame': 'map',
            'base_frame': 'base_footprint',
            'map_topic': 'map',
            'interest_target_topic': 'roaming/interest_target',
            'external_goal_topic': 'roaming/goal',
            'planner_action_name': 'compute_path_to_pose',
            'navigation_action_name': 'navigate_to_pose',
            'navigator_lifecycle_node': 'bt_navigator',
            'control_period_seconds': 0.2,
            'map_refresh_period_seconds': 2.0,
            'occupied_threshold': 65,
            'maximum_free_occupancy': 20,
            'candidate_spacing': 0.5,
            'minimum_clearance': 0.45,
            'open_clearance': 0.9,
            'peripheral_clearance': 0.7,
            'peripheral_probability': 0.22,
            'minimum_goal_distance': 1.3,
            'maximum_goal_distance': 7.0,
            'preferred_goal_distance': 4.0,
            'distance_scale': 1.5,
            'revisit_horizon_seconds': 180.0,
            'recent_goal_radius': 2.0,
            'recent_memory_size': 8,
            'failure_cooldown_seconds': 60.0,
            'idleness_weight': 3.0,
            'distance_weight': 1.5,
            'clearance_weight': 1.2,
            'novelty_weight': 2.0,
            'selection_top_k': 12,
            'selection_temperature': 0.35,
            'dwell_min_seconds': 1.0,
            'dwell_max_seconds': 4.0,
            'failure_retry_delay_seconds': 1.0,
            'random_seed': -1,
            'target_timeout_seconds': 1.5,
            'target_minimum_speed': 0.08,
            'target_standoff_distance': 1.2,
            'target_follow_max_seconds': 15.0,
            'target_follow_cooldown_seconds': 20.0,
            'target_replan_period_seconds': 1.0,
            'target_replan_distance': 0.4,
            'target_goal_snap_distance': 1.0,
            'resume_after_external_goal': True,
        }
        for name, default in defaults.items():
            self.declare_parameter(name, default)

    def _validate_parameters(self) -> None:
        positive = (
            'control_period_seconds',
            'map_refresh_period_seconds',
            'candidate_spacing',
            'open_clearance',
            'peripheral_clearance',
            'dwell_max_seconds',
            'failure_retry_delay_seconds',
            'target_timeout_seconds',
            'target_standoff_distance',
            'target_follow_max_seconds',
            'target_replan_period_seconds',
            'target_replan_distance',
            'target_goal_snap_distance',
        )
        for name in positive:
            if self._float_parameter(name) <= 0.0:
                raise ValueError(f'{name} must be positive')
        if self._float_parameter('minimum_clearance') < 0.0:
            raise ValueError('minimum_clearance must be non-negative')
        if (
            self._float_parameter('peripheral_clearance')
            < self._float_parameter('minimum_clearance')
        ):
            raise ValueError(
                'peripheral_clearance must cover minimum_clearance'
            )
        if (
            self._float_parameter('dwell_min_seconds') < 0.0
            or self._float_parameter('dwell_min_seconds')
            > self._float_parameter('dwell_max_seconds')
        ):
            raise ValueError('dwell duration bounds are invalid')

    def _policy_config(self) -> PolicyConfig:
        return PolicyConfig(
            minimum_goal_distance=self._float_parameter(
                'minimum_goal_distance'
            ),
            maximum_goal_distance=self._float_parameter(
                'maximum_goal_distance'
            ),
            preferred_goal_distance=self._float_parameter(
                'preferred_goal_distance'
            ),
            distance_scale=self._float_parameter('distance_scale'),
            open_clearance=self._float_parameter('open_clearance'),
            peripheral_clearance=self._float_parameter(
                'peripheral_clearance'
            ),
            peripheral_probability=self._float_parameter(
                'peripheral_probability'
            ),
            revisit_horizon_seconds=self._float_parameter(
                'revisit_horizon_seconds'
            ),
            recent_goal_radius=self._float_parameter('recent_goal_radius'),
            recent_memory_size=self._integer_parameter(
                'recent_memory_size'
            ),
            failure_cooldown_seconds=self._float_parameter(
                'failure_cooldown_seconds'
            ),
            idleness_weight=self._float_parameter('idleness_weight'),
            distance_weight=self._float_parameter('distance_weight'),
            clearance_weight=self._float_parameter('clearance_weight'),
            novelty_weight=self._float_parameter('novelty_weight'),
            top_k=self._integer_parameter('selection_top_k'),
            temperature=self._float_parameter('selection_temperature'),
        )

    def _map_callback(self, message: OccupancyGrid) -> None:
        now = self._now_seconds()
        if (
            self._grid is not None
            and now - self._last_map_refresh_seconds
            < self._float_parameter('map_refresh_period_seconds')
        ):
            return
        if message.header.frame_id and message.header.frame_id != self._map_frame:
            self.get_logger().error(
                f'ignoring map in frame {message.header.frame_id!r}; '
                f'expected {self._map_frame!r}'
            )
            return
        try:
            grid = GridMap.from_message(
                message,
                occupied_threshold=self._integer_parameter(
                    'occupied_threshold'
                ),
            )
            candidates = grid.candidates(
                spacing_m=self._float_parameter('candidate_spacing'),
                minimum_clearance_m=self._float_parameter(
                    'minimum_clearance'
                ),
                maximum_free_occupancy=self._integer_parameter(
                    'maximum_free_occupancy'
                ),
            )
        except ValueError as error:
            self.get_logger().error(f'invalid occupancy map: {error}')
            return
        self._grid = grid
        self._candidates = candidates
        self._last_map_refresh_seconds = now
        self._detail = f'loaded {len(candidates)} safe roaming candidates'
        self.get_logger().info(self._detail)
        self._publish_status()

    def _target_callback(self, message: PoseStamped) -> None:
        if message.header.frame_id != self._map_frame:
            self.get_logger().warning(
                f'ignoring interest target in {message.header.frame_id!r}; '
                f'publisher must localize it in {self._map_frame!r}'
            )
            return
        self._target.observe(
            Point2D(message.pose.position.x, message.pose.position.y),
            self._now_seconds(),
        )

    def _external_goal_callback(self, message: PoseStamped) -> None:
        if message.header.frame_id != self._map_frame:
            self.get_logger().error(
                f'ignoring external goal in {message.header.frame_id!r}; '
                f'expected {self._map_frame!r}'
            )
            return
        self._enabled = True
        self._navigator.cancel()
        self._current_candidate = None
        self._selection_mode = 'external'
        self._pending_external_pose = message
        self._state = RoamingState.WAITING
        self._detail = 'external goal queued until Nav2 is active'
        self._publish_status()

    def _tick(self) -> None:
        self._update_pose()
        now = self._now_seconds()
        active = self._navigator.active_request
        if active is not None and active.source == 'external':
            self._publish_status()
            return
        if not self._enabled or self._state == RoamingState.PAUSED:
            return
        if self._pending_external_pose is not None:
            if not self._nav2_is_active():
                self._wait('external goal waiting for active Nav2')
                return
            pose = self._pending_external_pose
            if self._request_navigation(pose, 'external'):
                self._pending_external_pose = None
            return
        if self._handle_target_interest(now):
            self._publish_status()
            return
        if self._state == RoamingState.DWELLING:
            if self._deadline_seconds is None or now < self._deadline_seconds:
                return
            self._deadline_seconds = None
            self._state = RoamingState.SELECTING
        if self._navigator.active_request is not None:
            return
        if self._state in {
            RoamingState.IDLE,
            RoamingState.WAITING,
            RoamingState.SELECTING,
            RoamingState.FOLLOWING,
        }:
            self._select_roaming_goal(now)

    def _handle_target_interest(self, now: float) -> bool:
        target = self._target.latest(now)
        moving = self._target.is_moving(now)
        following = self._follow_started_seconds is not None
        if following and (
            target is None
            or now - self._follow_started_seconds
            >= self._float_parameter('target_follow_max_seconds')
        ):
            active = self._navigator.active_request
            if active is not None and active.source == 'target':
                self._navigator.cancel()
            self._follow_started_seconds = None
            self._follow_cooldown_until = (
                now
                + self._float_parameter('target_follow_cooldown_seconds')
            )
            self._last_target_goal = None
            self._state = RoamingState.SELECTING
            self._detail = 'target interest ended; resuming roaming'
            return False
        if not following:
            if target is None or not moving or now < self._follow_cooldown_until:
                return False
            self._follow_started_seconds = now
            self._detail = 'fresh moving target received; entering interest mode'

        if target is None or self._current_pose is None or self._grid is None:
            return True
        period_elapsed = (
            now - self._last_target_replan_seconds
            >= self._float_parameter('target_replan_period_seconds')
        )
        target_moved = (
            self._last_target_goal is None
            or distance(target, self._last_target_goal)
            >= self._float_parameter('target_replan_distance')
        )
        if not period_elapsed or not target_moved:
            return True

        desired, yaw = standoff_point(
            self._current_pose,
            target,
            self._float_parameter('target_standoff_distance'),
        )
        candidate = self._grid.nearest_candidate(
            self._candidates,
            desired,
            self._float_parameter('target_goal_snap_distance'),
        )
        if candidate is None:
            self._detail = 'moving target has no safe nearby map goal'
            return True
        pose = make_pose(
            self,
            self._map_frame,
            candidate.x,
            candidate.y,
            yaw,
        )
        self._current_candidate = candidate
        self._selection_mode = 'target_interest'
        if self._request_navigation(pose, 'target'):
            self._last_target_goal = target
            self._last_target_replan_seconds = now
        return True

    def _select_roaming_goal(self, now: float) -> None:
        if not self._candidates:
            self._wait('waiting for a map with safe roaming candidates')
            return
        if self._current_pose is None:
            self._wait('waiting for map-to-base transform')
            return
        if not self._nav2_is_active():
            self._wait('waiting for active Nav2 lifecycle')
            return
        if not self._navigator.is_ready():
            self._wait('waiting for Nav2 action servers')
            return
        selection = self._policy.select(
            self._candidates,
            self._current_pose,
            now,
        )
        if selection is None:
            self._wait('no eligible roaming destination in configured range')
            return
        candidate, mode = selection
        yaw = math.atan2(
            candidate.y - self._current_pose.y,
            candidate.x - self._current_pose.x,
        )
        pose = make_pose(
            self,
            self._map_frame,
            candidate.x,
            candidate.y,
            yaw,
        )
        self._current_candidate = candidate
        self._selection_mode = mode
        self._request_navigation(pose, 'roaming')

    def _request_navigation(self, pose: PoseStamped, source: str) -> bool:
        if not self._nav2_is_active():
            self._wait('waiting for active Nav2 lifecycle')
            return False
        request = self._navigator.request(pose, source)
        if request is None:
            self._wait('Nav2 actions are not ready')
            return False
        self._state = (
            RoamingState.EXTERNAL
            if source == 'external'
            else RoamingState.FOLLOWING
            if source == 'target'
            else RoamingState.PLANNING
        )
        self._detail = (
            f'validating {source} goal '
            f'({pose.pose.position.x:.2f}, {pose.pose.position.y:.2f})'
        )
        self._goal_publisher.publish(pose)
        self._publish_status()
        return True

    def _navigation_started(self, request: NavigationRequest) -> None:
        self._state = (
            RoamingState.EXTERNAL
            if request.source == 'external'
            else RoamingState.FOLLOWING
            if request.source == 'target'
            else RoamingState.NAVIGATING
        )
        self._detail = f'Nav2 accepted {request.source} goal'
        self._publish_status()

    def _navigation_finished(
        self,
        request: NavigationRequest,
        outcome: NavigationOutcome,
        detail: str,
    ) -> None:
        now = self._now_seconds()
        candidate = self._current_candidate
        if request.source == 'roaming' and candidate is not None:
            if outcome == NavigationOutcome.SUCCEEDED:
                self._policy.record_success(candidate, now)
            elif outcome == NavigationOutcome.FAILED:
                self._policy.record_failure(candidate, now)
        if request.source == 'external':
            if (
                outcome == NavigationOutcome.SUCCEEDED
                and not self._boolean_parameter('resume_after_external_goal')
            ):
                self._enabled = False
                self._state = RoamingState.IDLE
            else:
                self._state = RoamingState.SELECTING
        elif request.source == 'target':
            if outcome == NavigationOutcome.FAILED:
                self._last_target_goal = None
            self._state = RoamingState.FOLLOWING
        elif outcome == NavigationOutcome.SUCCEEDED:
            self._state = RoamingState.DWELLING
            self._deadline_seconds = now + self._random.uniform(
                self._float_parameter('dwell_min_seconds'),
                self._float_parameter('dwell_max_seconds'),
            )
        else:
            self._state = RoamingState.DWELLING
            self._deadline_seconds = (
                now
                + self._float_parameter('failure_retry_delay_seconds')
            )
        self._detail = f'{request.source}: {outcome.value}; {detail}'
        self._publish_status()

    def _update_pose(self) -> None:
        try:
            transform = self._tf_buffer.lookup_transform(
                self._map_frame,
                self._base_frame,
                Time(),
            )
        except TransformException:
            return
        self._current_pose = Point2D(
            transform.transform.translation.x,
            transform.transform.translation.y,
        )

    def _nav2_is_active(self) -> bool:
        if self._nav2_active:
            return True
        if self._nav2_state_future is not None:
            if not self._nav2_state_future.done():
                return False
            try:
                result = self._nav2_state_future.result()
            except Exception as error:  # noqa: B902 - rclpy future boundary
                self.get_logger().warning(
                    f'cannot read Nav2 lifecycle state: {error}'
                )
                result = None
            self._nav2_state_future = None
            self._nav2_active = (
                result is not None
                and result.current_state.label == 'active'
            )
            return self._nav2_active
        if not self._nav2_state_client.service_is_ready():
            return False
        self._nav2_state_future = self._nav2_state_client.call_async(
            GetState.Request()
        )
        return False

    def _start_callback(self, _request, response):
        if self._enabled and self._state != RoamingState.PAUSED:
            return self._response(response, False, 'roaming is already active')
        self._start()
        return self._response(response, True, 'roaming started')

    def _pause_callback(self, _request, response):
        if not self._enabled or self._state == RoamingState.PAUSED:
            return self._response(response, False, 'roaming is not active')
        self._navigator.cancel()
        self._pending_external_pose = None
        self._state = RoamingState.PAUSED
        self._detail = 'roaming paused'
        self._publish_status()
        return self._response(response, True, self._detail)

    def _resume_callback(self, _request, response):
        if self._state != RoamingState.PAUSED:
            return self._response(response, False, 'roaming is not paused')
        self._enabled = True
        self._state = RoamingState.SELECTING
        self._detail = 'roaming resumed'
        self._publish_status()
        return self._response(response, True, self._detail)

    def _stop_callback(self, _request, response):
        if not self._enabled and self._state == RoamingState.IDLE:
            return self._response(response, False, 'roaming is already stopped')
        self._navigator.cancel()
        self._pending_external_pose = None
        self._enabled = False
        self._state = RoamingState.IDLE
        self._deadline_seconds = None
        self._follow_started_seconds = None
        self._detail = 'roaming stopped'
        self._publish_status()
        return self._response(response, True, self._detail)

    def _start(self) -> None:
        self._enabled = True
        self._state = RoamingState.SELECTING
        self._detail = 'roaming started'

    def _wait(self, detail: str) -> None:
        self._state = RoamingState.WAITING
        if detail != self._detail:
            self._detail = detail
            self.get_logger().info(detail)
            self._publish_status()

    def _publish_status(self) -> None:
        status = {
            'state': self._state.value,
            'enabled': self._enabled,
            'detail': self._detail,
            'candidate_count': len(self._candidates),
            'selection_mode': self._selection_mode,
            'pose': (
                None
                if self._current_pose is None
                else {'x': self._current_pose.x, 'y': self._current_pose.y}
            ),
            'goal': (
                None
                if self._navigator.active_request is None
                else {
                    'source': self._navigator.active_request.source,
                    'x': self._navigator.active_request.pose.pose.position.x,
                    'y': self._navigator.active_request.pose.pose.position.y,
                }
            ),
            'target_speed': self._target.speed(self._now_seconds()),
        }
        feedback = self._navigator.feedback
        if feedback is not None:
            status['distance_remaining'] = feedback.distance_remaining
            status['recoveries'] = feedback.number_of_recoveries
        message = String()
        message.data = json.dumps(status, sort_keys=True)
        self._status_publisher.publish(message)

    @staticmethod
    def _response(response, success: bool, message: str):
        response.success = success
        response.message = message
        return response

    def _now_seconds(self) -> float:
        return self.get_clock().now().nanoseconds / 1_000_000_000.0

    def _string_parameter(self, name: str) -> str:
        return str(self.get_parameter(name).value)

    def _float_parameter(self, name: str) -> float:
        return float(self.get_parameter(name).value)

    def _integer_parameter(self, name: str) -> int:
        return int(self.get_parameter(name).value)

    def _boolean_parameter(self, name: str) -> bool:
        return bool(self.get_parameter(name).value)


def main(args=None) -> None:
    """Run the roaming manager until shutdown."""
    rclpy.init(args=args)
    node = RoamingManager()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
