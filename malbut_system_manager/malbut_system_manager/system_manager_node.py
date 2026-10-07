"""ROS node exposing the unified Malbut mission execution contract."""

from dataclasses import dataclass, field
from collections import OrderedDict
from datetime import datetime, timezone
import json
import math
import signal
from threading import RLock
import time
from uuid import uuid4
import yaml

from ament_index_python.packages import get_package_prefix
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
from malbut_interfaces.srv import StopMovement
from std_msgs.msg import Empty, String

from .localization import LocalizationController, SlamProcess
from .manifest_registry import (
    ManifestError,
    ManifestRegistry,
    RequestValidationError,
)
from .mission_executor import MissionExecutor
from .mission_scheduler import is_movement, MissionScheduler
from .models import (
    ControlMode,
    ExecutionMode,
    ExecutionResource,
    LocalizationMode,
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
STOP_MOVEMENT_SERVICE = '/malbut/mission/stop_movement'


@dataclass
class _Admission:
    request: object
    sequence: int
    movement: bool
    mission_id: str = ''
    stopped: bool = False
    admitted: bool = False
    done: bool = False

    @property
    def identity(self):
        """Use the ROS UUID once acceptance has supplied the Goal handle."""
        return self.mission_id or f'admission:{self.sequence}'


@dataclass
class _StopOperation:
    fingerprint: tuple
    deadline: float
    affected: set[str] = field(default_factory=set)
    admissions: list[_Admission] = field(default_factory=list)
    localization_id: str = ''
    denied: tuple[str, ...] = ()


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
        self._admissions: dict[int, _Admission] = {}
        self._admission_sequence = 0
        self._movement_runtime_id = uuid4().hex
        self._movement_epoch = 0
        self._stop_operations: dict[str, _StopOperation] = {}
        self._stop_waiters = []
        self._stopped_mission_ids: set[str] = set()
        self._recent_results = OrderedDict()
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
        self._stop_timeout_s = goal_response_timeout_s + cancel_completion_timeout_s + 1.0
        # Bringup gates admission on its readiness report; standalone use
        # (simulation experiments, tests) stays ready immediately.
        ready_topic = self.declare_parameter('ready_topic', '').value
        localization_control = bool(
            self.declare_parameter('localization_control', False).value
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
        self._preempt_teleop = self.create_publisher(Empty, '/preempt_teleop', 1)
        self._movement_stop = self.create_publisher(String, '/malbut/movement_stop', 10)
        self._recent_publisher = self.create_publisher(
            String, '/malbut/mission/recent_results', state_qos)
        self._recent_publisher.publish(String(data='[]'))

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
            handle_accepted_callback=self._accepted,
            callback_group=self._server_group,
        )

        self._localization = None
        if localization_control:
            self._localization = self._create_localization()
        self._stop_service = self.create_service(
            StopMovement, STOP_MOVEMENT_SERVICE, self._stop_movement,
            callback_group=self._server_group,
        )
        self._stop_timer = self.create_timer(
            0.05, self._refresh_stop_gate, callback_group=self._server_group,
        )
        self._ready_subscription = None
        if ready_topic:
            ready_qos = QoSProfile(depth=1)
            ready_qos.reliability = ReliabilityPolicy.RELIABLE
            ready_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
            self._ready_subscription = self.create_subscription(
                String, ready_topic, self._on_readiness, ready_qos,
                callback_group=self._server_group,
            )
        with self._lock:
            effects = (
                SchedulerEffects() if ready_topic
                else self._scheduler.set_ready(True)
            )
            self._accepting_goals = True
        self._apply_effects(effects)
        self._publish_state()
        capabilities = ', '.join(
            manifest.capability_id for manifest in self._registry.all()
        )
        self.get_logger().info(
            f'System manager ready with capabilities: {capabilities}'
        )

    def _create_localization(self) -> LocalizationController:
        """Own SLAM and saved-map AMCL so only one map->odom source runs."""
        params_file = self.declare_parameter('slam_params_file', '').value
        if not params_file:
            raise ValueError('localization_control requires slam_params_file')
        scan_topic = self.declare_parameter('scan_topic', '/scan_raw').value
        stop_timeout_s = float(
            self.declare_parameter('slam_stop_timeout_s', 10.0).value
        )
        service_timeout_s = float(
            self.declare_parameter('localization_timeout_s', 30.0).value
        )
        relocalize_timeout_s = float(
            self.declare_parameter('relocalize_timeout_s', 90.0).value
        )
        _require_positive_finite('slam_stop_timeout_s', stop_timeout_s)
        _require_positive_finite('localization_timeout_s', service_timeout_s)
        _require_positive_finite('relocalize_timeout_s', relocalize_timeout_s)
        executable = (
            f'{get_package_prefix("slam_toolbox")}'
            '/lib/slam_toolbox/sync_slam_toolbox_node'
        )
        slam = SlamProcess([
            executable, '--ros-args', '-r', '__node:=slam_toolbox',
            '--params-file', params_file,
            '-p', 'use_sim_time:=false', '-p', f'scan_topic:={scan_topic}',
        ], stop_timeout_s)
        group = ReentrantCallbackGroup()
        controller = LocalizationController(
            self, group, slam=slam,
            on_mode=self._on_localization_mode,
            can_switch=self._base_is_free,
            on_pose_ready=self._on_pose_ready,
            admission_lock=self._effects_lock,
            movement_state=lambda: (self._movement_runtime_id, self._movement_epoch),
            lifecycle_service=self.declare_parameter(
                'localization_lifecycle_service',
                '/lifecycle_manager_localization/manage_nodes',
            ).value,
            map_server_load_service=self.declare_parameter(
                'map_server_load_service', '/map_server/load_map'
            ).value,
            service_timeout_s=service_timeout_s,
            # Bringup sets /relocalize to find the robot on each loaded map.
            relocalize_action=self.declare_parameter(
                'relocalize_action', ''
            ).value,
            relocalize_timeout_s=relocalize_timeout_s,
            default_map=self.declare_parameter('default_map', '').value,
            can_mapping=self._mapping_can_switch,
        )
        initial_map = self.declare_parameter('initial_map', '').value

        def start_once() -> None:
            timer.cancel()
            controller.start(initial_map)

        timer = self.create_timer(0.1, start_once, callback_group=group)
        return controller

    def _on_localization_mode(self, mode: LocalizationMode) -> None:
        with self._effects_lock:
            with self._lock:
                effects = self._scheduler.set_localization(mode)
            self._apply_effects(effects)
            self._publish_state()

    def _base_is_free(self) -> bool:
        with self._lock:
            return (self._accepting_goals and not self._state.movement_stopping
                    and not self._scheduler.base_busy()
                    and not any(item.movement or item.request.capability_id == 'fall_confirmation'
                                for item in self._admissions.values())
                    and (self._localization is None or not self._localization.stop_pending))

    def _on_pose_ready(self, ready: bool) -> None:
        with self._lock:
            self._state.pose_ready = ready

    def _mapping_can_switch(self) -> bool:
        """Allow AutoSLAM to start/stop its backend while it owns BASE."""
        with self._lock:
            active = list(self._state.active())
            if not any(ExecutionResource.BASE in mission.resources
                       and mission.capability.capability_id == 'autoslam'
                       for mission in active):
                return self._base_is_free()
            # A queued replacement must wait for AutoSLAM's backend cleanup.
            return all(ExecutionResource.BASE not in mission.resources
                       or mission.capability.capability_id == 'autoslam'
                       for mission in active)

    def _on_readiness(self, message: String) -> None:
        try:
            ready = json.loads(message.data).get('state') == 'READY'
        except (AttributeError, ValueError):
            return
        with self._effects_lock:
            with self._lock:
                if not ready or self._state.ready:
                    return
                effects = self._scheduler.set_ready(True)
            self.get_logger().info('Bringup reported READY; accepting missions')
            self._apply_effects(effects)
            self._publish_state()

    def destroy_node(self) -> bool:
        """Stop accepting work and release dynamic Action clients."""
        self._accepting_goals = False
        if getattr(self, '_localization', None) is not None:
            self._localization.close()
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
        with self._effects_lock:
            if not self._accepting_goals:
                return GoalResponse.REJECT
            movement = self._movement_request(request)
            if movement:
                if (self._state.movement_stopping
                        or self._localization is not None and self._localization.stop_pending):
                    return GoalResponse.REJECT
            self._admission_sequence += 1
            # rclpy passes this exact request to handle_accepted_callback.
            # Retain it until scheduler admission, including delayed execution.
            self._admissions[id(request)] = _Admission(
                request, self._admission_sequence, movement)
        return GoalResponse.ACCEPT

    def _movement_request(self, request) -> bool:
        try:
            capability = self._registry.get(request.capability_id.strip())
        except (ManifestError, RequestValidationError):
            return False
        return (ExecutionResource.BASE in capability.resources
                and capability.capability_id != 'fall_confirmation')

    def _accepted(self, goal_handle) -> None:
        with self._effects_lock:
            admission = self._admissions.get(id(goal_handle.request))
            if admission is not None:
                admission.mission_id = _goal_uuid(goal_handle)
        goal_handle.execute()

    def _stop_snapshot(self, operation):
        affected = set(operation.affected)
        unresolved = {item for item in affected if self._state.get(item) is not None}
        for admission in operation.admissions:
            affected.add(admission.identity)
            if not admission.done and (not admission.admitted
                                       or self._state.get(admission.identity) is not None):
                unresolved.add(admission.identity)
        if operation.localization_id:
            affected.add(operation.localization_id)
            if self._localization.movement_pending(operation.localization_id):
                unresolved.add(operation.localization_id)
        return sorted(affected), sorted(unresolved)

    def _refresh_stop_gate(self) -> None:
        with self._effects_lock:
            with self._lock:
                self._stopped_mission_ids.intersection_update(
                    item.mission_id for item in self._state.all())
                unresolved = bool(self._stopped_mission_ids) or any(
                    item.stopped and not item.done and not item.admitted
                    for item in self._admissions.values()
                )
                if self._localization is not None:
                    unresolved = unresolved or self._localization.stop_pending
                self._state.movement_stopping = unresolved
                pending = []
                for operation, response, ready in self._stop_waiters:
                    if ready.done():
                        continue
                    affected, unresolved_ids = self._stop_snapshot(operation)
                    if unresolved_ids and time.monotonic() < operation.deadline:
                        pending.append((operation, response, ready))
                        continue
                    response.stopped = not unresolved_ids
                    response.affected_mission_ids = affected
                    response.unresolved_mission_ids = unresolved_ids
                    response.code = 'stopped' if not unresolved_ids else 'stop_unconfirmed'
                    response.message = (
                        'Movement missions have ended' if not unresolved_ids else
                        'Movement termination is unconfirmed; new movement remains blocked')
                    ready.set_result(response)
                self._stop_waiters = pending

    async def _stop_movement(self, request, response):
        request_id = request.request_id
        if (not request_id.strip() or len(request_id) > 128
                or any(ord(char) < 32 for char in request_id)):
            response.code, response.message = 'invalid_request', 'A bounded request_id is required'
            return response
        if request.shutdown_runtime and not request.require_preemption_confirmation:
            response.code = 'invalid_request'
            response.message = 'Runtime shutdown requires confirmation of every active mission'
            return response
        fingerprint = (request.require_preemption_confirmation,
                       tuple(sorted(set(request.confirmed_preemption_mission_ids))),
                       request.shutdown_runtime)
        with self._effects_lock:
            with self._lock:
                operation = self._stop_operations.get(request_id)
                if operation is not None and operation.fingerprint != fingerprint:
                    response.code = 'request_id_conflict'
                    response.message = 'Request ID was reused'
                    return response
                if operation is None:
                    active = {item.mission_id for item in self._state.all() if is_movement(item)}
                    admissions = [item for item in self._admissions.values() if item.movement]
                    current = active | {item.identity for item in admissions if not item.done}
                    if request.shutdown_runtime:
                        current = {item.mission_id for item in self._state.all()}
                        current.update(item.identity for item in self._admissions.values()
                                       if not item.done)
                    localization_id = (self._localization.movement_identity
                                       if self._localization is not None else '')
                    if localization_id:
                        current.add(localization_id)
                    operation = _StopOperation(
                        fingerprint, time.monotonic() + self._stop_timeout_s)
                    self._stop_operations[request_id] = operation
                    if (request.require_preemption_confirmation
                            and not current.issubset(fingerprint[1])):
                        operation.denied = tuple(sorted(current))
                    else:
                        self._movement_epoch += 1
                        if request.shutdown_runtime:
                            self._accepting_goals = False
                        operation.affected = active
                        operation.admissions = admissions
                        operation.localization_id = localization_id
                        for admission in admissions:
                            admission.stopped = True
                        self._stopped_mission_ids.update(active)
                        effects = self._scheduler.request_stop_movement()
                        if self._localization is not None:
                            self._localization.stop_movement()
                        self._preempt_teleop.publish(Empty())
                        self._movement_stop.publish(String(data=request_id))
                        self._apply_effects(effects)
                        self._publish_state()
                if operation.denied:
                    response.unresolved_mission_ids = list(operation.denied)
                    response.code = 'preemption_confirmation_required'
                    response.message = 'Confirm cancellation of the current movement first'
                    return response
        ready = Future()
        # A persistent timer owns completion. Destroying per-request timers can
        # race a callback already taken by a Reentrant ROS executor.
        with self._effects_lock:
            self._stop_waiters.append((operation, response, ready))
            self._refresh_stop_gate()
        return await ready

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
        admission = self._admissions.get(id(goal_handle.request))
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
                    if admission is not None:
                        admission.done = True
                        self._admissions.pop(id(goal_handle.request), None)
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
            self._remember_result(MissionCompletion(
                mission_id,
                TerminalOutcome.CANCELED if canceled_early else TerminalOutcome.ABORTED,
                message=str(error),
            ), capability_id=goal_handle.request.capability_id)
            return result

        context = _MissionContext(goal_handle, Future())
        mission = MissionRecord(
            mission_id, manifest, arguments,
            require_preemption_confirmation=goal_handle.request.require_preemption_confirmation,
            confirmed_preemption_mission_ids=frozenset(
                goal_handle.request.confirmed_preemption_mission_ids),
        )
        with self._effects_lock:
            with self._lock:
                self._contexts[mission_id] = context
                if admission is not None:
                    admission.admitted = True
                    self._admissions.pop(id(goal_handle.request), None)
                canceled_early = (
                    mission_id in self._early_cancellations
                    or goal_handle.is_cancel_requested
                )
                self._early_cancellations.discard(mission_id)
                if admission is not None and admission.stopped:
                    admission.done = True
                    effects = SchedulerEffects(complete=[MissionCompletion(
                        mission_id, TerminalOutcome.ABORTED, message='movement_stopped')])
                elif canceled_early:
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
                    binding_error = (self._movement_epoch_error(goal_handle.request, mission)
                                     or self._localization_binding_error(goal_handle.request))
                    effects = (
                        SchedulerEffects(complete=[MissionCompletion(
                            mission_id, TerminalOutcome.ABORTED,
                            result_yaml=json.dumps(binding_error),
                            message=(
                                'Movement was stopped after this request was prepared'
                                if binding_error['code'] == 'movement_epoch_changed' else
                                'Localization changed after the request was prepared'),
                        )]) if binding_error else self._scheduler.submit(mission)
                    )

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

    def _movement_epoch_error(self, request, mission):
        if (is_movement(mission) and request.require_movement_epoch
                and (request.movement_runtime_id, request.movement_epoch)
                != (self._movement_runtime_id, self._movement_epoch)):
            return {
                'code': 'movement_epoch_changed',
                'movement_runtime_id': self._movement_runtime_id,
                'movement_epoch': self._movement_epoch,
            }
        return None

    def _localization_binding_error(self, request):
        expected = (request.expected_localization_runtime_id,
                    request.expected_localization_transition_id)
        if expected == ('', 0):
            return None
        actual = ((self._localization.runtime_id, self._localization.transition_id)
                  if self._localization is not None else ('', 0))
        if expected == actual:
            return None
        return {
            'code': 'localization_changed',
            'runtime_id': actual[0], 'transition_id': actual[1],
        }

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
                if (self._localization is not None
                        and mission.capability.capability_id == 'relocalize'):
                    try:
                        result = yaml.safe_load(result_yaml)
                        ready = (outcome is TerminalOutcome.SUCCEEDED
                                 and isinstance(result, dict) and result.get('success') is True)
                    except yaml.YAMLError:
                        ready = False
                    self._localization.record_pose_result(ready)
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
                self._remember_result(completion)
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
                if (self._localization is not None
                        and mission.capability.capability_id == 'relocalize'):
                    self._localization.record_pose_result(False)
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

    def _remember_result(self, completion, *, capability_id=None):
        with self._lock:
            identity = completion.mission_id
            context = self._contexts.get(identity)
            mission = self._state.get(identity)
            previous = self._recent_results.pop(identity, {})
            if capability_id is None:
                capability_id = (context.goal_handle.request.capability_id if context is not None
                                 else mission.capability.capability_id if mission is not None
                                 else previous.get('capability_id', ''))
            payload = completion.result_yaml
            self._recent_results[identity] = {
                'mission_id': identity,
                'capability_id': capability_id[:128],
                'state': completion.outcome.value,
                'result_yaml': payload[:4096], 'result_truncated': len(payload) > 4096,
                'message': completion.message[:512],
                'downstream_terminal': mission is None,
                'observed_at': datetime.now(timezone.utc).isoformat(),
            }
            while len(self._recent_results) > 20:
                self._recent_results.popitem(last=False)
            encoded = json.dumps(list(self._recent_results.values()), ensure_ascii=False)
            while len(encoded.encode('utf-8')) > 60 * 1024:
                self._recent_results.popitem(last=False)
                encoded = json.dumps(list(self._recent_results.values()), ensure_ascii=False)
        self._recent_publisher.publish(String(data=encoded))

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
            message.movement_runtime_id = self._movement_runtime_id
            message.movement_epoch = self._movement_epoch
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
