"""Content-free evidence for one text-to-Gazebo acceptance run."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Dict

from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboFaultProfile,
    TextGazeboScenarioProfile,
    pressure_contract,
)


EVIDENCE_FORMAT = 'malbut.text-gazebo-e2e-evidence.v4'
MAX_EVIDENCE_COUNT = 1_000_000
MAX_EVIDENCE_DURATION_SECONDS = 86_400.0

_RUN_ID = re.compile(r'run-[0-9a-f]{32}\Z')
_GIT_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')


class ReadinessState(str, Enum):
    """Stable, content-free runtime readiness states."""

    READY = 'ready'
    NOT_READY = 'not_ready'
    UNKNOWN = 'unknown'


class ConfirmationState(str, Enum):
    """Stable confirmation outcomes visible to the acceptance runner."""

    APPROVED = 'approved'
    NOT_APPROVED = 'not_approved'
    UNKNOWN = 'unknown'


class RobotActionState(str, Enum):
    """Stable terminal or non-created RobotAction states."""

    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    UNKNOWN = 'unknown'
    NOT_CREATED = 'not_created'


class DispatchState(str, Enum):
    """Stable durable-dispatch observation states."""

    TERMINAL = 'terminal'
    PENDING = 'pending'
    UNKNOWN = 'unknown'
    NOT_CREATED = 'not_created'


class NavigationState(str, Enum):
    """Stable Nav2 observations without exposing a goal identifier."""

    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    UNKNOWN = 'unknown'
    NOT_STARTED = 'not_started'


def _require_enum(value: object, expected: type[Enum], name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f'{name} must be a {expected.__name__}')


def _require_count(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_EVIDENCE_COUNT
    ):
        raise ValueError(f'{name} must be a bounded non-negative integer')


def _duration(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite duration')
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized < 0.0
        or normalized > MAX_EVIDENCE_DURATION_SECONDS
    ):
        raise ValueError(f'{name} must be a finite bounded duration')
    return 0.0 if normalized == 0.0 else normalized


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(',', ':'),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


@dataclass(frozen=True, slots=True)
class StableStates:
    """Bounded public states for the five observed runtime boundaries."""

    readiness: ReadinessState
    confirmation: ConfirmationState
    robot_action: RobotActionState
    dispatch: DispatchState
    navigation: NavigationState

    def __post_init__(self) -> None:
        """Reject strings and unknown enum types at construction time."""
        _require_enum(self.readiness, ReadinessState, 'readiness')
        _require_enum(self.confirmation, ConfirmationState, 'confirmation')
        _require_enum(self.robot_action, RobotActionState, 'robot_action')
        _require_enum(self.dispatch, DispatchState, 'dispatch')
        _require_enum(self.navigation, NavigationState, 'navigation')

    def as_dict(self) -> Dict[str, str]:
        """Return the exact public state projection."""
        return {
            'confirmation': self.confirmation.value,
            'dispatch': self.dispatch.value,
            'navigation': self.navigation.value,
            'readiness': self.readiness.value,
            'robot_action': self.robot_action.value,
        }


@dataclass(frozen=True, slots=True)
class EvidenceCounts:
    """Effect and ledger counts without their private identifiers."""

    agent_proposal_count: int
    confirmation_count: int
    approved_confirmation_count: int
    robot_action_count: int
    dispatch_intent_count: int
    robot_web_start_count: int
    robot_web_verified_target_count: int
    nav2_goal_count: int
    preapproval_nav2_goal_count: int
    terminal_result_count: int
    replay_additional_effect_count: int

    def __post_init__(self) -> None:
        """Require real, bounded integers rather than bool-like counts."""
        for name in self.__dataclass_fields__:
            _require_count(getattr(self, name), name)

    def as_dict(self) -> Dict[str, int]:
        """Return the exact public count projection."""
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class PressureEvidence:
    """Exact contention accounting without request or worker identities."""

    request_attempt_count: int
    approval_attempt_count: int
    worker_contender_count: int
    pressure_contender_count: int
    pressure_winner_count: int
    pressure_nonwinner_count: int

    def __post_init__(self) -> None:
        """Require bounded real integers for every public counter."""
        for name in self.__dataclass_fields__:
            _require_count(getattr(self, name), name)
        if (
            self.pressure_contender_count
            != self.pressure_winner_count + self.pressure_nonwinner_count
        ):
            raise ValueError(
                'pressure contenders must equal winners plus non-winners'
            )

    def as_dict(self) -> Dict[str, int]:
        """Return the fixed content-free pressure projection."""
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


def pressure_evidence_for(
    profile: TextGazeboFaultProfile,
) -> PressureEvidence:
    """Build the one exact pressure claim allowed for a fault profile."""
    contract = pressure_contract(profile)
    return PressureEvidence(
        request_attempt_count=contract.request_attempt_count,
        approval_attempt_count=contract.approval_attempt_count,
        worker_contender_count=contract.worker_contender_count,
        pressure_contender_count=contract.pressure_contender_count,
        pressure_winner_count=contract.pressure_winner_count,
        pressure_nonwinner_count=contract.pressure_nonwinner_count,
    )


@dataclass(frozen=True, slots=True)
class EvidenceDurations:
    """Bounded monotonic durations; wall-clock timestamps are excluded."""

    readiness_seconds: float
    execution_seconds: float
    cleanup_seconds: float
    total_seconds: float

    def __post_init__(self) -> None:
        """Normalize numeric inputs and reject NaN or infinity."""
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                _duration(getattr(self, name), name),
            )

    def as_dict(self) -> Dict[str, float]:
        """Return the exact public duration projection."""
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class CleanupEvidence:
    """Bounded cleanup result without process, node, socket, or path names."""

    completed: bool
    owned_processes_remaining: int
    ros_nodes_remaining: int
    owned_sockets_remaining: int
    forced_termination_count: int

    def __post_init__(self) -> None:
        """Require an explicit boolean and bounded aggregate counts."""
        if type(self.completed) is not bool:
            raise TypeError('completed must be a bool')
        for name in self.__dataclass_fields__:
            if name != 'completed':
                _require_count(getattr(self, name), name)

    def as_dict(self) -> Dict[str, object]:
        """Return the exact public cleanup projection."""
        return {
            'completed': self.completed,
            'forced_termination_count': self.forced_termination_count,
            'owned_processes_remaining': self.owned_processes_remaining,
            'owned_sockets_remaining': self.owned_sockets_remaining,
            'ros_nodes_remaining': self.ros_nodes_remaining,
        }


@dataclass(frozen=True, slots=True)
class TextGazeboEvidenceReceipt:
    """Immutable public facts from one text-to-Gazebo acceptance run."""

    run_id: str
    commit: str
    source_tree_digest: str
    installed_digest: str
    goal_set_digest: str
    runtime_binding_digest: str
    target_binding_digest: str
    scenario_profile: TextGazeboScenarioProfile
    states: StableStates
    counts: EvidenceCounts
    durations: EvidenceDurations
    cleanup: CleanupEvidence
    fault_profile: TextGazeboFaultProfile = TextGazeboFaultProfile.NONE
    pressure: PressureEvidence = field(
        default_factory=lambda: pressure_evidence_for(
            TextGazeboFaultProfile.NONE
        )
    )
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Accept only content-free identifiers and the typed projections."""
        if not isinstance(self.run_id, str) or not _RUN_ID.fullmatch(
            self.run_id
        ):
            raise ValueError(
                'run_id must use the public run identifier format'
            )
        if not isinstance(self.commit, str) or not _GIT_COMMIT.fullmatch(
            self.commit
        ):
            raise ValueError('commit must be a full lowercase Git object id')
        for name in (
            'installed_digest',
            'source_tree_digest',
            'goal_set_digest',
            'runtime_binding_digest',
            'target_binding_digest',
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f'{name} must be a lowercase SHA-256')
        _require_enum(
            self.scenario_profile,
            TextGazeboScenarioProfile,
            'scenario_profile',
        )
        _require_enum(
            self.fault_profile,
            TextGazeboFaultProfile,
            'fault_profile',
        )
        for name, expected in (
            ('states', StableStates),
            ('counts', EvidenceCounts),
            ('durations', EvidenceDurations),
            ('cleanup', CleanupEvidence),
            ('pressure', PressureEvidence),
        ):
            if not isinstance(getattr(self, name), expected):
                raise TypeError(f'{name} must be a {expected.__name__}')
        self._validate_success_claim()

    def _validate_success_claim(self) -> None:
        expected_states = StableStates(
            readiness=ReadinessState.READY,
            confirmation=ConfirmationState.APPROVED,
            robot_action=RobotActionState.SUCCEEDED,
            dispatch=DispatchState.TERMINAL,
            navigation=NavigationState.SUCCEEDED,
        )
        if self.states != expected_states:
            raise ValueError('a success receipt requires successful states')
        exact_counts = {
            'agent_proposal_count': 1,
            'confirmation_count': 1,
            'approved_confirmation_count': 1,
            'robot_action_count': 1,
            'dispatch_intent_count': 1,
            'robot_web_start_count': 1,
            'robot_web_verified_target_count': 1,
            'nav2_goal_count': 1,
            'preapproval_nav2_goal_count': 0,
            'terminal_result_count': 1,
            'replay_additional_effect_count': 0,
        }
        if any(
            getattr(self.counts, name) != expected
            for name, expected in exact_counts.items()
        ):
            raise ValueError('a success receipt requires exact-once counts')
        if self.pressure != pressure_evidence_for(self.fault_profile):
            raise ValueError(
                'a success receipt requires exact pressure evidence'
            )
        if (
            not self.cleanup.completed
            or self.cleanup.owned_processes_remaining != 0
            or self.cleanup.ros_nodes_remaining != 0
            or self.cleanup.owned_sockets_remaining != 0
            or self.cleanup.forced_termination_count != 0
        ):
            raise ValueError('a success receipt requires clean shutdown')

    def as_dict(self) -> Dict[str, object]:
        """Return the fixed, content-free receipt schema."""
        return {
            'cleanup': self.cleanup.as_dict(),
            'commit': self.commit,
            'counts': self.counts.as_dict(),
            'durations': self.durations.as_dict(),
            'fault_profile': self.fault_profile.value,
            'goal_set_digest': self.goal_set_digest,
            'installed_digest': self.installed_digest,
            'physical_authorized': self.physical_authorized,
            'pressure': self.pressure.as_dict(),
            'run_id': self.run_id,
            'runtime_binding_digest': self.runtime_binding_digest,
            'scenario_profile': self.scenario_profile.value,
            'simulation': self.simulation,
            'source_tree_digest': self.source_tree_digest,
            'states': self.states.as_dict(),
            'target_binding_digest': self.target_binding_digest,
        }

    def canonical_json(self) -> str:
        """Serialize the receipt deterministically without extra space."""
        return _canonical_json(self.as_dict())

    def digest(self) -> str:
        """Bind the complete canonical receipt with SHA-256."""
        return _sha256(self.canonical_json())

    def __repr__(self) -> str:
        """Show only the allowed run identifier and content digest."""
        return (
            'TextGazeboEvidenceReceipt('
            f'run_id={self.run_id!r}, digest={self.digest()!r})'
        )


@dataclass(frozen=True, slots=True)
class TextGazeboEvidenceManifest:
    """Versioned envelope binding one immutable acceptance receipt."""

    receipt: TextGazeboEvidenceReceipt
    format: str = field(default=EVIDENCE_FORMAT, init=False)
    receipt_digest: str = field(init=False)

    def __post_init__(self) -> None:
        """Bind the manifest to exactly one validated receipt."""
        if not isinstance(self.receipt, TextGazeboEvidenceReceipt):
            raise TypeError('receipt must be a TextGazeboEvidenceReceipt')
        object.__setattr__(self, 'receipt_digest', self.receipt.digest())

    def as_dict(self) -> Dict[str, object]:
        """Return the fixed, versioned manifest schema."""
        return {
            'format': self.format,
            'receipt': self.receipt.as_dict(),
            'receipt_digest': self.receipt_digest,
        }

    def canonical_json(self) -> str:
        """Serialize the manifest canonically for storage and verification."""
        return _canonical_json(self.as_dict())

    def digest(self) -> str:
        """Return a digest of the complete canonical manifest."""
        return _sha256(self.canonical_json())

    def __repr__(self) -> str:
        """Avoid expanding even the content-free receipt in diagnostics."""
        return (
            'TextGazeboEvidenceManifest('
            f'digest={self.digest()!r})'
        )


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError('evidence path cannot contain a symbolic link')


def _private_parent(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError('evidence path must be a pathlib.Path')
    if not path.is_absolute():
        raise ValueError('evidence path must be absolute')
    if path.name in {'', '.', '..'}:
        raise ValueError('evidence filename is invalid')
    parent = path.parent
    _reject_symlink_components(parent)
    created = False
    try:
        parent_metadata = os.lstat(parent)
    except FileNotFoundError:
        parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(parent, 0o700)
        created = True
        parent_metadata = os.lstat(parent)
    _reject_symlink_components(parent)
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError('evidence parent must be a directory')
    if parent_metadata.st_uid != os.getuid():
        raise PermissionError('evidence parent must be owned by this user')
    if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        if created:
            raise RuntimeError('new evidence parent is not private')
        raise PermissionError('evidence parent mode must be 0700')
    return parent


def write_evidence_manifest(
    path: Path,
    manifest: TextGazeboEvidenceManifest,
) -> str:
    """Atomically publish one owner-only manifest without overwriting."""
    if not isinstance(manifest, TextGazeboEvidenceManifest):
        raise TypeError('manifest must be a TextGazeboEvidenceManifest')
    destination = path
    parent = _private_parent(destination)
    try:
        existing = os.lstat(destination)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise ValueError('evidence destination cannot be a symbolic link')
        raise FileExistsError('evidence destination already exists')

    directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    directory_flags |= getattr(os, 'O_NOFOLLOW', 0)
    directory_descriptor = os.open(parent, directory_flags)
    descriptor = -1
    temporary = None
    try:
        parent_metadata = os.fstat(directory_descriptor)
        if (
            parent_metadata.st_uid != os.getuid()
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        ):
            raise PermissionError('evidence parent changed during write')
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{destination.name}.',
            suffix='.tmp',
            dir=parent,
            text=False,
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        payload = (manifest.canonical_json() + '\n').encode('utf-8')
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            raise FileExistsError(
                'evidence destination already exists'
            ) from None
        os.fsync(directory_descriptor)
        result_metadata = os.lstat(destination)
        if (
            not stat.S_ISREG(result_metadata.st_mode)
            or result_metadata.st_uid != os.getuid()
            or stat.S_IMODE(result_metadata.st_mode) != 0o600
        ):
            raise RuntimeError('published evidence permissions are invalid')
        return manifest.digest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(directory_descriptor)
