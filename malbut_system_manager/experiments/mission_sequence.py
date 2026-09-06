#!/usr/bin/env python3
"""Exercise installed applications through the manager using timed requests."""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import signal
import time

from action_msgs.msg import GoalStatus, GoalStatusArray
from map_msgs.msg import OccupancyGridUpdate
from malbut_interfaces.action import ExecuteMission, FollowPerson, Patrol
from malbut_interfaces.msg import MissionStatus, SystemState
from nav2_msgs.action import ComputePathToPose, NavigateToPose, Spin
from nav_msgs.msg import OccupancyGrid, Odometry
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data,
)
from rclpy.signals import SignalHandlerOptions
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import CameraInfo, Image, LaserScan
from std_msgs.msg import String
from tf2_ros import Buffer, TransformException, TransformListener
from vision_msgs.msg import Detection3DArray
import yaml


GROUPS = (
    'active_foreground_missions', 'active_background_missions',
    'suspended_missions', 'pending_missions',
)
LIVE = {GoalStatus.STATUS_ACCEPTED, GoalStatus.STATUS_EXECUTING,
        GoalStatus.STATUS_CANCELING}


class MissionSequence(Node):
    """Record public evidence; never import application or manager internals."""

    def __init__(self, options):
        super().__init__('manager_mission_sequence')
        self.options = options
        self.started = time.monotonic()
        self.sim_time = None
        self.clock_changes = 0
        self.output = Path(options.output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.log = (self.output / 'events.jsonl').open('w', encoding='utf-8')
        self.phase = 'startup'
        self.latest = {}
        self.state = None
        self.missions = {}
        self.feedback_states = defaultdict(set)
        self.child_status = {}
        self.child_seen = defaultdict(set)
        self.checks = []
        self.fatal = None
        self.previous_xy = None
        self.motion = defaultdict(float)
        self.stationary_since = None
        self.last_odom_log = 0.0
        self.last_odom = 0.0
        self.coverage = []
        self.visible_feedback = 0
        self.visible_by_mission = defaultdict(int)
        self.detection_frames = 0
        self.person_detection_frames = 0
        self.transforms = Buffer()
        self.transform_listener = TransformListener(self.transforms, self)
        self.manager = ActionClient(self, ExecuteMission, '/malbut/mission/execute')
        self.servers = {
            name: ActionClient(self, action_type, name)
            for name, action_type in (
                ('/patrol', Patrol), ('/follow_person', FollowPerson),
                ('/navigate_to_pose', NavigateToPose), ('/spin', Spin),
                ('/compute_path_to_pose', ComputePathToPose),
            )
        }
        latched = QoSProfile(
            depth=10, reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(SystemState, '/malbut/state', self.on_state, latched)
        self.create_subscription(String, '/patrol/status', self.on_patrol, 10)
        self.create_subscription(Odometry, '/odom', self.on_odom, qos_profile_sensor_data)
        self.create_subscription(Clock, '/clock', self.on_clock, qos_profile_sensor_data)
        self.create_subscription(Detection3DArray, '/perception/person/detections_3d',
                                 self.on_detections, qos_profile_sensor_data)
        for name, message_type, topic, qos in (
            ('map', OccupancyGrid, '/map', latched),
            ('costmap', OccupancyGrid, '/global_costmap/costmap', latched),
            ('costmap_update', OccupancyGridUpdate,
             '/global_costmap/costmap_updates', 10),
            ('rgb', Image, '/camera/color/image_raw', qos_profile_sensor_data),
            ('camera_info', CameraInfo, '/camera/color/camera_info', qos_profile_sensor_data),
            ('scan', LaserScan, '/scan', qos_profile_sensor_data),
        ):
            self.create_subscription(message_type, topic,
                                     lambda msg, key=name: self.on_sensor(key, msg), qos)
        for name in ('/patrol', '/follow_person', '/navigate_to_pose', '/spin'):
            self.create_subscription(GoalStatusArray, name + '/_action/status',
                                     lambda msg, key=name: self.on_child(key, msg), latched)
        self.record('configuration', arguments=vars(options))

    def record(self, event, **fields):
        """Write wall-clock elapsed and separately labelled simulation time."""
        row = {'elapsed_s': time.monotonic() - self.started,
               'wall_time_ns': time.time_ns(), 'sim_time_s': self.sim_time,
               'phase': self.phase, 'event': event, **fields}
        self.log.write(json.dumps(row, ensure_ascii=False) + '\n')
        self.log.flush()

    def on_clock(self, message):
        """Require a progressing simulator clock instead of a stale sample."""
        value = message.clock.sec + message.clock.nanosec * 1e-9
        if value != self.sim_time:
            self.clock_changes += 1
        self.sim_time = value
        self.latest['clock'] = time.monotonic()

    def on_sensor(self, name, message):
        """Check arrival without writing image or scan payloads into JSON."""
        if name in {'map', 'costmap'} and not message.info.width:
            return
        if name == 'camera_info' and message.k[0] <= 0:
            return
        if name not in self.latest:
            self.record('sensor_first_received', sensor=name,
                        frame_id=message.header.frame_id)
        self.latest[name] = time.monotonic()

    def transform_ready(self):
        """Require the latest map transforms actually used by the applications."""
        try:
            for frame in ('base_footprint', 'camera_depth_optical_frame'):
                self.transforms.lookup_transform('map', frame, rclpy.time.Time())
            return True
        except TransformException:
            return False

    def on_state(self, message):
        """Record aggregate state and reject overlapping BASE mission owners."""
        self.state = message
        groups = {
            group: [{'id': m.mission_id, 'capability': m.capability_id,
                     'state': m.state} for m in getattr(message, group)]
            for group in GROUPS
        }
        self.record('manager_state', system_state=message.system_state, **groups)
        base_owners = [
            m for group in GROUPS[:2] for m in getattr(message, group)
            if m.capability_id in {'patrol', 'navigate_to_pose', 'follow_person'}
            and m.state in {MissionStatus.RUNNING, MissionStatus.CANCELING}
        ]
        if len(base_owners) > 1:
            self.fatal = 'Multiple registered BASE missions active at once'

    def on_child(self, name, message):
        """Keep UUID and status transitions from real downstream servers."""
        values = {bytes(item.goal_info.goal_id.uuid).hex(): item.status
                  for item in message.status_list}
        if values != self.child_status.get(name):
            self.record('downstream_status', action=name, goals=values)
        self.child_status[name] = values
        self.child_seen[name].update(values)

    def on_patrol(self, message):
        """Capture coverage reports without equating them with visual proof."""
        try:
            value = json.loads(message.data)
        except ValueError:
            value = {'raw': message.data}
        self.coverage.append(value)
        self.record('patrol_status', payload=value)

    def on_detections(self, message):
        """Separate detector availability from follower target acquisition."""
        self.detection_frames += 1
        self.person_detection_frames += bool(message.detections)
        self.record('detection_frame', count=len(message.detections),
                    source_stamp_s=message.header.stamp.sec
                    + message.header.stamp.nanosec * 1e-9)

    def on_odom(self, message):
        """Accumulate actual odometry displacement and final stop evidence."""
        now = time.monotonic()
        point = message.pose.pose.position
        xy = (point.x, point.y)
        if self.previous_xy is not None:
            self.motion[self.phase] += math.dist(xy, self.previous_xy)
        self.previous_xy = xy
        if now - self.last_odom >= 1.0:
            self.stationary_since = None
        self.last_odom = now
        speed = message.twist.twist
        stopped = math.hypot(speed.linear.x, speed.linear.y) < 0.01
        stopped = stopped and abs(speed.angular.z) < 0.02
        self.stationary_since = (self.stationary_since or now) if stopped else None
        if now - self.last_odom_log >= 0.2:
            self.record('odometry', x=xy[0], y=xy[1],
                        vx=speed.linear.x, vy=speed.linear.y, wz=speed.angular.z)
            self.last_odom_log = now

    def wait(self, condition, description, timeout=None):
        """Spin until evidence arrives; all deadlines use monotonic wall time."""
        deadline = time.monotonic() + (timeout or self.options.settle_timeout)
        while rclpy.ok() and not condition():
            if self.fatal and self.phase != 'cleanup':
                raise AssertionError(self.fatal)
            if time.monotonic() >= deadline:
                raise TimeoutError(description)
            rclpy.spin_once(self, timeout_sec=0.05)
        if not rclpy.ok():
            raise RuntimeError('ROS context stopped during experiment')

    def check(self, condition, description):
        """Store an explicit assertion, distinct from measured application data."""
        if not condition:
            raise AssertionError(description)
        self.checks.append(description)
        self.record('check_passed', check=description)
        print('PASS:', description, flush=True)

    def hold(self, phase, duration=None):
        """Leave a human-like quiet gap while all callbacks keep processing."""
        self.phase = phase
        duration = self.options.gap_seconds if duration is None else duration
        self.record('hold_begin', duration_s=duration)
        print(f'{phase}: {duration:g}s', flush=True)
        end = time.monotonic() + duration
        self.wait(lambda: time.monotonic() >= end, 'hold', duration + 1.0)

    def startup(self):
        """Wait for actual installed servers and live simulator sensor streams."""
        def ready():
            now = time.monotonic()
            sensors = ('clock', 'rgb', 'scan', 'camera_info')
            costmap_age = now - max(
                self.latest.get('costmap', 0.0), self.latest.get('costmap_update', 0.0))
            return (self.manager.server_is_ready()
                    and all(client.server_is_ready() for client in self.servers.values())
                    and 'map' in self.latest and self.clock_changes > 1
                    and all(now - self.latest.get(key, 0.0) < 2.0 for key in sensors)
                    and 'costmap' in self.latest and costmap_age < 3.0
                    and self.transform_ready()
                    and now - self.last_odom < 5.0 and self.state is not None)
        self.wait(ready, 'Startup: servers, map, clock, camera, scan, odom',
                  self.options.startup_timeout)
        self.check(self.is_idle(), 'Manager starts IDLE without existing missions')

    def feedback(self, label, message):
        """Preserve manager lifecycle and the application's forwarded feedback."""
        feedback = message.feedback
        self.feedback_states[label].add(feedback.state)
        payload = yaml.safe_load(feedback.feedback_yaml) or {}
        if label in {'follow', 'initial_follow'} and payload.get('target_visible') is True:
            self.visible_feedback += 1
            self.visible_by_mission[label] += 1
        self.record('mission_feedback', label=label, mission_id=feedback.mission_id,
                    state=feedback.state, payload=payload)

    def submit(self, label, capability_id, arguments):
        """Request a managed mission, retaining its handle for owned-only cleanup."""
        self.record('request', label=label, capability=capability_id, arguments=arguments)
        goal = ExecuteMission.Goal(capability_id=capability_id,
                                   arguments_yaml=yaml.safe_dump(arguments))
        future = self.manager.send_goal_async(
            goal, feedback_callback=lambda msg: self.feedback(label, msg))
        mission = {'send': future, 'handle': None, 'result': None}
        self.missions[label] = mission

        def accepted(done):
            handle = done.result()
            mission['handle'] = handle
            if handle.accepted:
                result = handle.get_result_async()
                mission['result'] = result
                result.add_done_callback(lambda item: self.on_result(label, item))
        future.add_done_callback(accepted)
        self.wait(lambda: mission['handle'] is not None, f'{label} goal response')
        self.check(mission['handle'].accepted, f'{label}: manager accepted request')
        return mission

    def on_result(self, label, future):
        """Record actual ROS terminal status and the unmodified public result."""
        result = future.result()
        self.record('mission_result', label=label, status=result.status,
                    message=result.result.message, result_yaml=result.result.result_yaml)

    def running(self, label):
        """Find a requested mission in the public active mission collections."""
        mission = self.missions.get(label, {})
        handle = mission.get('handle')
        if self.state is None or handle is None:
            return False
        identifier = bytes(handle.goal_id.uuid).hex()
        return any(m.mission_id == identifier and m.state == MissionStatus.RUNNING
                   for group in GROUPS[:2] for m in getattr(self.state, group))

    def done(self, label):
        """Return whether the public mission has an actual terminal result."""
        future = self.missions[label]['result']
        return future is not None and future.done()

    def cancel(self, label):
        """Cancel only this client's goal, then await its terminal cancellation."""
        if self.done(label):
            return
        self.record('cancel_request', label=label)
        future = self.missions[label]['handle'].cancel_goal_async()
        self.wait(future.done, f'{label}: cancel response')
        self.check(bool(future.result().goals_canceling) or self.done(label),
                   f'{label}: cancellation accepted or already terminal')
        self.wait(lambda: self.done(label), f'{label}: terminal cancellation')
        self.check(self.missions[label]['result'].result().status == GoalStatus.STATUS_CANCELED,
                   f'{label}: terminal CANCELED')

    def is_idle(self):
        """Require IDLE plus all active, pending and suspended collections empty."""
        return (self.state is not None and self.state.system_state == SystemState.IDLE
                and all(not getattr(self.state, group) for group in GROUPS))

    def stopped(self):
        """Require downstream terminal states and fresh stationary odometry."""
        now = time.monotonic()
        return (self.is_idle() and self.stationary_since is not None
                and now - self.stationary_since >= 3.0 and now - self.last_odom < 1.0
                and all(status not in LIVE for goals in self.child_status.values()
                        for status in goals.values()))

    def run_sequence(self):
        """Run patrol, equal-priority preemption, follow, resume, and full stop."""
        self.startup()
        if self.options.initial_follow_seconds > 0:
            self.submit('initial_follow', 'follow_person', {
                'target_mode': 0, 'target_person_id': '', 'desired_distance_m': 1.0})
            self.wait(lambda: self.running('initial_follow')
                      and bool(self.child_seen['/follow_person']),
                      'Initial follower dispatched while actor enters')
            self.hold('initial_follow', self.options.initial_follow_seconds)
            self.check(self.visible_by_mission['initial_follow'] > 0,
                       'Initial follow acquired a visible target through real perception')
            self.cancel('initial_follow')
            self.wait(self.stopped, 'Initial follow canceled back to stationary IDLE')
            self.check(True, 'Initial follow cancellation returns to stationary IDLE')
        self.submit('patrol', 'patrol', {'thoroughness': 0})
        self.wait(lambda: self.running('patrol') and bool(self.child_seen['/patrol']),
                  'Patrol dispatched to application')
        self.check(True, 'Patrol RUNNING and downstream goal observed')
        self.hold('patrol')
        self.check(not self.done('patrol'), 'Patrol remains active before preemption')

        yaw = self.options.goal_yaw
        original_patrol_goals = set(self.child_seen['/patrol'])
        self.submit('navigate', 'navigate_to_pose', {'pose': {
            'header': {'frame_id': 'map'}, 'pose': {
                'position': {'x': self.options.goal_x, 'y': self.options.goal_y},
                'orientation': {'z': math.sin(yaw / 2), 'w': math.cos(yaw / 2)},
            }}})
        self.wait(lambda: 'SUSPENDED' in self.feedback_states['patrol']
                  and (self.running('navigate') or self.done('navigate')),
                  'Equal-priority navigation suspends patrol')
        self.check(True, 'Equal priority: new navigation preempts and suspends patrol')
        self.wait(lambda: all(self.child_status.get('/patrol', {}).get(goal) not in LIVE
                              for goal in original_patrol_goals),
                  'Patrol application really terminates before replacement')
        self.check(True, 'Preempted patrol downstream goal is terminal')
        self.hold('navigate')

        prior = 'navigate' if self.running('navigate') else 'patrol'
        self.check(self.running(prior), 'A foreground mission exists before follow request')
        previous_follow_goals = set(self.child_seen['/follow_person'])
        self.submit('follow', 'follow_person', {
            'target_mode': 0, 'target_person_id': '', 'desired_distance_m': 1.0})
        self.wait(lambda: self.running('follow')
                  and bool(self.child_seen['/follow_person'] - previous_follow_goals),
                  'Follower dispatched through manager')
        self.check(True, 'Follow RUNNING and downstream goal observed')
        self.hold('follow')
        self.check(not self.done('follow'), 'Follow mission stays active until cancellation')
        self.cancel('follow')
        self.wait(lambda: self.running(prior) or self.running('patrol'),
                  'Previously suspended mission resumes')
        self.check(True, 'Suspended mission resumes after follow cancellation')
        self.hold('resumed')
        self.phase = 'stopping'
        self.cancel('navigate')
        self.cancel('patrol')
        self.wait(self.stopped, 'IDLE, terminal child goals and stationary odometry')
        self.check(True, 'All missions removed; children terminal; stationary for 3s')
        self.check(all(m['result'].result().status in {
            GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_CANCELED,
        } for m in self.missions.values()), 'No application mission ended ABORTED')
        self.check(self.fatal is None, 'No overlapping registered BASE owners observed')

    def cleanup_owned(self):
        """Attempt bounded cleanup even after an assertion or interrupted send."""
        self.phase = 'cleanup'
        for label, mission in reversed(list(self.missions.items())):
            try:
                self.wait(lambda: mission['handle'] is not None,
                          f'{label}: late response during cleanup')
                handle = mission['handle']
                if handle.accepted and not self.done(label):
                    handle.cancel_goal_async()
                    self.record('cleanup_cancel', label=label)
            except Exception as error:
                self.record('cleanup_error', label=label, error=str(error))
        try:
            self.wait(lambda: all(not m['handle'] or not m['handle'].accepted
                                  or self.done(label) for label, m in self.missions.items()),
                      'Owned goal cleanup')
        except Exception as error:
            self.record('cleanup_error', error=str(error))

    def write_summary(self, error):
        """Report lifecycle assertions separately from observed application evidence."""
        summary = {
            'passed': error is None, 'error': error, 'checks_passed': self.checks,
            'elapsed_wall_s': time.monotonic() - self.started,
            'application_evidence': {
                'odometry_distance_m_by_phase': dict(self.motion),
                'patrol_status_reports': len(self.coverage),
                'patrol_last_status': self.coverage[-1] if self.coverage else None,
                'detections_3d_frames': self.detection_frames,
                'detections_3d_nonempty_frames': self.person_detection_frames,
                'follow_target_visible_feedback_count': self.visible_feedback,
                'target_visible_feedback_by_mission': dict(self.visible_by_mission),
                'downstream_goal_counts': {k: len(v) for k, v in self.child_seen.items()},
                'mission_terminal_status': {
                    label: m['result'].result().status
                    for label, m in self.missions.items() if self.done(label)
                },
            },
            'scope': 'Real application lifecycle/preemption/cancellation smoke; '
                     'not complete patrol coverage or tracking accuracy validation. '
                     'Independent ROS topic reception does not prove sub-ms event order.',
        }
        (self.output / 'summary.json').write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        self.record('finished', passed=error is None, error=error)
        self.log.close()


def main():
    """Run against an already launched, isolated simulator and installed workspace."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--gap-seconds', type=float, default=12.0)
    parser.add_argument('--settle-timeout', type=float, default=30.0)
    parser.add_argument('--startup-timeout', type=float, default=120.0)
    parser.add_argument('--initial-follow-seconds', type=float, default=20.0)
    parser.add_argument('--goal-x', type=float, default=-3.665503)
    parser.add_argument('--goal-y', type=float, default=-0.4874)
    parser.add_argument('--goal-yaw', type=float, default=0.0)
    options = parser.parse_args()
    if any(not math.isfinite(value) or value <= 0 for value in (
            options.gap_seconds, options.settle_timeout, options.startup_timeout)):
        parser.error('Durations must be positive finite seconds')
    if not math.isfinite(options.initial_follow_seconds) or options.initial_follow_seconds < 0:
        parser.error('--initial-follow-seconds must be nonnegative finite seconds')
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    node = MissionSequence(options)
    error = None
    try:
        node.run_sequence()
    except (Exception, KeyboardInterrupt) as exception:
        error = f'{type(exception).__name__}: {exception}'
        node.record('failed', error=error)
        print('FAIL:', error, flush=True)
    finally:
        node.cleanup_owned()
        node.write_summary(error)
        node.destroy_node()
        rclpy.shutdown()
    return 0 if error is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
