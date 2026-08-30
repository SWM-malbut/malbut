"""
Apply scenario-only dispatch Safety faults after one durable claim.

The production worker, Safety policy, state source, target resolver, and
repository remain unchanged.  This module wraps the real repository and state
source so an allowlisted acceptance fault can be applied exactly once between
the approved Action claim and its dispatch-time Safety checks.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Callable, Optional

from malbut_agent_server.domain.robot_action import (
    ActionState,
    DispatchAuthorization,
    RobotAction,
)
from malbut_agent_server.ports.action_repository import (
    ActionClaim,
    DispatchIntent,
)
from malbut_agent_server.robot_state_source import RobotStateEvidence
from malbut_agent_server.schemas import RobotState
from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboSafetyProfile,
    coerce_safety_profile,
    safety_contract,
)


DISPATCH_SAFETY_OBSERVATION_FILENAME = (
    'swm25-137-dispatch-safety-fault.json'
)
_OBSERVATION_BYTES_LIMIT = 1024
_OBSERVATION_KEYS = frozenset({
    'safety_profile',
    'result_code',
    'claim_arm_count',
    'preclaim_read_count',
    'postclaim_read_count',
    'fault_application_count',
    'map_switch_count',
})
_MAXIMUM_STATE_AGE_SECONDS = 60.0
_MAXIMUM_STALE_MARGIN_SECONDS = 5.0


class DispatchSafetyFaultError(RuntimeError):
    """Expose one bounded failure without identities, paths, or state."""

    _CODES = frozenset({
        'dispatch_safety_fault_closed',
        'dispatch_safety_fault_map_switch_failed',
        'dispatch_safety_fault_observation_invalid',
        'dispatch_safety_fault_sequence_invalid',
        'dispatch_safety_fault_stale_window_invalid',
        'dispatch_safety_fault_state_invalid',
    })

    def __init__(self, code: str) -> None:
        """Normalize unknown diagnostics to one content-free error code."""
        normalized = (
            code
            if code in self._CODES
            else 'dispatch_safety_fault_sequence_invalid'
        )
        super().__init__(normalized)
        self.code = normalized


@dataclass(frozen=True, slots=True)
class DispatchSafetyFaultObservation:
    """Content-free proof of one exact post-claim Safety injection."""

    safety_profile: TextGazeboSafetyProfile
    result_code: str | None
    claim_arm_count: int
    preclaim_read_count: int
    postclaim_read_count: int
    fault_application_count: int
    map_switch_count: int

    def __post_init__(self) -> None:
        """Accept only the exact count contract for the selected profile."""
        if not isinstance(self.safety_profile, TextGazeboSafetyProfile):
            raise TypeError(
                'safety_profile must be a TextGazeboSafetyProfile'
            )
        contract = safety_contract(self.safety_profile)
        for name in (
            'claim_arm_count',
            'preclaim_read_count',
            'postclaim_read_count',
            'fault_application_count',
            'map_switch_count',
        ):
            if type(getattr(self, name)) is not int:
                raise TypeError(f'{name} must be an integer')
        if (
            self.result_code != contract.result_code
            or self.claim_arm_count != 1
            or self.preclaim_read_count != 1
            or self.postclaim_read_count != 1
            or self.fault_application_count
            != contract.fault_application_count
            or self.map_switch_count != contract.map_switch_count
        ):
            raise ValueError('dispatch Safety observation is invalid')

    def as_dict(self) -> dict[str, object]:
        """Return the fixed public-safe JSON projection."""
        return {
            'claim_arm_count': self.claim_arm_count,
            'fault_application_count': self.fault_application_count,
            'map_switch_count': self.map_switch_count,
            'postclaim_read_count': self.postclaim_read_count,
            'preclaim_read_count': self.preclaim_read_count,
            'result_code': self.result_code,
            'safety_profile': self.safety_profile.value,
        }


def dispatch_safety_observation_path(
    database_path: str | Path,
) -> Path:
    """Return the fixed private observation beside one durable runtime DB."""
    if type(database_path) is str:
        if (
            not database_path
            or database_path != database_path.strip()
        ):
            raise ValueError('database_path is invalid')
        raw = Path(database_path)
    elif isinstance(database_path, Path):
        raw = database_path
    else:
        raise ValueError('database_path is invalid')
    name = raw.name
    if (
        not name
        or name != name.strip()
        or name in {'.', '..', ':memory:'}
    ):
        raise ValueError(
            'dispatch Safety evidence requires a durable database'
        )
    try:
        database = raw.expanduser().resolve(strict=False)
        parent = database.parent.resolve(strict=True)
        if database.exists() and not database.is_file():
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise DispatchSafetyFaultError(
            'dispatch_safety_fault_observation_invalid'
        ) from None
    return parent / DISPATCH_SAFETY_OBSERVATION_FILENAME


def read_dispatch_safety_observation(
    path: Path,
) -> DispatchSafetyFaultObservation:
    """Read one owner-private, regular, canonical observation file."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise DispatchSafetyFaultError(
            'dispatch_safety_fault_observation_invalid'
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
        observation = DispatchSafetyFaultObservation(
            safety_profile=TextGazeboSafetyProfile(
                value['safety_profile']
            ),
            result_code=value['result_code'],
            claim_arm_count=value['claim_arm_count'],
            preclaim_read_count=value['preclaim_read_count'],
            postclaim_read_count=value['postclaim_read_count'],
            fault_application_count=value['fault_application_count'],
            map_switch_count=value['map_switch_count'],
        )
        if payload != _canonical_observation(observation):
            raise ValueError
        return observation
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise DispatchSafetyFaultError(
            'dispatch_safety_fault_observation_invalid'
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _unique_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
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


def _canonical_observation(
    observation: DispatchSafetyFaultObservation,
) -> bytes:
    return json.dumps(
        observation.as_dict(),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('ascii')


class DispatchMapRevisionGate:
    """Switch one catalog loader exactly once at the injected boundary."""

    def __init__(
        self,
        initial_loader: Callable[[], object],
        changed_loader: Callable[[], object],
    ) -> None:
        """Bind two server-owned loaders without reading either one."""
        if not callable(initial_loader) or not callable(changed_loader):
            raise TypeError('map revision loaders must be callable')
        self._initial_loader = initial_loader
        self._changed_loader = changed_loader
        self._lock = threading.Lock()
        self._switched = False
        self._switch_count = 0

    def __repr__(self) -> str:
        """Render only whether the bounded switch has occurred."""
        with self._lock:
            switched = self._switched
        return f'DispatchMapRevisionGate(switched={switched!r})'

    @property
    def switch_count(self) -> int:
        """Return the aggregate number of completed switches."""
        with self._lock:
            return self._switch_count

    def load(self) -> object:
        """Load from the currently selected server-owned catalog source."""
        with self._lock:
            loader = (
                self._changed_loader
                if self._switched
                else self._initial_loader
            )
        return loader()

    def switch(self) -> None:
        """Make the changed loader authoritative exactly once."""
        with self._lock:
            if self._switched or self._switch_count != 0:
                raise DispatchSafetyFaultError(
                    'dispatch_safety_fault_map_switch_failed'
                )
            self._switched = True
            self._switch_count = 1


class DispatchSafetyFaultCoordinator:
    """Apply one allowlisted fault after one successful durable claim."""

    def __init__(
        self,
        database_path: str | Path,
        safety_profile: TextGazeboSafetyProfile | str,
        *,
        observation_path: Path | None = None,
        map_switch_callback: Callable[[], None] | None = None,
        maximum_state_age_seconds: float = 2.0,
        stale_margin_seconds: float = 0.1,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind one exact claim/read sequence and fixed private output."""
        profile = coerce_safety_profile(safety_profile)
        maximum_age = _positive_finite(
            maximum_state_age_seconds,
            'maximum_state_age_seconds',
        )
        margin = _positive_finite(
            stale_margin_seconds,
            'stale_margin_seconds',
        )
        if maximum_age > _MAXIMUM_STATE_AGE_SECONDS:
            raise ValueError('maximum_state_age_seconds is too large')
        if margin > _MAXIMUM_STALE_MARGIN_SECONDS:
            raise ValueError('stale_margin_seconds is too large')
        if not callable(clock) or not callable(sleeper):
            raise TypeError('clock and sleeper must be callable')
        if profile is TextGazeboSafetyProfile.MAP_REVISION_CHANGED:
            if not callable(map_switch_callback):
                raise TypeError(
                    'map revision fault requires a switch callback'
                )
        elif map_switch_callback is not None:
            raise ValueError(
                'map switch callback is valid only for map revision fault'
            )

        expected_path = dispatch_safety_observation_path(database_path)
        if observation_path is None:
            selected_path = expected_path
        elif (
            not isinstance(observation_path, Path)
            or not observation_path.is_absolute()
            or observation_path != expected_path
        ):
            raise DispatchSafetyFaultError(
                'dispatch_safety_fault_observation_invalid'
            )
        else:
            selected_path = observation_path
        try:
            os.lstat(selected_path)
        except FileNotFoundError:
            pass
        except OSError:
            raise DispatchSafetyFaultError(
                'dispatch_safety_fault_observation_invalid'
            ) from None
        else:
            raise DispatchSafetyFaultError(
                'dispatch_safety_fault_observation_invalid'
            )

        self._profile = profile
        self._observation_path = selected_path
        self._map_switch_callback = map_switch_callback
        self._maximum_state_age_seconds = maximum_age
        self._stale_margin_seconds = margin
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._closed = False
        self._error: DispatchSafetyFaultError | None = None
        self._claim_created_at: float | None = None
        self._claim_arm_count = 0
        self._preclaim_read_count = 0
        self._postclaim_read_count = 0
        self._fault_application_count = 0
        self._map_switch_count = 0
        self._observation: DispatchSafetyFaultObservation | None = None

    def __repr__(self) -> str:
        """Render only the allowlisted profile and aggregate completion."""
        with self._lock:
            completed = self._observation is not None
        return (
            'DispatchSafetyFaultCoordinator('
            f'safety_profile={self._profile.value!r}, '
            f'completed={completed!r})'
        )

    @property
    def observation_path(self) -> Path:
        """Return the fixed private output for its explicit owner."""
        return self._observation_path

    @property
    def safety_profile(self) -> TextGazeboSafetyProfile:
        """Return the selected allowlisted behavior profile."""
        return self._profile

    def close(self) -> None:
        """Make any incomplete future claim/read sequence fail closed."""
        with self._lock:
            self._closed = True

    def arm_after_claim(self, claim: ActionClaim) -> None:
        """Arm only after the production repository returned one claim."""
        if (
            type(claim) is not ActionClaim
            or type(claim.action) is not RobotAction
            or claim.action.state is not ActionState.CLAIMED
        ):
            raise DispatchSafetyFaultError(
                'dispatch_safety_fault_sequence_invalid'
            )
        with self._lock:
            self._raise_if_unavailable_locked()
            if (
                self._claim_arm_count != 0
                or self._preclaim_read_count != 1
                or self._postclaim_read_count != 0
            ):
                self._fail_locked(
                    'dispatch_safety_fault_sequence_invalid'
                )
                raise self._error  # type: ignore[misc]
            self._claim_created_at = float(claim.action.created_at)
            self._claim_arm_count = 1

    def after_real_read(
        self,
        evidence: RobotStateEvidence,
    ) -> RobotStateEvidence:
        """Return the real sample or apply one post-claim fault to it."""
        if type(evidence) is not RobotStateEvidence:
            raise DispatchSafetyFaultError(
                'dispatch_safety_fault_state_invalid'
            )
        with self._lock:
            self._raise_if_unavailable_locked()
            if self._claim_arm_count == 0:
                if (
                    self._preclaim_read_count != 0
                    or self._postclaim_read_count != 0
                ):
                    self._fail_locked(
                        'dispatch_safety_fault_sequence_invalid'
                    )
                    raise self._error  # type: ignore[misc]
                self._preclaim_read_count = 1
                return evidence
            if (
                self._claim_arm_count != 1
                or self._preclaim_read_count != 1
                or self._postclaim_read_count != 0
                or self._observation is not None
            ):
                self._fail_locked(
                    'dispatch_safety_fault_sequence_invalid'
                )
                raise self._error  # type: ignore[misc]
            self._postclaim_read_count = 1
            claim_created_at = self._claim_created_at

        try:
            transformed = self._apply_fault(evidence, claim_created_at)
        except DispatchSafetyFaultError as error:
            with self._lock:
                self._fail_locked(error.code)
                assert self._error is not None
                raise self._error from None
        except Exception:
            with self._lock:
                code = (
                    'dispatch_safety_fault_map_switch_failed'
                    if self._profile
                    is TextGazeboSafetyProfile.MAP_REVISION_CHANGED
                    else 'dispatch_safety_fault_sequence_invalid'
                )
                self._fail_locked(code)
                assert self._error is not None
                raise self._error from None

        with self._lock:
            self._raise_if_unavailable_locked()
            contract = safety_contract(self._profile)
            self._fault_application_count = (
                contract.fault_application_count
            )
            self._map_switch_count = contract.map_switch_count
            observation = DispatchSafetyFaultObservation(
                safety_profile=self._profile,
                result_code=contract.result_code,
                claim_arm_count=self._claim_arm_count,
                preclaim_read_count=self._preclaim_read_count,
                postclaim_read_count=self._postclaim_read_count,
                fault_application_count=self._fault_application_count,
                map_switch_count=self._map_switch_count,
            )
            try:
                _write_observation(self._observation_path, observation)
            except DispatchSafetyFaultError as error:
                self._fail_locked(error.code)
                assert self._error is not None
                raise self._error from None
            self._observation = observation
            return transformed

    def completed_observation(self) -> DispatchSafetyFaultObservation:
        """Return the complete proof or fail instead of exposing partials."""
        with self._lock:
            self._raise_if_unavailable_locked()
            if self._observation is None:
                raise DispatchSafetyFaultError(
                    'dispatch_safety_fault_sequence_invalid'
                )
            return self._observation

    def _apply_fault(
        self,
        evidence: RobotStateEvidence,
        claim_created_at: float | None,
    ) -> RobotStateEvidence:
        profile = self._profile
        if profile is TextGazeboSafetyProfile.NONE:
            return evidence
        if profile is TextGazeboSafetyProfile.STALE_STATE:
            if (
                claim_created_at is None
                or evidence.observed_at < claim_created_at
            ):
                raise DispatchSafetyFaultError(
                    'dispatch_safety_fault_stale_window_invalid'
                )
            observed_at = float(evidence.observed_at)
            now = self._now()
            original_age = now - observed_at
            if (
                original_age < 0
                or original_age > self._maximum_state_age_seconds
            ):
                raise DispatchSafetyFaultError(
                    'dispatch_safety_fault_stale_window_invalid'
                )
            stale_at = (
                observed_at
                + self._maximum_state_age_seconds
                + self._stale_margin_seconds
            )
            if now < stale_at:
                self._sleeper(stale_at - now)
            if (
                self._now() - observed_at
                <= self._maximum_state_age_seconds
            ):
                raise DispatchSafetyFaultError(
                    'dispatch_safety_fault_stale_window_invalid'
                )
            return evidence
        if profile is TextGazeboSafetyProfile.EMERGENCY_STOP:
            return _emergency_stop_evidence(evidence)
        if profile is TextGazeboSafetyProfile.MAP_REVISION_CHANGED:
            callback = self._map_switch_callback
            if callback is None:
                raise DispatchSafetyFaultError(
                    'dispatch_safety_fault_map_switch_failed'
                )
            callback()
            return evidence
        raise DispatchSafetyFaultError(
            'dispatch_safety_fault_sequence_invalid'
        )

    def _raise_if_unavailable_locked(self) -> None:
        if self._error is not None:
            raise self._error
        if self._closed:
            self._fail_locked('dispatch_safety_fault_closed')
            raise self._error  # type: ignore[misc]

    def _fail_locked(self, code: str) -> None:
        if self._error is None:
            self._error = DispatchSafetyFaultError(code)

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception:
            raise DispatchSafetyFaultError(
                'dispatch_safety_fault_stale_window_invalid'
            ) from None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise DispatchSafetyFaultError(
                'dispatch_safety_fault_stale_window_invalid'
            )
        return float(value)


class DispatchSafetyRobotStateSource:
    """Read the real source first, then apply one coordinated test fault."""

    def __init__(
        self,
        delegate: object,
        coordinator: DispatchSafetyFaultCoordinator,
    ) -> None:
        """Bind one real source and its post-claim coordinator."""
        if not callable(getattr(delegate, 'read', None)):
            raise TypeError('delegate must implement read')
        if not isinstance(coordinator, DispatchSafetyFaultCoordinator):
            raise TypeError('coordinator is invalid')
        self._delegate = delegate
        self._coordinator = coordinator

    def __repr__(self) -> str:
        """Render no delegate, evidence, identity, or filesystem value."""
        return (
            'DispatchSafetyRobotStateSource('
            f'safety_profile={self._coordinator.safety_profile.value!r})'
        )

    def read(self) -> RobotStateEvidence:
        """Consume one real observation before scenario processing."""
        evidence = self._delegate.read()
        if type(evidence) is not RobotStateEvidence:
            raise DispatchSafetyFaultError(
                'dispatch_safety_fault_state_invalid'
            )
        return self._coordinator.after_real_read(evidence)


class ClaimArmedActionRepository:
    """Arm the Safety fault only after the real repository returns a claim."""

    def __init__(
        self,
        repository: object,
        coordinator: DispatchSafetyFaultCoordinator,
    ) -> None:
        """Bind one production repository without owning or changing it."""
        for name in (
            'get',
            'find_by_confirmation',
            'claim_next',
            'record_dispatch_intent',
            'block',
            'mark_started',
            'finish',
            'recover_uncertain_after_restart',
        ):
            if not callable(getattr(repository, name, None)):
                raise TypeError('repository does not implement the port')
        if not isinstance(coordinator, DispatchSafetyFaultCoordinator):
            raise TypeError('coordinator is invalid')
        self._repository = repository
        self._coordinator = coordinator
        self._armed = False

    def __repr__(self) -> str:
        """Never render the wrapped repository or durable identity."""
        return 'ClaimArmedActionRepository(configured=True)'

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
        """Delegate every claim and arm once after the first real winner."""
        claim = self._repository.claim_next(
            worker_id,
            now=now,
            lease_for=lease_for,
        )
        if claim is not None and not self._armed:
            if type(claim) is not ActionClaim:
                raise DispatchSafetyFaultError(
                    'dispatch_safety_fault_sequence_invalid'
                )
            self._coordinator.arm_after_claim(claim)
            self._armed = True
        return claim

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
        """Delegate one known terminal execution result."""
        return self._repository.finish(
            intent,
            state,
            result_code=result_code,
            now=now,
        )

    def recover_uncertain_after_restart(self, *, now: float) -> int:
        """Delegate conservative restart reconciliation."""
        return self._repository.recover_uncertain_after_restart(now=now)


def _emergency_stop_evidence(
    evidence: RobotStateEvidence,
) -> RobotStateEvidence:
    """Clone through the real RobotState type and bind a new evidence ID."""
    source = evidence.state
    state = RobotState(
        battery_percent=source.battery_percent,
        navigation_available=source.navigation_available,
        localization_ok=source.localization_ok,
        emergency_stop=True,
        camera_available=source.camera_available,
        privacy_mode=source.privacy_mode,
        docked=source.docked,
        forbidden_zones=tuple(source.forbidden_zones),
    )
    material = (
        evidence.evidence_id + '\0swm25-137-emergency-stop'
    ).encode('utf-8')
    return RobotStateEvidence(
        state=state,
        observed_at=evidence.observed_at,
        evidence_id=(
            'swm25-137-estop-'
            + hashlib.sha256(material).hexdigest()
        ),
        trusted=evidence.trusted,
    )


def _positive_finite(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f'{name} must be a positive finite number')
    return float(value)


def _write_observation(
    path: Path,
    observation: DispatchSafetyFaultObservation,
) -> None:
    """Publish one complete file without following or overwriting names."""
    payload = _canonical_observation(observation)
    if not payload or len(payload) > _OBSERVATION_BYTES_LIMIT:
        raise DispatchSafetyFaultError(
            'dispatch_safety_fault_observation_invalid'
        )
    parent = path.parent
    temporary = parent / (
        '.swm25-137-dispatch-safety-'
        + secrets.token_hex(16)
        + '.tmp'
    )
    descriptor: Optional[int] = None
    temporary_created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
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
        raise DispatchSafetyFaultError(
            'dispatch_safety_fault_observation_invalid'
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
            except (FileNotFoundError, OSError):
                pass


__all__ = [
    'ClaimArmedActionRepository',
    'DISPATCH_SAFETY_OBSERVATION_FILENAME',
    'DispatchMapRevisionGate',
    'DispatchSafetyFaultCoordinator',
    'DispatchSafetyFaultError',
    'DispatchSafetyFaultObservation',
    'DispatchSafetyRobotStateSource',
    'dispatch_safety_observation_path',
    'read_dispatch_safety_observation',
]
