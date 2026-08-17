"""
Read-only ROS observations for the trusted RobotState collector.

This module deliberately has no ROS imports.  A narrow ROS node may translate
service, action-server, TF, and Homecam observations into strict tri-state
values, while this module owns freshness evaluation and atomic collector
updates.  It cannot send a goal, publish velocity, or authorize a physical
action.
"""

import math
import threading
import uuid
from dataclasses import dataclass, fields
from typing import Any, Callable, Optional, Tuple

from malbut_agent_server.robot_state import (
    MAX_ROBOT_STATE_LIFETIME_NS,
    trusted_boottime_ns,
)
from malbut_agent_server.robot_state_collector import (
    RobotStateBindingToken,
    RobotStateCollectorError,
    RobotStateFieldUpdate,
    RobotStateSnapshotStore,
)


NAV2_LIFECYCLE_NAMES = (
    'amcl',
    'bt_navigator',
    'planner_server',
    'controller_server',
    'global_costmap',
)
NAV2_ENDPOINT_NAMES = (
    'compute_path_to_pose',
    'navigate_to_pose',
    'global_costmap_service',
)
NAVIGATION_SOURCE = 'nav2_lifecycle_endpoints_v1'
LOCALIZATION_SOURCE = 'amcl_map_tf_v1'
HOMECAM_MEDIA_EVIDENCE_TOPIC = '/homecam/media_evidence'
HOMECAM_MEDIA_SOURCE = 'homecam_media_evidence_v1'
HOMECAM_MEDIA_SCHEMA_VERSION = 1
HOMECAM_STATE_UNKNOWN = 0
HOMECAM_STATE_FALSE = 1
HOMECAM_STATE_TRUE = 2
_MAX_U64 = (1 << 64) - 1


def _positive_u64(value: object, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > (1 << 64) - 1
    ):
        raise ValueError(f'{field_name} is invalid')
    return value


def _u64(value: object, field_name: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > _MAX_U64
    ):
        raise ValueError(f'{field_name} is invalid')
    return value


def _canonical_uuid(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{field_name} is invalid')
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f'{field_name} is invalid') from None
    if str(parsed) != value:
        raise ValueError(f'{field_name} is invalid')
    return value


def _canonical_uuid4_or_empty(value: object) -> str:
    if value == '':
        return ''
    result = _canonical_uuid(value, 'active_session_id')
    parsed = uuid.UUID(result)
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError('active_session_id is invalid')
    return result


def _homecam_state(value: object, field_name: str) -> Optional[bool]:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{field_name} is invalid')
    if value == HOMECAM_STATE_UNKNOWN:
        return None
    if value == HOMECAM_STATE_FALSE:
        return False
    if value == HOMECAM_STATE_TRUE:
        return True
    raise ValueError(f'{field_name} is invalid')


def _timeout_ns(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError('observation timeout is invalid')
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError('observation timeout is invalid') from None
    if (
        not math.isfinite(numeric)
        or numeric <= 0
        or numeric > MAX_ROBOT_STATE_LIFETIME_NS / 1_000_000_000
    ):
        raise ValueError('observation timeout is invalid')
    result = int(numeric * 1_000_000_000)
    if result < 1 or result > MAX_ROBOT_STATE_LIFETIME_NS:
        raise ValueError('observation timeout is invalid')
    return result


@dataclass(frozen=True)
class TimedBoolObservation:
    """One explicit boolean observation or an unknown value."""

    value: Optional[bool]
    received_boottime_ns: Optional[int]

    def __post_init__(self) -> None:
        """Reject truthy aliases and evidence-free known values."""
        if self.value is None:
            if self.received_boottime_ns is not None:
                raise ValueError('unknown observation cannot carry evidence')
            return
        if type(self.value) is not bool:
            raise ValueError('observation value is invalid')
        _positive_u64(
            self.received_boottime_ns,
            'received_boottime_ns',
        )

    @classmethod
    def unknown(cls) -> 'TimedBoolObservation':
        """Return an observation that grants no state knowledge."""
        return cls(None, None)


@dataclass(frozen=True)
class Nav2ObservationBatch:
    """One map-bound snapshot of all read-only Nav2 observations."""

    binding_token: RobotStateBindingToken
    amcl_active: TimedBoolObservation
    bt_navigator_active: TimedBoolObservation
    planner_server_active: TimedBoolObservation
    controller_server_active: TimedBoolObservation
    global_costmap_active: TimedBoolObservation
    compute_path_ready: TimedBoolObservation
    navigate_ready: TimedBoolObservation
    global_costmap_ready: TimedBoolObservation
    map_tf_fresh: TimedBoolObservation

    def __post_init__(self) -> None:
        """Require the exact immutable observation shape."""
        if not isinstance(self.binding_token, RobotStateBindingToken):
            raise TypeError('binding_token is required')
        for item in fields(self):
            if item.name == 'binding_token':
                continue
            if not isinstance(
                getattr(self, item.name),
                TimedBoolObservation,
            ):
                raise TypeError('timed observations are required')

    @classmethod
    def unknown(
        cls,
        binding_token: RobotStateBindingToken,
    ) -> 'Nav2ObservationBatch':
        """Return a completely unknown batch for startup and failures."""
        unknown = TimedBoolObservation.unknown
        return cls(
            binding_token=binding_token,
            amcl_active=unknown(),
            bt_navigator_active=unknown(),
            planner_server_active=unknown(),
            controller_server_active=unknown(),
            global_costmap_active=unknown(),
            compute_path_ready=unknown(),
            navigate_ready=unknown(),
            global_costmap_ready=unknown(),
            map_tf_fresh=unknown(),
        )


@dataclass(frozen=True)
class HomecamMediaObservationBatch:
    """One locally received, map-fenced camera/privacy observation."""

    binding_token: RobotStateBindingToken
    camera_available: Optional[bool]
    privacy_mode: Optional[bool]
    received_boottime_ns: int
    valid_for_ns: Optional[int]

    def __post_init__(self) -> None:
        """Keep unknown distinct and require one shared exact lifetime."""
        if not isinstance(self.binding_token, RobotStateBindingToken):
            raise TypeError('binding_token is required')
        receipt = _positive_u64(
            self.received_boottime_ns,
            'received_boottime_ns',
        )
        object.__setattr__(self, 'received_boottime_ns', receipt)
        for name in ('camera_available', 'privacy_mode'):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError('homecam observation is invalid')
        has_known = (
            self.camera_available is not None
            or self.privacy_mode is not None
        )
        if not has_known:
            if self.valid_for_ns is not None:
                raise ValueError('unknown observation lifetime is invalid')
            return
        lifetime = _u64(
            self.valid_for_ns,
            'valid_for_ns',
            minimum=1,
        )
        if lifetime > MAX_ROBOT_STATE_LIFETIME_NS:
            raise ValueError('valid_for_ns is invalid')
        object.__setattr__(self, 'valid_for_ns', lifetime)

    @classmethod
    def unknown(
        cls,
        binding_token: RobotStateBindingToken,
        received_boottime_ns: int,
    ) -> 'HomecamMediaObservationBatch':
        """Return one atomic camera/privacy clear at a local receipt."""
        return cls(
            binding_token=binding_token,
            camera_available=None,
            privacy_mode=None,
            received_boottime_ns=received_boottime_ns,
            valid_for_ns=None,
        )


@dataclass(frozen=True)
class _ParsedHomecamMediaEvidence:
    source_instance_id: str
    sequence: int
    control_plane_generation: int
    camera_available: Optional[bool]
    privacy_mode: Optional[bool]
    backend_device_bound: bool
    received_boottime_ns: int
    valid_for_ns: int
    identity: Tuple[Any, ...]

    @property
    def is_restart_marker(self) -> bool:
        """Return whether this is a non-authoritative restart handshake."""
        return (
            self.sequence == 1
            and self.control_plane_generation == 0
            and not self.backend_device_bound
            and self.camera_available is None
            and self.privacy_mode is None
        )


def _parse_homecam_media_evidence(
    message: Any,
    *,
    expected_device_id: str,
    received_boottime_ns: int,
    require_physical_authority: bool,
    maximum_valid_for_ns: int,
) -> _ParsedHomecamMediaEvidence:
    """Validate a fixed-schema envelope without trusting its clock."""
    receipt = _positive_u64(
        received_boottime_ns,
        'received_boottime_ns',
    )
    if (
        not isinstance(expected_device_id, str)
        or not expected_device_id
        or expected_device_id.strip() != expected_device_id
    ):
        raise ValueError('expected_device_id is invalid')
    if type(require_physical_authority) is not bool:
        raise ValueError('require_physical_authority is invalid')
    local_cap = _u64(
        maximum_valid_for_ns,
        'maximum_valid_for_ns',
        minimum=1,
    )
    if local_cap > MAX_ROBOT_STATE_LIFETIME_NS:
        raise ValueError('maximum_valid_for_ns is invalid')
    try:
        schema_version = message.schema_version
        device_id = message.device_id
        source_instance_id = message.source_instance_id
        sequence = message.sequence
        control_generation = message.control_plane_generation
        observed_ns = message.observed_boottime_ns
        valid_until_ns = message.valid_until_boottime_ns
        camera_state = message.camera_available_state
        privacy_state = message.privacy_mode_state
        last_frame_ns = message.last_valid_frame_boottime_ns
        frame_generation = message.frame_generation
        session_id = message.active_session_id
        session_generation = message.active_session_generation
        backend_bound = message.backend_device_bound
        physical_authority = message.physical_authority
    except Exception:
        raise ValueError('homecam media evidence is invalid') from None
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != HOMECAM_MEDIA_SCHEMA_VERSION
        or not isinstance(device_id, str)
        or device_id != expected_device_id
    ):
        raise ValueError('homecam media evidence is invalid')
    source = _canonical_uuid(source_instance_id, 'source_instance_id')
    source_uuid = uuid.UUID(source)
    if source_uuid.version != 4 or source_uuid.variant != uuid.RFC_4122:
        raise ValueError('source_instance_id is invalid')
    seq = _u64(sequence, 'sequence', minimum=1)
    generation = _u64(
        control_generation,
        'control_plane_generation',
    )
    observed = _u64(
        observed_ns,
        'observed_boottime_ns',
        minimum=1,
    )
    valid_until = _u64(
        valid_until_ns,
        'valid_until_boottime_ns',
        minimum=1,
    )
    if (
        observed > receipt
        or valid_until <= receipt
        or valid_until <= observed
        or valid_until - observed > MAX_ROBOT_STATE_LIFETIME_NS
    ):
        raise ValueError('homecam media evidence is stale')
    remaining = valid_until - receipt
    valid_for_ns = min(remaining, local_cap)
    if valid_for_ns < 1:
        raise ValueError('homecam media evidence is stale')
    camera = _homecam_state(
        camera_state,
        'camera_available_state',
    )
    privacy = _homecam_state(privacy_state, 'privacy_mode_state')
    last_frame = _u64(
        last_frame_ns,
        'last_valid_frame_boottime_ns',
    )
    frame_gen = _u64(frame_generation, 'frame_generation')
    active_session = _canonical_uuid4_or_empty(session_id)
    session_gen = _u64(
        session_generation,
        'active_session_generation',
    )
    if type(backend_bound) is not bool or type(physical_authority) is not bool:
        raise ValueError('homecam authority is invalid')
    if physical_authority and not backend_bound:
        raise ValueError('homecam authority is invalid')
    if not backend_bound:
        if (
            generation != 0
            or physical_authority
            or camera is not None
            or privacy is not None
            or last_frame != 0
            or frame_gen != 0
            or active_session
            or session_gen != 0
        ):
            raise ValueError('unbound media evidence is invalid')
    elif generation < 1:
        raise ValueError('control plane generation is invalid')
    if (
        require_physical_authority
        and backend_bound
        and not physical_authority
    ):
        raise ValueError('physical media evidence is invalid')
    if camera is True:
        if (
            not backend_bound
            or generation < 1
            or last_frame < 1
            or frame_gen != generation
            or last_frame > observed
            or receipt - last_frame >= MAX_ROBOT_STATE_LIFETIME_NS
            or valid_until > last_frame + MAX_ROBOT_STATE_LIFETIME_NS
        ):
            raise ValueError('camera evidence is invalid')
    elif last_frame != 0 or frame_gen != 0:
        raise ValueError('camera evidence is invalid')
    if privacy is True and camera is not False:
        raise ValueError('privacy evidence is invalid')
    if privacy is not False:
        if active_session or session_gen != 0:
            raise ValueError('session evidence is invalid')
    elif active_session:
        if not backend_bound or session_gen != generation:
            raise ValueError('session evidence is invalid')
    elif session_gen != 0:
        raise ValueError('session evidence is invalid')
    return _ParsedHomecamMediaEvidence(
        source_instance_id=source,
        sequence=seq,
        control_plane_generation=generation,
        camera_available=camera,
        privacy_mode=privacy,
        backend_device_bound=backend_bound,
        received_boottime_ns=receipt,
        valid_for_ns=valid_for_ns,
        identity=(
            HOMECAM_MEDIA_SCHEMA_VERSION,
            device_id,
            source,
            seq,
            generation,
            observed,
            valid_until,
            camera,
            privacy,
            last_frame,
            frame_gen,
            active_session,
            session_gen,
            backend_bound,
            physical_authority,
        ),
    )


class HomecamMediaEvidenceTracker:
    """Validate Homecam snapshots and fence process restarts/replays."""

    _MAX_RETIRED_SOURCES = 1024

    def __init__(
        self,
        expected_device_id: str,
        *,
        require_physical_authority: bool,
        maximum_lifetime_seconds: float,
    ) -> None:
        """Create one device-scoped in-process anti-replay tracker."""
        if (
            not isinstance(expected_device_id, str)
            or not expected_device_id
            or expected_device_id.strip() != expected_device_id
        ):
            raise ValueError('expected_device_id is invalid')
        if type(require_physical_authority) is not bool:
            raise ValueError('require_physical_authority is invalid')
        self._expected_device_id = expected_device_id
        self._require_physical_authority = require_physical_authority
        self._maximum_valid_for_ns = _timeout_ns(
            maximum_lifetime_seconds,
        )
        self._active_source: Optional[str] = None
        self._retired_sources = set()
        self._last_sequence = 0
        self._last_control_generation = 0
        self._last_identity: Optional[Tuple[Any, ...]] = None
        self._candidate_source: Optional[str] = None
        self._candidate_sequence = 0
        self._candidate_control_generation = 0
        self._candidate_identity: Optional[Tuple[Any, ...]] = None
        self._candidate_valid_until_ns: Optional[int] = None
        self._known_valid_until_ns: Optional[int] = None
        self._clock_high_water_ns: Optional[int] = None
        self._restart_exhausted = False
        self._lock = threading.RLock()

    def observe(
        self,
        message: Any,
        *,
        binding_token: RobotStateBindingToken,
        received_boottime_ns: int,
    ) -> Optional[HomecamMediaObservationBatch]:
        """Return an update, a fail-closed clear, or an exact replay no-op."""
        unknown = HomecamMediaObservationBatch.unknown(
            binding_token,
            received_boottime_ns,
        )
        with self._lock:
            if (
                self._clock_high_water_ns is not None
                and received_boottime_ns < self._clock_high_water_ns
            ):
                self._known_valid_until_ns = None
                return unknown
            self._clock_high_water_ns = received_boottime_ns
        try:
            parsed = _parse_homecam_media_evidence(
                message,
                expected_device_id=self._expected_device_id,
                received_boottime_ns=received_boottime_ns,
                require_physical_authority=(
                    self._require_physical_authority
                ),
                maximum_valid_for_ns=self._maximum_valid_for_ns,
            )
        except Exception:
            with self._lock:
                self._known_valid_until_ns = None
            return unknown
        with self._lock:
            if self._restart_exhausted:
                self._known_valid_until_ns = None
                return unknown
            source = parsed.source_instance_id
            candidate_expiry = self._candidate_valid_until_ns
            if (
                candidate_expiry is not None
                and parsed.received_boottime_ns >= candidate_expiry
            ):
                self._clear_candidate_locked()
            if source in self._retired_sources:
                self._known_valid_until_ns = None
                return unknown
            if self._candidate_source is not None:
                if source != self._candidate_source:
                    self._known_valid_until_ns = None
                    return unknown
                if parsed.sequence == self._candidate_sequence:
                    if parsed.identity == self._candidate_identity:
                        return None
                    self._known_valid_until_ns = None
                    return unknown
                if (
                    parsed.sequence < self._candidate_sequence
                    or parsed.control_plane_generation
                    < self._candidate_control_generation
                ):
                    self._known_valid_until_ns = None
                    return unknown
                self._active_source = source
                self._last_sequence = parsed.sequence
                self._last_control_generation = (
                    parsed.control_plane_generation
                )
                self._last_identity = parsed.identity
                self._clear_candidate_locked()
                return self._accepted_or_unknown_locked(
                    parsed,
                    binding_token,
                    unknown,
                )
            if self._active_source is None:
                if self._retired_sources:
                    self._remember_candidate_locked(parsed)
                    self._known_valid_until_ns = None
                    return unknown
                self._active_source = source
            elif source != self._active_source:
                if len(self._retired_sources) >= self._MAX_RETIRED_SOURCES:
                    self._restart_exhausted = True
                    self._known_valid_until_ns = None
                    return unknown
                self._retired_sources.add(self._active_source)
                self._active_source = None
                self._last_sequence = 0
                self._last_control_generation = 0
                self._last_identity = None
                self._remember_candidate_locked(parsed)
                self._known_valid_until_ns = None
                return unknown
            elif parsed.sequence == self._last_sequence:
                if parsed.identity == self._last_identity:
                    return None
                self._known_valid_until_ns = None
                return unknown
            elif (
                parsed.sequence < self._last_sequence
                or parsed.control_plane_generation
                < self._last_control_generation
            ):
                self._known_valid_until_ns = None
                return unknown
            self._last_sequence = parsed.sequence
            self._last_control_generation = (
                parsed.control_plane_generation
            )
            self._last_identity = parsed.identity
            return self._accepted_or_unknown_locked(
                parsed,
                binding_token,
                unknown,
            )

    def _remember_candidate_locked(
        self,
        parsed: _ParsedHomecamMediaEvidence,
    ) -> None:
        self._candidate_source = parsed.source_instance_id
        self._candidate_sequence = parsed.sequence
        self._candidate_control_generation = (
            parsed.control_plane_generation
        )
        self._candidate_identity = parsed.identity
        self._candidate_valid_until_ns = (
            parsed.received_boottime_ns + parsed.valid_for_ns
        )

    def _clear_candidate_locked(self) -> None:
        self._candidate_source = None
        self._candidate_sequence = 0
        self._candidate_control_generation = 0
        self._candidate_identity = None
        self._candidate_valid_until_ns = None

    def _accepted_or_unknown_locked(
        self,
        parsed: _ParsedHomecamMediaEvidence,
        binding_token: RobotStateBindingToken,
        unknown: HomecamMediaObservationBatch,
    ) -> HomecamMediaObservationBatch:
        if (
            parsed.camera_available is None
            and parsed.privacy_mode is None
        ):
            self._known_valid_until_ns = None
            return unknown
        self._known_valid_until_ns = (
            parsed.received_boottime_ns + parsed.valid_for_ns
        )
        return HomecamMediaObservationBatch(
            binding_token=binding_token,
            camera_available=parsed.camera_available,
            privacy_mode=parsed.privacy_mode,
            received_boottime_ns=parsed.received_boottime_ns,
            valid_for_ns=parsed.valid_for_ns,
        )

    def expire(
        self,
        *,
        binding_token: RobotStateBindingToken,
        now_boottime_ns: int,
    ) -> Optional[HomecamMediaObservationBatch]:
        """Return exactly one clear when locally bounded evidence expires."""
        now = _positive_u64(now_boottime_ns, 'now_boottime_ns')
        with self._lock:
            if (
                self._clock_high_water_ns is not None
                and now < self._clock_high_water_ns
            ):
                raise ValueError('boottime clock regressed')
            self._clock_high_water_ns = now
            expiry = self._known_valid_until_ns
            if expiry is None or now < expiry:
                return None
            self._known_valid_until_ns = None
        return HomecamMediaObservationBatch.unknown(binding_token, now)


class HomecamMediaObservationPublisher:
    """Atomically publish camera and software-privacy tri-state evidence."""

    _CLEAR_RETRY_CODES = frozenset({
        'robot_state_collector_binding_mismatch',
        'robot_state_collector_receipt_conflict',
        'robot_state_collector_receipt_regression',
        'robot_state_collector_receipt_replay',
        'robot_state_collector_stale_update',
    })

    def __init__(self, store: RobotStateSnapshotStore) -> None:
        """Bind the media publisher to one trusted collector store."""
        self._initialize(store, boottime_ns=trusted_boottime_ns)

    @classmethod
    def _for_test(
        cls,
        store: RobotStateSnapshotStore,
        *,
        boottime_ns: Callable[[], int],
    ) -> 'HomecamMediaObservationPublisher':
        """Inject one deterministic trusted-clock stand-in for tests."""
        instance = cls.__new__(cls)
        instance._initialize(store, boottime_ns=boottime_ns)
        return instance

    def _initialize(
        self,
        store: RobotStateSnapshotStore,
        *,
        boottime_ns: Callable[[], int],
    ) -> None:
        if not isinstance(store, RobotStateSnapshotStore):
            raise TypeError('RobotStateSnapshotStore is required')
        if not callable(boottime_ns):
            raise TypeError('boottime clock is required')
        self._store = store
        self._boottime_ns = boottime_ns

    def publish(self, batch: HomecamMediaObservationBatch) -> int:
        """Commit both media fields together under the supplied map token."""
        if not isinstance(batch, HomecamMediaObservationBatch):
            raise TypeError('HomecamMediaObservationBatch is required')
        updates = {}
        for name, value in (
            ('camera_available', batch.camera_available),
            ('privacy_mode', batch.privacy_mode),
        ):
            if value is None:
                updates[name] = RobotStateFieldUpdate(
                    value=None,
                    received_boottime_ns=batch.received_boottime_ns,
                )
            else:
                updates[name] = RobotStateFieldUpdate(
                    value=value,
                    source=HOMECAM_MEDIA_SOURCE,
                    received_boottime_ns=batch.received_boottime_ns,
                    valid_for_ns=batch.valid_for_ns,
                )
        return self._store.update_fields(
            updates,
            binding_token=batch.binding_token,
        )

    def publish_fail_closed(
        self,
        batch: HomecamMediaObservationBatch,
    ) -> int:
        """On a map race, retry only an atomic clear on the new binding."""
        try:
            return self.publish(batch)
        except RobotStateCollectorError as error:
            if error.code not in self._CLEAR_RETRY_CODES:
                raise
            last_error = error
        minimum_receipt = batch.received_boottime_ns
        for _attempt in range(32):
            clear_receipt = _positive_u64(
                self._boottime_ns(),
                'boottime clock',
            )
            if clear_receipt <= minimum_receipt:
                continue
            clear_updates = {
                'camera_available': RobotStateFieldUpdate(
                    value=None,
                    received_boottime_ns=clear_receipt,
                ),
                'privacy_mode': RobotStateFieldUpdate(
                    value=None,
                    received_boottime_ns=clear_receipt,
                ),
            }
            token = self._store.binding_token()
            try:
                return self._store.update_fields(
                    clear_updates,
                    binding_token=token,
                )
            except RobotStateCollectorError as error:
                last_error = error
                if error.code not in self._CLEAR_RETRY_CODES:
                    raise
                minimum_receipt = clear_receipt
        assert last_error is not None
        raise last_error


@dataclass(frozen=True)
class _CombinedObservation:
    value: Optional[bool]
    received_boottime_ns: Optional[int]


def _combine(
    observations: Tuple[TimedBoolObservation, ...],
    *,
    now_boottime_ns: int,
    timeout_ns: int,
) -> _CombinedObservation:
    current = []
    for observation in observations:
        receipt = observation.received_boottime_ns
        if (
            observation.value is None
            or receipt is None
            or receipt > now_boottime_ns
            or now_boottime_ns - receipt >= timeout_ns
        ):
            current.append(None)
        else:
            current.append(observation)
    negatives = [
        item for item in current
        if item is not None and item.value is False
    ]
    if negatives:
        return _CombinedObservation(
            False,
            min(item.received_boottime_ns for item in negatives),
        )
    if any(item is None for item in current):
        return _CombinedObservation(None, None)
    known = [item for item in current if item is not None]
    return _CombinedObservation(
        True,
        min(item.received_boottime_ns for item in known),
    )


class RobotStateObservationPublisher:
    """Evaluate and atomically publish Nav2 and localization evidence."""

    def __init__(
        self,
        store: RobotStateSnapshotStore,
        *,
        observation_timeout_seconds: float = 1.0,
    ) -> None:
        """Use the production host boot clock and one trusted store."""
        self._initialize(
            store,
            observation_timeout_seconds=observation_timeout_seconds,
            boottime_ns=trusted_boottime_ns,
        )

    @classmethod
    def _for_test(
        cls,
        store: RobotStateSnapshotStore,
        *,
        observation_timeout_seconds: float,
        boottime_ns: Callable[[], int],
    ) -> 'RobotStateObservationPublisher':
        """Create deterministic test wiring without widening production."""
        instance = cls.__new__(cls)
        instance._initialize(
            store,
            observation_timeout_seconds=observation_timeout_seconds,
            boottime_ns=boottime_ns,
        )
        return instance

    def _initialize(
        self,
        store: RobotStateSnapshotStore,
        *,
        observation_timeout_seconds: float,
        boottime_ns: Callable[[], int],
    ) -> None:
        if not isinstance(store, RobotStateSnapshotStore):
            raise TypeError('RobotStateSnapshotStore is required')
        if not callable(boottime_ns):
            raise TypeError('boottime clock is required')
        self._store = store
        self._timeout_ns = _timeout_ns(observation_timeout_seconds)
        self._boottime_ns = boottime_ns
        self._last_published = {
            'navigation_available': _CombinedObservation(None, None),
            'localization_ok': _CombinedObservation(None, None),
        }
        self._last_binding_token = store.binding_token()
        self._lock = threading.RLock()

    def publish(self, batch: Nav2ObservationBatch) -> int:
        """Publish only changed aggregate fields in one collector CAS."""
        if not isinstance(batch, Nav2ObservationBatch):
            raise TypeError('Nav2ObservationBatch is required')
        now = _positive_u64(self._boottime_ns(), 'boottime clock')
        navigation = _combine(
            (
                batch.amcl_active,
                batch.bt_navigator_active,
                batch.planner_server_active,
                batch.controller_server_active,
                batch.global_costmap_active,
                batch.compute_path_ready,
                batch.navigate_ready,
                batch.global_costmap_ready,
            ),
            now_boottime_ns=now,
            timeout_ns=self._timeout_ns,
        )
        localization = _combine(
            (batch.amcl_active, batch.map_tf_fresh),
            now_boottime_ns=now,
            timeout_ns=self._timeout_ns,
        )
        desired = {
            'navigation_available': navigation,
            'localization_ok': localization,
        }
        with self._lock:
            sequence = self._store.validate_binding_token(
                batch.binding_token,
            )
            if batch.binding_token != self._last_binding_token:
                self._last_published = {
                    'navigation_available': _CombinedObservation(None, None),
                    'localization_ok': _CombinedObservation(None, None),
                }
                self._last_binding_token = batch.binding_token
            updates = {}
            committed = {}
            for name, observation in desired.items():
                if observation == self._last_published[name]:
                    continue
                if observation.value is None:
                    update = RobotStateFieldUpdate(
                        value=None,
                        received_boottime_ns=now,
                    )
                    committed[name] = _CombinedObservation(None, None)
                else:
                    source = (
                        NAVIGATION_SOURCE
                        if name == 'navigation_available'
                        else LOCALIZATION_SOURCE
                    )
                    update = RobotStateFieldUpdate(
                        value=observation.value,
                        source=source,
                        received_boottime_ns=(
                            observation.received_boottime_ns
                        ),
                        valid_for_ns=self._timeout_ns,
                    )
                    committed[name] = observation
                updates[name] = update
            if not updates:
                return sequence
            sequence = self._store.update_fields(
                updates,
                binding_token=batch.binding_token,
            )
            self._last_published.update(committed)
            return sequence
