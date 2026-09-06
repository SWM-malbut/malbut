"""Deterministic admission, preemption, cancellation, and resume rules."""

from collections.abc import Callable

from .models import (
    CancelReason,
    ControlMode,
    ExecutionMode,
    MissionCompletion,
    MissionRecord,
    MissionState,
    SchedulerEffects,
    TerminalOutcome,
)
from .state_store import StateStore


ConflictPolicy = Callable[[MissionRecord, MissionRecord], bool]


def foregrounds_conflict(
    first: MissionRecord,
    second: MissionRecord,
) -> bool:
    """Use the safe v1 policy until coexistence pairs are specified."""
    return (
        first.mode is ExecutionMode.FOREGROUND
        and second.mode is ExecutionMode.FOREGROUND
    )


class MissionScheduler:
    """Turn mission events into executor effects without using ROS APIs."""

    def __init__(
        self,
        state: StateStore,
        *,
        conflict_policy: ConflictPolicy = foregrounds_conflict,
    ) -> None:
        self.state = state
        self._conflicts = conflict_policy

    def submit(self, mission: MissionRecord) -> SchedulerEffects:
        """Start, reject, or stage a validated mission for preemption."""
        effects = SchedulerEffects()
        if self.state.get(mission.mission_id) is not None:
            return self._reject(mission, 'mission_id is already active')
        gate_error = self._gate_error(mission)
        if gate_error:
            return self._reject(mission, gate_error)

        if mission.mode is ExecutionMode.BACKGROUND:
            self.state.activate(mission)
            effects.start.append(mission.mission_id)
            effects.updated.add(mission.mission_id)
            return effects

        if self.state.pending:
            pending = next(iter(self.state.pending.values()))
            if mission.priority < pending.priority:
                return self._reject(
                    mission,
                    f'pending mission {pending.mission_id} has higher '
                    f'priority {pending.priority.name}',
                )
            return self._replace_pending(pending, mission)

        conflicts = [
            active
            for active in self.state.active_foreground.values()
            if self._conflicts(active, mission)
        ]
        if not conflicts:
            self.state.activate(mission)
            effects.start.append(mission.mission_id)
            effects.updated.add(mission.mission_id)
            return effects

        higher = [
            active
            for active in conflicts
            if active.priority > mission.priority
        ]
        if higher:
            blocker = max(higher, key=lambda item: item.priority)
            return self._reject(
                mission,
                f'conflicting mission {blocker.mission_id} has higher '
                f'priority {blocker.priority.name}',
            )

        mission.waiting_for = {item.mission_id for item in conflicts}
        self.state.add_pending(mission)
        effects.updated.add(mission.mission_id)
        for active in conflicts:
            active.state = MissionState.CANCELING
            active.cancel_reason = CancelReason.PREEMPTION
            active.preempted_by = mission.mission_id
            effects.cancel.append(active.mission_id)
            effects.updated.add(active.mission_id)
        return effects

    def request_cancel(
        self,
        mission_id: str,
    ) -> tuple[bool, SchedulerEffects]:
        """Apply an explicit client cancellation to any live mission state."""
        effects = SchedulerEffects()
        mission = self.state.get(mission_id)
        if mission is None:
            return False, effects
        mission.user_cancel_requested = True

        if mission_id in self.state.pending:
            self.state.remove(mission_id)
            self._release_preempted_by(mission_id, effects)
            effects.complete.append(
                MissionCompletion(
                    mission_id,
                    TerminalOutcome.CANCELED,
                    message='mission canceled before execution',
                )
            )
            effects.updated.add(mission_id)
            effects.extend(self._start_ready())
            return True, effects

        if mission_id in self.state.suspended:
            self.state.remove(mission_id)
            self._release_preempted_by(mission_id, effects)
            effects.complete.append(
                MissionCompletion(
                    mission_id,
                    TerminalOutcome.CANCELED,
                    message='suspended mission canceled by client',
                )
            )
            effects.updated.add(mission_id)
            effects.extend(self._start_ready())
            return True, effects

        if mission.state is MissionState.CANCELING:
            effects.updated.add(mission_id)
            return True, effects

        mission.state = MissionState.CANCELING
        mission.cancel_reason = CancelReason.USER
        effects.cancel.append(mission_id)
        effects.updated.add(mission_id)
        return True, effects

    def request_shutdown(self) -> SchedulerEffects:
        """Cancel active work and finish work that has not been dispatched."""
        effects = SchedulerEffects()
        for mission in list(self.state.pending.values()):
            self.state.remove(mission.mission_id)
            effects.complete.append(
                MissionCompletion(
                    mission.mission_id,
                    TerminalOutcome.ABORTED,
                    message='system manager is shutting down',
                )
            )
            effects.updated.add(mission.mission_id)
        for mission in list(self.state.suspended.values()):
            self.state.remove(mission.mission_id)
            effects.complete.append(
                MissionCompletion(
                    mission.mission_id,
                    TerminalOutcome.ABORTED,
                    message='system manager is shutting down',
                )
            )
            effects.updated.add(mission.mission_id)
        for mission in list(self.state.active()):
            mission.state = MissionState.CANCELING
            mission.cancel_reason = CancelReason.SHUTDOWN
            mission.preempted_by = None
            effects.cancel.append(mission.mission_id)
            effects.updated.add(mission.mission_id)
        return effects

    def force_shutdown(self) -> SchedulerEffects:
        """Finish every remaining mission after shutdown cancellation times out."""
        effects = SchedulerEffects()
        for mission in list(self.state.all()):
            self.state.remove(mission.mission_id)
            effects.complete.append(
                MissionCompletion(
                    mission.mission_id,
                    TerminalOutcome.ABORTED,
                    message='system manager shutdown timed out',
                )
            )
            effects.updated.add(mission.mission_id)
        return effects

    def handle_terminal(
        self,
        mission_id: str,
        outcome: TerminalOutcome,
        *,
        result_yaml: str = '',
        message: str = '',
    ) -> SchedulerEffects:
        """Consume the terminal status of the current downstream generation."""
        effects = SchedulerEffects()
        mission = self.state.active_foreground.get(mission_id)
        if mission is None:
            mission = self.state.active_background.get(mission_id)
        if mission is None:
            return effects

        self.state.remove(mission_id)
        was_preempted = (
            outcome is TerminalOutcome.CANCELED
            and mission.cancel_reason is CancelReason.PREEMPTION
            and not mission.user_cancel_requested
            and mission.resumable
        )
        if was_preempted:
            self.state.suspend(mission)
            effects.updated.add(mission_id)
        else:
            if mission.user_cancel_requested:
                outcome = TerminalOutcome.CANCELED
            if (
                outcome is TerminalOutcome.CANCELED
                and mission.cancel_reason
                not in (CancelReason.USER, CancelReason.SHUTDOWN)
            ):
                outcome = TerminalOutcome.ABORTED
                message = message or (
                    'Downstream goal canceled without a manager request'
                )
            if mission.cancel_reason is CancelReason.SHUTDOWN:
                outcome = TerminalOutcome.ABORTED
                message = message or 'system manager is shutting down'
            effects.complete.append(
                MissionCompletion(
                    mission_id,
                    outcome,
                    result_yaml=result_yaml,
                    message=message,
                )
            )
            effects.updated.add(mission_id)
            self._release_preempted_by(mission_id, effects)

        for pending in self.state.pending.values():
            pending.waiting_for.discard(mission_id)
        effects.extend(self._start_ready())
        return effects

    def handle_cancel_rejected(
        self,
        mission_id: str,
        message: str,
    ) -> SchedulerEffects:
        """Abort a preemptor when a conflicting Action refuses to stop."""
        effects = SchedulerEffects()
        mission = self.state.active_foreground.get(mission_id)
        if mission is None:
            mission = self.state.active_background.get(mission_id)
        if mission is None or mission.state is not MissionState.CANCELING:
            return effects

        if mission.cancel_reason is CancelReason.USER:
            mission.state = MissionState.RUNNING
            mission.cancel_reason = None
            mission.user_cancel_requested = False
            mission.resumable = False
            effects.complete.append(
                MissionCompletion(
                    mission_id,
                    TerminalOutcome.ABORTED,
                    message=(
                        f'{message}; downstream mission is still running'
                    ),
                )
            )
            effects.updated.add(mission_id)
            return effects
        if mission.cancel_reason is CancelReason.SHUTDOWN:
            mission.state = MissionState.CANCELING
            effects.updated.add(mission_id)
            return effects

        preemptor_id = mission.preempted_by
        if mission.user_cancel_requested:
            mission.state = MissionState.RUNNING
            mission.cancel_reason = None
            mission.user_cancel_requested = False
            mission.resumable = False
            effects.complete.append(
                MissionCompletion(
                    mission_id,
                    TerminalOutcome.ABORTED,
                    message=(
                        f'{message}; downstream mission is still running'
                    ),
                )
            )
        else:
            mission.state = MissionState.RUNNING
            mission.cancel_reason = None
        mission.preempted_by = None
        effects.updated.add(mission_id)
        if preemptor_id is None:
            return effects

        pending = self.state.pending.pop(preemptor_id, None)
        if pending is not None:
            effects.complete.append(
                MissionCompletion(
                    preemptor_id,
                    TerminalOutcome.ABORTED,
                    message=message,
                )
            )
            effects.updated.add(preemptor_id)
        self._release_preempted_by(preemptor_id, effects)
        effects.extend(self._start_ready())
        return effects

    def handle_dispatch_timeout(
        self,
        mission_id: str,
        message: str,
    ) -> SchedulerEffects:
        """Fail the caller but retain an unresolved downstream execution."""
        effects = SchedulerEffects()
        mission = self.state.active_foreground.get(mission_id)
        if mission is None:
            mission = self.state.active_background.get(mission_id)
        if mission is None:
            return effects

        preemptor_id = mission.preempted_by
        mission.state = MissionState.RUNNING
        mission.cancel_reason = None
        mission.user_cancel_requested = False
        mission.preempted_by = None
        mission.resumable = False
        effects.complete.append(
            MissionCompletion(
                mission_id,
                TerminalOutcome.ABORTED,
                message=message,
            )
        )
        effects.updated.add(mission_id)

        if preemptor_id is not None:
            pending = self.state.pending.pop(preemptor_id, None)
            if pending is not None:
                effects.complete.append(
                    MissionCompletion(
                        preemptor_id,
                        TerminalOutcome.ABORTED,
                        message=(
                            'conflicting mission dispatch could not be '
                            'resolved'
                        ),
                    )
                )
                effects.updated.add(preemptor_id)
            self._release_preempted_by(preemptor_id, effects)
        effects.extend(self._start_ready())
        return effects

    def _replace_pending(
        self,
        pending: MissionRecord,
        incoming: MissionRecord,
    ) -> SchedulerEffects:
        active_foreground = list(self.state.active_foreground.values())
        conflicts = [
            active
            for active in active_foreground
            if self._conflicts(active, incoming)
        ]
        higher = [
            active
            for active in conflicts
            if active.priority > incoming.priority
        ]
        if higher:
            blocker = max(higher, key=lambda item: item.priority)
            return self._reject(
                incoming,
                f'conflicting mission {blocker.mission_id} has higher '
                f'priority {blocker.priority.name}',
            )

        effects = SchedulerEffects()
        self.state.remove(pending.mission_id)
        effects.complete.append(
            MissionCompletion(
                pending.mission_id,
                TerminalOutcome.ABORTED,
                message=(
                    f'superseded before execution by '
                    f'{incoming.mission_id}'
                ),
            )
        )
        effects.updated.add(pending.mission_id)

        incoming.waiting_for = {item.mission_id for item in conflicts}
        conflict_ids = incoming.waiting_for
        for active in active_foreground:
            was_preempted = active.preempted_by == pending.mission_id
            is_conflict = active.mission_id in conflict_ids
            if was_preempted and not is_conflict:
                active.preempted_by = None
                effects.updated.add(active.mission_id)
            if not is_conflict:
                continue
            active.preempted_by = incoming.mission_id
            if active.state is not MissionState.CANCELING:
                active.state = MissionState.CANCELING
                active.cancel_reason = CancelReason.PREEMPTION
                effects.cancel.append(active.mission_id)
            effects.updated.add(active.mission_id)

        for suspended in self.state.suspended.values():
            if suspended.preempted_by != pending.mission_id:
                continue
            suspended.preempted_by = (
                incoming.mission_id
                if self._conflicts(suspended, incoming)
                else None
            )
            effects.updated.add(suspended.mission_id)

        if conflicts:
            self.state.add_pending(incoming)
        else:
            self.state.activate(incoming)
            effects.start.append(incoming.mission_id)
        effects.updated.add(incoming.mission_id)
        effects.extend(self._start_ready())
        return effects

    def set_ready(self, ready: bool) -> SchedulerEffects:
        """Open or close mission admission after startup validation."""
        self.state.ready = ready
        return self._start_ready() if ready else SchedulerEffects()

    def set_control_mode(self, mode: ControlMode) -> SchedulerEffects:
        """Update the internal control gate without defining a ROS endpoint."""
        self.state.control_mode = mode
        if mode is ControlMode.AUTONOMOUS:
            return self._start_ready()
        return SchedulerEffects()

    def set_recharging(self, enabled: bool) -> SchedulerEffects:
        """Update the charging gate without defining a ROS endpoint."""
        self.state.recharging = enabled
        return self._start_ready() if not enabled else SchedulerEffects()

    def set_emergency(self, enabled: bool) -> SchedulerEffects:
        """Update the safety gate without defining a ROS endpoint."""
        self.state.emergency = enabled
        return self._start_ready() if not enabled else SchedulerEffects()

    def _start_ready(self) -> SchedulerEffects:
        effects = SchedulerEffects()
        for mission in list(self.state.pending.values()):
            if mission.waiting_for or self._gate_error(mission):
                continue
            conflicts = [
                active
                for active in self.state.active_foreground.values()
                if self._conflicts(active, mission)
            ]
            if conflicts:
                continue
            self.state.activate(mission)
            effects.start.append(mission.mission_id)
            effects.updated.add(mission.mission_id)

        for mission in list(self.state.suspended.values()):
            if mission.preempted_by is not None or self._gate_error(mission):
                continue
            conflicts = [
                active
                for active in self.state.active_foreground.values()
                if self._conflicts(active, mission)
            ]
            if conflicts:
                continue
            mission.cancel_reason = None
            mission.user_cancel_requested = False
            self.state.activate(mission)
            effects.start.append(mission.mission_id)
            effects.updated.add(mission.mission_id)
        return effects

    def _release_preempted_by(
        self,
        mission_id: str,
        effects: SchedulerEffects,
    ) -> None:
        for suspended in self.state.suspended.values():
            if suspended.preempted_by == mission_id:
                suspended.preempted_by = None
                effects.updated.add(suspended.mission_id)
        for active in self.state.active_foreground.values():
            if active.preempted_by == mission_id:
                active.preempted_by = None

    def _gate_error(self, mission: MissionRecord) -> str:
        if not self.state.ready:
            return 'system manager is still booting'
        if self.state.emergency:
            return 'emergency stop is active'
        if (
            mission.mode is ExecutionMode.FOREGROUND
            and self.state.control_mode is ControlMode.MANUAL
        ):
            return 'manual control owns foreground authority'
        if mission.mode is ExecutionMode.FOREGROUND and self.state.recharging:
            return 'foreground missions are blocked while recharging'
        return ''

    @staticmethod
    def _reject(mission: MissionRecord, reason: str) -> SchedulerEffects:
        return SchedulerEffects(
            complete=[
                MissionCompletion(
                    mission.mission_id,
                    TerminalOutcome.ABORTED,
                    message=reason,
                )
            ],
            updated={mission.mission_id},
        )
