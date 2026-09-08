"""ROS node exposing the unified Malbut mission execution contract."""

from dataclasses import dataclass
import math
import signal
from threading import RLock
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.signals import SignalHandlerOptions
from rclpy.task import Future

from malbut_interfaces.action import ExecuteMission
from malbut_interfaces.msg import MissionStatus, SystemState as SystemStateMsg

from .manifest_registry import (
    ManifestError,
    ManifestRegistry,
    RequestValidationError,
)
from .mission_executor import MissionExecutor
from .mission_scheduler import MissionScheduler
from .models import (
    ControlMode,
    ExecutionMode,
    MissionCompletion,
    MissionPriority,
    MissionRecord,
    MissionState,
    SchedulerEffects,
    SystemState,
    TerminalOutcome,
)
from .state_store import StateStore


EXECUTE_MISSION_ACTION = '/malbut/mission/execute'
STATE_TOPIC = '/malbut/state'


@dataclass
class _MissionContext:
    goal_handle: object
    completion: Future
    last_feedback_yaml: str = ''


class SystemManagerNode(Node):
    """Validate, schedule, and report registered Action or Service missions."""

    def __init__(
        self,
        *,
        manifest_directory: str | None = None,
    ) -> None:
        super().__init__('system_manager')
        self._lock = RLock()
        self._effects_lock = RLock()
        self._accepting_goals = False
        self._early_cancellations: set[str] = set()
        self._deferred_downstream_cancellations: set[str] = set()
        self._contexts: dict[str, _MissionContext] = {}
        self._state = StateStore()
        self._scheduler = MissionScheduler(self._state)
        self._server_group = ReentrantCallbackGroup()
        self._client_group = ReentrantCallbackGroup()

        configured_directory = self.declare_parameter(
            'manifest_directory',
            manifest_directory or '',
        ).value
        self._shutdown_timeout_s = float(
            self.declare_parameter('shutdown_timeout_s', 3.0).value
        )
        goal_response_timeout_s = float(
            self.declare_parameter('goal_response_timeout_s', 5.0).value
        )
        cancel_completion_timeout_s = float(
            self.declare_parameter(
                'cancel_completion_timeout_s',
                5.0,
            ).value
        )
        _require_positive_finite(
            'shutdown_timeout_s',
            self._shutdown_timeout_s,
        )
        _require_positive_finite(
            'goal_response_timeout_s',
            goal_response_timeout_s,
        )
        _require_positive_finite(
            'cancel_completion_timeout_s',
            cancel_completion_timeout_s,
        )
        state_qos = QoSProfile(depth=1)
        state_qos.reliability = ReliabilityPolicy.RELIABLE
        state_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self._state_publisher = self.create_publisher(
            SystemStateMsg,
            STATE_TOPIC,
            state_qos,
        )
        self._publish_state()

        self._registry = ManifestRegistry(configured_directory or None)
        self._executor_bridge = MissionExecutor(
            self,
            self._client_group,
            on_feedback=self._on_downstream_feedback,
            on_terminal=self._on_downstream_terminal,
            on_cancel_rejected=self._on_cancel_rejected,
            on_dispatch_timeout=self._on_dispatch_timeout,
            goal_response_timeout_s=goal_response_timeout_s,
            cancel_completion_timeout_s=cancel_completion_timeout_s,
        )
        self._action_server = ActionServer(
            self,
            ExecuteMission,
            EXECUTE_MISSION_ACTION,
            execute_callback=self._execute,
            goal_callback=self._goal,
            cancel_callback=self._cancel,
            callback_group=self._server_group,
        )

        with self._lock:
            effects = self._scheduler.set_ready(True)
            self._accepting_goals = True
        self._apply_effects(effects)
        self._publish_state()
        capabilities = ', '.join(
            manifest.capability_id for manifest in self._registry.all()
        )
        self.get_logger().info(
            f'System manager ready with capabilities: {capabilities}'
        )

    def destroy_node(self) -> bool:
        """Stop accepting work and release dynamic Action clients."""
        self._accepting_goals = False
        if hasattr(self, '_action_server'):
            self._action_server.destroy()
        if hasattr(self, '_executor_bridge'):
            self._executor_bridge.destroy()
        return super().destroy_node()

    def begin_shutdown(self) -> int:
        """Stop admission and cancel every non-terminal managed mission."""
        with self._effects_lock:
            self._accepting_goals = False
            with self._lock:
                effects = self._scheduler.request_shutdown()
            self._apply_effects(effects)
            self._publish_state()
            self._executor_bridge.begin_shutdown()
            return self._executor_bridge.active_count

    def force_shutdown(self) -> None:
        """Resolve manager goals that cannot finish during bounded shutdown."""
        with self._effects_lock:
            with self._lock:
                effects = self._scheduler.force_shutdown()
                completed_ids = {
                    completion.mission_id
                    for completion in effects.complete
                }
                for mission_id, context in self._contexts.items():
                    if (
                        mission_id not in completed_ids
                        and not context.completion.done()
                    ):
                        effects.complete.append(
                            MissionCompletion(
                                mission_id,
                                TerminalOutcome.ABORTED,
                                message='system manager shutdown timed out',
                            )
                        )
            self._apply_effects(effects)
            self._publish_state()

    @property
    def downstream_execution_count(self) -> int:
        """Return active downstream generations during bounded shutdown."""
        return self._executor_bridge.active_count

    @property
    def unresolved_context_count(self) -> int:
        """Return public Action requests without a terminal completion."""
        with self._lock:
            return sum(
                not context.completion.done()
                for context in self._contexts.values()
            )

    @property
    def public_context_count(self) -> int:
        """Return public Action callbacks that have not returned yet."""
        with self._lock:
            return len(self._contexts)

    @property
    def shutdown_timeout_s(self) -> float:
        """Return the configured bound for each shutdown phase."""
        return self._shutdown_timeout_s

    def _goal(self, request: ExecuteMission.Goal) -> GoalResponse:
        del request
        with self._effects_lock:
            accepting_goals = self._accepting_goals
        return GoalResponse.ACCEPT if accepting_goals else GoalResponse.REJECT

    def _cancel(self, goal_handle) -> CancelResponse:
        mission_id = _goal_uuid(goal_handle)
        with self._effects_lock:
            with self._lock:
                if mission_id not in self._contexts:
                    self._early_cancellations.add(mission_id)
                    return CancelResponse.ACCEPT
                accepted, effects = self._scheduler.request_cancel(mission_id)
            if accepted:
                self._apply_effects(effects)
                self._publish_state()
                return CancelResponse.ACCEPT
            return CancelResponse.REJECT

    async def _execute(self, goal_handle) -> ExecuteMission.Result:
        mission_id = _goal_uuid(goal_handle)
        try:
            manifest = self._registry.get(
                goal_handle.request.capability_id.strip()
            )
            arguments, _ = self._registry.parse_arguments(
                manifest,
                goal_handle.request.arguments_yaml,
            )
        except (ManifestError, RequestValidationError) as error:
            result = _public_result(mission_id, message=str(error))
            with self._effects_lock:
                with self._lock:
                    canceled_early = (
                        mission_id in self._early_cancellations
                        or goal_handle.is_cancel_requested
                    )
                    self._early_cancellations.discard(mission_id)
            if canceled_early:
                await self._wait_for_public_cancel_state(goal_handle)
                goal_handle.canceled()
            else:
                goal_handle.abort()
            return result

        context = _MissionContext(goal_handle, Future())
        mission = MissionRecord(mission_id, manifest, arguments)
        with self._effects_lock:
            with self._lock:
                self._contexts[mission_id] = context
                canceled_early = (
                    mission_id in self._early_cancellations
                    or goal_handle.is_cancel_requested
                )
                self._early_cancellations.discard(mission_id)
                if canceled_early:
                    effects = SchedulerEffects(
                        complete=[
                            MissionCompletion(
                                mission_id,
                                TerminalOutcome.CANCELED,
                                message='mission canceled before admission',
                            )
                        ]
                    )
                elif not self._accepting_goals:
                    effects = SchedulerEffects(
                        complete=[
                            MissionCompletion(
                                mission_id,
                                TerminalOutcome.ABORTED,
                                message='system manager is shutting down',
                            )
                        ]
                    )
                else:
                    effects = self._scheduler.submit(mission)

            self._apply_effects(effects)
            self._publish_state()
        completion: MissionCompletion = await context.completion

        result = _public_result(
            mission_id,
            result_yaml=completion.result_yaml,
            message=completion.message,
        )
        try:
            if completion.outcome is TerminalOutcome.SUCCEEDED:
                goal_handle.succeed()
            elif completion.outcome is TerminalOutcome.CANCELED:
                await self._wait_for_public_cancel_state(goal_handle)
                goal_handle.canceled()
            else:
                goal_handle.abort()
        finally:
            with self._lock:
                self._contexts.pop(mission_id, None)
        return result

    def _on_downstream_feedback(
        self,
        mission_id: str,
        generation: int,
        feedback_yaml: str,
    ) -> None:
        with self._effects_lock:
            with self._lock:
                mission = self._state.get(mission_id)
                context = self._contexts.get(mission_id)
                if (
                    mission is None
                    or context is None
                    or mission.generation != generation
                ):
                    return
                context.last_feedback_yaml = feedback_yaml
            self._publish_mission_feedback(mission_id)

    def _on_downstream_terminal(
        self,
        mission_id: str,
        generation: int,
        outcome: TerminalOutcome,
        result_yaml: str,
        message: str,
    ) -> None:
        with self._effects_lock:
            with self._lock:
                mission = self._state.get(mission_id)
                if mission is None or mission.generation != generation:
                    return
                self._deferred_downstream_cancellations.discard(mission_id)
                effects = self._scheduler.handle_terminal(
                    mission_id,
                    outcome,
                    result_yaml=result_yaml,
                    message=message,
                )
            self._apply_effects(effects)
            self._publish_state()

    def _on_cancel_rejected(
        self,
        mission_id: str,
        generation: int,
        message: str,
    ) -> None:
        with self._effects_lock:
            with self._lock:
                mission = self._state.get(mission_id)
                if mission is None or mission.generation != generation:
                    return
                effects = self._scheduler.handle_cancel_rejected(
                    mission_id,
                    message,
                )
            self.get_logger().warning(message)
            self._apply_effects(effects)
            self._publish_state()

    def _on_dispatch_timeout(
        self,
        mission_id: str,
        generation: int,
        message: str,
    ) -> None:
        with self._effects_lock:
            with self._lock:
                mission = self._state.get(mission_id)
                if mission is None or mission.generation != generation:
                    return
                effects = self._scheduler.handle_dispatch_timeout(
                    mission_id,
                    message,
                )
            self.get_logger().error(message)
            self._apply_effects(effects)
            self._publish_state()

    def _apply_effects(self, effects: SchedulerEffects) -> None:
        with self._effects_lock:
            with self._lock:
                for mission_id in effects.start:
                    context = self._contexts.get(mission_id)
                    if context is not None:
                        context.last_feedback_yaml = ''
            for mission_id in effects.updated:
                self._publish_mission_feedback(mission_id)
            for completion in effects.complete:
                with self._lock:
                    self._deferred_downstream_cancellations.discard(
                        completion.mission_id
                    )
                    context = self._contexts.get(completion.mission_id)
                    if context is not None and not context.completion.done():
                        context.completion.set_result(completion)
            for mission_id in effects.cancel:
                if self._executor_bridge.cancel(mission_id):
                    continue
                with self._lock:
                    if self._state.get(mission_id) is not None:
                        self._deferred_downstream_cancellations.add(
                            mission_id
                        )
            for mission_id in effects.start:
                with self._lock:
                    mission = self._state.get(mission_id)
                    if mission is None:
                        continue
                    try:
                        request = self._registry.build_message(
                            mission.capability,
                            mission.arguments,
                        )
                    except RequestValidationError as error:
                        failure = self._scheduler.handle_terminal(
                            mission_id,
                            TerminalOutcome.ABORTED,
                            message=str(error),
                        )
                        self._apply_effects(failure)
                        continue
                self._executor_bridge.start(mission, request)
                with self._lock:
                    deferred_cancel = (
                        mission_id
                        in self._deferred_downstream_cancellations
                    )
                    self._deferred_downstream_cancellations.discard(
                        mission_id
                    )
                if deferred_cancel:
                    self._executor_bridge.cancel(mission_id)

    async def _wait_for_public_cancel_state(self, goal_handle) -> None:
        """Wait until rclpy applies ACCEPT from the cancel callback."""
        if goal_handle.is_cancel_requested:
            return
        ready = Future()
        timer = self.create_timer(
            0.01,
            lambda: _set_ready_when_canceling(goal_handle, ready),
            callback_group=self._server_group,
        )
        try:
            _set_ready_when_canceling(goal_handle, ready)
            if not ready.done():
                await ready
        finally:
            timer.cancel()
            self.destroy_timer(timer)

    def _publish_mission_feedback(self, mission_id: str) -> None:
        with self._lock:
            mission = self._state.get(mission_id)
            context = self._contexts.get(mission_id)
            if mission is None or context is None:
                return
            feedback = ExecuteMission.Feedback()
            feedback.mission_id = mission_id
            feedback.state = mission.state.value
            feedback.feedback_yaml = context.last_feedback_yaml
            goal_handle = context.goal_handle
        try:
            goal_handle.publish_feedback(feedback)
        except RuntimeError:
            pass

    def _publish_state(self) -> None:
        with self._lock:
            message = SystemStateMsg()
            message.system_state = _system_state_value(
                self._state.system_state
            )
            message.control_mode = _control_mode_value(
                self._state.control_mode
            )
            message.active_foreground_missions = [
                _mission_status(item)
                for item in self._state.active_foreground.values()
            ]
            message.active_background_missions = [
                _mission_status(item)
                for item in self._state.active_background.values()
            ]
            message.suspended_missions = [
                _mission_status(item)
                for item in self._state.suspended.values()
            ]
            message.pending_missions = [
                _mission_status(item)
                for item in self._state.pending.values()
            ]
        self._state_publisher.publish(message)


def _goal_uuid(goal_handle) -> str:
    return bytes(goal_handle.goal_id.uuid).hex()


def _public_result(
    mission_id: str,
    *,
    result_yaml: str = '',
    message: str = '',
) -> ExecuteMission.Result:
    result = ExecuteMission.Result()
    result.mission_id = mission_id
    result.result_yaml = result_yaml
    result.message = message
    return result


def _mission_status(mission: MissionRecord) -> MissionStatus:
    message = MissionStatus()
    message.mission_id = mission.mission_id
    message.capability_id = mission.capability.capability_id
    message.mode = {
        ExecutionMode.FOREGROUND: MissionStatus.FOREGROUND,
        ExecutionMode.BACKGROUND: MissionStatus.BACKGROUND,
    }[mission.mode]
    message.priority = {
        MissionPriority.LOW: MissionStatus.LOW,
        MissionPriority.NORMAL: MissionStatus.NORMAL,
        MissionPriority.HIGH: MissionStatus.HIGH,
        MissionPriority.URGENT: MissionStatus.URGENT,
    }[mission.priority]
    message.state = {
        MissionState.PENDING: MissionStatus.PENDING,
        MissionState.RUNNING: MissionStatus.RUNNING,
        MissionState.CANCELING: MissionStatus.CANCELING,
        MissionState.SUSPENDED: MissionStatus.SUSPENDED,
    }[mission.state]
    return message


def _system_state_value(state: SystemState) -> int:
    return {
        SystemState.BOOTING: SystemStateMsg.BOOTING,
        SystemState.IDLE: SystemStateMsg.IDLE,
        SystemState.EXECUTING_MISSION: SystemStateMsg.EXECUTING_MISSION,
        SystemState.RECHARGING: SystemStateMsg.RECHARGING,
        SystemState.EMERGENCY: SystemStateMsg.EMERGENCY,
    }[state]


def _control_mode_value(mode: ControlMode) -> int:
    return {
        ControlMode.AUTONOMOUS: SystemStateMsg.AUTONOMOUS,
        ControlMode.MANUAL: SystemStateMsg.MANUAL,
    }[mode]


def main(args=None) -> None:
    """Run the system manager on a multithreaded executor."""
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = None
    executor = MultiThreadedExecutor(num_threads=4)
    try:
        node = SystemManagerNode()
        executor.add_node(node)
        while rclpy.ok():
            executor.spin_once(timeout_sec=0.1)
    except (ExternalShutdownException, KeyboardInterrupt):
        pass
    finally:
        if node is not None:
            if rclpy.ok():
                node.begin_shutdown()
                deadline = time.monotonic() + node.shutdown_timeout_s
                while (
                    node.downstream_execution_count
                    and time.monotonic() < deadline
                    and rclpy.ok()
                ):
                    executor.spin_once(timeout_sec=0.05)
                if node.downstream_execution_count:
                    node.get_logger().error(
                        'Timed out while canceling downstream Actions '
                        'during shutdown'
                    )
                if (
                    node.downstream_execution_count
                    or node.unresolved_context_count
                ):
                    node.force_shutdown()
                drain_deadline = time.monotonic() + node.shutdown_timeout_s
                while (
                    node.public_context_count
                    and time.monotonic() < drain_deadline
                    and rclpy.ok()
                ):
                    executor.spin_once(timeout_sec=0.05)
                if node.public_context_count:
                    node.get_logger().error(
                        'Timed out while finishing public mission callbacks'
                    )
        executor_stopped = executor.shutdown(
            timeout_sec=(node.shutdown_timeout_s if node else 3.0)
        )
        if node is not None and not executor_stopped:
            node.get_logger().error(
                'Timed out while stopping the ROS executor'
            )
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        signal.signal(signal.SIGTERM, previous_sigterm_handler)


def _raise_keyboard_interrupt(_signum, _frame) -> None:
    """Turn SIGTERM into the same orderly path used by Ctrl-C."""
    raise KeyboardInterrupt


def _set_ready_when_canceling(goal_handle, ready: Future) -> None:
    if goal_handle.is_cancel_requested and not ready.done():
        ready.set_result(None)


def _require_positive_finite(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f'{name} must be a finite number greater than zero')


if __name__ == '__main__':
    main()
