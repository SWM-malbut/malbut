"""Trusted composition seam for live Gazebo monitor-room preflight."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import json
import math
import time
from types import MappingProxyType
from typing import Any

from malbut_agent_server.homecam_semantic import (
    VerifiedSemanticSnapshotEvidence,
)

from malbut_gazebo.gazebo_monitor_room_active_map import (
    ActiveMapEvidence,
    ActiveMapEvidenceResolver,
    ActiveMapStaticNavigationProjection,
)
from malbut_gazebo.gazebo_monitor_room_navigation_safety import (
    MapCostGrid,
    PathPoint,
    PathSafetyFailure,
    PathSafetyProof,
    RestrictedZones,
    SamplePath,
    validate_sample_path,
)
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    Nav2CancelRequest,
    Nav2PreflightRequest,
    Nav2StartRequest,
)
from malbut_gazebo.gazebo_monitor_room_nav2_ros_port import (
    Nav2CancelAuthorization,
    Nav2LivePreflightValidation,
    Nav2StartAuthorization,
    TrustedGazeboMonitorRoomNav2Validator,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    DispatchClaimEvidence,
    GazeboMonitorRoomStore,
    GoalTransition,
    OperationObservation,
    PrivateOperationBinding,
    PrivateStoredSample,
)


MAX_LIVE_EVIDENCE_AGE_SECONDS = 2.0
_HEX = frozenset('0123456789abcdef')
_ZONE_TOP_FIELDS = frozenset({
    'type', 'format', 'map_id', 'map_revision', 'frame_id', 'features',
})
_ZONE_FEATURE_FIELDS = frozenset({
    'type', 'id', 'properties', 'geometry',
})
_ZONE_GEOMETRY_FIELDS = frozenset({'type', 'coordinates'})
_ZONE_BEHAVIORS = frozenset({'allow', 'avoid', 'restricted'})


class GazeboMonitorRoomLiveValidatorError(RuntimeError):
    """Expose only one bounded validator failure code."""

    _CODES = frozenset({
        'live_validator_invalid_configuration',
        'live_validator_invalid_request',
        'live_start_authority_rejected',
        'live_cancel_authority_rejected',
    })

    def __init__(self, code: str) -> None:
        """Create a content-free public error."""
        normalized = (
            code if type(code) is str and code in self._CODES
            else 'live_validator_invalid_request'
        )
        super().__init__(normalized)
        self.code = normalized

    def __getattribute__(self, name):
        """Hide collaborator exception chains at this boundary."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


class GazeboMonitorRoomLiveEvidenceUnavailableError(RuntimeError):
    """Let a trusted source report temporary live-data unavailability."""


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _valid_digest(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and value != '0' * 64
        and all(character in _HEX for character in value)
    )


def _time_value(value: Any) -> float:
    if type(value) is not float or not math.isfinite(value) or value < 0.0:
        raise ValueError
    return value


def _copy_path_point(value: Any) -> PathPoint:
    if type(value) is not PathPoint:
        raise ValueError
    try:
        copy = PathPoint(
            object.__getattribute__(value, '_x_m'),
            object.__getattribute__(value, '_y_m'),
        )
        cached = object.__getattribute__(value, '_digest')
    except Exception:
        raise ValueError from None
    if type(cached) is not str or cached != copy.digest:
        raise ValueError
    return copy


def _copy_costmap(value: Any) -> MapCostGrid:
    if type(value) is not MapCostGrid:
        raise ValueError
    try:
        copy = MapCostGrid(
            object.__getattribute__(value, '_frame_id'),
            object.__getattribute__(value, '_width'),
            object.__getattribute__(value, '_height'),
            object.__getattribute__(value, '_resolution_m'),
            object.__getattribute__(value, '_origin_x_m'),
            object.__getattribute__(value, '_origin_y_m'),
            object.__getattribute__(value, '_origin_yaw_rad'),
            object.__getattribute__(value, '_costs'),
        )
        cached = object.__getattribute__(value, '_digest')
    except Exception:
        raise ValueError from None
    if type(cached) is not str or cached != copy.digest:
        raise ValueError
    return copy


def _copy_path(value: Any) -> SamplePath:
    if type(value) is not SamplePath:
        raise ValueError
    try:
        frame_id = object.__getattribute__(value, '_frame_id')
        points = object.__getattribute__(value, '_points')
        cached = object.__getattribute__(value, '_digest')
        if type(points) is not tuple:
            raise ValueError
        copied_points = [
            PathPoint(point[0], point[1])
            for point in points
            if type(point) is tuple and len(point) == 2
        ]
        if len(copied_points) != len(points):
            raise ValueError
        copy = SamplePath(frame_id, copied_points)
    except Exception:
        raise ValueError from None
    if type(cached) is not str or cached != copy.digest:
        raise ValueError
    return copy


class TrustedGazeboMonitorRoomLiveEvidenceSource(ABC):
    """Fetch current lifecycle, TF, costmap, and ComputePath evidence."""

    @abstractmethod
    def capture(
        self,
        request: Nav2PreflightRequest,
        *,
        checked_at: float,
        active_map_evidence_digest: str,
        semantic_content_digest: str,
    ) -> 'GazeboMonitorRoomLiveEvidence':
        """Capture one bounded read-only live snapshot without motion."""


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class GazeboMonitorRoomLiveEvidence:
    """Frozen redacted live inputs for one exact planner-path check."""

    request_fingerprint: str
    operation_id: str
    goal_uuid: str
    active_map_evidence_digest: str
    semantic_content_digest: str
    captured_at: float
    valid_until: float
    lifecycle_ready: bool
    tf_ready: bool
    planner_succeeded: bool
    lifecycle_evidence_digest: str
    transform_evidence_digest: str
    compute_path_evidence_digest: str
    start_point: PathPoint = field(repr=False)
    target_point: PathPoint = field(repr=False)
    costmap: MapCostGrid = field(repr=False)
    path: SamplePath = field(repr=False)
    frame_id: str = field(default='map', init=False)
    use_sim_time: bool = field(default=True, init=False)
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _issued_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate exact scalar types and cache a coordinate-free digest."""
        for value in (
            self.request_fingerprint,
            self.active_map_evidence_digest,
            self.semantic_content_digest,
            self.lifecycle_evidence_digest,
            self.transform_evidence_digest,
            self.compute_path_evidence_digest,
        ):
            if not _valid_digest(value):
                raise ValueError('live evidence is invalid')
        if (
            type(self.operation_id) is not str
            or not self.operation_id
            or type(self.goal_uuid) is not str
            or not self.goal_uuid
            or any(
                value is not True
                for value in (
                    self.lifecycle_ready,
                    self.tf_ready,
                    self.planner_succeeded,
                )
            )
            or type(self.start_point) is not PathPoint
            or type(self.target_point) is not PathPoint
            or type(self.costmap) is not MapCostGrid
            or type(self.path) is not SamplePath
        ):
            raise ValueError('live evidence is invalid')
        object.__setattr__(
            self, 'start_point', _copy_path_point(self.start_point)
        )
        object.__setattr__(
            self, 'target_point', _copy_path_point(self.target_point)
        )
        object.__setattr__(self, 'costmap', _copy_costmap(self.costmap))
        object.__setattr__(self, 'path', _copy_path(self.path))
        captured_at = _time_value(self.captured_at)
        valid_until = _time_value(self.valid_until)
        if (
            valid_until <= captured_at
            or valid_until - captured_at
            > MAX_LIVE_EVIDENCE_AGE_SECONDS
        ):
            raise ValueError('live evidence is invalid')
        object.__setattr__(self, '_issued_digest', self._current_digest())

    def _current_digest(self) -> str:
        """Hash all live evidence without serializing private coordinates."""
        return _digest_json({
            'contract': 'gazebo-monitor-room-live-evidence-v1',
            'request_fingerprint': self.request_fingerprint,
            'operation_id': self.operation_id,
            'goal_uuid': self.goal_uuid,
            'active_map_evidence_digest': self.active_map_evidence_digest,
            'semantic_content_digest': self.semantic_content_digest,
            'captured_at': self.captured_at,
            'valid_until': self.valid_until,
            'lifecycle_evidence_digest': self.lifecycle_evidence_digest,
            'transform_evidence_digest': self.transform_evidence_digest,
            'compute_path_evidence_digest': (
                self.compute_path_evidence_digest
            ),
            'start_point_digest': self.start_point.digest,
            'target_point_digest': self.target_point.digest,
            'costmap_digest': self.costmap.digest,
            'path_digest': self.path.digest,
            'frame_id': self.frame_id,
            'use_sim_time': self.use_sim_time,
            'runtime_mode': self.runtime_mode,
            'simulation': self.simulation,
            'physical_authorized': self.physical_authorized,
            'physical_effects': self.physical_effects,
            'viewer_live': self.viewer_live,
            'camera_coverage_validated': self.camera_coverage_validated,
            'coverage_achieved': self.coverage_achieved,
        })

    @property
    def evidence_digest(self) -> str:
        """Return the issued digest, detecting object-level mutation."""
        current = self._current_digest()
        if current != self._issued_digest:
            raise ValueError('live evidence is invalid')
        return current

    def canonical_copy(self) -> 'GazeboMonitorRoomLiveEvidence':
        """Rebuild the frozen envelope and detect nested DTO mutation."""
        current = self.evidence_digest
        copy = GazeboMonitorRoomLiveEvidence(
            request_fingerprint=self.request_fingerprint,
            operation_id=self.operation_id,
            goal_uuid=self.goal_uuid,
            active_map_evidence_digest=self.active_map_evidence_digest,
            semantic_content_digest=self.semantic_content_digest,
            captured_at=self.captured_at,
            valid_until=self.valid_until,
            lifecycle_ready=self.lifecycle_ready,
            tf_ready=self.tf_ready,
            planner_succeeded=self.planner_succeeded,
            lifecycle_evidence_digest=self.lifecycle_evidence_digest,
            transform_evidence_digest=self.transform_evidence_digest,
            compute_path_evidence_digest=(
                self.compute_path_evidence_digest
            ),
            start_point=self.start_point,
            target_point=self.target_point,
            costmap=self.costmap,
            path=self.path,
        )
        if copy.evidence_digest != current:
            raise ValueError('live evidence is invalid')
        return copy

    def __repr__(self) -> str:
        """Keep coordinates and source content out of logs."""
        return 'GazeboMonitorRoomLiveEvidence(<redacted>)'


class GazeboMonitorRoomLiveValidator(
    TrustedGazeboMonitorRoomNav2Validator
):
    """Compose durable intent with current map, semantics, and path proof."""

    def __init__(
        self,
        store: GazeboMonitorRoomStore,
        semantic_evidence_source: Any,
        active_map_resolver: ActiveMapEvidenceResolver,
        live_evidence_source: TrustedGazeboMonitorRoomLiveEvidenceSource,
        *,
        clock=None,
    ) -> None:
        """Bind collaborators without fetching data or issuing ROS calls."""
        if (
            type(store) is not GazeboMonitorRoomStore
            or type(active_map_resolver) is not ActiveMapEvidenceResolver
            or not isinstance(
                live_evidence_source,
                TrustedGazeboMonitorRoomLiveEvidenceSource,
            )
        ):
            raise GazeboMonitorRoomLiveValidatorError(
                'live_validator_invalid_configuration'
            )
        try:
            semantic_fetch = (
                semantic_evidence_source.fetch_snapshot_evidence
            )
            active_map_resolve = (
                active_map_resolver.resolve_static_navigation_projection
            )
            live_capture = live_evidence_source.capture
        except Exception:
            raise GazeboMonitorRoomLiveValidatorError(
                'live_validator_invalid_configuration'
            ) from None
        if not all(callable(value) for value in (
            semantic_fetch, active_map_resolve, live_capture,
        )):
            raise GazeboMonitorRoomLiveValidatorError(
                'live_validator_invalid_configuration'
            )
        self._store = store
        self._semantic_fetch = semantic_fetch
        self._active_map_resolve = active_map_resolve
        self._live_capture = live_capture
        self._clock = _host_boottime if clock is None else clock

    def validate_preflight(self, request, *, checked_at):
        """Validate one exact current request without granting authority."""
        canonical = _canonical_preflight_or_error(request)
        checked = _checked_at_or_error(checked_at)
        try:
            live_digest, path_digest = self._validate_current(
                canonical,
                checked_at=checked,
                expected_state='preflighting',
            )
        except GazeboMonitorRoomLiveEvidenceUnavailableError:
            return _rejected_validation(canonical, 'retryable')
        except Exception:
            return _rejected_validation(canonical, 'rejected')
        return Nav2LivePreflightValidation(
            request_fingerprint=canonical.request_fingerprint,
            binding_digest=canonical.binding_digest,
            goal_uuid=canonical.goal_uuid,
            outcome='ready',
            code='live_preflight_ready',
            live_binding_digest=live_digest,
            path_evidence_digest=path_digest,
        )

    def authorize_start(self, request, *, checked_at):
        """Revalidate live safety and prove the durable start claim."""
        canonical = _canonical_start_or_error(request)
        checked = _checked_at_or_error(checked_at)
        failure = False
        authorization = None
        try:
            live_digest, path_digest = self._validate_current(
                canonical.preflight,
                checked_at=checked,
                expected_state='send_intent',
                start_request=canonical,
            )
            boundary_now = self._now_not_before(checked)
            transition = _start_transition(canonical)
            claim = self._store.assert_start_dispatch_claim(
                transition,
                start_fingerprint=canonical.request_fingerprint,
                binding_digest=canonical.preflight.binding_digest,
                preflight_digest=canonical.preflight_digest,
                wire_payload_digest=canonical.wire_payload_digest,
                now=boundary_now,
            )
            claim_digest = _assert_start_claim(claim, canonical, boundary_now)
            authority_digest = _digest_json({
                'contract': 'gazebo-monitor-room-start-authority-v1',
                'request_fingerprint': canonical.request_fingerprint,
                'wire_payload_digest': canonical.wire_payload_digest,
                'preflight_digest': canonical.preflight_digest,
                'binding_digest': canonical.preflight.binding_digest,
                'dispatch_claim_evidence_digest': claim_digest,
                'live_binding_digest': live_digest,
                'path_evidence_digest': path_digest,
                'checked_at': checked,
                'boundary_checked_at': boundary_now,
                'runtime_mode': 'gazebo',
                'simulation': True,
                'physical_authorized': False,
                'physical_effects': False,
                'viewer_live': False,
                'camera_coverage_validated': False,
                'coverage_achieved': False,
            })
            authorization = Nav2StartAuthorization(
                operation_id=canonical.preflight.operation_id,
                worker_id=canonical.worker_id,
                goal_uuid=canonical.preflight.goal_uuid,
                binding_digest=canonical.preflight.binding_digest,
                fence_epoch=canonical.fence_epoch,
                request_fingerprint=canonical.request_fingerprint,
                wire_payload_digest=canonical.wire_payload_digest,
                checked_at=checked,
                authority_evidence_digest=authority_digest,
            )
        except Exception:
            failure = True
        if failure or authorization is None:
            raise GazeboMonitorRoomLiveValidatorError(
                'live_start_authority_rejected'
            )
        return authorization

    def authorize_cancel(self, request, *, checked_at):
        """Prove only the durable exact-goal cancel claim."""
        canonical = _canonical_cancel_or_error(request)
        checked = _checked_at_or_error(checked_at)
        failure = False
        authorization = None
        try:
            boundary_now = self._now_not_before(checked)
            cancel_snapshot = _cancel_observation(
                self._store, canonical, boundary_now
            )
            observation = cancel_snapshot[0]
            transition = _cancel_transition(
                canonical, observation.current_sample_index
            )
            claim = self._store.assert_cancel_dispatch_claim(
                transition,
                cancel_request_id=canonical.cancel_request_id,
                request_fingerprint=canonical.request_fingerprint,
                binding_digest=canonical.binding_digest,
                wire_payload_digest=canonical.wire_payload_digest,
                now=boundary_now,
            )
            claim_digest = _assert_cancel_claim(
                claim,
                canonical,
                boundary_now,
                observation.current_sample_index,
            )
            _cancel_observation(
                self._store,
                canonical,
                boundary_now,
                expected=cancel_snapshot,
            )
            authority_digest = _digest_json({
                'contract': 'gazebo-monitor-room-cancel-authority-v1',
                'request_fingerprint': canonical.request_fingerprint,
                'wire_payload_digest': canonical.wire_payload_digest,
                'binding_digest': canonical.binding_digest,
                'dispatch_claim_evidence_digest': claim_digest,
                'checked_at': checked,
                'boundary_checked_at': boundary_now,
                'runtime_mode': 'gazebo',
                'simulation': True,
                'physical_authorized': False,
                'physical_effects': False,
                'viewer_live': False,
                'camera_coverage_validated': False,
                'coverage_achieved': False,
            })
            authorization = Nav2CancelAuthorization(
                operation_id=canonical.operation_id,
                worker_id=canonical.worker_id,
                cancel_request_id=canonical.cancel_request_id,
                goal_uuid=canonical.goal_uuid,
                binding_digest=canonical.binding_digest,
                fence_epoch=canonical.fence_epoch,
                request_fingerprint=canonical.request_fingerprint,
                wire_payload_digest=canonical.wire_payload_digest,
                checked_at=checked,
                authority_evidence_digest=authority_digest,
            )
        except Exception:
            failure = True
        if failure or authorization is None:
            raise GazeboMonitorRoomLiveValidatorError(
                'live_cancel_authority_rejected'
            )
        return authorization

    def _now_not_before(self, checked_at: float) -> float:
        current = self._clock()
        if type(current) not in (int, float) or isinstance(current, bool):
            raise ValueError
        current = float(current)
        if not math.isfinite(current) or current < checked_at:
            raise ValueError
        return current

    def _validate_current(
        self,
        request: Nav2PreflightRequest,
        *,
        checked_at: float,
        expected_state: str,
        start_request: Nav2StartRequest | None = None,
    ) -> tuple[str, str]:
        store_values = _assert_store_request(
            self._store,
            request,
            expected_state=expected_state,
            checked_at=checked_at,
            start_request=start_request,
        )
        semantic = self._semantic_fetch()
        if type(semantic) is not VerifiedSemanticSnapshotEvidence:
            raise ValueError
        semantic = semantic.canonical_copy()
        projection = self._active_map_resolve()
        if type(projection) is not ActiveMapStaticNavigationProjection:
            raise ValueError
        projection = projection.canonical_copy()
        active_map = projection.active_map_evidence.canonical_copy()
        _assert_map_and_semantic(request, active_map, semantic)
        zones = _restricted_zones(semantic, request)
        live = self._live_capture(
            request,
            checked_at=checked_at,
            active_map_evidence_digest=active_map.evidence_digest,
            semantic_content_digest=semantic.content_sha256,
        )
        if type(live) is not GazeboMonitorRoomLiveEvidence:
            raise ValueError
        live = live.canonical_copy()
        current = self._now_not_before(checked_at)
        _assert_live_snapshot(
            live,
            request,
            active_map,
            semantic,
            current,
        )
        proof = validate_sample_path(
            start_point=live.start_point,
            target_point=live.target_point,
            target_binding_digest=request.target_binding_digest,
            operation_binding_digest=request.binding_digest,
            map_content_digest=active_map.evidence_digest,
            semantic_content_digest=semantic.content_sha256,
            zones_digest=zones.digest,
            restricted_zones=zones,
            costmap=live.costmap,
            static_clearance=projection.static_clearance_grid,
            path=live.path,
        )
        if (
            type(proof) is PathSafetyFailure
            or type(proof) is not PathSafetyProof
        ):
            raise ValueError
        if (
            proof.authority_claimed is not False
            or proof.coverage_claimed is not False
            or proof.physical_execution_observed is not False
            or proof.viewer_observed is not False
            or proof.restricted_zone_validation_performed is not True
        ):
            raise ValueError
        latest_semantic = self._semantic_fetch()
        if type(latest_semantic) is not VerifiedSemanticSnapshotEvidence:
            raise ValueError
        latest_semantic = latest_semantic.canonical_copy()
        latest_projection = self._active_map_resolve()
        if type(latest_projection) is not ActiveMapStaticNavigationProjection:
            raise ValueError
        latest_projection = latest_projection.canonical_copy()
        latest_map = (
            latest_projection.active_map_evidence.canonical_copy()
        )
        _assert_map_and_semantic(request, latest_map, latest_semantic)
        if (
            latest_semantic.content_sha256 != semantic.content_sha256
            or latest_map.evidence_digest != active_map.evidence_digest
            or latest_projection.projection_digest
            != projection.projection_digest
            or latest_projection.static_clearance_grid.digest
            != projection.static_clearance_grid.digest
        ):
            raise ValueError
        current = self._now_not_before(current)
        _assert_live_snapshot(
            live,
            request,
            latest_map,
            latest_semantic,
            current,
        )
        _assert_store_request(
            self._store,
            request,
            expected_state=expected_state,
            checked_at=current,
            start_request=start_request,
            expected=store_values,
        )
        semantic.canonical_copy()
        active_map.canonical_copy()
        projection.canonical_copy()
        latest_semantic.canonical_copy()
        latest_projection.canonical_copy()
        live.canonical_copy()
        binding_digest = _digest_json({
            'contract': 'gazebo-monitor-room-live-binding-v1',
            'request_fingerprint': request.request_fingerprint,
            'operation_binding_digest': request.binding_digest,
            'map_id': request.map_id,
            'map_revision': request.map_revision,
            'semantic_revision': request.semantic_revision,
            'semantic_zones_digest': request.zones_digest,
            'restricted_geometry_digest': zones.digest,
            'target_binding_digest': request.target_binding_digest,
            'effects_digest': request.effects_digest,
            'coverage_profile_digest': request.profile_digest,
            'coverage_plan_digest': request.plan_digest,
            'active_map_evidence_digest': active_map.evidence_digest,
            'active_map_image_sha256': active_map.map_image_sha256,
            'static_clearance_projection_digest': (
                projection.projection_digest
            ),
            'static_clearance_digest': (
                projection.static_clearance_grid.digest
            ),
            'semantic_content_digest': semantic.content_sha256,
            'live_evidence_digest': live.evidence_digest,
            'path_safety_proof_digest': proof.proof_digest,
            'navigation_safety_profile_digest': proof.profile_digest,
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        })
        return binding_digest, proof.proof_digest


def _host_boottime() -> float:
    try:
        return float(time.clock_gettime(time.CLOCK_BOOTTIME))
    except Exception:
        raise GazeboMonitorRoomLiveValidatorError(
            'live_validator_invalid_configuration'
        ) from None


def _checked_at_or_error(value: Any) -> float:
    try:
        return _time_value(value)
    except Exception:
        raise GazeboMonitorRoomLiveValidatorError(
            'live_validator_invalid_request'
        ) from None


def _same_exact_dataclass(first: Any, second: Any) -> bool:
    try:
        left = first.__dict__
        right = second.__dict__
    except AttributeError:
        return False
    return left.keys() == right.keys() and all(
        type(left[key]) is type(right[key])  # noqa: E721
        and left[key] == right[key]
        for key in left
    )


def _canonical_preflight_or_error(value: Any) -> Nav2PreflightRequest:
    try:
        if type(value) is not Nav2PreflightRequest:
            raise ValueError
        result = Nav2PreflightRequest(
            operation_id=value.operation_id,
            robot_id=value.robot_id,
            map_id=value.map_id,
            map_revision=value.map_revision,
            semantic_revision=value.semantic_revision,
            zones_digest=value.zones_digest,
            target_binding_digest=value.target_binding_digest,
            effects_digest=value.effects_digest,
            profile_digest=value.profile_digest,
            plan_digest=value.plan_digest,
            sample_count=value.sample_count,
            sample_index=value.sample_index,
            polygon_ordinal=value.polygon_ordinal,
            row_ordinal=value.row_ordinal,
            goal_uuid=value.goal_uuid,
            binding_digest=value.binding_digest,
            x_m=value.x_m,
            y_m=value.y_m,
            frame_id=value.frame_id,
        )
        if (
            not _same_exact_dataclass(result, value)
            or result.request_fingerprint != value.request_fingerprint
        ):
            raise ValueError
        return result
    except Exception:
        raise GazeboMonitorRoomLiveValidatorError(
            'live_validator_invalid_request'
        ) from None


def _canonical_start_or_error(value: Any) -> Nav2StartRequest:
    try:
        if type(value) is not Nav2StartRequest:
            raise ValueError
        result = Nav2StartRequest(
            preflight=_canonical_preflight_or_error(value.preflight),
            worker_id=value.worker_id,
            fence_epoch=value.fence_epoch,
            lease_expires_at=value.lease_expires_at,
            deadline=value.deadline,
            preflight_digest=value.preflight_digest,
        )
        if (
            not _same_exact_dataclass(result, value)
            or result.request_fingerprint != value.request_fingerprint
            or result.wire_payload_digest != value.wire_payload_digest
        ):
            raise ValueError
        return result
    except GazeboMonitorRoomLiveValidatorError:
        raise
    except Exception:
        raise GazeboMonitorRoomLiveValidatorError(
            'live_validator_invalid_request'
        ) from None


def _canonical_cancel_or_error(value: Any) -> Nav2CancelRequest:
    try:
        if type(value) is not Nav2CancelRequest:
            raise ValueError
        result = Nav2CancelRequest(
            operation_id=value.operation_id,
            worker_id=value.worker_id,
            fence_epoch=value.fence_epoch,
            cancel_request_id=value.cancel_request_id,
            goal_uuid=value.goal_uuid,
            binding_digest=value.binding_digest,
        )
        if (
            not _same_exact_dataclass(result, value)
            or result.request_fingerprint != value.request_fingerprint
            or result.wire_payload_digest != value.wire_payload_digest
        ):
            raise ValueError
        return result
    except Exception:
        raise GazeboMonitorRoomLiveValidatorError(
            'live_validator_invalid_request'
        ) from None


def _rejected_validation(
    request: Nav2PreflightRequest, outcome: str
) -> Nav2LivePreflightValidation:
    code = (
        'live_evidence_unavailable'
        if outcome == 'retryable'
        else 'live_evidence_rejected'
    )
    return Nav2LivePreflightValidation(
        request_fingerprint=request.request_fingerprint,
        binding_digest=request.binding_digest,
        goal_uuid=request.goal_uuid,
        outcome=outcome,
        code=code,
        live_binding_digest=_digest_json({
            'contract': 'gazebo-monitor-room-live-rejection-v1',
            'kind': 'binding',
            'request_fingerprint': request.request_fingerprint,
            'code': code,
        }),
        path_evidence_digest=_digest_json({
            'contract': 'gazebo-monitor-room-live-rejection-v1',
            'kind': 'path',
            'request_fingerprint': request.request_fingerprint,
            'code': code,
        }),
    )


def _exact_float(first: Any, second: Any) -> bool:
    return (
        type(first) is float
        and type(second) is float
        and first.hex() == second.hex()
    )


def _store_tuple(
    store: GazeboMonitorRoomStore, operation_id: str
) -> tuple[OperationObservation, PrivateOperationBinding, PrivateStoredSample]:
    observation = store.observe(operation_id)
    binding = store.private_operation_binding(operation_id)
    sample = store.private_current_sample(operation_id)
    if (
        type(observation) is not OperationObservation
        or type(binding) is not PrivateOperationBinding
        or type(sample) is not PrivateStoredSample
    ):
        raise ValueError
    binding.binding_digest
    return observation, binding, sample


def _assert_store_request(
    store: GazeboMonitorRoomStore,
    request: Nav2PreflightRequest,
    *,
    expected_state: str,
    checked_at: float,
    start_request: Nav2StartRequest | None = None,
    expected: tuple | None = None,
) -> tuple:
    first = _store_tuple(store, request.operation_id)
    second = _store_tuple(store, request.operation_id)
    if first != second or (expected is not None and first != expected):
        raise ValueError
    observation, binding, sample = first
    fields = (
        ('operation_id', request.operation_id),
        ('robot_id', request.robot_id),
        ('map_id', request.map_id),
        ('map_revision', request.map_revision),
        ('semantic_revision', request.semantic_revision),
        ('zones_digest', request.zones_digest),
        ('target_binding_digest', request.target_binding_digest),
        ('effects_digest', request.effects_digest),
        ('profile_digest', request.profile_digest),
        ('plan_digest', request.plan_digest),
        ('sample_count', request.sample_count),
    )
    if any(
        type(getattr(binding, name)) is not type(value)
        or getattr(binding, name) != value
        for name, value in fields
    ):
        raise ValueError
    if binding.binding_digest != request.binding_digest:
        raise ValueError
    if (
        observation.operation_id != request.operation_id
        or observation.robot_id != request.robot_id
        or observation.state != expected_state
        or observation.current_sample_state != expected_state
        or observation.current_sample_index != request.sample_index
        or observation.current_goal_uuid != request.goal_uuid
        or observation.navigation_samples_total != request.sample_count
        or sample.operation_id != request.operation_id
        or sample.index != request.sample_index
        or sample.polygon_ordinal != request.polygon_ordinal
        or sample.row_ordinal != request.row_ordinal
        or sample.goal_uuid != request.goal_uuid
        or sample.frame_id != request.frame_id
        or sample.state != expected_state
        or not _exact_float(sample.x_m, request.x_m)
        or not _exact_float(sample.y_m, request.y_m)
        or observation.lease_owner is None
        or observation.lease_expires_at is None
        or checked_at >= observation.lease_expires_at
        or checked_at >= observation.deadline
    ):
        raise ValueError
    if start_request is not None and (
        observation.lease_owner != start_request.worker_id
        or observation.fence_epoch != start_request.fence_epoch
        or not _exact_float(
            observation.lease_expires_at,
            start_request.lease_expires_at,
        )
        or not _exact_float(observation.deadline, start_request.deadline)
        or not _exact_float(binding.deadline, start_request.deadline)
    ):
        raise ValueError
    return first


def _assert_map_and_semantic(
    request: Nav2PreflightRequest,
    active_map: ActiveMapEvidence,
    semantic: VerifiedSemanticSnapshotEvidence,
) -> None:
    snapshot = semantic.snapshot
    if (
        active_map.map_id != request.map_id
        or active_map.map_revision != request.map_revision
        or active_map.frame_id != request.frame_id
        or snapshot.map_id != request.map_id
        or snapshot.map_revision != request.map_revision
        or snapshot.semantic_revision != request.semantic_revision
        or snapshot.zones_digest != request.zones_digest
        or snapshot.frame_id != request.frame_id
    ):
        raise ValueError


def _plain_zone_value(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int, float}:
        if type(value) is float and not math.isfinite(value):
            raise ValueError
        return value
    if type(value) is tuple:
        return [_plain_zone_value(item) for item in value]
    if type(value) is MappingProxyType:
        if any(type(key) is not str for key in value):
            raise ValueError
        return {key: _plain_zone_value(item) for key, item in value.items()}
    raise ValueError


def _restricted_zones(
    semantic: VerifiedSemanticSnapshotEvidence,
    request: Nav2PreflightRequest,
) -> RestrictedZones:
    value = semantic.zones
    if value is None:
        return RestrictedZones('map', [])
    if type(value) is not MappingProxyType or frozenset(value) != (
        _ZONE_TOP_FIELDS
    ):
        raise ValueError
    if (
        value['type'] != 'FeatureCollection'
        or value['format'] != 'malbut-semantic-zones-v1'
        or value['map_id'] != request.map_id
        or value['map_revision'] != request.map_revision
        or value['frame_id'] != 'map'
        or type(value['features']) is not tuple
    ):
        raise ValueError
    restricted = []
    identifiers = set()
    for feature in value['features']:
        if (
            type(feature) is not MappingProxyType
            or frozenset(feature) != _ZONE_FEATURE_FIELDS
            or feature['type'] != 'Feature'
            or type(feature['id']) is not str
            or not feature['id']
            or feature['id'] in identifiers
            or type(feature['properties']) is not MappingProxyType
            or type(feature['geometry']) is not MappingProxyType
            or frozenset(feature['geometry']) != _ZONE_GEOMETRY_FIELDS
        ):
            raise ValueError
        identifiers.add(feature['id'])
        properties = feature['properties']
        behavior = properties.get('behavior')
        if (
            properties.get('role') != 'semantic_zone'
            or type(behavior) is not str
            or behavior not in _ZONE_BEHAVIORS
        ):
            raise ValueError
        geometry = _plain_zone_value(feature['geometry'])
        RestrictedZones('map', [geometry])
        if behavior == 'restricted':
            restricted.append(geometry)
    return RestrictedZones('map', restricted)


def _assert_live_snapshot(
    live: GazeboMonitorRoomLiveEvidence,
    request: Nav2PreflightRequest,
    active_map: ActiveMapEvidence,
    semantic: VerifiedSemanticSnapshotEvidence,
    current: float,
) -> None:
    expected_target = PathPoint(request.x_m, request.y_m)
    try:
        cost_width = object.__getattribute__(live.costmap, '_width')
        cost_height = object.__getattribute__(live.costmap, '_height')
        cost_resolution = object.__getattribute__(
            live.costmap, '_resolution_m'
        )
        cost_origin_x = object.__getattribute__(
            live.costmap, '_origin_x_m'
        )
        cost_origin_y = object.__getattribute__(
            live.costmap, '_origin_y_m'
        )
        cost_origin_yaw = object.__getattribute__(
            live.costmap, '_origin_yaw_rad'
        )
    except AttributeError:
        raise ValueError from None
    if (
        live.request_fingerprint != request.request_fingerprint
        or live.operation_id != request.operation_id
        or live.goal_uuid != request.goal_uuid
        or live.active_map_evidence_digest != active_map.evidence_digest
        or live.semantic_content_digest != semantic.content_sha256
        or live.target_point.digest != expected_target.digest
        or live.captured_at < 0.0
        or live.captured_at < current - MAX_LIVE_EVIDENCE_AGE_SECONDS
        or live.captured_at > current
        or current >= live.valid_until
        or cost_width != active_map.width
        or cost_height != active_map.height
        or not _exact_float(cost_resolution, active_map.resolution)
        or not _exact_float(cost_origin_x, active_map.origin_x)
        or not _exact_float(cost_origin_y, active_map.origin_y)
        or not _exact_float(cost_origin_yaw, active_map.origin_yaw)
    ):
        raise ValueError


def _start_transition(request: Nav2StartRequest) -> GoalTransition:
    preflight = request.preflight
    return GoalTransition(
        operation_id=preflight.operation_id,
        worker_id=request.worker_id,
        fence_epoch=request.fence_epoch,
        sample_index=preflight.sample_index,
        goal_uuid=preflight.goal_uuid,
        expected_operation_state='send_intent',
        expected_sample_state='send_intent',
    )


def _cancel_transition(
    request: Nav2CancelRequest, sample_index: int
) -> GoalTransition:
    return GoalTransition(
        operation_id=request.operation_id,
        worker_id=request.worker_id,
        fence_epoch=request.fence_epoch,
        sample_index=sample_index,
        goal_uuid=request.goal_uuid,
        expected_operation_state='cancel_requested',
        expected_sample_state='cancel_requested',
    )


def _assert_start_claim(
    claim: Any, request: Nav2StartRequest, checked_at: float
) -> str:
    preflight = request.preflight
    if (
        type(claim) is not DispatchClaimEvidence
        or claim.phase != 'start'
        or claim.operation_id != preflight.operation_id
        or claim.sample_index != preflight.sample_index
        or claim.goal_uuid != preflight.goal_uuid
        or claim.worker_id != request.worker_id
        or claim.fence_epoch != request.fence_epoch
        or claim.start_fingerprint != request.request_fingerprint
        or claim.binding_digest != preflight.binding_digest
        or claim.preflight_digest != request.preflight_digest
        or claim.wire_payload_digest != request.wire_payload_digest
        or not _exact_float(claim.checked_at, checked_at)
        or not _exact_float(
            claim.claim_lease_expires_at, request.lease_expires_at
        )
        or not _exact_float(
            claim.current_lease_expires_at, request.lease_expires_at
        )
        or not _exact_float(claim.operation_deadline, request.deadline)
    ):
        raise ValueError
    return claim.evidence_digest


def _assert_cancel_claim(
    claim: Any,
    request: Nav2CancelRequest,
    checked_at: float,
    sample_index: int,
) -> str:
    if (
        type(claim) is not DispatchClaimEvidence
        or claim.phase != 'cancel'
        or claim.operation_id != request.operation_id
        or claim.sample_index != sample_index
        or claim.goal_uuid != request.goal_uuid
        or claim.worker_id != request.worker_id
        or claim.fence_epoch != request.fence_epoch
        or claim.cancel_request_id != request.cancel_request_id
        or claim.cancel_request_fingerprint != request.request_fingerprint
        or claim.binding_digest != request.binding_digest
        or claim.wire_payload_digest != request.wire_payload_digest
        or not _exact_float(claim.checked_at, checked_at)
    ):
        raise ValueError
    return claim.evidence_digest


def _cancel_observation(
    store: GazeboMonitorRoomStore,
    request: Nav2CancelRequest,
    checked_at: float,
    *,
    expected: tuple | None = None,
) -> tuple[
    OperationObservation,
    PrivateOperationBinding,
    PrivateStoredSample,
]:
    first = _store_tuple(store, request.operation_id)
    second = _store_tuple(store, request.operation_id)
    observation, binding, sample = first
    if (
        first != second
        or (expected is not None and first != expected)
        or observation.operation_id != request.operation_id
        or observation.state != 'cancel_requested'
        or observation.current_sample_state != 'cancel_requested'
        or observation.current_goal_uuid != request.goal_uuid
        or observation.cancel_request_id != request.cancel_request_id
        or observation.lease_owner != request.worker_id
        or observation.fence_epoch != request.fence_epoch
        or observation.lease_expires_at is None
        or checked_at >= observation.lease_expires_at
        or binding.operation_id != request.operation_id
        or binding.binding_digest != request.binding_digest
        or sample.operation_id != request.operation_id
        or sample.index != observation.current_sample_index
        or sample.goal_uuid != request.goal_uuid
        or sample.state != 'cancel_requested'
    ):
        raise ValueError
    return first
