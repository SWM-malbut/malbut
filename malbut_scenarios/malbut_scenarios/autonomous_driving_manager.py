"""Coordinate the autonomous-driving demo without replacing Nav2."""

from collections import deque
from enum import Enum
import json
import math
from pathlib import Path
from threading import RLock

from action_msgs.msg import GoalStatus
from action_msgs.srv import CancelGoal
from geometry_msgs.msg import PoseStamped, Twist
from malbut_interfaces.action import FollowPerson
from nav2_msgs.action import NavigateToPose
import rclpy
from rclpy.action import (
    ActionClient,
    ActionServer,
    CancelResponse,
    GoalResponse,
)
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger

from malbut_scenarios.scenario_config import (
    Waypoint,
    load_room_routes,
    room_for_goal,
)


class ScenarioMode(Enum):
    """Small, observable state set used only by this demonstration."""

    IDLE = 'idle'
    PATROLLING = 'patrolling'
    WEB_NAVIGATION = 'web_navigation'
    ROOM_PATROL = 'room_patrol'
    PERSON_TRACKING = 'person_tracking'
    MANUAL = 'manual'


class AutonomousDrivingManager(Node):
    """Serialize demo missions and arbitrate autonomous/manual velocity."""

    def __init__(self) -> None:
        super().__init__('autonomous_driving_manager')
        self._declare_parameters()
        _, self._rooms = load_room_routes(Path(
            str(self.get_parameter('room_routes_file').value)
        ))
        self._callbacks = ReentrantCallbackGroup()
        self._lock = RLock()
        self._mode = ScenarioMode.IDLE
        self._detail = 'scenario manager ready'
        self._active_room = None
        self._manual_active = False
        self._navigation_token = 0
        self._navigation_handle = None
        self._web_goal_handle = None
        self._tracking_handle = None
        self._tracking_token = 0
        self._room_waypoints: deque[Waypoint] = deque()
        self._autostart_requested = bool(
            self.get_parameter('patrol_autostart').value
        )

        transient = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self._status_publisher = self.create_publisher(
            String, '/scenario/status', transient
        )
        self._velocity_publisher = self.create_publisher(
            Twist,
            str(self.get_parameter('output_cmd_vel_topic').value),
            10,
        )
        self.create_subscription(
            Twist,
            str(self.get_parameter('nav_cmd_vel_topic').value),
            self._nav_velocity_callback,
            10,
        )
        self.create_subscription(
            Twist,
            str(self.get_parameter('manual_cmd_vel_topic').value),
            self._manual_velocity_callback,
            10,
        )

        self._roaming_start = self.create_client(
            Trigger, '/roaming/start', callback_group=self._callbacks
        )
        self._roaming_pause = self.create_client(
            Trigger, '/roaming/pause', callback_group=self._callbacks
        )
        self._roaming_stop = self.create_client(
            Trigger, '/roaming/stop', callback_group=self._callbacks
        )
        self.create_service(
            Trigger,
            '/scenario/start_patrol',
            self._start_patrol_callback,
            callback_group=self._callbacks,
        )
        self.create_service(
            Trigger,
            '/scenario/stop',
            self._stop_callback,
            callback_group=self._callbacks,
        )
        self.create_service(
            Trigger,
            '/scenario/start_person_tracking',
            self._start_tracking_callback,
            callback_group=self._callbacks,
        )
        self.create_service(
            Trigger,
            '/scenario/stop_person_tracking',
            self._stop_tracking_callback,
            callback_group=self._callbacks,
        )

        self._navigator = ActionClient(
            self,
            NavigateToPose,
            str(self.get_parameter('navigation_action_name').value),
            callback_group=self._callbacks,
        )
        self._tracker = ActionClient(
            self,
            FollowPerson,
            str(self.get_parameter('follow_person_action_name').value),
            callback_group=self._callbacks,
        )
        self._web_navigation = ActionServer(
            self,
            NavigateToPose,
            str(self.get_parameter('web_navigation_action_name').value),
            execute_callback=self._execute_web_navigation,
            goal_callback=self._web_goal_callback,
            cancel_callback=self._web_cancel_callback,
            callback_group=self._callbacks,
        )
        cancel_names = (
            '/navigate_to_pose/_action/cancel_goal',
            '/follow_path/_action/cancel_goal',
            '/spin/_action/cancel_goal',
            '/follow_person/_action/cancel_goal',
            '/scenario/navigate_to_pose/_action/cancel_goal',
        )
        self._cancel_clients = tuple(
            self.create_client(
                CancelGoal, name, callback_group=self._callbacks
            )
            for name in cancel_names
        )
        self._autostart_timer = self.create_timer(
            1.0, self._autostart_tick, callback_group=self._callbacks
        )
        self._publish_status()

    def _declare_parameters(self) -> None:
        defaults = {
            'room_routes_file': '',
            'patrol_autostart': False,
            'navigation_action_name': '/navigate_to_pose',
            'web_navigation_action_name': '/scenario/navigate_to_pose',
            'follow_person_action_name': '/follow_person',
            'nav_cmd_vel_topic': '/scenario/nav_cmd_vel',
            'manual_cmd_vel_topic': '/cmd_vel_manual',
            'output_cmd_vel_topic': '/scenario/safety_input',
            'manual_deadband': 0.01,
            'desired_distance_m': 1.2,
            'minimum_distance_m': 0.65,
            'maximum_linear_speed_mps': 0.30,
            'target_lost_timeout_s': 8.0,
        }
        for name, default in defaults.items():
            self.declare_parameter(name, default)
        if not str(self.get_parameter('room_routes_file').value):
            raise ValueError('room_routes_file is required')
        if float(self.get_parameter('manual_deadband').value) < 0.0:
            raise ValueError('manual_deadband must be non-negative')

    def destroy_node(self):
        """Release action resources before destroying the ROS node."""
        self._cancel_navigation()
        if self._tracking_handle is not None:
            self._tracking_handle.cancel_goal_async()
        self._web_navigation.destroy()
        self._navigator.destroy()
        self._tracker.destroy()
        return super().destroy_node()

    def _autostart_tick(self) -> None:
        if not self._autostart_requested:
            return
        if not self._roaming_start.service_is_ready():
            return
        self._autostart_requested = False
        self._enter_patrol('patrol autostart requested')

    def _start_patrol_callback(self, _request, response):
        self._enter_patrol('patrol requested')
        response.success = True
        response.message = 'autonomous patrol started'
        return response

    def _enter_patrol(self, detail: str) -> None:
        with self._lock:
            self._manual_active = False
            self._tracking_token += 1
            if self._tracking_handle is not None:
                self._tracking_handle.cancel_goal_async()
                self._tracking_handle = None
            self._cancel_navigation()
            self._room_waypoints.clear()
            self._active_room = None
            self._mode = ScenarioMode.PATROLLING
            self._detail = detail
            self._velocity_publisher.publish(Twist())
            self._call_trigger(self._roaming_start)
            self._publish_status()

    def _stop_callback(self, _request, response):
        with self._lock:
            self._tracking_token += 1
            if self._tracking_handle is not None:
                self._tracking_handle.cancel_goal_async()
                self._tracking_handle = None
            self._cancel_navigation()
            self._room_waypoints.clear()
            self._active_room = None
            self._manual_active = False
            self._call_trigger(self._roaming_stop)
            self._velocity_publisher.publish(Twist())
            self._set_mode(ScenarioMode.IDLE, 'scenario stopped')
        response.success = True
        response.message = 'scenario stopped'
        return response

    def _web_goal_callback(self, _goal_request):
        return GoalResponse.ACCEPT

    def _web_cancel_callback(self, _goal_handle):
        with self._lock:
            self._cancel_navigation()
        return CancelResponse.ACCEPT

    async def _execute_web_navigation(self, goal_handle):
        """Pause roaming, execute the web goal, then patrol its room."""
        with self._lock:
            self._manual_active = False
            self._tracking_token += 1
            if self._tracking_handle is not None:
                self._tracking_handle.cancel_goal_async()
                self._tracking_handle = None
            self._cancel_navigation()
            self._room_waypoints.clear()
            self._active_room = None
            self._call_trigger(self._roaming_pause)
            self._web_goal_handle = goal_handle
            self._mode = ScenarioMode.WEB_NAVIGATION
            self._detail = 'moving to the web-selected location'
            self._velocity_publisher.publish(Twist())
            self._publish_status()
            self._navigation_token += 1
            token = self._navigation_token

        result = NavigateToPose.Result()
        if not self._navigator.wait_for_server(timeout_sec=5.0):
            goal_handle.abort()
            self._finish_web_failure('Nav2 navigation action is unavailable')
            return result
        send_future = self._navigator.send_goal_async(
            goal_handle.request,
            feedback_callback=lambda message: self._forward_web_feedback(
                goal_handle, message
            ),
        )
        downstream = await send_future
        with self._lock:
            if token != self._navigation_token:
                if downstream.accepted:
                    downstream.cancel_goal_async()
                goal_handle.canceled()
                return result
            if not downstream.accepted:
                goal_handle.abort()
                self._finish_web_failure('Nav2 rejected the selected goal')
                return result
            self._navigation_handle = downstream
        wrapped = await downstream.get_result_async()
        with self._lock:
            if token != self._navigation_token or self._manual_active:
                goal_handle.canceled()
                self._web_goal_handle = None
                return result
            self._navigation_handle = None
            self._web_goal_handle = None
            if (
                goal_handle.is_cancel_requested
                or wrapped.status == GoalStatus.STATUS_CANCELED
            ):
                goal_handle.canceled()
                self._resume_roaming('web navigation canceled')
                return result
            if wrapped.status != GoalStatus.STATUS_SUCCEEDED:
                goal_handle.abort()
                self._finish_web_failure('web navigation failed')
                return result
            goal_handle.succeed()
            pose = goal_handle.request.pose.pose.position
            self._begin_room_patrol(pose.x, pose.y)
        return result

    def _forward_web_feedback(self, goal_handle, message) -> None:
        if goal_handle.is_active and not goal_handle.is_cancel_requested:
            goal_handle.publish_feedback(message.feedback)

    def _finish_web_failure(self, detail: str) -> None:
        with self._lock:
            self._navigation_handle = None
            self._web_goal_handle = None
            self._resume_roaming(detail)

    def _begin_room_patrol(self, x: float, y: float) -> None:
        room = room_for_goal(self._rooms, x, y)
        if room is None:
            self._resume_roaming('selected location has no room route')
            return
        self._active_room = room.room_id
        self._room_waypoints = deque(room.ordered_from(x, y))
        self._mode = ScenarioMode.ROOM_PATROL
        self._detail = f'patrolling {room.name}'
        self._publish_status()
        self._send_next_room_waypoint()

    def _send_next_room_waypoint(self) -> None:
        if self._mode != ScenarioMode.ROOM_PATROL:
            return
        if not self._room_waypoints:
            self._active_room = None
            self._resume_roaming('room patrol completed')
            return
        if not self._navigator.server_is_ready():
            self._resume_roaming('Nav2 became unavailable during room patrol')
            return
        waypoint = self._room_waypoints.popleft()
        goal = NavigateToPose.Goal()
        goal.pose = self._pose(waypoint)
        self._navigation_token += 1
        token = self._navigation_token
        future = self._navigator.send_goal_async(goal)
        future.add_done_callback(
            lambda completed: self._room_goal_response(completed, token)
        )

    def _room_goal_response(self, future, token: int) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self.get_logger().warning(f'room waypoint request failed: {error}')
            self._send_next_room_waypoint()
            return
        with self._lock:
            if token != self._navigation_token:
                if goal_handle.accepted:
                    goal_handle.cancel_goal_async()
                return
            if not goal_handle.accepted:
                self._detail = 'room waypoint rejected; trying the next one'
                self._publish_status()
                self._send_next_room_waypoint()
                return
            self._navigation_handle = goal_handle
            goal_handle.get_result_async().add_done_callback(
                lambda completed: self._room_goal_result(completed, token)
            )

    def _room_goal_result(self, future, token: int) -> None:
        try:
            wrapped = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self.get_logger().warning(f'room waypoint failed: {error}')
            wrapped = None
        with self._lock:
            if token != self._navigation_token:
                return
            self._navigation_handle = None
            if wrapped is not None and wrapped.status != GoalStatus.STATUS_SUCCEEDED:
                self._detail = 'room waypoint failed; trying the next one'
                self._publish_status()
            self._send_next_room_waypoint()

    def _start_tracking_callback(self, _request, response):
        with self._lock:
            if not self._tracker.server_is_ready():
                response.success = False
                response.message = 'FollowPerson action is not ready'
                return response
            self._manual_active = False
            self._cancel_navigation()
            self._room_waypoints.clear()
            self._active_room = None
            self._call_trigger(self._roaming_pause)
            self._tracking_token += 1
            token = self._tracking_token
            goal = FollowPerson.Goal()
            goal.desired_distance_m = float(
                self.get_parameter('desired_distance_m').value
            )
            goal.minimum_distance_m = float(
                self.get_parameter('minimum_distance_m').value
            )
            goal.maximum_linear_speed_mps = float(
                self.get_parameter('maximum_linear_speed_mps').value
            )
            timeout = float(
                self.get_parameter('target_lost_timeout_s').value
            )
            goal.target_lost_timeout.sec = math.floor(timeout)
            goal.target_lost_timeout.nanosec = round(
                (timeout - math.floor(timeout)) * 1_000_000_000
            )
            self._mode = ScenarioMode.PERSON_TRACKING
            self._detail = 'waiting for the first detected person'
            self._velocity_publisher.publish(Twist())
            self._publish_status()
            future = self._tracker.send_goal_async(goal)
            future.add_done_callback(
                lambda completed: self._tracking_goal_response(
                    completed, token
                )
            )
        response.success = True
        response.message = 'person tracking requested'
        return response

    def _tracking_goal_response(self, future, token: int) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self.get_logger().error(f'FollowPerson request failed: {error}')
            self._resume_roaming('person tracking request failed')
            return
        with self._lock:
            if token != self._tracking_token:
                if goal_handle.accepted:
                    goal_handle.cancel_goal_async()
                return
            if not goal_handle.accepted:
                self._resume_roaming('FollowPerson rejected the request')
                return
            self._tracking_handle = goal_handle
            goal_handle.get_result_async().add_done_callback(
                lambda completed: self._tracking_result(completed, token)
            )

    def _tracking_result(self, future, token: int) -> None:
        try:
            future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self.get_logger().warning(f'FollowPerson ended with error: {error}')
        with self._lock:
            if token != self._tracking_token:
                return
            self._tracking_handle = None
            if not self._manual_active:
                self._resume_roaming('person tracking ended')

    def _stop_tracking_callback(self, _request, response):
        with self._lock:
            self._tracking_token += 1
            if self._tracking_handle is not None:
                self._tracking_handle.cancel_goal_async()
                self._tracking_handle = None
            self._resume_roaming('person tracking stopped')
        response.success = True
        response.message = 'person tracking stopped'
        return response

    def _manual_velocity_callback(self, message: Twist) -> None:
        active_command = self._twist_magnitude(message) > float(
            self.get_parameter('manual_deadband').value
        )
        with self._lock:
            if active_command and not self._manual_active:
                self._manual_active = True
                self._tracking_token += 1
                self._navigation_token += 1
                self._room_waypoints.clear()
                self._active_room = None
                self._call_trigger(self._roaming_pause)
                self._cancel_all_actions()
                self._mode = ScenarioMode.MANUAL
                self._detail = (
                    'manual input canceled autonomous motion and took control'
                )
                self._publish_status()
            if self._manual_active:
                self._velocity_publisher.publish(message)

    def _nav_velocity_callback(self, message: Twist) -> None:
        with self._lock:
            if not self._manual_active:
                self._velocity_publisher.publish(message)

    @staticmethod
    def _twist_magnitude(message: Twist) -> float:
        return max(
            abs(message.linear.x),
            abs(message.linear.y),
            abs(message.linear.z),
            abs(message.angular.x),
            abs(message.angular.y),
            abs(message.angular.z),
        )

    def _cancel_all_actions(self) -> None:
        self._cancel_navigation()
        if self._tracking_handle is not None:
            self._tracking_handle.cancel_goal_async()
            self._tracking_handle = None
        for client in self._cancel_clients:
            if client.service_is_ready():
                client.call_async(CancelGoal.Request())
        self._velocity_publisher.publish(Twist())

    def _cancel_navigation(self) -> None:
        self._navigation_token += 1
        if self._navigation_handle is not None:
            self._navigation_handle.cancel_goal_async()
            self._navigation_handle = None

    def _resume_roaming(self, detail: str) -> None:
        if self._manual_active:
            return
        self._mode = ScenarioMode.PATROLLING
        self._detail = detail
        self._call_trigger(self._roaming_start)
        self._publish_status()

    @staticmethod
    def _call_trigger(client) -> None:
        if client.service_is_ready():
            client.call_async(Trigger.Request())

    def _pose(self, waypoint: Waypoint) -> PoseStamped:
        pose = PoseStamped()
        pose.header.frame_id = 'map'
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.pose.position.x = waypoint.x
        pose.pose.position.y = waypoint.y
        pose.pose.orientation.z = math.sin(waypoint.yaw / 2.0)
        pose.pose.orientation.w = math.cos(waypoint.yaw / 2.0)
        return pose

    def _set_mode(self, mode: ScenarioMode, detail: str) -> None:
        self._mode = mode
        self._detail = detail
        self._publish_status()

    def _publish_status(self) -> None:
        message = String()
        message.data = json.dumps({
            'mode': self._mode.value,
            'detail': self._detail,
            'active_room': self._active_room,
            'manual_control': self._manual_active,
        }, sort_keys=True)
        self._status_publisher.publish(message)


def main(args=None) -> None:
    """Run the demonstration coordinator with concurrent action callbacks."""
    rclpy.init(args=args)
    node = AutonomousDrivingManager()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
