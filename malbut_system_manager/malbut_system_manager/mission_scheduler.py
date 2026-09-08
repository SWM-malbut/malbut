"""Deterministic admission, replacement preemption, and cancellation rules."""

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


def resources_conflict(
    first: MissionRecord,
    second: MissionRecord,
) -> bool:
    """Conflict only when two missions claim the same exclusive output."""
    return not first.resources.isdisjoint(second.resources)


class MissionScheduler:
    """Turn mission events into executor effects without using ROS APIs."""

    def __init__(
        self,
        state: StateStore,
        *,
        conflict_policy: ConflictPolicy = resources_conflict,
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

        conflicts = [
            active
            for active in self.state.active()
            if self._conflicts(active, mission)
        ]
        pending_conflicts = [
            pending
            for pending in self.state.pending.values()
            if self._conflicts(pending, mission)
        ]
        higher = [
            other
            for other in conflicts + pending_conflicts
            if other.priority > mission.priority
        ]
        if higher:
            blocker = max(higher, key=lambda item: item.priority)
            return self._reject(
                mission,
                f'conflicting mission {blocker.mission_id} has higher '
                f'priority {blocker.priority.name}',
            )

        # Validate all blockers before replacing any already accepted request.
        for pending in pending_conflicts:
            self._supersede_pending(pending, mission, effects)

        mission.waiting_for = {item.mission_id for item in conflicts}
        if conflicts:
            self.state.add_pending(mission)
        else:
            self.state.activate(mission)
            effects.start.append(mission.mission_id)
        effects.updated.add(mission.mission_id)
        for active in conflicts:
            active.preempted_by.add(mission.mission_id)
            if active.state is not MissionState.CANCELING:
                active.state = MissionState.CANCELING
                active.cancel_reason = CancelReason.PREEMPTION
                effects.cancel.append(active.mission_id)
            effects.updated.add(active.mission_id)
        effects.extend(self._start_ready())
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
            mission.preempted_by.clear()
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
        if mission.user_cancel_requested:
            outcome = TerminalOutcome.CANCELED
        elif outcome is TerminalOutcome.CANCELED:
            if mission.cancel_reason is CancelReason.PREEMPTION:
                # Server-driven replacement is terminal, never suspension.
                # ROS CANCELED requires the upper client to request cancel;
                # report server-driven interruption as ABORTED with a reason.
                outcome = TerminalOutcome.ABORTED
                message = 'mission preempted by a replacement request'
            elif mission.cancel_reason not in (
                CancelReason.USER, CancelReason.SHUTDOWN,
            ):
                outcome = TerminalOutcome.ABORTED
                message = message or 'Downstream goal canceled without a manager request'
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
            mission.preempted_by.clear()
            self._abort_waiters(mission_id, message, effects)
            effects.updated.add(mission_id)
            effects.extend(self._start_ready())
            return effects
        if mission.cancel_reason is CancelReason.SHUTDOWN:
            mission.state = MissionState.CANCELING
            effects.updated.add(mission_id)
            return effects

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
        mission.preempted_by.clear()
        effects.updated.add(mission_id)
        self._abort_waiters(mission_id, message, effects)
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

        mission.state = MissionState.RUNNING
        mission.cancel_reason = None
        mission.user_cancel_requested = False
        mission.preempted_by.clear()
        mission.resumable = False
        effects.complete.append(
            MissionCompletion(
                mission_id,
                TerminalOutcome.ABORTED,
                message=message,
            )
        )
        effects.updated.add(mission_id)

        self._abort_waiters(
            mission_id,
            'conflicting mission dispatch could not be resolved',
            effects,
        )
        effects.extend(self._start_ready())
        return effects

    def _abort_waiters(
        self,
        blocker_id: str,
        message: str,
        effects: SchedulerEffects,
    ) -> None:
        """Fail every request waiting for an action that cannot stop."""
        for pending in list(self.state.pending.values()):
            if blocker_id not in pending.waiting_for:
                continue
            self.state.remove(pending.mission_id)
            effects.complete.append(
                MissionCompletion(
                    pending.mission_id,
                    TerminalOutcome.ABORTED,
                    message=message,
                )
            )
            effects.updated.add(pending.mission_id)
            self._release_preempted_by(pending.mission_id, effects)

    def _supersede_pending(
        self,
        pending: MissionRecord,
        incoming: MissionRecord,
        effects: SchedulerEffects,
    ) -> None:
        """Replace one conflicting reservation and transfer resume links."""
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

        for previous in list(self.state.active()) + list(
            self.state.suspended.values()
        ):
            if pending.mission_id not in previous.preempted_by:
                continue
            previous.preempted_by.discard(pending.mission_id)
            if self._conflicts(previous, incoming):
                previous.preempted_by.add(incoming.mission_id)
            effects.updated.add(previous.mission_id)

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
                for active in self.state.active()
                if self._conflicts(active, mission)
            ]
            if conflicts:
                continue
            self.state.activate(mission)
            effects.start.append(mission.mission_id)
            effects.updated.add(mission.mission_id)

        for mission in list(self.state.suspended.values()):
            if mission.preempted_by or self._gate_error(mission):
                continue
            conflicts = [
                other
                for other in list(self.state.active()) + list(
                    self.state.pending.values()
                )
                if self._conflicts(other, mission)
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
        for previous in list(self.state.active()) + list(
            self.state.suspended.values()
        ):
            if mission_id in previous.preempted_by:
                previous.preempted_by.discard(mission_id)
                effects.updated.add(previous.mission_id)

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
