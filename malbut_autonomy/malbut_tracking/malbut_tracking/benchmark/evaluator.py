"""Measure a fixed-duration sensor-only person-following benchmark."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import statistics

from geometry_msgs.msg import PoseStamped
from malbut_interfaces.action import FollowPerson
from malbut_interfaces.msg import TrackingCommandTrace
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import String
from tf2_msgs.msg import TFMessage


@dataclass(frozen=True)
class PlanarPose:
    """One exact simulator pose projected onto the floor plane."""

    x: float
    y: float
    yaw: float


def _time_seconds(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


def _yaw(rotation) -> float:
    siny_cosp = 2.0 * (
        rotation.w * rotation.z + rotation.x * rotation.y
    )
    cosy_cosp = 1.0 - 2.0 * (
        rotation.y * rotation.y + rotation.z * rotation.z
    )
    return math.atan2(siny_cosp, cosy_cosp)


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _circle_intersects_oriented_box(
    circle: PlanarPose,
    box: PlanarPose,
    circle_radius_m: float,
    box_half_length_m: float,
    box_half_width_m: float,
) -> bool:
    """Test the actor cylinder against the robot footprint rectangle."""
    delta_x = circle.x - box.x
    delta_y = circle.y - box.y
    cosine = math.cos(box.yaw)
    sine = math.sin(box.yaw)
    local_x = cosine * delta_x + sine * delta_y
    local_y = -sine * delta_x + cosine * delta_y
    closest_x = min(box_half_length_m, max(-box_half_length_m, local_x))
    closest_y = min(box_half_width_m, max(-box_half_width_m, local_y))
    return math.hypot(local_x - closest_x, local_y - closest_y) <= (
        circle_radius_m
    )


class PersonTrackingBenchmark(Node):
    """Run FollowPerson and record metrics without feeding truth to it."""

    def __init__(self) -> None:
        super().__init__('person_tracking_benchmark')
        self._declare_parameters()
        self._validate_parameters()

        self._robot_name = str(
            self.get_parameter('robot_entity_name').value
        )
        self._person_name = str(
            self.get_parameter('person_entity_name').value
        )
        self._scenario_name = str(
            self.get_parameter('scenario_name').value
        )
        self._world_name = str(self.get_parameter('world_name').value)
        self._trajectory_name = str(
            self.get_parameter('trajectory_name').value
        )
        self._measurement_duration_s = float(
            self.get_parameter('measurement_duration_s').value
        )
        self._desired_distance_m = float(
            self.get_parameter('desired_distance_m').value
        )
        self._person_collision_radius_m = float(
            self.get_parameter('person_collision_radius_m').value
        )
        self._robot_footprint_half_length_m = float(
            self.get_parameter('robot_footprint_half_length_m').value
        )
        self._robot_footprint_half_width_m = float(
            self.get_parameter('robot_footprint_half_width_m').value
        )
        self._robot_pose: PlanarPose | None = None
        self._person_pose: PlanarPose | None = None
        self._previous_person_pose: PlanarPose | None = None
        self._estimate_pose: PlanarPose | None = None
        self._estimate_stamp_s: float | None = None
        self._state = 'STOPPED'
        self._target_visible = False
        self._contact_active = False
        self._collision_count = 0
        self._path_progress_m = 0.0
        self._measurement_start_s: float | None = None
        self._measurement_end_s: float | None = None
        self._action_sent = False
        self._action_goal_handle = None
        self._finalized = False
        self._shutdown_timer = None

        self._sample_count = 0
        self._distance_error_sum = 0.0
        self._tracking_valid_count = 0
        self._prediction_sample_count = 0
        self._prediction_error_sum = 0.0
        self._prediction_outside_count = 0
        self._latencies_ms: list[float] = []

        self._result_directory = self._create_result_directory()
        self._sample_stream = (
            self._result_directory / 'samples.csv'
        ).open('w', encoding='utf-8', newline='')
        self._sample_writer = csv.writer(self._sample_stream)
        self._sample_writer.writerow([
            'sim_time_s',
            'robot_x',
            'robot_y',
            'robot_yaw',
            'person_x',
            'person_y',
            'person_yaw',
            'distance_m',
            'distance_abs_error_m',
            'estimate_x',
            'estimate_y',
            'estimate_age_s',
            'prediction_error_m',
            'tracking_valid',
            'follower_state',
            'target_visible',
            'collision_active',
            'path_progress_m',
        ])
        self._event_stream = (
            self._result_directory / 'events.jsonl'
        ).open('w', encoding='utf-8')

        self._follow_client = ActionClient(
            self,
            FollowPerson,
            str(self.get_parameter('follow_action').value),
        )
        self.create_subscription(
            TFMessage,
            str(self.get_parameter('ground_truth_topic').value),
            self._on_ground_truth,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            PoseStamped,
            str(self.get_parameter('target_pose_topic').value),
            self._on_estimate,
            10,
        )
        self.create_subscription(
            String,
            str(self.get_parameter('tracking_status_topic').value),
            self._on_tracking_status,
            10,
        )
        self.create_subscription(
            TrackingCommandTrace,
            str(self.get_parameter('command_trace_topic').value),
            self._on_command_trace,
            10,
        )
        sample_rate_hz = float(
            self.get_parameter('sample_rate_hz').value
        )
        self.create_timer(1.0 / sample_rate_hz, self._sample)
        self.create_timer(0.25, self._try_start_following)
        self.get_logger().info(
            'Benchmark ready: measurement begins with actor movement and '
            f'runs for {self._measurement_duration_s:.1f} seconds; results '
            'will be written '
            f'to {self._result_directory}'
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter('ground_truth_topic', '/benchmark/ground_truth')
        self.declare_parameter(
            'target_pose_topic', '/tracking/person/estimated_target_pose'
        )
        self.declare_parameter(
            'tracking_status_topic', '/tracking/person/status'
        )
        self.declare_parameter(
            'command_trace_topic', '/tracking/person/command_trace'
        )
        self.declare_parameter('follow_action', '/follow_person')
        self.declare_parameter('robot_entity_name', 'malbut')
        self.declare_parameter('person_entity_name', 'benchmark_person')
        self.declare_parameter('scenario_name', 'test_arena_perimeter')
        self.declare_parameter('world_name', 'test_arena')
        self.declare_parameter('trajectory_name', 'perimeter_loop')
        self.declare_parameter('person_collision_radius_m', 0.18)
        self.declare_parameter('robot_footprint_half_length_m', 0.1385)
        self.declare_parameter('robot_footprint_half_width_m', 0.127)
        self.declare_parameter('desired_distance_m', 1.0)
        self.declare_parameter('sample_rate_hz', 20.0)
        self.declare_parameter('movement_threshold_m', 0.002)
        self.declare_parameter('tracking_position_tolerance_m', 0.75)
        self.declare_parameter('prediction_position_tolerance_m', 0.75)
        self.declare_parameter('maximum_estimate_age_s', 0.50)
        self.declare_parameter('measurement_duration_s', 180.0)
        self.declare_parameter('output_directory', '')

    def _validate_parameters(self) -> None:
        required_text = (
            'scenario_name',
            'world_name',
            'trajectory_name',
            'robot_entity_name',
            'person_entity_name',
        )
        for name in required_text:
            if not str(self.get_parameter(name).value).strip():
                raise ValueError(f'{name} must not be empty')
        positive = (
            'person_collision_radius_m',
            'robot_footprint_half_length_m',
            'robot_footprint_half_width_m',
            'desired_distance_m',
            'sample_rate_hz',
            'movement_threshold_m',
            'tracking_position_tolerance_m',
            'prediction_position_tolerance_m',
            'maximum_estimate_age_s',
            'measurement_duration_s',
        )
        for name in positive:
            if float(self.get_parameter(name).value) <= 0.0:
                raise ValueError(f'{name} must be positive')

    def _create_result_directory(self) -> Path:
        configured = str(
            self.get_parameter('output_directory').value
        ).strip()
        root = (
            Path(configured).expanduser()
            if configured
            else Path.home() / '.ros' / 'malbut' / 'benchmarks'
        )
        run_id = (
            f'{self._scenario_name}-'
            + datetime.now().strftime('%Y%m%d-%H%M%S')
        )
        result = root / run_id
        suffix = 1
        while result.exists():
            result = root / f'{run_id}-{suffix}'
            suffix += 1
        result.mkdir(parents=True)
        return result

    def _on_ground_truth(self, message: TFMessage) -> None:
        # The official ros_gz_bridge preserves each Gazebo entity name as the
        # transform child frame. No truth topic is visible to tracking nodes.
        poses = {
            transform.child_frame_id: transform.transform
            for transform in message.transforms
        }
        robot = poses.get(self._robot_name)
        person = poses.get(self._person_name)
        if robot is None or person is None:
            return
        self._robot_pose = PlanarPose(
            float(robot.translation.x),
            float(robot.translation.y),
            _yaw(robot.rotation),
        )
        self._person_pose = PlanarPose(
            float(person.translation.x),
            float(person.translation.y),
            _yaw(person.rotation),
        )

    def _on_estimate(self, message: PoseStamped) -> None:
        position = message.pose.position
        self._estimate_pose = PlanarPose(
            float(position.x),
            float(position.y),
            _yaw(message.pose.orientation),
        )
        stamp_s = _time_seconds(message.header.stamp)
        self._estimate_stamp_s = (
            stamp_s if stamp_s > 0.0 else self._now_seconds()
        )

    def _on_tracking_status(self, message: String) -> None:
        try:
            status = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            return
        self._state = str(status.get('state', self._state))
        self._target_visible = bool(status.get('target_visible', False))

    def _on_command_trace(self, message: TrackingCommandTrace) -> None:
        if self._measurement_start_s is None or self._finalized:
            return
        if message.source == 'last_seen_recovery':
            return
        source_s = _time_seconds(message.source_stamp)
        dispatch_s = _time_seconds(message.dispatch_stamp)
        latency_ms = 1000.0 * (dispatch_s - source_s)
        if latency_ms < 0.0:
            return
        self._latencies_ms.append(latency_ms)
        self._write_event(
            'navigation_command_dispatch',
            dispatch_s,
            sequence=int(message.sequence),
            source=message.source,
            latency_ms=latency_ms,
        )

    def _try_start_following(self) -> None:
        if self._action_sent or self._finalized:
            return
        if self._measurement_start_s is None:
            return
        if self._robot_pose is None or self._person_pose is None:
            return
        if not self._follow_client.server_is_ready():
            return
        goal = FollowPerson.Goal()
        goal.target_mode = FollowPerson.Goal.VISIBLE_PERSON
        goal.target_person_id = ''
        goal.desired_distance_m = self._desired_distance_m
        self._action_sent = True
        future = self._follow_client.send_goal_async(goal)
        future.add_done_callback(self._on_follow_goal_response)
        self._write_event('follow_goal_dispatched', self._now_seconds())

    def _on_follow_goal_response(self, future) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._finalize('FAILED', f'follow action failed: {error}')
            return
        if not goal_handle.accepted:
            self._finalize('FAILED', 'follow action rejected the goal')
            return
        self._action_goal_handle = goal_handle
        goal_handle.get_result_async().add_done_callback(
            self._on_follow_result
        )
        self._write_event('follow_goal_accepted', self._now_seconds())

    def _on_follow_result(self, future) -> None:
        """Fail the run if tracking terminates before measurement finishes."""
        if self._finalized:
            return
        try:
            response = future.result()
            result = response.result
            detail = f'{result.final_state}: {result.message}'
        except Exception as error:  # noqa: B902 - rclpy future boundary
            detail = str(error)
        self._finalize(
            'FAILED',
            f'follow action ended before benchmark completion: {detail}',
        )

    def _sample(self) -> None:
        if self._finalized:
            return
        now_s = self._now_seconds()
        if self._robot_pose is None or self._person_pose is None:
            return

        self._update_movement_progress(now_s)
        if self._measurement_start_s is None:
            return
        measurement_deadline_s = (
            self._measurement_start_s + self._measurement_duration_s
        )
        if now_s >= measurement_deadline_s:
            self._measurement_end_s = measurement_deadline_s
            self._finalize(
                'COMPLETED',
                f'{self._measurement_duration_s:.1f}-second measurement '
                'completed',
            )
            return

        robot = self._robot_pose
        person = self._person_pose
        collision_active = _circle_intersects_oriented_box(
            person,
            robot,
            self._person_collision_radius_m,
            self._robot_footprint_half_length_m,
            self._robot_footprint_half_width_m,
        )
        if collision_active and not self._contact_active:
            self._collision_count += 1
            self._write_event('collision_enter', now_s)
        elif not collision_active and self._contact_active:
            self._write_event('collision_exit', now_s)
        self._contact_active = collision_active
        distance_m = math.hypot(person.x - robot.x, person.y - robot.y)
        distance_error_m = abs(distance_m - self._desired_distance_m)
        self._sample_count += 1
        self._distance_error_sum += distance_error_m

        estimate_age_s = None
        prediction_error_m = None
        tracking_valid = False
        if (
            self._estimate_pose is not None
            and self._estimate_stamp_s is not None
        ):
            estimate_age_s = max(0.0, now_s - self._estimate_stamp_s)
            prediction_error_m = math.hypot(
                self._estimate_pose.x - person.x,
                self._estimate_pose.y - person.y,
            )
            self._prediction_sample_count += 1
            self._prediction_error_sum += prediction_error_m
            if prediction_error_m > float(
                self.get_parameter('prediction_position_tolerance_m').value
            ):
                self._prediction_outside_count += 1
            tracking_valid = (
                self._state in {'TRACKING', 'RECOVERING'}
                and estimate_age_s <= float(
                    self.get_parameter('maximum_estimate_age_s').value
                )
                and prediction_error_m <= float(
                    self.get_parameter(
                        'tracking_position_tolerance_m'
                    ).value
                )
            )
        else:
            # A missing prediction is not silently removed from a time-ratio
            # metric; it is an invalid prediction interval.
            self._prediction_outside_count += 1
        if tracking_valid:
            self._tracking_valid_count += 1

        self._sample_writer.writerow([
            f'{now_s:.9f}',
            f'{robot.x:.6f}',
            f'{robot.y:.6f}',
            f'{robot.yaw:.6f}',
            f'{person.x:.6f}',
            f'{person.y:.6f}',
            f'{person.yaw:.6f}',
            f'{distance_m:.6f}',
            f'{distance_error_m:.6f}',
            (
                ''
                if self._estimate_pose is None
                else f'{self._estimate_pose.x:.6f}'
            ),
            (
                ''
                if self._estimate_pose is None
                else f'{self._estimate_pose.y:.6f}'
            ),
            '' if estimate_age_s is None else f'{estimate_age_s:.6f}',
            '' if prediction_error_m is None else f'{prediction_error_m:.6f}',
            int(tracking_valid),
            self._state,
            int(self._target_visible),
            int(self._contact_active),
            f'{self._path_progress_m:.6f}',
        ])
        self._sample_stream.flush()

    def _update_movement_progress(self, now_s: float) -> None:
        person = self._person_pose
        if person is None:
            return
        previous = self._previous_person_pose
        self._previous_person_pose = person
        if previous is None:
            return
        moved_m = math.hypot(person.x - previous.x, person.y - previous.y)
        if self._measurement_start_s is None:
            if moved_m < float(
                self.get_parameter('movement_threshold_m').value
            ):
                return
            self._measurement_start_s = now_s
            self._path_progress_m = moved_m
            self._write_event(
                'measurement_started',
                now_s,
                measurement_duration_s=self._measurement_duration_s,
            )
            self.get_logger().info(
                'Actor started moving; tracking and measurement began'
            )
            self._try_start_following()
            return
        self._path_progress_m += moved_m

    def _write_event(self, event: str, sim_time_s: float, **fields) -> None:
        if self._event_stream.closed:
            return
        record = {'event': event, 'sim_time_s': sim_time_s, **fields}
        self._event_stream.write(
            json.dumps(record, ensure_ascii=False, separators=(',', ':'))
            + '\n'
        )
        self._event_stream.flush()

    def _finalize(self, status: str, reason: str) -> None:
        if self._finalized:
            return
        self._finalized = True
        now_s = self._now_seconds()
        if self._measurement_end_s is None:
            self._measurement_end_s = now_s
        if self._action_goal_handle is not None:
            self._action_goal_handle.cancel_goal_async()
        duration_s = (
            None
            if self._measurement_start_s is None
            else max(0.0, self._measurement_end_s - self._measurement_start_s)
        )
        prediction_mae = (
            None
            if self._prediction_sample_count == 0
            else self._prediction_error_sum / self._prediction_sample_count
        )
        summary = {
            'status': status,
            'reason': reason,
            'scenario': {
                'name': self._scenario_name,
                'world': self._world_name,
                'trajectory': self._trajectory_name,
                'desired_distance_m': self._desired_distance_m,
                'configured_duration_s': self._measurement_duration_s,
                'duration_s': duration_s,
                'path_progress_m': self._path_progress_m,
            },
            'metrics': {
                'collision_count': self._collision_count,
                'distance_mae_m': (
                    None
                    if self._sample_count == 0
                    else self._distance_error_sum / self._sample_count
                ),
                'target_tracking_ratio': (
                    None
                    if self._sample_count == 0
                    else self._tracking_valid_count / self._sample_count
                ),
                'prediction_horizon_d_m': prediction_mae,
                'prediction_horizon_t_ratio': (
                    None
                    if self._sample_count == 0
                    else self._prediction_outside_count / self._sample_count
                ),
                'end_to_end_latency_ms': {
                    'median': (
                        None
                        if not self._latencies_ms
                        else statistics.median(self._latencies_ms)
                    ),
                    'p95': _nearest_rank(self._latencies_ms, 0.95),
                    'max': (
                        None
                        if not self._latencies_ms
                        else max(self._latencies_ms)
                    ),
                    'sample_count': len(self._latencies_ms),
                },
            },
            'sample_count': self._sample_count,
            'prediction_sample_count': self._prediction_sample_count,
        }
        (self._result_directory / 'summary.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        self._write_event('benchmark_finished', now_s, status=status)
        self._sample_stream.close()
        self._event_stream.close()
        self.get_logger().info(
            f'Benchmark {status}: {reason}; '
            f'summary={self._result_directory / "summary.json"}'
        )
        self._shutdown_timer = self.create_timer(
            0.1, self._shutdown_after_finalize
        )

    def _shutdown_after_finalize(self) -> None:
        if self._shutdown_timer is not None:
            self._shutdown_timer.cancel()
        if rclpy.ok():
            rclpy.shutdown()

    def _now_seconds(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9

    def destroy_node(self):
        if not self._finalized:
            self._finalize('INTERRUPTED', 'benchmark node stopped')
        self._follow_client.destroy()
        return super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = PersonTrackingBenchmark()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
