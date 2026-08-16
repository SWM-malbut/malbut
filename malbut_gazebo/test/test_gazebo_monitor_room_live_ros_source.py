"""Tests for the read-only ROS 2 Humble live-evidence source."""

from copy import deepcopy
import inspect
from types import SimpleNamespace

from builtin_interfaces.msg import Duration, Time
from geometry_msgs.msg import PoseStamped, TransformStamped
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import ComputePathToPose
from nav2_msgs.msg import Costmap
from nav2_msgs.srv import GetCostmap
from nav_msgs.msg import Path
import pytest

import malbut_gazebo.gazebo_monitor_room_live_ros_source as source_module
from malbut_gazebo.gazebo_monitor_room_live_ros_source import (
    BASE_FRAME,
    COMPUTE_PATH_ACTION_FQN,
    GLOBAL_COSTMAP_SERVICE_FQN,
    LIFECYCLE_SERVICE_FQNS,
    MAP_FRAME,
    ROS_CALL_TIMEOUT_SECONDS,
    GazeboMonitorRoomLiveRosSource,
    GazeboMonitorRoomLiveRosSourceError,
    TrustedGazeboMonitorRoomLiveRosFacade,
)
from malbut_gazebo.gazebo_monitor_room_live_validator import (
    GazeboMonitorRoomLiveEvidence,
    GazeboMonitorRoomLiveEvidenceUnavailableError,
    TrustedGazeboMonitorRoomLiveEvidenceSource,
)
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    Nav2PreflightRequest,
)
from malbut_gazebo.gazebo_monitor_room_navigation_safety import PathPoint


_ROS_NOW_NS = 10_000_000_000
_MAP_DIGEST = 'a' * 64
_SEMANTIC_DIGEST = 'b' * 64


def _stamp(nanoseconds=_ROS_NOW_NS):
    value = Time()
    value.sec = nanoseconds // 1_000_000_000
    value.nanosec = nanoseconds % 1_000_000_000
    return value


def _preflight(*, x_m=2.5, y_m=0.5):
    return Nav2PreflightRequest(
        operation_id='operation-1',
        robot_id='robot-1',
        map_id='home-map',
        map_revision='map-revision-1',
        semantic_revision='semantic-revision-1',
        zones_digest='1' * 64,
        target_binding_digest='2' * 64,
        effects_digest='3' * 64,
        profile_digest='4' * 64,
        plan_digest='5' * 64,
        sample_count=3,
        sample_index=1,
        polygon_ordinal=2,
        row_ordinal=4,
        goal_uuid='00112233445566778899aabbccddeeff',
        binding_digest='6' * 64,
        x_m=x_m,
        y_m=y_m,
    )


def _lifecycle(*, state_id=State.PRIMARY_STATE_ACTIVE, label='active'):
    response = GetState.Response()
    response.current_state.id = state_id
    response.current_state.label = label
    return response


def _transform(*, x_m=0.5, y_m=0.5, frame_id=MAP_FRAME):
    result = TransformStamped()
    result.header.frame_id = frame_id
    result.header.stamp = _stamp()
    result.child_frame_id = BASE_FRAME
    result.transform.translation.x = x_m
    result.transform.translation.y = y_m
    result.transform.translation.z = 0.0
    result.transform.rotation.w = 1.0
    return result


def _costmap(*, width=4, height=4, frame_id=MAP_FRAME):
    response = GetCostmap.Response()
    message = Costmap()
    message.header.frame_id = frame_id
    message.header.stamp = _stamp()
    message.metadata.map_load_time = _stamp(1_000_000_000)
    message.metadata.update_time = _stamp()
    message.metadata.layer = 'global_costmap'
    message.metadata.resolution = 1.0
    message.metadata.size_x = width
    message.metadata.size_y = height
    message.metadata.origin.orientation.w = 1.0
    message.data = [0] * (width * height)
    response.map = message
    return response


def _pose(x_m, y_m, *, frame_id=MAP_FRAME):
    result = PoseStamped()
    result.header.frame_id = frame_id
    result.header.stamp = _stamp()
    result.pose.position.x = x_m
    result.pose.position.y = y_m
    result.pose.orientation.w = 1.0
    return result


def _path_result(*, target_x=2.5, target_y=0.5, frame_id=MAP_FRAME):
    result = ComputePathToPose.Result()
    path = Path()
    path.header.frame_id = frame_id
    path.header.stamp = _stamp()
    path.poses = [
        _pose(0.5, 0.5, frame_id=frame_id),
        _pose(1.5, 0.5, frame_id=frame_id),
        _pose(target_x, target_y, frame_id=frame_id),
    ]
    result.path = path
    result.planning_time = Duration(sec=0, nanosec=100_000_000)
    return result


class _FakeFacade(TrustedGazeboMonitorRoomLiveRosFacade):
    def __init__(self):
        self.configuration_calls = 0
        self.lifecycle_calls = []
        self.transform_calls = []
        self.costmap_calls = []
        self.compute_calls = []
        self.ros_now_calls = 0
        self.configurations = [source_module._FIXED_CONFIGURATION_DIGEST]
        self.lifecycle_responses = [_lifecycle()]
        self.transform_responses = [_transform()]
        self.costmap_responses = [_costmap()]
        self.path_result = _path_result()
        self.compute_error = None
        self.mutate_goal = False
        self.request_to_mutate = None
        self.ros_now_values = [_ROS_NOW_NS]

    @staticmethod
    def _selected(values, call_count):
        return values[min(call_count, len(values) - 1)]

    def assert_fixed_configuration(self):
        """Return the next fixed-configuration observation."""
        selected = self._selected(
            self.configurations, self.configuration_calls
        )
        self.configuration_calls += 1
        return selected

    def lifecycle_state(self, service_fqn, timeout_seconds):
        """Return a detached lifecycle response and record authority."""
        assert service_fqn in LIFECYCLE_SERVICE_FQNS
        assert type(timeout_seconds) is float
        assert 0.0 < timeout_seconds <= ROS_CALL_TIMEOUT_SECONDS
        call_index = len(self.lifecycle_calls)
        self.lifecycle_calls.append((service_fqn, timeout_seconds))
        return deepcopy(self._selected(self.lifecycle_responses, call_index))

    def lookup_transform(
        self, target_frame, source_frame, timeout_seconds
    ):
        """Return a detached transform and record its fixed frames."""
        assert target_frame == MAP_FRAME
        assert source_frame == BASE_FRAME
        assert type(timeout_seconds) is float
        assert 0.0 < timeout_seconds <= ROS_CALL_TIMEOUT_SECONDS
        call_index = len(self.transform_calls)
        self.transform_calls.append(
            (target_frame, source_frame, timeout_seconds)
        )
        return deepcopy(self._selected(self.transform_responses, call_index))

    def global_costmap(self, service_fqn, timeout_seconds):
        """Return a detached costmap and record its fixed service."""
        assert service_fqn == GLOBAL_COSTMAP_SERVICE_FQN
        assert type(timeout_seconds) is float
        assert 0.0 < timeout_seconds <= ROS_CALL_TIMEOUT_SECONDS
        call_index = len(self.costmap_calls)
        self.costmap_calls.append((service_fqn, timeout_seconds))
        return deepcopy(self._selected(self.costmap_responses, call_index))

    def compute_path(self, action_fqn, goal, timeout_seconds):
        """Return planner evidence without exposing any motion method."""
        assert action_fqn == COMPUTE_PATH_ACTION_FQN
        assert type(timeout_seconds) is float
        assert 0.0 < timeout_seconds <= ROS_CALL_TIMEOUT_SECONDS
        assert type(goal) is ComputePathToPose.Goal
        self.compute_calls.append((action_fqn, deepcopy(goal)))
        if self.compute_error is not None:
            raise self.compute_error
        if self.mutate_goal:
            goal.goal.pose.position.x += 1.0
        if self.request_to_mutate is not None:
            object.__setattr__(self.request_to_mutate, 'x_m', 99.0)
        return deepcopy(self.path_result)

    def ros_now_nanoseconds(self):
        """Return the next deterministic simulated ROS time."""
        selected = self._selected(
            self.ros_now_values, self.ros_now_calls
        )
        self.ros_now_calls += 1
        return selected


def _source(facade=None, *, clock=None):
    selected = _FakeFacade() if facade is None else facade
    return GazeboMonitorRoomLiveRosSource(
        selected,
        clock=(lambda: 5.0) if clock is None else clock,
    ), selected


def _capture(source, request=None):
    return source.capture(
        _preflight() if request is None else request,
        checked_at=5.0,
        active_map_evidence_digest=_MAP_DIGEST,
        semantic_content_digest=_SEMANTIC_DIGEST,
    )


def test_construction_performs_no_ros_observation_or_planner_call() -> None:
    """Construction validates config but sends no service/action request."""
    source, facade = _source()

    assert isinstance(source, TrustedGazeboMonitorRoomLiveEvidenceSource)
    assert facade.configuration_calls == 2
    assert facade.lifecycle_calls == []
    assert facade.transform_calls == []
    assert facade.costmap_calls == []
    assert facade.compute_calls == []


def test_happy_capture_returns_exact_redacted_live_evidence() -> None:
    """A stable active Nav2 snapshot yields one strict evidence envelope."""
    source, facade = _source()

    evidence = _capture(source)

    assert type(evidence) is GazeboMonitorRoomLiveEvidence
    assert evidence.request_fingerprint == _preflight().request_fingerprint
    assert evidence.operation_id == 'operation-1'
    assert evidence.goal_uuid == '00112233445566778899aabbccddeeff'
    assert evidence.active_map_evidence_digest == _MAP_DIGEST
    assert evidence.semantic_content_digest == _SEMANTIC_DIGEST
    assert evidence.captured_at == 5.0
    assert evidence.valid_until == 6.0
    assert evidence.path.point_count == 3
    assert evidence.costmap.width == 4
    assert evidence.costmap.height == 4
    assert repr(evidence) == 'GazeboMonitorRoomLiveEvidence(<redacted>)'
    assert len(facade.lifecycle_calls) == len(LIFECYCLE_SERVICE_FQNS) * 2
    assert len(facade.transform_calls) == 2
    assert len(facade.costmap_calls) == 2
    assert len(facade.compute_calls) == 1
    goal = facade.compute_calls[0][1]
    assert goal.use_start is True
    assert goal.planner_id == ''
    assert goal.start.header.frame_id == MAP_FRAME
    assert goal.goal.header.frame_id == MAP_FRAME
    assert goal.goal.pose.position.x == 2.5
    assert goal.goal.pose.position.y == 0.5


def test_inactive_lifecycle_is_rejected_before_planning() -> None:
    """Any inactive required Nav2 lifecycle node fails closed."""
    facade = _FakeFacade()
    facade.lifecycle_responses = [
        _lifecycle(state_id=State.PRIMARY_STATE_INACTIVE, label='inactive')
    ]
    source, facade = _source(facade)

    with pytest.raises(GazeboMonitorRoomLiveRosSourceError) as captured:
        _capture(source)

    assert captured.value.code == 'live_ros_source_evidence_rejected'
    assert facade.compute_calls == []


def test_temporary_ros_unavailability_uses_retryable_error() -> None:
    """A bounded transport timeout maps to the validator retryable class."""
    facade = _FakeFacade()
    facade.compute_error = source_module._RosFacadeUnavailableError()
    source, _facade = _source(facade)

    with pytest.raises(
        GazeboMonitorRoomLiveEvidenceUnavailableError
    ) as captured:
        _capture(source)

    assert str(captured.value) == 'live_evidence_unavailable'
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    'field,mutator',
    [
        ('tf', lambda facade: setattr(
            facade.transform_responses[0].header,
            'frame_id',
            'odom',
        )),
        ('costmap', lambda facade: setattr(
            facade.costmap_responses[0].map.header,
            'frame_id',
            'odom',
        )),
        ('path', lambda facade: setattr(
            facade.path_result.path.header,
            'frame_id',
            'odom',
        )),
    ],
)
def test_frame_mismatch_is_rejected(field, mutator) -> None:
    """TF, costmap, and planner evidence must all use the map frame."""
    facade = _FakeFacade()
    mutator(facade)
    source, _facade = _source(facade)

    with pytest.raises(GazeboMonitorRoomLiveRosSourceError):
        _capture(source)


def test_costmap_shape_origin_and_bytes_are_validated() -> None:
    """Malformed dimensions, origin yaw, and byte counts fail closed."""
    for mutation in ('shape', 'yaw', 'bytes'):
        facade = _FakeFacade()
        if mutation == 'shape':
            facade.costmap_responses[0].map.metadata.size_x = 0
        elif mutation == 'yaw':
            orientation = (
                facade.costmap_responses[0].map.metadata.origin.orientation
            )
            orientation.z = 0.1
            orientation.w = (1.0 - 0.01) ** 0.5
        else:
            facade.costmap_responses[0].map.data = [0] * 15
        source, _facade = _source(facade)

        with pytest.raises(GazeboMonitorRoomLiveRosSourceError):
            _capture(source)


@pytest.mark.parametrize('kind', ['tf', 'costmap', 'path'])
def test_ros_message_timestamps_must_be_current(kind) -> None:
    """Stale TF, costmap, and planner stamps cannot authorize preflight."""
    facade = _FakeFacade()
    stale = _stamp(1_000_000_000)
    if kind == 'tf':
        facade.transform_responses[0].header.stamp = stale
    elif kind == 'costmap':
        facade.costmap_responses[0].map.header.stamp = stale
        facade.costmap_responses[0].map.metadata.update_time = stale
    else:
        facade.path_result.path.header.stamp = stale
    source, _facade = _source(facade)

    with pytest.raises(GazeboMonitorRoomLiveRosSourceError):
        _capture(source)


def test_nonfinite_transform_geometry_is_rejected() -> None:
    """Non-finite localization geometry cannot enter a path DTO."""
    facade = _FakeFacade()
    facade.transform_responses[0].transform.translation.x = float('nan')
    source, _facade = _source(facade)

    with pytest.raises(GazeboMonitorRoomLiveRosSourceError):
        _capture(source)


def test_path_endpoint_must_echo_exact_requested_target_nearby() -> None:
    """A planner path ending away from the exact requested target rejects."""
    facade = _FakeFacade()
    facade.path_result = _path_result(target_x=2.6)
    source, _facade = _source(facade)

    with pytest.raises(GazeboMonitorRoomLiveRosSourceError):
        _capture(source)


@pytest.mark.parametrize('kind', ['tf', 'map', 'lifecycle', 'clock'])
def test_post_compute_recheck_detects_live_state_drift(kind) -> None:
    """Lifecycle, TF, map, and ROS-clock races cannot cross the boundary."""
    facade = _FakeFacade()
    if kind == 'tf':
        facade.transform_responses = [_transform(), _transform(x_m=0.6)]
    elif kind == 'map':
        changed = _costmap()
        changed.map.data[5] = 1
        facade.costmap_responses = [_costmap(), changed]
    elif kind == 'lifecycle':
        facade.lifecycle_responses = (
            [_lifecycle()] * len(LIFECYCLE_SERVICE_FQNS)
            + [_lifecycle(
                state_id=State.PRIMARY_STATE_INACTIVE,
                label='inactive',
            )]
        )
    else:
        facade.ros_now_values = [_ROS_NOW_NS] * 6 + [_ROS_NOW_NS - 1]
    source, _facade = _source(facade)

    with pytest.raises(GazeboMonitorRoomLiveRosSourceError):
        _capture(source)


def test_goal_mutation_and_spoofed_ros_response_are_rejected() -> None:
    """Mutable planner inputs and response lookalikes are not trusted."""
    mutating = _FakeFacade()
    mutating.mutate_goal = True
    source, _facade = _source(mutating)
    with pytest.raises(GazeboMonitorRoomLiveRosSourceError):
        _capture(source)

    spoofing = _FakeFacade()
    spoofing.costmap_responses = [SimpleNamespace(map=_costmap().map)]
    source, _facade = _source(spoofing)
    with pytest.raises(GazeboMonitorRoomLiveRosSourceError):
        _capture(source)


def test_request_is_detached_before_any_ros_call() -> None:
    """Later mutation of the caller DTO cannot change planner authority."""
    request = _preflight()
    facade = _FakeFacade()
    facade.request_to_mutate = request
    source, facade = _source(facade)

    evidence = _capture(source, request)

    assert request.x_m == 99.0
    assert evidence.target_point.digest == PathPoint(2.5, 0.5).digest
    assert facade.compute_calls[0][1].goal.pose.position.x == 2.5


def test_one_aggregate_deadline_bounds_the_full_capture() -> None:
    """Repeated individually-fast reads still share one overall budget."""
    class _StepClock:
        def __init__(self):
            self.value = 5.0

        def __call__(self):
            current = self.value
            self.value += 0.25
            return current

    facade = _FakeFacade()
    source, facade = _source(facade, clock=_StepClock())

    with pytest.raises(GazeboMonitorRoomLiveEvidenceUnavailableError):
        _capture(source)

    assert len(facade.lifecycle_calls) < len(LIFECYCLE_SERVICE_FQNS) * 2
    assert facade.compute_calls == []


def test_bound_collaborator_and_source_configuration_are_sealed() -> None:
    """Replacing facade methods later cannot replace the captured seam."""
    source, facade = _source()
    original = facade.compute_path
    facade.compute_path = lambda *_args: (_ for _ in ()).throw(AssertionError)

    assert type(_capture(source)) is GazeboMonitorRoomLiveEvidence
    assert len(facade.compute_calls) == 1
    facade.compute_path = original
    with pytest.raises(AttributeError):
        source._compute_path = lambda *_args: None


def test_object_setattr_cannot_replace_source_callbacks_or_capture() -> None:
    """External seals reject low-level callback and method shadowing."""
    source, facade = _source()
    poisoned_calls = []

    def poisoned_compute(*_args, **_kwargs):
        poisoned_calls.append(True)
        return _path_result()

    object.__setattr__(source, '_compute_path', poisoned_compute)
    with pytest.raises(GazeboMonitorRoomLiveRosSourceError) as captured:
        _capture(source)
    assert captured.value.code == 'live_ros_source_invalid_configuration'
    assert poisoned_calls == []
    assert facade.compute_calls == []

    shadowed, shadowed_facade = _source()
    object.__setattr__(
        shadowed,
        '_capture',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError),
    )
    with pytest.raises(GazeboMonitorRoomLiveRosSourceError) as captured:
        _capture(shadowed)
    assert captured.value.code == 'live_ros_source_invalid_configuration'
    assert shadowed_facade.compute_calls == []


def test_configuration_drift_is_rejected_without_ros_side_effects() -> None:
    """Endpoint or sim-time drift after construction fails before capture."""
    facade = _FakeFacade()
    source, facade = _source(facade)
    facade.configurations.append('c' * 64)

    with pytest.raises(GazeboMonitorRoomLiveRosSourceError) as captured:
        _capture(source)

    assert captured.value.code == 'live_ros_source_invalid_configuration'
    assert facade.lifecycle_calls == []
    assert facade.compute_calls == []


def test_surface_has_no_navigation_cancel_or_velocity_capability() -> None:
    """The production module exposes planner reads but no motion primitive."""
    assert 'NavigateToPose' not in source_module.__dict__
    assert 'Twist' not in source_module.__dict__
    public_methods = {
        name
        for name, value in inspect.getmembers(
            TrustedGazeboMonitorRoomLiveRosFacade,
            predicate=inspect.isfunction,
        )
        if not name.startswith('_')
    }
    assert public_methods == {
        'assert_fixed_configuration',
        'lifecycle_state',
        'lookup_transform',
        'global_costmap',
        'compute_path',
        'ros_now_nanoseconds',
    }


def test_humble_message_api_matches_the_validated_contract() -> None:
    """Installed Humble interfaces retain the exact fields we validate."""
    assert GetState.Request.get_fields_and_field_types() == {}
    assert GetState.Response.get_fields_and_field_types() == {
        'current_state': 'lifecycle_msgs/State'
    }
    assert GetCostmap.Request.get_fields_and_field_types() == {
        'specs': 'nav2_msgs/CostmapMetaData'
    }
    assert GetCostmap.Response.get_fields_and_field_types() == {
        'map': 'nav2_msgs/Costmap'
    }
    assert ComputePathToPose.Goal.get_fields_and_field_types() == {
        'goal': 'geometry_msgs/PoseStamped',
        'start': 'geometry_msgs/PoseStamped',
        'planner_id': 'string',
        'use_start': 'boolean',
    }
    assert ComputePathToPose.Result.get_fields_and_field_types() == {
        'path': 'nav_msgs/Path',
        'planning_time': 'builtin_interfaces/Duration',
    }
