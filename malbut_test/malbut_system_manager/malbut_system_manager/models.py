"""Shared data models for manifests and managed mission instances."""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Mapping


class CommandKind(str, Enum):
    """ROS request mechanism declared by a Capability Manifest."""

    ACTION = 'ACTION'
    SERVICE = 'SERVICE'


class ExecutionMode(str, Enum):
    """Whether a mission affects the foreground system state."""

    FOREGROUND = 'FOREGROUND'
    BACKGROUND = 'BACKGROUND'


class ExecutionResource(str, Enum):
    """Robot outputs that only one managed mission may control at a time."""

    BASE = 'BASE'
    SPEAKER = 'SPEAKER'
    BUZZER = 'BUZZER'
    LED = 'LED'
    DISPLAY = 'DISPLAY'


class MissionPriority(IntEnum):
    """Ordering used only after missions are known to conflict."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    URGENT = 3


class MissionState(str, Enum):
    """Manager-owned lifecycle of a mission request."""

    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    CANCELING = 'CANCELING'
    SUSPENDED = 'SUSPENDED'


class TerminalOutcome(str, Enum):
    """Terminal outcome reported to the public ExecuteMission Action."""

    SUCCEEDED = 'SUCCEEDED'
    CANCELED = 'CANCELED'
    ABORTED = 'ABORTED'


class CancelReason(str, Enum):
    """Why a downstream Action cancellation was requested."""

    USER = 'USER'
    PREEMPTION = 'PREEMPTION'
    SHUTDOWN = 'SHUTDOWN'


class ControlMode(str, Enum):
    """Current owner of foreground robot control."""

    AUTONOMOUS = 'AUTONOMOUS'
    MANUAL = 'MANUAL'


class SystemState(str, Enum):
    """Aggregate externally visible runtime state."""

    BOOTING = 'BOOTING'
    IDLE = 'IDLE'
    EXECUTING_MISSION = 'EXECUTING_MISSION'
    RECHARGING = 'RECHARGING'
    EMERGENCY = 'EMERGENCY'


@dataclass(frozen=True)
class InputField:
    """One public Goal or Request field described by a Manifest."""

    name: str
    ros_type: str
    description: str
    has_default: bool = False
    default: Any = None


@dataclass(frozen=True)
class CapabilityManifest:
    """Validated registration metadata for one application capability."""

    capability_id: str
    title: str
    description: str
    command_kind: CommandKind
    command_name: str
    command_type: str
    execution_mode: ExecutionMode
    priority: MissionPriority
    input_fields: Mapping[str, InputField]
    interface_type: Any = field(repr=False, compare=False)
    resources: frozenset[ExecutionResource]
    source_path: str = ''


@dataclass
class MissionRecord:
    """Manager-owned state for one public ExecuteMission goal."""

    mission_id: str
    capability: CapabilityManifest
    arguments: dict[str, Any]
    state: MissionState = MissionState.PENDING
    generation: int = 0
    waiting_for: set[str] = field(default_factory=set)
    preempted_by: set[str] = field(default_factory=set)
    cancel_reason: CancelReason | None = None
    user_cancel_requested: bool = False
    resumable: bool = True

    @property
    def mode(self) -> ExecutionMode:
        """Return the Manifest execution mode."""
        return self.capability.execution_mode

    @property
    def priority(self) -> MissionPriority:
        """Return the Manifest priority."""
        return self.capability.priority

    @property
    def resources(self) -> frozenset[ExecutionResource]:
        """Return the exclusive output resources declared by the Manifest."""
        return self.capability.resources


@dataclass(frozen=True)
class MissionCompletion:
    """Final data that resolves one public ExecuteMission goal."""

    mission_id: str
    outcome: TerminalOutcome
    result_yaml: str = ''
    message: str = ''


@dataclass
class SchedulerEffects:
    """Side effects emitted by a pure scheduler transition."""

    start: list[str] = field(default_factory=list)
    cancel: list[str] = field(default_factory=list)
    complete: list[MissionCompletion] = field(default_factory=list)
    updated: set[str] = field(default_factory=set)

    def extend(self, other: 'SchedulerEffects') -> None:
        """Append effects while preserving their deterministic order."""
        self.start.extend(other.start)
        self.cancel.extend(other.cancel)
        self.complete.extend(other.complete)
        self.updated.update(other.updated)
