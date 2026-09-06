"""Single source of truth for mission collections and aggregate state."""

from collections import OrderedDict
from collections.abc import Iterable

from .models import (
    ControlMode,
    ExecutionMode,
    MissionRecord,
    MissionState,
    SystemState,
)


class StateStore:
    """Own all mutable runtime collections used by the scheduler."""

    def __init__(self) -> None:
        self.ready = False
        self.recharging = False
        self.emergency = False
        self.control_mode = ControlMode.AUTONOMOUS
        self.active_foreground: OrderedDict[str, MissionRecord] = (
            OrderedDict()
        )
        self.active_background: OrderedDict[str, MissionRecord] = (
            OrderedDict()
        )
        self.suspended: OrderedDict[str, MissionRecord] = OrderedDict()
        self.pending: OrderedDict[str, MissionRecord] = OrderedDict()

    @property
    def system_state(self) -> SystemState:
        """Derive the public state using the agreed precedence."""
        if self.emergency:
            return SystemState.EMERGENCY
        if not self.ready:
            return SystemState.BOOTING
        if self.recharging:
            return SystemState.RECHARGING
        if self.active_foreground:
            return SystemState.EXECUTING_MISSION
        return SystemState.IDLE

    def add_pending(self, mission: MissionRecord) -> None:
        """Store a validated mission until conflicts have stopped."""
        self._detach(mission.mission_id)
        mission.state = MissionState.PENDING
        self.pending[mission.mission_id] = mission

    def activate(self, mission: MissionRecord) -> None:
        """Move a mission into its active collection."""
        self._detach(mission.mission_id)
        mission.state = MissionState.RUNNING
        target = (
            self.active_foreground
            if mission.mode is ExecutionMode.FOREGROUND
            else self.active_background
        )
        target[mission.mission_id] = mission

    def suspend(self, mission: MissionRecord) -> None:
        """Keep a preempted public goal alive for later re-execution."""
        self._detach(mission.mission_id)
        mission.state = MissionState.SUSPENDED
        self.suspended[mission.mission_id] = mission

    def remove(self, mission_id: str) -> MissionRecord | None:
        """Remove and return a mission from whichever collection owns it."""
        for collection in self._collections():
            mission = collection.pop(mission_id, None)
            if mission is not None:
                return mission
        return None

    def get(self, mission_id: str) -> MissionRecord | None:
        """Find one live mission by its public goal UUID."""
        for collection in self._collections():
            mission = collection.get(mission_id)
            if mission is not None:
                return mission
        return None

    def active(self) -> Iterable[MissionRecord]:
        """Iterate all currently dispatched or canceling missions."""
        yield from self.active_foreground.values()
        yield from self.active_background.values()

    def all(self) -> Iterable[MissionRecord]:
        """Iterate every non-terminal mission exactly once."""
        for collection in self._collections():
            yield from collection.values()

    def _collections(self):
        return (
            self.active_foreground,
            self.active_background,
            self.suspended,
            self.pending,
        )

    def _detach(self, mission_id: str) -> None:
        for collection in self._collections():
            collection.pop(mission_id, None)
