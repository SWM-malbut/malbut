"""
Own Bringup processes and manually restart only confirmed stopped children.

The launch file supplies the original stage order, probes and Nav2 component
loader. Commands and environments stay in memory, never in a ROS message/file.
Only a manual request may also restart an owned, ROS-unresponsive Nav2 container.
There is no watchdog, automatic retry, broad process kill or motion command.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import signal
import sys
import threading
import time

from geometry_msgs.msg import PoseWithCovarianceStamped
from launch import LaunchDescription, LaunchService
from launch.actions import ExecuteProcess, IncludeLaunchDescription, OpaqueFunction
from launch.actions import RegisterEventHandler
from launch.event_handlers import OnProcessExit, OnProcessStart, OnShutdown
from launch.events import matches_action
from launch.events.process import ShutdownProcess, SignalProcess
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node as LaunchNode
from malbut_interfaces.action import ExecuteMission
import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.signals import SignalHandlerOptions
from std_msgs.msg import String
import yaml

from .lifecycle_recovery import LifecycleRecovery, LifecycleUnavailable


@dataclass
class ProcessRecord:
    """An owned process; ROS graph absence alone never proves it has exited."""

    action: object
    stage: tuple
    details: dict
    probe: bool = False
    required: bool = True
    returncode: int | None = None
    started: bool = True
    followup: object = None
    lifecycle: object = None
    pid: int | None = None
    restart_unresponsive: bool = False


class RecoveryOwner(Node):
    """Expose one manual Action while the original LaunchService stays alive."""

    def __init__(self, service):
        super().__init__('bringup_recovery')
        self.service = service
        self.condition = threading.Condition(threading.RLock())
        self.records = []
        self.by_action = {}
        self.launch_context = None
        self.busy = False
        self.stopping = False
        self.localization = {}
        self.pose = None
        self.current_probe = None
        self.lifecycle_recovery = LifecycleRecovery(self)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(String, '/malbut/localization/state', self._localization, latched)
        self.create_subscription(PoseWithCovarianceStamped, '/amcl_pose', self._pose, latched)
        self.server = ActionServer(
            self, ExecuteMission, '/malbut/bringup/recover',
            goal_callback=self._goal, cancel_callback=lambda _: CancelResponse.ACCEPT,
            execute_callback=self._execute, callback_group=ReentrantCallbackGroup())

    def _localization(self, message):
        try:
            state = json.loads(message.data)
            if isinstance(state, dict):
                with self.condition:
                    if state.get('map') != self.localization.get('map'):
                        self.pose = None
                    self.localization = state
        except (ValueError, TypeError):
            pass

    def _pose(self, message):
        with self.condition:
            self.pose = message

    def complete(self):
        """Only an initially successful Bringup can enter manual recovery."""
        return self.launch_context is not None and getattr(
            self.launch_context.locals, 'malbut_startup_complete', False)

    def started(self, event, context):
        """Remember fully expanded launch commands without publishing secrets."""
        with self.condition:
            self.launch_context = context
            record = self.by_action.get(event.action)
            if record is None:
                if not isinstance(event.action, LaunchNode):
                    return []  # Speech preflight / vendor one-shot subprocesses.
                raw = context.launch_configurations.get('malbut_startup_stage')
                if raw is None:
                    return []  # Optional measurement and the owner are not stages.
                stage = tuple(json.loads(raw))
                record = ProcessRecord(
                    event.action, stage, {},
                    probe=getattr(event.action, '_malbut_readiness_probe', False),
                    followup=getattr(event.action, '_malbut_recovery_followup', None),
                    lifecycle=getattr(event.action, '_malbut_recovery_lifecycle', None),
                    restart_unresponsive=getattr(
                        event.action, '_malbut_restart_unresponsive', False))
                self.records.append(record)
                self.by_action[event.action] = record
            record.details = dict(cmd=event.cmd, cwd=event.cwd, env=event.env,
                                  name=record.details.get('name', event.process_name))
            record.started = True
            record.pid = getattr(event, 'pid', None)
            record.returncode = None
            self.condition.notify_all()
        return []

    def exited(self, event, context):
        """Record exits; initial failures retain the launch's fail-fast behavior."""
        with self.condition:
            record = self.by_action.get(event.action)
            if record is not None and record.action is event.action:
                record.returncode = event.returncode
                if record.lifecycle is not None:
                    self.lifecycle_recovery.process_exited()
                if (not self.complete() and not record.probe and event.returncode == 0
                        and record.lifecycle is None):
                    record.required = False  # Successful one-shot initialization.
                self.condition.notify_all()
        return []

    def _goal(self, request):
        with self.condition:
            if (request.capability_id != 'recovery'
                    or request.arguments_yaml.strip() not in ('', '{}')
                    or self.busy or self.stopping or not self.recoverable()
                    or self.lifecycle_recovery.busy
                    or (self.current_probe is not None
                        and self.current_probe.returncode is None)):
                return GoalResponse.REJECT
            self.busy = True
            return GoalResponse.ACCEPT

    def recoverable(self):
        """Allow manual recovery after READY or after a reached startup gate failed."""
        return self.complete() or (self.launch_context is not None and getattr(
            self.launch_context.locals, 'malbut_startup_failed', False))

    def _launch(self, record, goal):
        """Schedule on the launch loop; recheck the exit before spawning."""
        with self.condition:
            if record.returncode is None:
                return
            record.started = False  # Do not mistake the old probe's exit for this run.

        def restart(context):
            with self.condition:
                if self.stopping or goal.is_cancel_requested:
                    return []
                if record.returncode is None:
                    return []
                original = record.details
                action = ExecuteProcess(
                    cmd=original['cmd'], cwd=original['cwd'], env=original['env'],
                    name='recovery_child', output='screen')
                record.action = action
                record.returncode = None
                record.started = False
                self.by_action[action] = record
                extra = (record.followup(dict(self.localization), self.pose)
                         if record.followup else [])
                return [action, *extra]
        self.service.include_launch_description(LaunchDescription([
            OpaqueFunction(function=restart)]))

    def _stop_unresponsive(self, record, goal, timeout):
        """Target only this owner's Nav2 action and wait for its actual exit event."""
        with self.condition:
            if (not any(item is record for item in self.records)
                    or not record.restart_unresponsive or record.lifecycle is None):
                raise RuntimeError(
                    'Unresponsive process is not an owned recoverable Nav2 container')
            if goal.is_cancel_requested or self.stopping:
                raise RuntimeError('Recovery canceled before container shutdown')
            if record.returncode is not None:
                return
            if not record.started:
                raise RuntimeError('Nav2 has not started; cannot confirm shutdown target')
            # ROS launch owns SIGINT -> SIGTERM -> SIGKILL escalation and cancels
            # its timers on exit. No PID-name search, killall or whole-Bringup kill.
            self.service.emit_event(ShutdownProcess(process_matcher=matches_action(record.action)))
            deadline = time.monotonic() + timeout
            while record.returncode is None:
                if goal.is_cancel_requested or self.stopping:
                    raise RuntimeError(
                        'Recovery canceled; requested container shutdown may finish')
                if time.monotonic() >= deadline:
                    raise RuntimeError('Nav2 exit was not confirmed; replacement was NOT started')
                self.condition.wait(timeout=0.1)
            self.lifecycle_recovery.process_exited()

    def _feedback(self, goal, state, stage, restarted, lifecycle=None):
        goal.publish_feedback(ExecuteMission.Feedback(
            mission_id=bytes(goal.goal_id.uuid).hex(), state='RUNNING',
            feedback_yaml=yaml.safe_dump(dict(
                state=state, stage=stage, restarted=list(restarted),
                lifecycle=lifecycle), allow_unicode=True)))

    def _continue_startup(self, goal):
        """Continue unstarted groups only after every reached group passed recovery."""
        if self.complete():
            return
        callback, timeout = self.launch_context.locals.malbut_startup_resume
        dispatched = threading.Event()

        def resume(context):
            if self.stopping or goal.is_cancel_requested:
                dispatched.set()
                return []
            actions = callback(context)
            dispatched.set()
            return actions

        self.service.include_launch_description(LaunchDescription([
            OpaqueFunction(function=resume)]))
        deadline = time.monotonic() + timeout
        with self.condition:
            while not self.complete():
                if self.stopping or goal.is_cancel_requested:
                    raise RuntimeError('Recovery canceled while startup was continuing')
                if dispatched.is_set() and getattr(
                        self.launch_context.locals, 'malbut_startup_failed', False):
                    raise RuntimeError('A later Bringup stage failed; startup remains paused')
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        'Continued Bringup did not become ready before its deadline')
                self.condition.wait(timeout=0.1)

    def _execute(self, goal):
        restarted = []
        passed = []
        lifecycle = []
        stage_label = ''
        message = ''
        success = False
        try:
            # Fixed baseline from the original successful startup. Optional/off
            # features and completed one-shots never become recovery targets.
            with self.condition:
                stages = sorted({r.stage for r in self.records if r.required or r.probe})
                if (self.localization.get('mode') == 'ERROR'
                        and 'slam_toolbox exited' in self.localization.get('message', '')):
                    raise RuntimeError(
                        'SLAM exited: unsaved in-memory map cannot be recovered; '
                        'explicitly select a saved map or start a new mapping session')
            for stage in stages:
                stage_label = stage[1]
                if goal.is_cancel_requested or self.stopping:
                    break
                self._feedback(goal, 'CHECKING', stage_label, restarted)
                with self.condition:
                    targets = [r for r in self.records
                               if r.stage == stage and r.required and not r.probe]
                    probes = [r for r in self.records if r.stage == stage and r.probe]
                    stopped = [r for r in targets if r.returncode is not None]
                if not probes:
                    raise RuntimeError(f'{stage_label}: readiness probe is unavailable')
                for record in stopped:
                    restarted.append(record.details['name'])
                    self._feedback(goal, 'RESTARTING', stage_label, restarted)
                    self._launch(record, goal)
                # Live does not imply ACTIVE. Repair responsive Nav2 groups before
                # asking the read-only probe to verify TF, costmaps and Actions.
                deadline = time.monotonic() + self._probe_timeout(probes[0])
                self.lifecycle_recovery.response_timeout_s = self._probe_parameter(
                    probes[0], 'sensor_timeout_s', 3.0)

                def report(observation):
                    snapshot = dict(observation)
                    snapshot['process'] = dict(
                        name=record.details['name'], pid=record.pid,
                        state=('EXITED' if record.returncode is not None else
                               'RUNNING' if record.started else 'STARTING'),
                        returncode=record.returncode)
                    lifecycle.append(snapshot)
                    self._feedback(goal, snapshot['state'], stage_label, restarted, snapshot)

                for record in targets:
                    if record.lifecycle is not None:
                        # Distinguish process death during a ROS call from a live
                        # process that cannot answer. Neither warrants a duplicate.
                        def interrupted():
                            if record.started and record.returncode is not None:
                                raise RuntimeError(
                                    f'{record.details["name"]} exited during lifecycle recovery '
                                    f'(code {record.returncode}); retry manual recovery')
                            return self.stopping or goal.is_cancel_requested

                        with self.condition:
                            specs = record.lifecycle(dict(self.localization), self.pose)
                        try:
                            for spec in specs:
                                self.lifecycle_recovery.recover(
                                    spec, deadline, interrupted, report)
                        except LifecycleUnavailable as error:
                            if (not record.restart_unresponsive or record in stopped
                                    or error.reason == 'deadline_exceeded'):
                                raise
                            self._feedback(goal, 'STOPPING_UNRESPONSIVE', stage_label, restarted)
                            self._stop_unresponsive(record, goal, self._probe_timeout(probes[0]))
                            restarted.append(record.details['name'])
                            self._feedback(goal, 'RESTARTING', stage_label, restarted)
                            self._launch(record, goal)
                            deadline = time.monotonic() + self._probe_timeout(probes[0])
                            # Exactly one restart per manual request. If the new
                            # container also fails, report failure, never loop.
                            for spec in specs:
                                self.lifecycle_recovery.recover(
                                    spec, deadline, interrupted, report)
                # Fresh process, subscriptions and TF buffer on every pass;
                # never trust the initial launch's cached READY result.
                probe = probes[0]
                self.current_probe = probe
                self._launch(probe, goal)
                # The probe has its original per-stage timeout. Also bound a
                # failed process creation that never produces ProcessStarted.
                deadline = time.monotonic() + self._probe_timeout(probe) + 5.0
                with self.condition:
                    while not self.stopping and not goal.is_cancel_requested:
                        self.condition.wait(timeout=0.1)
                        if probe.started and probe.returncode is not None:
                            if probe.returncode != 0:
                                raise RuntimeError(
                                    f'{stage_label}: readiness failed; see Bringup log')
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f'{stage_label}: readiness timed out')
                    else:
                        break
                    failed = [r.details['name'] for r in targets
                              if r.returncode is not None or not r.started]
                    if failed:
                        raise RuntimeError(f'{stage_label}: exited again: {", ".join(failed)}')
                passed.append(stage_label)
                self.current_probe = None
                self._feedback(goal, 'READY' if stopped else 'PASS', stage_label, restarted)
            if not goal.is_cancel_requested and not self.stopping:
                self._continue_startup(goal)
                with self.condition:
                    failed = [r.details['name'] for r in self.records
                              if r.required and not r.probe
                              and (r.returncode is not None or not r.started)]
                if failed:
                    raise RuntimeError('Stopped during recovery: ' + ', '.join(failed))
                success = True
                message = 'Bringup recovery complete; no previous mission was resumed'
        except Exception as error:
            message = str(error)
        finally:
            # Cancel only our short-lived read-only probe, never a runtime node.
            probe = self.current_probe
            if probe is not None and probe.returncode is None and not self.stopping:
                self.service.emit_event(SignalProcess(
                    signal_number=signal.SIGINT, process_matcher=matches_action(probe.action)))
            if probe is not None and probe.returncode is not None:
                self.current_probe = None
            with self.condition:
                self.busy = False
        canceled = goal.is_cancel_requested
        if canceled:
            success = False
            goal.canceled()
            message = 'Recovery canceled; already restarted nodes remain running'
        elif success:
            goal.succeed()
        else:
            goal.abort()
            message = message or 'Bringup is stopping'
        return ExecuteMission.Result(
            mission_id=bytes(goal.goal_id.uuid).hex(), message=message,
            result_yaml=yaml.safe_dump(
                dict(success=success, message=message, restarted=restarted,
                     passed=passed, stage=stage_label, lifecycle=lifecycle), allow_unicode=True))

    @staticmethod
    def _probe_timeout(record):
        """Read the already-resolved probe parameter file, not another timeout knob."""
        return RecoveryOwner._probe_parameter(record, 'startup_timeout_s', 120.0)

    @staticmethod
    def _probe_parameter(record, name, default):
        command = record.details['cmd']
        for index, item in enumerate(command[:-1]):
            if item == '--params-file':
                with open(command[index + 1], encoding='utf-8') as stream:
                    document = yaml.safe_load(stream)
                for node in document.values():
                    value = node.get('ros__parameters', {}).get(name)
                    if value is not None:
                        return float(value)
        return default


def main(args=None):
    """Run a supplied Bringup launch file with ownership retained for recovery."""
    arguments = list(sys.argv[1:] if args is None else args)
    if not arguments or not Path(arguments[0]).is_file():
        raise SystemExit('managed_bringup requires an existing launch file')
    launch_file, *settings = arguments
    launch_arguments = [item.split(':=', 1) for item in settings]
    if any(len(item) != 2 for item in launch_arguments):
        raise SystemExit('launch arguments must use name:=value')
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    service = LaunchService()
    owner = RecoveryOwner(service)
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(owner)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()

    def bind(context):
        owner.launch_context = context
        context.extend_globals({'malbut_recovery_owner': True})
        return []

    def shutdown(event, context):
        with owner.condition:
            owner.stopping = True
            owner.condition.notify_all()
        return []

    service.include_launch_description(LaunchDescription([
        OpaqueFunction(function=bind),
        RegisterEventHandler(OnProcessStart(on_start=owner.started)),
        RegisterEventHandler(OnProcessExit(on_exit=owner.exited)),
        RegisterEventHandler(OnShutdown(on_shutdown=shutdown)),
        IncludeLaunchDescription(PythonLaunchDescriptionSource(launch_file),
                                 launch_arguments=launch_arguments),
    ]))
    try:
        result = service.run()
    finally:
        with owner.condition:
            owner.stopping = True
            owner.condition.notify_all()
        executor.shutdown(timeout_sec=5.0)
        owner.destroy_node()
        rclpy.shutdown()
        thread.join(timeout=5.0)
    return result
