"""Pure patrol progress state machine independent of ROS."""

from dataclasses import dataclass
from enum import Enum

from malbut_patrol.route_loader import PatrolRoute, Waypoint


class PatrolState(Enum):
    """Observable states of one patrol manager."""

    IDLE = 'idle'
    NAVIGATING = 'navigating'
    DWELLING = 'dwelling'
    RETRY_WAIT = 'retry_wait'
    INTERVAL_WAIT = 'interval_wait'
    PAUSING = 'pausing'
    PAUSED = 'paused'
    STOPPING = 'stopping'
    COMPLETED = 'completed'
    ABORTED = 'aborted'


class PatrolCommand(Enum):
    """Side effects requested by a state transition."""

    NONE = 'none'
    SEND_GOAL = 'send_goal'
    CANCEL_GOAL = 'cancel_goal'
    START_DWELL = 'start_dwell'
    START_RETRY_WAIT = 'start_retry_wait'
    START_INTERVAL_WAIT = 'start_interval_wait'


@dataclass(frozen=True)
class Transition:
    """One state transition and the side effect it requests."""

    command: PatrolCommand
    duration_seconds: float = 0.0
    message: str = ''


class PatrolProgress:
    """Track waypoint, retry, cycle, pause, and repeat progress."""

    def __init__(self, route: PatrolRoute):
        """Create idle progress for a validated route."""
        self.route = route
        self.state = PatrolState.IDLE
        self.current_index = 0
        self.current_retries = 0
        self.run_cycles_completed = 0
        self.total_cycles_completed = 0
        self._paused_from: PatrolState | None = None

    @property
    def current_waypoint(self) -> Waypoint:
        """Return the current route waypoint."""
        return self.route.waypoints[self.current_index]

    @property
    def is_active(self) -> bool:
        """Return whether a patrol run or repeat schedule is active."""
        return self.state in {
            PatrolState.DWELLING,
            PatrolState.INTERVAL_WAIT,
            PatrolState.NAVIGATING,
            PatrolState.PAUSING,
            PatrolState.PAUSED,
            PatrolState.RETRY_WAIT,
            PatrolState.STOPPING,
        }

    def start(self) -> Transition:
        """Start a new finite run from the first waypoint."""
        if self.is_active:
            raise RuntimeError(f'cannot start while state is {self.state.value}')
        self.current_index = 0
        self.current_retries = 0
        self.run_cycles_completed = 0
        self._paused_from = None
        self.state = PatrolState.NAVIGATING
        return Transition(
            PatrolCommand.SEND_GOAL,
            message='patrol started',
        )

    def stop(self) -> Transition:
        """Stop the current run and disarm interval repetition."""
        if self.state == PatrolState.STOPPING:
            raise RuntimeError('patrol is already stopping')
        if self.state == PatrolState.PAUSING:
            self.state = PatrolState.STOPPING
            self._paused_from = None
            return Transition(
                PatrolCommand.NONE,
                message='patrol stop requested; waiting for Nav2 to stop',
            )
        if self.state == PatrolState.NAVIGATING:
            self.state = PatrolState.STOPPING
            self._paused_from = None
            return Transition(
                PatrolCommand.CANCEL_GOAL,
                message='patrol stop requested; waiting for Nav2 to stop',
            )
        self._finish_stop()
        return Transition(
            PatrolCommand.NONE,
            message='patrol stopped',
        )

    def _finish_stop(self) -> None:
        """Reset progress after motion is confirmed stopped."""
        self.state = PatrolState.IDLE
        self.current_index = 0
        self.current_retries = 0
        self.run_cycles_completed = 0
        self._paused_from = None

    def pause(self) -> Transition:
        """Pause the active run and cancel an active Nav2 goal."""
        if (
            not self.is_active
            or self.state
            in {
                PatrolState.PAUSED,
                PatrolState.PAUSING,
                PatrolState.STOPPING,
            }
        ):
            raise RuntimeError(f'cannot pause while state is {self.state.value}')
        self._paused_from = self.state
        if self.state == PatrolState.NAVIGATING:
            self.state = PatrolState.PAUSING
            return Transition(
                PatrolCommand.CANCEL_GOAL,
                message='patrol pause requested; waiting for Nav2 to stop',
            )
        self.state = PatrolState.PAUSED
        return Transition(
            PatrolCommand.NONE,
            message='patrol paused',
        )

    def resume(self) -> Transition:
        """Resume the paused phase from its beginning."""
        if self.state != PatrolState.PAUSED or self._paused_from is None:
            raise RuntimeError(f'cannot resume while state is {self.state.value}')
        previous = self._paused_from
        self._paused_from = None
        self.state = previous
        if previous == PatrolState.NAVIGATING:
            return Transition(
                PatrolCommand.SEND_GOAL,
                message='navigation resumed',
            )
        if previous == PatrolState.DWELLING:
            return Transition(
                PatrolCommand.START_DWELL,
                self.current_waypoint.dwell_seconds,
                'waypoint dwell resumed',
            )
        if previous == PatrolState.RETRY_WAIT:
            return Transition(
                PatrolCommand.START_RETRY_WAIT,
                self.current_waypoint.retry_backoff_seconds,
                'retry wait resumed',
            )
        if previous == PatrolState.INTERVAL_WAIT:
            return Transition(
                PatrolCommand.START_INTERVAL_WAIT,
                self.route.schedule.interval_seconds,
                'patrol interval resumed',
            )
        raise RuntimeError(f'unsupported paused state: {previous.value}')

    def cancellation_finished(self, goal_succeeded: bool) -> Transition:
        """Finish an in-flight pause or stop after the goal is terminal."""
        if self.state == PatrolState.STOPPING:
            self._finish_stop()
            return Transition(
                PatrolCommand.NONE,
                message='patrol stopped',
            )
        self._require(PatrolState.PAUSING)
        if not goal_succeeded:
            self.state = PatrolState.PAUSED
            return Transition(
                PatrolCommand.NONE,
                message='patrol paused',
            )

        self.state = PatrolState.NAVIGATING
        transition = self.goal_succeeded()
        if self.state in {
            PatrolState.DWELLING,
            PatrolState.INTERVAL_WAIT,
            PatrolState.NAVIGATING,
            PatrolState.RETRY_WAIT,
        }:
            self._paused_from = self.state
            self.state = PatrolState.PAUSED
            return Transition(
                PatrolCommand.NONE,
                message='patrol paused after the current goal completed',
            )
        return transition

    def goal_succeeded(self) -> Transition:
        """Handle successful completion of the current Nav2 goal."""
        self._require(PatrolState.NAVIGATING)
        self.current_retries = 0
        if self.current_waypoint.dwell_seconds > 0.0:
            self.state = PatrolState.DWELLING
            return Transition(
                PatrolCommand.START_DWELL,
                self.current_waypoint.dwell_seconds,
                f'reached {self.current_waypoint.name}',
            )
        return self._advance()

    def goal_failed(self, reason: str) -> Transition:
        """Apply retry, skip, or abort policy for a failed Nav2 goal."""
        self._require(PatrolState.NAVIGATING)
        waypoint = self.current_waypoint
        if self.current_retries < waypoint.max_retries:
            self.current_retries += 1
            self.state = PatrolState.RETRY_WAIT
            return Transition(
                PatrolCommand.START_RETRY_WAIT,
                waypoint.retry_backoff_seconds,
                (
                    f'{waypoint.name} failed; retry '
                    f'{self.current_retries}/{waypoint.max_retries}: {reason}'
                ),
            )
        self.current_retries = 0
        if waypoint.on_failure == 'skip':
            return self._advance(
                message=f'skipped {waypoint.name}: {reason}'
            )
        self.state = PatrolState.ABORTED
        return Transition(
            PatrolCommand.NONE,
            message=f'patrol aborted at {waypoint.name}: {reason}',
        )

    def goal_canceled(self, reason: str) -> Transition:
        """Stop automatic movement after an unexpected external cancel."""
        self._require(PatrolState.NAVIGATING)
        self.state = PatrolState.ABORTED
        return Transition(
            PatrolCommand.NONE,
            message=f'patrol aborted at {self.current_waypoint.name}: {reason}',
        )

    def dwell_elapsed(self) -> Transition:
        """Advance after the current waypoint dwell finishes."""
        self._require(PatrolState.DWELLING)
        return self._advance()

    def retry_wait_elapsed(self) -> Transition:
        """Retry the current waypoint after backoff."""
        self._require(PatrolState.RETRY_WAIT)
        self.state = PatrolState.NAVIGATING
        return Transition(
            PatrolCommand.SEND_GOAL,
            message=f'retrying {self.current_waypoint.name}',
        )

    def interval_elapsed(self) -> Transition:
        """Start the next scheduled run after the repeat interval."""
        self._require(PatrolState.INTERVAL_WAIT)
        self.current_index = 0
        self.current_retries = 0
        self.run_cycles_completed = 0
        self.state = PatrolState.NAVIGATING
        return Transition(
            PatrolCommand.SEND_GOAL,
            message='scheduled patrol run started',
        )

    def _advance(self, message: str = '') -> Transition:
        if self.current_index + 1 < len(self.route.waypoints):
            self.current_index += 1
            self.state = PatrolState.NAVIGATING
            return Transition(
                PatrolCommand.SEND_GOAL,
                message=message or f'next waypoint: {self.current_waypoint.name}',
            )

        self.run_cycles_completed += 1
        self.total_cycles_completed += 1
        if self.run_cycles_completed < self.route.cycles_per_run:
            self.current_index = 0
            self.state = PatrolState.NAVIGATING
            return Transition(
                PatrolCommand.SEND_GOAL,
                message='starting next route cycle',
            )

        if self.route.schedule.mode == 'interval':
            self.state = PatrolState.INTERVAL_WAIT
            return Transition(
                PatrolCommand.START_INTERVAL_WAIT,
                self.route.schedule.interval_seconds,
                'patrol run completed; waiting for next interval',
            )

        self.state = PatrolState.COMPLETED
        return Transition(
            PatrolCommand.NONE,
            message='patrol run completed',
        )

    def _require(self, expected: PatrolState) -> None:
        if self.state != expected:
            raise RuntimeError(
                f'expected state {expected.value}, got {self.state.value}'
            )
