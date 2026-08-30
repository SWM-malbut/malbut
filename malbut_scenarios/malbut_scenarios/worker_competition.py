"""
Scenario-only coordination for a two-worker SQLite claim race.

The production repository, worker, and lease/fence algorithms remain
unchanged.  This module merely holds both acceptance workers immediately
before their first claim until one approved action is durably visible, then
releases both contenders and waits for both claim outcomes before allowing
the winner to continue toward an external effect.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
from typing import Callable, Optional

from malbut_agent_server.application.approved_action_worker import (
    ApprovedActionWorker,
)
from malbut_agent_server.domain.robot_action import (
    ActionState,
    DispatchAuthorization,
    RobotAction,
)
from malbut_agent_server.ports.action_repository import (
    ActionClaim,
    DispatchIntent,
)


WORKER_COMPETITION_OBSERVATION_FILENAME = (
    'swm25-136-worker-competition.json'
)
_OBSERVATION_KEYS = frozenset({
    'fault_profile',
    'contender_count',
    'winner_count',
    'nonwinner_count',
})
_OBSERVATION_BYTES_LIMIT = 512
_DEFAULT_GATE_TIMEOUT_SECONDS = 120.0
_POLL_INTERVAL_SECONDS = 0.025


class WorkerCompetitionError(RuntimeError):
    """Expose one stable failure without identities, tokens, or paths."""

    _CODES = frozenset({
        'worker_competition_closed',
        'worker_competition_gate_timeout',
        'worker_competition_contender_invalid',
        'worker_competition_outcome_invalid',
        'worker_competition_observation_invalid',
    })

    def __init__(self, code: str) -> None:
        """Normalize every diagnostic to one public-safe error code."""
        normalized = (
            code
            if code in self._CODES
            else 'worker_competition_outcome_invalid'
        )
        super().__init__(normalized)
        self.code = normalized


@dataclass(frozen=True, slots=True)
class WorkerCompetitionObservation:
    """Content-free proof of the bounded two-contender claim result."""

    fault_profile: str = 'competing_workers'
    contender_count: int = 2
    winner_count: int = 1
    nonwinner_count: int = 1

    def __post_init__(self) -> None:
        """Reject anything except the exact bounded acceptance result."""
        if (
            self.fault_profile != 'competing_workers'
            or type(self.contender_count) is not int
            or type(self.winner_count) is not int
            or type(self.nonwinner_count) is not int
            or self.contender_count != 2
            or self.winner_count != 1
            or self.nonwinner_count != 1
        ):
            raise ValueError('worker competition observation is invalid')

    def as_dict(self) -> dict[str, object]:
        """Return the strict public-safe JSON projection."""
        return {
            'fault_profile': self.fault_profile,
            'contender_count': self.contender_count,
            'winner_count': self.winner_count,
            'nonwinner_count': self.nonwinner_count,
        }


def worker_competition_observation_path(database_path: str) -> Path:
    """Return the fixed private observation beside the fresh runtime DB."""
    if type(database_path) is not str or not database_path.strip():
        raise ValueError('database_path is invalid')
    if database_path == ':memory:':
        raise ValueError('worker competition requires a durable database')
    try:
        database = Path(database_path).expanduser().resolve(strict=False)
        parent = database.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise WorkerCompetitionError(
            'worker_competition_observation_invalid'
        ) from None
    return parent / WORKER_COMPETITION_OBSERVATION_FILENAME


def read_worker_competition_observation(
    path: Path,
) -> WorkerCompetitionObservation:
    """Read one owner-private, regular, strict observation file."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise WorkerCompetitionError(
            'worker_competition_observation_invalid'
        )
    descriptor: Optional[int] = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_size <= 0
            or metadata.st_size > _OBSERVATION_BYTES_LIMIT
        ):
            raise ValueError
        payload = b''
        while len(payload) <= _OBSERVATION_BYTES_LIMIT:
            chunk = os.read(
                descriptor,
                _OBSERVATION_BYTES_LIMIT + 1 - len(payload),
            )
            if not chunk:
                break
            payload += chunk
        if len(payload) != metadata.st_size:
            raise ValueError
        value = json.loads(
            payload.decode('utf-8', errors='strict'),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        if type(value) is not dict or set(value) != _OBSERVATION_KEYS:
            raise ValueError
        observation = WorkerCompetitionObservation(
            fault_profile=value['fault_profile'],
            contender_count=value['contender_count'],
            winner_count=value['winner_count'],
            nonwinner_count=value['nonwinner_count'],
        )
        canonical = json.dumps(
            observation.as_dict(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('ascii')
        if payload != canonical:
            raise ValueError
        return observation
    except (
        KeyError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise WorkerCompetitionError(
            'worker_competition_observation_invalid'
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate or non-string JSON object keys."""
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    """Reject non-standard NaN and infinity tokens."""
    raise ValueError


class WorkerCompetitionCoordinator:
    """Open and verify exactly one bounded two-worker claim window."""

    def __init__(
        self,
        database_path: str,
        *,
        observation_path: Optional[Path] = None,
        timeout_seconds: float = _DEFAULT_GATE_TIMEOUT_SECONDS,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind one fresh durable DB and one fixed private output file."""
        if type(database_path) is not str or not database_path.strip():
            raise ValueError('database_path is invalid')
        if database_path == ':memory:':
            raise ValueError('worker competition requires a durable database')
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.0 < float(timeout_seconds) <= 300.0
        ):
            raise ValueError('timeout_seconds is invalid')
        if not callable(monotonic_clock):
            raise TypeError('monotonic_clock must be callable')
        self._database_path = str(
            Path(database_path).expanduser().resolve(strict=False)
        )
        expected_path = worker_competition_observation_path(database_path)
        if observation_path is None:
            selected_path = expected_path
        elif (
            not isinstance(observation_path, Path)
            or not observation_path.is_absolute()
            or observation_path != expected_path
        ):
            raise WorkerCompetitionError(
                'worker_competition_observation_invalid'
            )
        else:
            selected_path = observation_path
        try:
            os.lstat(selected_path)
        except FileNotFoundError:
            pass
        except OSError:
            raise WorkerCompetitionError(
                'worker_competition_observation_invalid'
            ) from None
        else:
            raise WorkerCompetitionError(
                'worker_competition_observation_invalid'
            )
        self._observation_path = selected_path
        self._timeout_seconds = float(timeout_seconds)
        self._monotonic_clock = monotonic_clock
        self._condition = threading.Condition()
        self._entered: set[int] = set()
        self._outcomes: dict[int, bool] = {}
        self._gate_open = False
        self._resolved = False
        self._closed = False
        self._deadline: Optional[float] = None
        self._error: Optional[WorkerCompetitionError] = None

    def __repr__(self) -> str:
        """Never render the private database or observation path."""
        return 'WorkerCompetitionCoordinator(configured=True)'

    @property
    def observation_path(self) -> Path:
        """Return the private path for its explicit acceptance owner."""
        return self._observation_path

    def close(self) -> None:
        """Unblock contenders during owner-directed shutdown."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def await_preclaim(self, contender: int) -> None:
        """Hold one worker before its claim-time clock is sampled."""
        if contender not in {0, 1}:
            raise WorkerCompetitionError(
                'worker_competition_contender_invalid'
            )
        with self._condition:
            if self._resolved:
                return
            if contender in self._entered:
                raise WorkerCompetitionError(
                    'worker_competition_contender_invalid'
                )
            self._entered.add(contender)
            if self._deadline is None:
                self._deadline = self._now() + self._timeout_seconds
            self._await_gate_locked()

    def claim(
        self,
        contender: int,
        claim_operation: Callable[[], Optional[ActionClaim]],
    ) -> Optional[ActionClaim]:
        """Synchronize both durable claim outcomes before either returns."""
        if contender not in {0, 1} or not callable(claim_operation):
            raise WorkerCompetitionError(
                'worker_competition_contender_invalid'
            )
        with self._condition:
            self._raise_if_stopped_locked()
            if (
                contender not in self._entered
                or not self._gate_open
                or contender in self._outcomes
            ):
                raise WorkerCompetitionError(
                    'worker_competition_contender_invalid'
                )

        try:
            claim = claim_operation()
        except Exception:
            with self._condition:
                self._fail_locked('worker_competition_outcome_invalid')
                assert self._error is not None
                raise self._error from None

        with self._condition:
            if contender in self._outcomes:
                self._fail_locked('worker_competition_outcome_invalid')
            self._outcomes[contender] = claim is not None
            self._condition.notify_all()
            self._await_outcomes_locked()
            if self._error is not None:
                raise self._error
            return claim

    def _await_gate_locked(self) -> None:
        while not self._gate_open:
            self._raise_if_stopped_locked()
            if len(self._entered) == 2 and self._approved_action_visible():
                self._gate_open = True
                self._condition.notify_all()
                return
            self._wait_locked()

    def _await_outcomes_locked(self) -> None:
        while len(self._outcomes) < 2 and self._error is None:
            self._raise_if_stopped_locked()
            self._wait_locked()
        if self._error is not None:
            raise self._error
        winners = sum(self._outcomes.values())
        if winners != 1:
            self._fail_locked('worker_competition_outcome_invalid')
            raise self._error  # type: ignore[misc]
        if not self._resolved:
            try:
                _write_observation(
                    self._observation_path,
                    WorkerCompetitionObservation(),
                )
            except Exception:
                self._fail_locked(
                    'worker_competition_observation_invalid'
                )
                raise self._error  # type: ignore[misc]
            self._resolved = True
            self._condition.notify_all()

    def _approved_action_visible(self) -> bool:
        try:
            connection = sqlite3.connect(
                self._database_path,
                timeout=0.1,
            )
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM robot_actions "
                    "WHERE state = 'PENDING_PREFLIGHT'"
                ).fetchone()
            finally:
                connection.close()
            return bool(row is not None and int(row[0]) == 1)
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return False

    def _wait_locked(self) -> None:
        assert self._deadline is not None
        remaining = self._deadline - self._now()
        if remaining <= 0:
            self._fail_locked('worker_competition_gate_timeout')
            raise self._error  # type: ignore[misc]
        self._condition.wait(
            timeout=min(_POLL_INTERVAL_SECONDS, remaining)
        )

    def _raise_if_stopped_locked(self) -> None:
        if self._error is not None:
            raise self._error
        if self._closed:
            self._fail_locked('worker_competition_closed')
            raise self._error  # type: ignore[misc]

    def _fail_locked(self, code: str) -> None:
        if self._error is None:
            self._error = WorkerCompetitionError(code)
        self._condition.notify_all()

    def _now(self) -> float:
        value = self._monotonic_clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise WorkerCompetitionError(
                'worker_competition_gate_timeout'
            )
        return float(value)


class CoordinatedActionRepository:
    """Delegate the repository port while coordinating its first claim."""

    def __init__(
        self,
        repository: object,
        coordinator: WorkerCompetitionCoordinator,
        *,
        contender: int,
    ) -> None:
        """Bind one independent repository to its contender number."""
        self._repository = repository
        self._coordinator = coordinator
        self._contender = contender
        self._competition_complete = False

    def __repr__(self) -> str:
        """Never render the wrapped repository or worker identity."""
        return 'CoordinatedActionRepository(configured=True)'

    def get(self, action_id: str) -> Optional[RobotAction]:
        """Delegate one latest-action lookup."""
        return self._repository.get(action_id)

    def find_by_confirmation(
        self,
        confirmation_request_id: str,
    ) -> Optional[RobotAction]:
        """Delegate one confirmation-bound action lookup."""
        return self._repository.find_by_confirmation(
            confirmation_request_id
        )

    def claim_next(
        self,
        worker_id: str,
        *,
        now: float,
        lease_for: float,
    ) -> Optional[ActionClaim]:
        """Coordinate only the first claim, then delegate normally."""
        def operation() -> Optional[ActionClaim]:
            return self._repository.claim_next(
                worker_id,
                now=now,
                lease_for=lease_for,
            )
        if not self._competition_complete:
            result = self._coordinator.claim(self._contender, operation)
            self._competition_complete = True
            return result
        return operation()

    def record_dispatch_intent(
        self,
        claim: ActionClaim,
        authorization: DispatchAuthorization,
        *,
        now: float,
    ) -> DispatchIntent:
        """Delegate one durable pre-effect dispatch intent."""
        return self._repository.record_dispatch_intent(
            claim,
            authorization,
            now=now,
        )

    def block(
        self,
        claim: ActionClaim,
        *,
        result_code: str,
        now: float,
    ) -> RobotAction:
        """Delegate one definite pre-dispatch block."""
        return self._repository.block(
            claim,
            result_code=result_code,
            now=now,
        )

    def mark_started(
        self,
        intent: DispatchIntent,
        *,
        now: float,
    ) -> DispatchIntent:
        """Delegate one known external-start record."""
        return self._repository.mark_started(intent, now=now)

    def finish(
        self,
        intent: DispatchIntent,
        state: ActionState,
        *,
        result_code: str,
        now: float,
    ) -> RobotAction:
        """Delegate one terminal execution result."""
        return self._repository.finish(
            intent,
            state,
            result_code=result_code,
            now=now,
        )

    def recover_uncertain_after_restart(self, *, now: float) -> int:
        """Delegate conservative restart reconciliation."""
        return self._repository.recover_uncertain_after_restart(now=now)


class CompetingApprovedActionWorker(ApprovedActionWorker):
    """Enter the scenario gate before sampling the real claim timestamp."""

    def __init__(
        self,
        *args: object,
        competition_coordinator: WorkerCompetitionCoordinator,
        contender: int,
        **kwargs: object,
    ) -> None:
        """Bind a normal worker to its scenario-only pre-claim gate."""
        if not isinstance(
            competition_coordinator,
            WorkerCompetitionCoordinator,
        ):
            raise TypeError('competition_coordinator is invalid')
        if contender not in {0, 1}:
            raise ValueError('contender is invalid')
        self._competition_coordinator = competition_coordinator
        self._competition_contender = contender
        self._competition_entered = False
        super().__init__(*args, **kwargs)

    def run_once(self) -> Optional[RobotAction]:
        """Coordinate only the first claim attempt for this worker."""
        if not self._competition_entered:
            self._competition_coordinator.await_preclaim(
                self._competition_contender
            )
            self._competition_entered = True
        return super().run_once()


def _write_observation(
    path: Path,
    observation: WorkerCompetitionObservation,
) -> None:
    """Publish one complete file without following or overwriting names."""
    payload = json.dumps(
        observation.as_dict(),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('ascii')
    if not payload or len(payload) > _OBSERVATION_BYTES_LIMIT:
        raise WorkerCompetitionError(
            'worker_competition_observation_invalid'
        )
    parent = path.parent
    temporary = parent / (
        '.swm25-136-worker-competition-'
        + secrets.token_hex(16)
        + '.tmp'
    )
    descriptor: Optional[int] = None
    temporary_created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        temporary_created = True
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, TypeError, ValueError):
        raise WorkerCompetitionError(
            'worker_competition_observation_invalid'
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_created:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass


__all__ = [
    'CompetingApprovedActionWorker',
    'CoordinatedActionRepository',
    'WORKER_COMPETITION_OBSERVATION_FILENAME',
    'WorkerCompetitionCoordinator',
    'WorkerCompetitionError',
    'WorkerCompetitionObservation',
    'read_worker_competition_observation',
    'worker_competition_observation_path',
]
