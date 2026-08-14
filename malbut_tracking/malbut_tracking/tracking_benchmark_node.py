"""Measure person-tracking retention over one simulated humanoid lap."""

import json
from pathlib import Path
import sys
import time

from malbut_interfaces.action import FollowPerson
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from .tracking_metrics import TrackingDurationMetrics


class TrackingBenchmarkNode(Node):
    """Run FollowPerson and report durations instead of a pass threshold."""

    def __init__(self) -> None:
        """Create the action client, status subscriber, and lap timer."""
        super().__init__('tracking_benchmark')
        self._declare_parameters()
        self._validate_parameters()
        self._world_name = str(self.get_parameter('world_name').value)
        self._lap_duration_s = float(
            self.get_parameter('lap_duration_s').value
        )
        self._acquisition_timeout_s = float(
            self.get_parameter('acquisition_timeout_s').value
        )
        self._status_stale_timeout_s = float(
            self.get_parameter('status_stale_timeout_s').value
        )
        self._result_file = str(self.get_parameter('result_file').value)
        self._action = ActionClient(
            self,
            FollowPerson,
            str(self.get_parameter('action_name').value),
        )
        self._status_subscription = self.create_subscription(
            String,
            str(self.get_parameter('status_topic').value),
            self._on_status,
            10,
        )
        result_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self._result_publisher = self.create_publisher(
            String,
            str(self.get_parameter('result_topic').value),
            result_qos,
        )
        self._metrics = TrackingDurationMetrics()
        self._latest_tracking = False
        self._latest_recovery = False
        self._latest_status_s: float | None = None
        self._goal_request_sent = False
        self._goal_handle = None
        self._goal_result_state = 'WAITING'
        self._startup_monotonic_s = time.monotonic()
        self._finished_monotonic_s: float | None = None
        self._done = False
        self._exit_code = 0
        self._last_progress_log_s = -float('inf')
        self._timer = self.create_timer(0.05, self._tick)
        self.get_logger().info(
            f'Waiting to benchmark one {self._world_name} lap '
            f'({self._lap_duration_s:.3f}s)'
        )

    @property
    def done(self) -> bool:
        """Return whether the report has had time to leave the publisher."""
        return self._done

    @property
    def exit_code(self) -> int:
        """Return zero for a completed measurement, not tracking quality."""
        return self._exit_code

    def _declare_parameters(self) -> None:
        self.declare_parameter('use_sim_time', True)
        self.declare_parameter('world_name', '')
        self.declare_parameter('lap_duration_s', 0.0)
        self.declare_parameter('acquisition_timeout_s', 120.0)
        self.declare_parameter('status_stale_timeout_s', 0.50)
        self.declare_parameter('action_name', '/follow_person')
        self.declare_parameter('status_topic', '/tracking/person/status')
        self.declare_parameter(
            'result_topic', '/tracking/person/benchmark_result'
        )
        self.declare_parameter('result_file', '')
        self.declare_parameter('desired_distance_m', 1.20)
        self.declare_parameter('minimum_distance_m', 0.65)
        self.declare_parameter('maximum_linear_speed_mps', 0.30)

    def _validate_parameters(self) -> None:
        if not str(self.get_parameter('world_name').value):
            raise ValueError('world_name must not be empty')
        for name in (
            'lap_duration_s',
            'acquisition_timeout_s',
            'status_stale_timeout_s',
            'desired_distance_m',
            'minimum_distance_m',
            'maximum_linear_speed_mps',
        ):
            if float(self.get_parameter(name).value) <= 0.0:
                raise ValueError(f'{name} must be positive')

    def _on_status(self, message: String) -> None:
        try:
            status = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            return
        self._latest_tracking = bool(
            status.get('target_visible')
            and status.get('state') == 'TRACKING'
        )
        self._latest_recovery = (
            status.get('state') == 'TEMPORARILY_LOST'
        )
        self._latest_status_s = self._now_s()

    def _tick(self) -> None:
        if self._finished_monotonic_s is not None:
            if time.monotonic() - self._finished_monotonic_s >= 0.5:
                self._done = True
            return
        if not self._goal_request_sent:
            self._try_send_goal()
        now_s = self._now_s()
        tracking, recovering = self._current_status(now_s)
        if self._metrics.started_s is None:
            if tracking and self._goal_handle is not None:
                self._metrics.start(now_s)
                self.get_logger().info(
                    'First visible person acquired; lap measurement started'
                )
            elif (
                time.monotonic() - self._startup_monotonic_s
                >= self._acquisition_timeout_s
            ):
                self._finish('acquisition_timeout', now_s, exit_code=2)
            return

        self._metrics.sample(now_s, tracking, recovering)
        elapsed_s = self._metrics.elapsed(now_s)
        if elapsed_s - self._last_progress_log_s >= 10.0:
            self._last_progress_log_s = elapsed_s
            report = self._metrics.report(now_s)
            self.get_logger().info(
                'Tracking benchmark progress: '
                f'{elapsed_s:.1f}/{self._lap_duration_s:.1f}s, '
                f'tracked={report["tracking_duration_s"]:.1f}s'
            )
        if elapsed_s >= self._lap_duration_s:
            self._finish('lap_complete', now_s)

    def _try_send_goal(self) -> None:
        if not self._action.server_is_ready():
            return
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
        timeout_s = self._lap_duration_s + 30.0
        goal.target_lost_timeout.sec = int(timeout_s)
        goal.target_lost_timeout.nanosec = int((timeout_s % 1.0) * 1e9)
        self._goal_request_sent = True
        future = self._action.send_goal_async(goal)
        future.add_done_callback(self._on_goal_response)

    def _on_goal_response(self, future) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self.get_logger().error(f'FollowPerson request failed: {error}')
            self._finish('action_request_failed', self._now_s(), exit_code=2)
            return
        if not goal_handle.accepted:
            self.get_logger().error('FollowPerson goal was rejected')
            self._finish('action_rejected', self._now_s(), exit_code=2)
            return
        self._goal_handle = goal_handle
        self._goal_result_state = 'ACTIVE'
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(self._on_goal_result)

    def _on_goal_result(self, future) -> None:
        try:
            wrapped = future.result()
            self._goal_result_state = str(wrapped.result.final_state)
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._goal_result_state = f'RESULT_ERROR: {error}'

    def _current_status(self, now_s: float) -> tuple[bool, bool]:
        if self._latest_status_s is None:
            return False, False
        if now_s - self._latest_status_s > self._status_stale_timeout_s:
            return False, False
        return self._latest_tracking, self._latest_recovery

    def _finish(
        self,
        reason: str,
        now_s: float,
        exit_code: int = 0,
    ) -> None:
        if self._finished_monotonic_s is not None:
            return
        report: dict[str, object] = {
            'world_name': self._world_name,
            'lap_duration_s': self._lap_duration_s,
            'termination_reason': reason,
            'action_result_state': self._goal_result_state,
        }
        if self._metrics.started_s is not None:
            report.update(self._metrics.report(now_s))
        else:
            report.update({
                'elapsed_s': 0.0,
                'tracking_duration_s': 0.0,
                'tracking_ratio': 0.0,
                'longest_continuous_tracking_s': 0.0,
                'recovery_duration_s': 0.0,
                'reacquisition_count': 0,
            })
        report = {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in report.items()
        }
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        message = String()
        message.data = encoded
        self._result_publisher.publish(message)
        self.get_logger().info(f'TRACKING_BENCHMARK_RESULT {encoded}')
        if self._result_file:
            destination = Path(self._result_file).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(encoded + '\n', encoding='utf-8')
            self.get_logger().info(f'Wrote benchmark report to {destination}')
        if self._goal_handle is not None:
            self._goal_handle.cancel_goal_async()
        self._exit_code = exit_code
        self._finished_monotonic_s = time.monotonic()

    def _now_s(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9

    def destroy_node(self):
        """Release the action client before the ROS node is destroyed."""
        self._action.destroy()
        return super().destroy_node()


def main(args=None) -> int:
    """Run one time-based tracking benchmark."""
    rclpy.init(args=args)
    node = None
    try:
        node = TrackingBenchmarkNode()
        while rclpy.ok() and not node.done:
            rclpy.spin_once(node, timeout_sec=0.1)
        return node.exit_code
    except (KeyboardInterrupt, ExternalShutdownException):
        return 130
    except (RuntimeError, ValueError) as error:
        print(f'tracking benchmark failed to start: {error}', file=sys.stderr)
        return 2
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
