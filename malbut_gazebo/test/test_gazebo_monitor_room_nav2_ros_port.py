"""Unit tests for the Gazebo-only ROS 2 Nav2 transport boundary."""

import hashlib
import inspect
import json
from types import SimpleNamespace

from action_msgs.msg import GoalInfo, GoalStatus, GoalStatusArray
from action_msgs.srv import CancelGoal
from builtin_interfaces.msg import Time
from nav2_msgs.action import NavigateToPose
import pytest
from rclpy.action import ActionClient
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    ReliabilityPolicy,
)
from unique_identifier_msgs.msg import UUID

import malbut_gazebo.gazebo_monitor_room_nav2_ros_port as ros_port_module
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    Nav2CancelReport,
    Nav2CancelRequest,
    Nav2GoalQuery,
    Nav2GoalReport,
    Nav2PreflightReport,
    Nav2PreflightRequest,
    Nav2StartRequest,
)
from malbut_gazebo.gazebo_monitor_room_nav2_ros_port import (
    GazeboMonitorRoomNav2RosPort,
    GazeboMonitorRoomNav2RosPortError,
    NAVIGATE_ACTION_FQN,
    NAVIGATE_CANCEL_SERVICE_FQN,
    NAVIGATE_STATUS_TOPIC_FQN,
    Nav2CancelAuthorization,
    Nav2LivePreflightValidation,
    Nav2StartAuthorization,
    TrustedGazeboMonitorRoomNav2Validator,
)


_GOAL_UUID = '00112233445566778899aabbccddeeff'
_OTHER_GOAL_UUID = 'ffeeddccbbaa99887766554433221100'
_BINDING = '1' * 64
_LIVE_BINDING = '8' * 64
_PATH_EVIDENCE = '9' * 64
_START_AUTHORITY = 'a' * 64
_CANCEL_AUTHORITY = 'b' * 64


def _digest(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _preflight(*, goal_uuid=_GOAL_UUID, x_m=1.25, y_m=-2.5):
    return Nav2PreflightRequest(
        operation_id='operation-1',
        robot_id='robot-1',
        map_id='home-map',
        map_revision='map-revision-1',
        semantic_revision='semantic-revision-1',
        zones_digest='2' * 64,
        target_binding_digest='3' * 64,
        effects_digest='4' * 64,
        profile_digest='5' * 64,
        plan_digest='6' * 64,
        sample_count=3,
        sample_index=1,
        polygon_ordinal=2,
        row_ordinal=4,
        goal_uuid=goal_uuid,
        binding_digest=_BINDING,
        x_m=x_m,
        y_m=y_m,
    )


def _start(
    *,
    goal_uuid=_GOAL_UUID,
    lease_expires_at=100.0,
    deadline=90.0,
):
    return Nav2StartRequest(
        preflight=_preflight(goal_uuid=goal_uuid),
        worker_id='worker-1',
        fence_epoch=7,
        lease_expires_at=lease_expires_at,
        deadline=deadline,
        preflight_digest='7' * 64,
    )


def _query(*, goal_uuid=_GOAL_UUID, binding_digest=_BINDING):
    return Nav2GoalQuery(
        operation_id='operation-1',
        worker_id='worker-1',
        fence_epoch=7,
        goal_uuid=goal_uuid,
        binding_digest=binding_digest,
    )


def _cancel(*, goal_uuid=_GOAL_UUID, cancel_request_id='cancel-1'):
    return Nav2CancelRequest(
        operation_id='operation-1',
        worker_id='worker-1',
        fence_epoch=7,
        cancel_request_id=cancel_request_id,
        goal_uuid=goal_uuid,
        binding_digest=_BINDING,
    )


def _uuid_message(value):
    return UUID(uuid=list(bytes.fromhex(value)))


def _goal_info(value):
    result = GoalInfo()
    result.goal_id = _uuid_message(value)
    return result


def _status_message(*entries):
    message = GoalStatusArray()
    for goal_uuid, status in entries:
        item = GoalStatus()
        item.goal_info = _goal_info(goal_uuid)
        item.status = status
        message.status_list.append(item)
    return message


def _cancel_response(return_code, *goal_uuids):
    response = CancelGoal.Response()
    response.return_code = return_code
    response.goals_canceling = [
        _goal_info(goal_uuid) for goal_uuid in goal_uuids
    ]
    return response


def _result_response(status):
    response = NavigateToPose.Impl.GetResultService.Response()
    response.status = status
    return response


class _Clock:
    def __init__(self, value=10.0):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


class _SequenceClock:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class _ImmediateFuture:
    def __init__(self, result=None, error=None, result_hook=None):
        self._result = result
        self._error = error
        self._result_hook = result_hook

    def add_done_callback(self, callback):
        callback(self)

    def result(self):
        if self._result_hook is not None:
            hook = self._result_hook
            self._result_hook = None
            hook()
        if self._error is not None:
            raise self._error
        return self._result


class _PendingFuture:
    def __init__(self):
        self.callbacks = []
        self._result = None

    def add_done_callback(self, callback):
        self.callbacks.append(callback)

    def result(self):
        return self._result

    def complete(self, result):
        self._result = result
        for callback in tuple(self.callbacks):
            callback(self)


class _GoalHandle:
    def __init__(
        self,
        *,
        accepted=True,
        goal_id=None,
        stamp=None,
        result_future=None,
        result_error=None,
    ):
        self.accepted = accepted
        self.goal_id = goal_id or _uuid_message(_GOAL_UUID)
        self.stamp = stamp if stamp is not None else Time()
        self.result_future = result_future or _PendingFuture()
        self.result_error = result_error
        self.result_calls = 0

    def get_result_async(self):
        self.result_calls += 1
        if self.result_error is not None:
            raise self.result_error
        return self.result_future


class _FakeActionClient:
    def __init__(self):
        self.ready = True
        self.send_future = _ImmediateFuture(_GoalHandle())
        self.send_error = None
        self.send_hook = None
        self.sent = []
        self.destroy_calls = 0

    def server_is_ready(self):
        return self.ready

    def send_goal_async(self, goal, *, goal_uuid):
        self.sent.append((goal, goal_uuid))
        if self.send_hook is not None:
            self.send_hook()
        if self.send_error is not None:
            raise self.send_error
        return self.send_future

    def destroy(self):
        self.destroy_calls += 1


class _FakeCancelClient:
    def __init__(self):
        self.ready = True
        self.future = _ImmediateFuture(
            _cancel_response(CancelGoal.Response.ERROR_NONE, _GOAL_UUID)
        )
        self.call_error = None
        self.calls = []

    def service_is_ready(self):
        return self.ready

    def call_async(self, request):
        self.calls.append(request)
        if self.call_error is not None:
            raise self.call_error
        return self.future


class _FakeRosClock:
    def __init__(self):
        self.calls = 0

    def now(self):
        self.calls += 1
        return self

    def to_msg(self):
        return Time(sec=123, nanosec=456)


class _FakeNode:
    def __init__(self):
        self.action = _FakeActionClient()
        self.cancel = _FakeCancelClient()
        self.ros_clock = _FakeRosClock()
        self.use_sim_time = True
        self.topic_remaps = {}
        self.service_remaps = {}
        self.client_error = None
        self.subscription_error = None
        self.subscription_result = object()
        self.clients = []
        self.subscriptions = []
        self.destroyed_clients = []
        self.destroyed_subscriptions = []
        self.action_entities = []

    def get_parameter(self, name):
        assert name == 'use_sim_time'
        return SimpleNamespace(value=self.use_sim_time)

    def resolve_topic_name(self, name):
        return self.topic_remaps.get(name, name)

    def resolve_service_name(self, name):
        return self.service_remaps.get(name, name)

    def create_client(self, service_type, name):
        if self.client_error is not None:
            raise self.client_error
        self.clients.append((service_type, name))
        return self.cancel

    def create_subscription(
        self,
        message_type,
        name,
        callback,
        qos,
    ):
        if self.subscription_error is not None:
            raise self.subscription_error
        self.subscriptions.append((message_type, name, callback, qos))
        return self.subscription_result

    def destroy_client(self, client):
        self.destroyed_clients.append(client)

    def destroy_subscription(self, subscription):
        self.destroyed_subscriptions.append(subscription)

    def get_clock(self):
        return self.ros_clock


class _Validator(TrustedGazeboMonitorRoomNav2Validator):
    def __init__(self):
        self.outcome = 'ready'
        self.code = 'validator_private_reason'
        self.live_binding_digest = _LIVE_BINDING
        self.path_evidence_digest = _PATH_EVIDENCE
        self.start_authority_digest = _START_AUTHORITY
        self.cancel_authority_digest = _CANCEL_AUTHORITY
        self.preflight_hook = None
        self.start_hook = None
        self.cancel_hook = None
        self.preflights = []
        self.starts = []
        self.cancels = []

    def validate_preflight(self, request, *, checked_at):
        self.preflights.append((request, checked_at))
        if self.preflight_hook is not None:
            return self.preflight_hook(request, checked_at)
        return Nav2LivePreflightValidation(
            request_fingerprint=request.request_fingerprint,
            binding_digest=request.binding_digest,
            goal_uuid=request.goal_uuid,
            outcome=self.outcome,
            code=self.code,
            live_binding_digest=self.live_binding_digest,
            path_evidence_digest=self.path_evidence_digest,
        )

    def authorize_start(self, request, *, checked_at):
        self.starts.append((request, checked_at))
        if self.start_hook is not None:
            return self.start_hook(request, checked_at)
        return Nav2StartAuthorization(
            operation_id=request.preflight.operation_id,
            worker_id=request.worker_id,
            goal_uuid=request.preflight.goal_uuid,
            binding_digest=request.preflight.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=self.start_authority_digest,
        )

    def authorize_cancel(self, request, *, checked_at):
        self.cancels.append((request, checked_at))
        if self.cancel_hook is not None:
            return self.cancel_hook(request, checked_at)
        return Nav2CancelAuthorization(
            operation_id=request.operation_id,
            worker_id=request.worker_id,
            cancel_request_id=request.cancel_request_id,
            goal_uuid=request.goal_uuid,
            binding_digest=request.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=self.cancel_authority_digest,
        )


def _port(
    *,
    node=None,
    validator=None,
    clock=None,
    response_timeout_seconds=0.01,
    cancel_timeout_seconds=0.01,
):
    selected_node = node or _FakeNode()
    selected_validator = validator or _Validator()

    def action_factory(passed_node, action_type, name):
        selected_node.action_entities.append(
            (passed_node, action_type, name)
        )
        return selected_node.action

    port = GazeboMonitorRoomNav2RosPort(
        selected_node,
        validator=selected_validator,
        clock=clock or _Clock(),
        response_timeout_seconds=response_timeout_seconds,
        cancel_timeout_seconds=cancel_timeout_seconds,
        action_client_factory=action_factory,
    )
    return port, selected_node, selected_validator


def test_construction_uses_fixed_entities_qos_and_has_no_wire_calls():
    """Construction creates the exact entities but sends no request."""
    port, node, _validator = _port()

    assert node.clients == [(CancelGoal, NAVIGATE_CANCEL_SERVICE_FQN)]
    assert node.action_entities == [
        (node, NavigateToPose, NAVIGATE_ACTION_FQN)
    ]
    assert len(node.subscriptions) == 1
    message_type, topic, _callback, qos = node.subscriptions[0]
    assert message_type is GoalStatusArray
    assert topic == NAVIGATE_STATUS_TOPIC_FQN
    assert qos.history == HistoryPolicy.KEEP_LAST
    assert qos.depth == 1
    assert qos.reliability == ReliabilityPolicy.RELIABLE
    assert qos.durability == DurabilityPolicy.TRANSIENT_LOCAL
    assert node.action.sent == []
    assert node.cancel.calls == []
    assert node.ros_clock.calls == 0
    assert (
        port.BLOCKING_CALL_CONTEXT
        == 'dedicated_non_executor_worker'
    )


def test_humble_action_client_exposes_explicit_goal_uuid_api():
    """The installed transport API supports caller-owned stable UUID bytes."""
    parameters = inspect.signature(
        ActionClient.send_goal_async
    ).parameters

    assert 'goal_uuid' in parameters
    assert parameters['goal_uuid'].default is None


def test_default_validator_is_fail_closed_without_wire_calls():
    """No composition-root validator can never invent readiness."""
    node = _FakeNode()
    clock = _Clock()
    port = GazeboMonitorRoomNav2RosPort(
        node,
        clock=clock,
        action_client_factory=lambda *_args: node.action,
        response_timeout_seconds=0.01,
        cancel_timeout_seconds=0.01,
    )

    report = Nav2PreflightReport(**port.preflight(_preflight()))
    start = Nav2GoalReport(**port.ensure_started(_start()))
    cancel = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert (report.outcome, report.code) == (
        'rejected',
        'preflight_rejected',
    )
    assert start.status == 'rejected'
    assert cancel.status == 'rejected'
    assert node.action.sent == []
    assert node.cancel.calls == []


@pytest.mark.parametrize('goal_uuid', ('0' * 32, 'g' * 32, 'a' * 31))
def test_port_rejects_non_wire_goal_uuid_before_collaborators(goal_uuid):
    """ROS goal IDs must be nonzero 16-byte lowercase hex values."""
    port, node, validator = _port()

    with pytest.raises(GazeboMonitorRoomNav2RosPortError) as raised:
        port.preflight(_preflight(goal_uuid=goal_uuid))

    assert raised.value.code == 'nav2_ros_port_invalid_request'
    assert validator.preflights == []
    assert node.action.sent == []
    assert node.cancel.calls == []


@pytest.mark.parametrize(
    ('outcome', 'public_code'),
    (
        ('ready', 'preflight_ready'),
        ('retryable', 'preflight_retryable'),
        ('rejected', 'preflight_rejected'),
    ),
)
def test_preflight_projects_only_fixed_public_outcome_codes(
    outcome,
    public_code,
):
    """Validator reason strings never become durable public codes."""
    validator = _Validator()
    validator.outcome = outcome
    validator.code = 'private_secret_marker'
    port, _node, _validator = _port(validator=validator)

    raw = port.preflight(_preflight())
    report = Nav2PreflightReport(**raw)

    assert report.outcome == outcome
    assert report.code == public_code
    assert 'private_secret_marker' not in repr(raw)


def test_public_projection_and_status_policy_tables_are_immutable():
    """Collaborators cannot rewrite closed wire-to-public policy mappings."""
    with pytest.raises(TypeError):
        ros_port_module._PREFLIGHT_PUBLIC_CODE['ready'] = (
            'private_secret_marker'
        )
    with pytest.raises(TypeError):
        ros_port_module._OBSERVE_STATUS[GoalStatus.STATUS_ACCEPTED] = (
            'private_secret_marker'
        )
    with pytest.raises(TypeError):
        ros_port_module._ALLOWED_STATUS_TRANSITIONS[
            GoalStatus.STATUS_SUCCEEDED
        ] = frozenset({GoalStatus.STATUS_EXECUTING})

    port, node, _validator = _port()
    node.subscriptions[0][2](
        _status_message((_GOAL_UUID, GoalStatus.STATUS_ACCEPTED))
    )
    report = port.preflight(_preflight())
    observed = port.observe_goal(_query())

    assert report['code'] == 'preflight_ready'
    assert observed['status'] == 'accepted'
    assert 'private_secret_marker' not in repr((report, observed))


def test_preflight_evidence_does_not_encode_validator_reason_code():
    """Changing an injected raw reason cannot create a digest side channel."""
    first_validator = _Validator()
    first_validator.code = 'first_private_secret'
    second_validator = _Validator()
    second_validator.code = 'second_private_secret'
    first, _node, _validator = _port(validator=first_validator)
    second, _node, _validator = _port(validator=second_validator)

    first_report = first.preflight(_preflight())
    second_report = second.preflight(_preflight())

    assert first_report == second_report


def test_preflight_rejects_echoed_request_fields_as_live_evidence():
    """Request-controlled digests cannot masquerade as live/path proof."""
    validator = _Validator()
    validator.live_binding_digest = _BINDING
    port, node, _validator = _port(validator=validator)

    report = Nav2PreflightReport(**port.preflight(_preflight()))

    assert (report.outcome, report.code) == (
        'rejected',
        'preflight_rejected',
    )
    assert node.action.sent == []
    assert node.cancel.calls == []


class _SecretCode(str):
    pass


def test_preflight_rejects_string_subclass_code_without_disclosure():
    """A string subclass cannot inject custom behavior or public content."""
    validator = _Validator()
    validator.code = _SecretCode('private_secret_marker')
    port, _node, _validator = _port(validator=validator)

    raw = port.preflight(_preflight())

    assert raw['outcome'] == 'rejected'
    assert raw['code'] == 'preflight_rejected'
    assert 'private_secret_marker' not in repr(raw)


def test_preflight_rejects_validator_request_mutation():
    """A validator cannot rewrite the exact sample it was asked to check."""
    validator = _Validator()

    def mutate(request, _checked_at):
        object.__setattr__(request, 'x_m', 55.0)
        return Nav2LivePreflightValidation(
            request_fingerprint=request.request_fingerprint,
            binding_digest=request.binding_digest,
            goal_uuid=request.goal_uuid,
            outcome='ready',
            code='private_mutated_reason',
            live_binding_digest=_LIVE_BINDING,
            path_evidence_digest=_PATH_EVIDENCE,
        )

    validator.preflight_hook = mutate
    port, node, _validator = _port(validator=validator)

    report = Nav2PreflightReport(**port.preflight(_preflight()))

    assert report.outcome == 'rejected'
    assert report.code == 'preflight_rejected'
    assert node.action.sent == []


@pytest.mark.parametrize('use_sim_time', (False, 1, 'true', None))
def test_construction_rejects_non_exact_sim_time_before_entities(
    use_sim_time,
):
    """The Gazebo-only wire contract requires exact bool use_sim_time."""
    node = _FakeNode()
    node.use_sim_time = use_sim_time
    factory_calls = []

    with pytest.raises(GazeboMonitorRoomNav2RosPortError) as raised:
        GazeboMonitorRoomNav2RosPort(
            node,
            validator=_Validator(),
            clock=_Clock(),
            action_client_factory=lambda *_args: factory_calls.append(True),
        )

    assert raised.value.code == 'nav2_ros_port_invalid_configuration'
    assert raised.value.__cause__ is None
    assert factory_calls == []
    assert node.clients == []
    assert node.subscriptions == []


@pytest.mark.parametrize('kind', ('topic', 'service'))
def test_construction_rejects_endpoint_remapping_before_entities(kind):
    """Digest-declared absolute endpoints cannot be silently remapped."""
    node = _FakeNode()
    if kind == 'topic':
        node.topic_remaps[NAVIGATE_STATUS_TOPIC_FQN] = '/private/status'
    else:
        node.service_remaps[NAVIGATE_CANCEL_SERVICE_FQN] = '/private/cancel'
    factory_calls = []

    with pytest.raises(GazeboMonitorRoomNav2RosPortError):
        GazeboMonitorRoomNav2RosPort(
            node,
            validator=_Validator(),
            clock=_Clock(),
            action_client_factory=lambda *_args: factory_calls.append(True),
        )

    assert factory_calls == []


@pytest.mark.parametrize('failure_step', ('client', 'subscription', 'none'))
def test_constructor_failure_cleans_partial_entities(failure_step):
    """Partial ROS entity creation is unwound and normalized."""
    node = _FakeNode()
    if failure_step == 'client':
        node.client_error = RuntimeError('/private/client')
    elif failure_step == 'subscription':
        node.subscription_error = RuntimeError('/private/subscription')
    else:
        node.subscription_result = None

    with pytest.raises(GazeboMonitorRoomNav2RosPortError) as raised:
        GazeboMonitorRoomNav2RosPort(
            node,
            validator=_Validator(),
            clock=_Clock(),
            action_client_factory=lambda *_args: node.action,
        )

    assert raised.value.code == 'nav2_ros_port_invalid_configuration'
    assert raised.value.__cause__ is None
    assert node.action.destroy_calls == 1
    if failure_step != 'client':
        assert node.destroyed_clients == [node.cancel]


def test_action_client_constructor_failure_is_content_free():
    """The first ROS entity failure does not leak raw construction context."""
    node = _FakeNode()

    def fail(*_args):
        raise RuntimeError('/private/action-construction')

    with pytest.raises(GazeboMonitorRoomNav2RosPortError) as raised:
        GazeboMonitorRoomNav2RosPort(
            node,
            validator=_Validator(),
            clock=_Clock(),
            action_client_factory=fail,
        )

    assert raised.value.code == 'nav2_ros_port_invalid_configuration'
    assert raised.value.__cause__ is None
    assert '/private/' not in repr(raised.value)
    assert node.clients == []
    assert node.subscriptions == []


def test_timeout_overflow_is_content_free_configuration_failure():
    """Raw float overflow cannot escape constructor validation."""
    node = _FakeNode()

    with pytest.raises(GazeboMonitorRoomNav2RosPortError) as raised:
        GazeboMonitorRoomNav2RosPort(
            node,
            validator=_Validator(),
            response_timeout_seconds=10 ** 400,
            action_client_factory=lambda *_args: node.action,
        )

    assert raised.value.code == 'nav2_ros_port_invalid_configuration'
    assert raised.value.__cause__ is None
    assert node.clients == []


def test_start_sends_canonical_pose_with_stable_explicit_uuid():
    """One authorized request becomes the exact fixed NavigateToPose wire."""
    host_clock = _Clock(10.0)
    port, node, validator = _port(clock=host_clock)
    request = _start()

    report = Nav2GoalReport(**port.ensure_started(request))

    assert report.status == 'accepted'
    assert len(node.action.sent) == 1
    goal, goal_id = node.action.sent[0]
    assert type(goal) is NavigateToPose.Goal
    assert bytes(goal_id.uuid) == bytes.fromhex(_GOAL_UUID)
    assert goal.pose.header.frame_id == 'map'
    assert goal.pose.header.stamp == Time(sec=123, nanosec=456)
    assert (
        goal.pose.pose.position.x,
        goal.pose.pose.position.y,
        goal.pose.pose.position.z,
    ) == (1.25, -2.5, 0.0)
    assert (
        goal.pose.pose.orientation.x,
        goal.pose.pose.orientation.y,
        goal.pose.pose.orientation.z,
        goal.pose.pose.orientation.w,
    ) == (0.0, 0.0, 0.0, 1.0)
    assert goal.behavior_tree == ''
    assert node.ros_clock.calls == 1
    assert validator.starts[0][1] == 10.0
    assert host_clock.calls == 3

    repeated = Nav2GoalReport(**port.ensure_started(request))
    assert repeated.status == 'accepted'
    assert len(node.action.sent) == 1
    assert len(validator.starts) == 2


def test_start_wire_digest_matches_independent_fixed_policy():
    """The contract digest and port reconstruction bind the same payload."""
    request = _start()
    expected = _digest(
        {
            'contract': 'malbut-nav2-navigate-to-pose-wire-v1',
            'action_fqn': '/navigate_to_pose',
            'goal_uuid': _GOAL_UUID,
            'frame_id': 'map',
            'position': {'x': 1.25, 'y': -2.5, 'z': 0.0},
            'orientation': {
                'x': 0.0,
                'y': 0.0,
                'z': 0.0,
                'w': 1.0,
            },
            'behavior_tree': '',
            'pose_stamp_policy': 'ros_now_at_enqueue',
            'runtime_mode': 'gazebo',
            'use_sim_time': True,
        }
    )

    assert request.wire_payload_digest == expected
    assert ros_port_module._start_wire_digest(request) == expected


def test_start_rechecks_host_deadline_after_goal_build_and_authority():
    """No send occurs when the fresh side-effect-boundary clock expires."""
    clock = _SequenceClock((1.0, 2.0, 90.0))
    port, node, validator = _port(clock=clock)

    report = Nav2GoalReport(**port.ensure_started(_start(deadline=90.0)))

    assert report.status == 'rejected'
    assert node.action.sent == []
    assert validator.starts[0][1] == 2.0
    assert node.ros_clock.calls == 1


def test_start_reauthorizes_after_build_at_wire_boundary():
    """A durable target revoked during build cannot cross send_goal_async."""
    validator = _Validator()
    revoked = {'value': False}

    def authorize(request, checked_at):
        if revoked['value']:
            raise RuntimeError('/private/revoked-target')
        return Nav2StartAuthorization(
            operation_id=request.preflight.operation_id,
            worker_id=request.worker_id,
            goal_uuid=request.preflight.goal_uuid,
            binding_digest=request.preflight.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=_START_AUTHORITY,
        )

    validator.start_hook = authorize
    node = _FakeNode()

    def revoke_during_stamp():
        revoked['value'] = True
        return Time(sec=123, nanosec=456)

    node.ros_clock.to_msg = revoke_during_stamp
    port, node, validator = _port(node=node, validator=validator)

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'rejected'
    assert len(validator.starts) == 2
    assert node.action.sent == []
    assert '/private/' not in repr(report)


def test_start_rejects_validator_request_mutation_without_send():
    """Authorization for a validator-rewritten payload is never usable."""
    validator = _Validator()

    def mutate(request, checked_at):
        object.__setattr__(request.preflight, 'x_m', 44.0)
        return Nav2StartAuthorization(
            operation_id=request.preflight.operation_id,
            worker_id=request.worker_id,
            goal_uuid=request.preflight.goal_uuid,
            binding_digest=request.preflight.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=_START_AUTHORITY,
        )

    validator.start_hook = mutate
    port, node, _validator = _port(validator=validator)

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'rejected'
    assert node.action.sent == []


def test_start_revalidates_request_after_goal_build_hook():
    """A build collaborator cannot alter digest inputs before enqueue."""
    port, node, _validator = _port()
    original = port._build_goal

    def mutate(request, stamp):
        goal = original(request, stamp)
        object.__setattr__(request.preflight, 'y_m', 33.0)
        return goal

    port._build_goal = mutate

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'rejected'
    assert node.action.sent == []


def test_start_rejects_noncanonical_goal_returned_by_build_hook():
    """The built message, not only its source DTO, is checked before send."""
    port, node, _validator = _port()
    original = port._build_goal

    def wrong_wire(request, stamp):
        goal = original(request, stamp)
        goal.pose.pose.position.x = 999.0
        return goal

    port._build_goal = wrong_wire

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'rejected'
    assert node.action.sent == []


@pytest.mark.parametrize('malformation', ('wrong_type', 'nanosec_range'))
def test_start_rejects_malformed_ros_pose_stamp(malformation):
    """Only an exact bounded builtin Time can stamp the canonical pose."""
    port, node, _validator = _port()
    if malformation == 'wrong_type':
        node.ros_clock.to_msg = lambda: object()
    else:
        stamp = Time(sec=123, nanosec=456)
        stamp._nanosec = 2 ** 32
        node.ros_clock.to_msg = lambda: stamp

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'rejected'
    assert node.action.sent == []


def test_start_detaches_stamp_retained_by_ros_clock_collaborator():
    """Mutating the object returned by to_msg cannot mutate the sent goal."""
    retained = Time(sec=123, nanosec=456)

    class MutatingClock:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls == 3:
                retained._nanosec = 999
            return 10.0

    node = _FakeNode()
    node.ros_clock.to_msg = lambda: retained
    port, node, _validator = _port(node=node, clock=MutatingClock())

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'accepted'
    assert node.action.sent[0][0].pose.header.stamp == Time(
        sec=123, nanosec=456
    )


def test_start_detects_sim_time_flip_during_runtime_resolution():
    """Endpoint resolution cannot hide a concurrent Gazebo-mode change."""
    port, node, _validator = _port()
    original = node.resolve_service_name

    def mutate_runtime(name):
        node.use_sim_time = False
        return original(name)

    node.resolve_service_name = mutate_runtime

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'rejected'
    assert node.action.sent == []


def test_start_rechecks_sim_time_after_authorization():
    """A runtime mode change before enqueue prevents the wire side effect."""
    validator = _Validator()
    port, node, _validator = _port(validator=validator)

    def change_runtime(request, checked_at):
        node.use_sim_time = False
        return Nav2StartAuthorization(
            operation_id=request.preflight.operation_id,
            worker_id=request.worker_id,
            goal_uuid=request.preflight.goal_uuid,
            binding_digest=request.preflight.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=_START_AUTHORITY,
        )

    validator.start_hook = change_runtime

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'rejected'
    assert node.action.sent == []


def test_start_clock_rollback_fails_closed_without_send():
    """An injected host clock rollback cannot authorize a side effect."""
    clock = _SequenceClock((2.0, 3.0, 1.0))
    port, node, _validator = _port(clock=clock)

    with pytest.raises(GazeboMonitorRoomNav2RosPortError) as raised:
        port.ensure_started(_start())

    assert raised.value.code == 'nav2_ros_port_invalid_configuration'
    assert node.action.sent == []


def test_host_clock_overflow_fails_content_free_without_send():
    """An unusable host clock never falls back to simulation time."""
    port, node, _validator = _port(clock=_Clock(10 ** 400))

    with pytest.raises(GazeboMonitorRoomNav2RosPortError) as raised:
        port.ensure_started(_start())

    assert raised.value.code == 'nav2_ros_port_invalid_configuration'
    assert raised.value.__cause__ is None
    assert node.action.sent == []


@pytest.mark.parametrize('ready_value', (False, 1, 'true', None))
def test_start_requires_exact_ready_bool_without_send(ready_value):
    """Bool-like server readiness does not cross the authority boundary."""
    port, node, validator = _port()
    node.action.ready = ready_value

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'rejected'
    assert node.action.sent == []
    assert validator.starts == []


@pytest.mark.parametrize('accepted_value', (1, 'false', None))
def test_start_goal_response_requires_exact_bool(accepted_value):
    """A truthy or bool-like accepted value is delivery-unknown."""
    port, node, _validator = _port()
    handle = _GoalHandle(accepted=accepted_value)
    node.action.send_future = _ImmediateFuture(handle)

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'unknown'
    assert handle.result_calls == 0
    assert len(node.action.sent) == 1


def test_start_response_goal_handle_must_match_stable_uuid():
    """A response for another UUID cannot acknowledge the durable dispatch."""
    port, node, _validator = _port()
    handle = _GoalHandle(goal_id=_uuid_message(_OTHER_GOAL_UUID))
    node.action.send_future = _ImmediateFuture(handle)

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'unknown'
    assert handle.result_calls == 0
    assert len(node.action.sent) == 1


def test_false_response_after_invalid_status_snapshot_is_unknown():
    """An invalid concurrent status snapshot cannot become rejection proof."""
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]
    node.action.send_future = _ImmediateFuture(
        _GoalHandle(accepted=False),
        result_hook=lambda: callback(
            _status_message(
                (_GOAL_UUID, GoalStatus.STATUS_EXECUTING),
                (_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED),
            )
        ),
    )

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'unknown'
    assert len(node.action.sent) == 1


@pytest.mark.parametrize(
    ('ros_status', 'expected'),
    (
        (GoalStatus.STATUS_EXECUTING, 'active'),
        (GoalStatus.STATUS_SUCCEEDED, 'succeeded'),
    ),
)
def test_true_response_uses_concurrent_exact_status(ros_status, expected):
    """A status race is checked after the accepted response future completes."""
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]
    node.action.send_future = _ImmediateFuture(
        _GoalHandle(accepted=True),
        result_hook=lambda: callback(
            _status_message((_GOAL_UUID, ros_status))
        ),
    )

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == expected


def test_true_response_after_invalid_status_snapshot_is_unknown():
    """An accepted bit cannot override a duplicate concurrent snapshot."""
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]
    node.action.send_future = _ImmediateFuture(
        _GoalHandle(accepted=True),
        result_hook=lambda: callback(
            _status_message(
                (_GOAL_UUID, GoalStatus.STATUS_EXECUTING),
                (_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED),
            )
        ),
    )

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'unknown'


@pytest.mark.parametrize(
    ('entries', 'expected'),
    (
        (
            (
                (_GOAL_UUID, GoalStatus.STATUS_EXECUTING),
                (_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED),
            ),
            'unknown',
        ),
        (((_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED),), 'succeeded'),
    ),
)
def test_start_rechecks_status_after_result_callback_registration(
    entries,
    expected,
):
    """Result registration collaborators cannot leave a stale start report."""
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]
    result_future = _PendingFuture()
    original_add = result_future.add_done_callback

    def hook_then_add(done_callback):
        callback(_status_message(*entries))
        original_add(done_callback)

    result_future.add_done_callback = hook_then_add
    node.action.send_future = _ImmediateFuture(
        _GoalHandle(accepted=True, result_future=result_future)
    )

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == expected
    assert port.observe_goal(_query())['status'] == expected


@pytest.mark.parametrize('stamp', (None, object()))
def test_start_response_requires_strict_acceptance_stamp(stamp):
    """A goal handle without exact bounded Time evidence remains unknown."""
    port, node, _validator = _port()
    handle = _GoalHandle()
    handle.stamp = stamp
    node.action.send_future = _ImmediateFuture(handle)

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'unknown'
    assert handle.result_calls == 0


def test_exact_false_without_live_status_is_unknown_and_not_resent():
    """A false response cannot distinguish policy rejection from duplicate."""
    port, node, validator = _port()
    node.action.send_future = _ImmediateFuture(_GoalHandle(accepted=False))

    first = Nav2GoalReport(**port.ensure_started(_start()))
    second = Nav2GoalReport(**port.ensure_started(_start()))

    assert first.status == second.status == 'unknown'
    assert len(node.action.sent) == 1
    assert len(validator.starts) == 2


@pytest.mark.parametrize(
    'failure',
    ('send_call', 'future_timeout', 'future_result', 'result_call'),
)
def test_post_authorization_start_failures_are_unknown_and_not_resent(
    failure,
):
    """Every ambiguous post-boundary failure records a no-resend attempt."""
    port, node, validator = _port(response_timeout_seconds=0.001)
    if failure == 'send_call':
        node.action.send_error = RuntimeError('/private/send')
    elif failure == 'future_timeout':
        node.action.send_future = _PendingFuture()
    elif failure == 'future_result':
        node.action.send_future = _ImmediateFuture(
            error=RuntimeError('/private/result')
        )
    else:
        node.action.send_future = _ImmediateFuture(
            _GoalHandle(result_error=RuntimeError('/private/get-result'))
        )

    first = Nav2GoalReport(**port.ensure_started(_start()))
    second = Nav2GoalReport(**port.ensure_started(_start()))

    assert first.status == second.status == 'unknown'
    assert len(node.action.sent) == 1
    assert len(validator.starts) == 2
    assert '/private/' not in repr(first)


def test_authority_digest_is_bound_into_start_ack_evidence():
    """Changing exact authority proof changes only the evidence digest."""
    first_validator = _Validator()
    second_validator = _Validator()
    second_validator.start_authority_digest = 'c' * 64
    first, _node, _validator = _port(validator=first_validator)
    second, _node, _validator = _port(validator=second_validator)

    first_report = first.ensure_started(_start())
    second_report = second.ensure_started(_start())

    assert set(first_report) == set(second_report)
    assert first_report['status'] == second_report['status'] == 'accepted'
    assert (
        first_report['evidence_digest']
        != second_report['evidence_digest']
    )


def test_start_uses_detached_validated_authorization_snapshot():
    """Later mutation of a returned proof object cannot rewrite ACK evidence."""
    validator = _Validator()
    issued = []

    def authorize(request, checked_at):
        result = Nav2StartAuthorization(
            operation_id=request.preflight.operation_id,
            worker_id=request.worker_id,
            goal_uuid=request.preflight.goal_uuid,
            binding_digest=request.preflight.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=_START_AUTHORITY,
        )
        issued.append(result)
        return result

    validator.start_hook = authorize
    node = _FakeNode()

    def mutate_after_authorization():
        object.__setattr__(
            issued[-1], 'authority_evidence_digest', 'd' * 64
        )
        return Time(sec=123, nanosec=456)

    node.ros_clock.to_msg = mutate_after_authorization
    port, _node, _validator = _port(node=node, validator=validator)
    node.action.send_hook = lambda: object.__setattr__(
        issued[-1], 'authority_evidence_digest', 'd' * 64
    )
    request = _start()

    report = port.ensure_started(request)

    assert report['evidence_digest'] == port._goal_evidence(
        request.request_fingerprint,
        _GOAL_UUID,
        'accepted',
        'send_response',
        _START_AUTHORITY,
    )


def test_start_ack_binds_fresh_boundary_authority_evidence():
    """Dispatch evidence uses the second authorization, not a stale proof."""
    validator = _Validator()
    calls = {'count': 0}

    def authorize(request, checked_at):
        calls['count'] += 1
        digest = _START_AUTHORITY if calls['count'] == 1 else 'd' * 64
        return Nav2StartAuthorization(
            operation_id=request.preflight.operation_id,
            worker_id=request.worker_id,
            goal_uuid=request.preflight.goal_uuid,
            binding_digest=request.preflight.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=digest,
        )

    validator.start_hook = authorize
    port, _node, _validator = _port(validator=validator)
    request = _start()

    report = port.ensure_started(request)

    assert report['evidence_digest'] == port._goal_evidence(
        request.request_fingerprint,
        _GOAL_UUID,
        'accepted',
        'send_response',
        'd' * 64,
    )


def test_synchronous_terminal_result_wins_start_response_race():
    """A completed exact result is reported instead of stale accepted state."""
    handle = _GoalHandle(
        result_future=_ImmediateFuture(
            _result_response(GoalStatus.STATUS_SUCCEEDED)
        )
    )
    port, node, _validator = _port()
    node.action.send_future = _ImmediateFuture(handle)

    report = Nav2GoalReport(**port.ensure_started(_start()))

    assert report.status == 'succeeded'


@pytest.mark.parametrize(
    ('ros_status', 'expected'),
    (
        (GoalStatus.STATUS_ACCEPTED, 'accepted'),
        (GoalStatus.STATUS_EXECUTING, 'active'),
        (GoalStatus.STATUS_CANCELING, 'active'),
        (GoalStatus.STATUS_SUCCEEDED, 'succeeded'),
        (GoalStatus.STATUS_ABORTED, 'aborted'),
        (GoalStatus.STATUS_CANCELED, 'canceled'),
    ),
)
def test_exact_uuid_status_snapshot_maps_known_states(ros_status, expected):
    """Observation uses one exact UUID from the authoritative snapshot."""
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]

    callback(_status_message((_GOAL_UUID, ros_status)))
    report = Nav2GoalReport(**port.observe_goal(_query()))

    assert report.status == expected
    assert node.action.sent == []
    assert node.cancel.calls == []


def test_status_snapshot_ignores_unrelated_uuid():
    """A status for another goal cannot authorize an exact observation."""
    port, node, _validator = _port()

    node.subscriptions[0][2](
        _status_message((_OTHER_GOAL_UUID, GoalStatus.STATUS_EXECUTING))
    )

    assert port.observe_goal(_query())['status'] == 'unknown'


def test_local_dispatch_fallback_requires_exact_operation_binding():
    """Local ACK state is never echoed under a different durable binding."""
    port, _node, _validator = _port()
    assert port.ensure_started(_start())['status'] == 'accepted'

    report = port.observe_goal(_query(binding_digest='f' * 64))

    assert report['status'] == 'unknown'


def test_duplicate_uuid_invalidates_whole_status_snapshot():
    """Duplicate UUID entries are ambiguous rather than last-wins."""
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]
    callback(
        _status_message(
            (_GOAL_UUID, GoalStatus.STATUS_EXECUTING),
            (_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED),
        )
    )

    assert port.observe_goal(_query())['status'] == 'unknown'


def test_invalid_status_snapshot_blocks_new_start_and_cancel_calls():
    """Ambiguous server state is fail-closed at both side-effect boundaries."""
    port, node, validator = _port()
    node.subscriptions[0][2](
        _status_message(
            (_GOAL_UUID, GoalStatus.STATUS_EXECUTING),
            (_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED),
        )
    )

    start = port.ensure_started(_start())
    cancel = port.cancel_goal(_cancel())

    assert start['status'] == cancel['status'] == 'unknown'
    assert node.action.sent == []
    assert node.cancel.calls == []
    assert validator.starts == []
    assert validator.cancels == []


def test_terminal_to_active_status_regression_invalidates_snapshot():
    """A terminal UUID cannot regress to an active state without ambiguity."""
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]
    callback(
        _status_message((_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED))
    )
    callback(
        _status_message((_GOAL_UUID, GoalStatus.STATUS_EXECUTING))
    )

    assert port.observe_goal(_query())['status'] == 'unknown'


def test_bool_status_invalidates_whole_snapshot():
    """Bool aliases are not accepted as generated integer status enums."""
    message = _status_message(
        (_GOAL_UUID, GoalStatus.STATUS_EXECUTING)
    )
    message.status_list[0]._status = True
    port, node, _validator = _port()

    node.subscriptions[0][2](message)

    assert port.observe_goal(_query())['status'] == 'unknown'


@pytest.mark.parametrize('malformation', ('wrong_type', 'nanosec_range'))
def test_status_goal_info_requires_strict_acceptance_stamp(malformation):
    """Malformed GoalInfo time cannot promote an exact UUID status."""
    message = _status_message(
        (_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED)
    )
    if malformation == 'wrong_type':
        message.status_list[0].goal_info._stamp = object()
    else:
        message.status_list[0].goal_info.stamp._nanosec = 2 ** 32
    port, node, _validator = _port()

    node.subscriptions[0][2](message)

    assert port.observe_goal(_query())['status'] == 'unknown'


def test_empty_snapshot_does_not_erase_terminal_transition_history():
    """Expired visibility cannot permit a terminal UUID to become active."""
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]
    callback(_status_message((_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED)))
    callback(GoalStatusArray())
    callback(_status_message((_GOAL_UUID, GoalStatus.STATUS_EXECUTING)))

    assert port.observe_goal(_query())['status'] == 'unknown'


def test_oversized_status_snapshot_is_bounded_and_invalid():
    """A snapshot beyond the fixed cache bound produces no partial truth."""
    entries = tuple(
        (f'{index + 1:032x}', GoalStatus.STATUS_EXECUTING)
        for index in range(ros_port_module._MAX_STATUS_GOALS + 1)
    )
    port, node, _validator = _port()

    node.subscriptions[0][2](_status_message(*entries))

    assert port.observe_goal(_query())['status'] == 'unknown'


def test_status_history_bound_counts_all_staged_new_uuids(monkeypatch):
    """A bulk snapshot cannot exceed the persistent transition-history cap."""
    monkeypatch.setattr(ros_port_module, '_MAX_TRACKED_OPERATIONS', 1)
    port, node, _validator = _port()

    node.subscriptions[0][2](
        _status_message(
            (_GOAL_UUID, GoalStatus.STATUS_EXECUTING),
            (_OTHER_GOAL_UUID, GoalStatus.STATUS_EXECUTING),
        )
    )

    assert port.observe_goal(_query())['status'] == 'unknown'
    assert port._status_history == {}


def test_status_history_bound_rejects_new_uuid_near_limit(monkeypatch):
    """A later UUID is rejected without evicting existing terminal history."""
    monkeypatch.setattr(ros_port_module, '_MAX_TRACKED_OPERATIONS', 1)
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]
    callback(
        _status_message((_OTHER_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED))
    )

    callback(_status_message((_GOAL_UUID, GoalStatus.STATUS_EXECUTING)))

    assert port.observe_goal(_query())['status'] == 'unknown'
    assert port._status_history == {
        _OTHER_GOAL_UUID: GoalStatus.STATUS_SUCCEEDED
    }


def test_result_is_observable_then_empty_restart_snapshot_is_unknown():
    """A newer server snapshot can supersede local terminal evidence."""
    result_future = _PendingFuture()
    handle = _GoalHandle(result_future=result_future)
    port, node, _validator = _port()
    node.action.send_future = _ImmediateFuture(handle)

    assert port.ensure_started(_start())['status'] == 'accepted'
    result_future.complete(_result_response(GoalStatus.STATUS_SUCCEEDED))
    assert port.observe_goal(_query())['status'] == 'succeeded'

    node.subscriptions[0][2](GoalStatusArray())

    assert port.observe_goal(_query())['status'] == 'unknown'
    assert port.ensure_started(_start())['status'] == 'unknown'
    assert len(node.action.sent) == 1


def test_malformed_result_never_becomes_terminal():
    """A lookalike or bool-status result response is ignored."""
    result_future = _PendingFuture()
    handle = _GoalHandle(result_future=result_future)
    port, node, _validator = _port()
    node.action.send_future = _ImmediateFuture(handle)
    port.ensure_started(_start())

    result_future.complete(
        SimpleNamespace(status=GoalStatus.STATUS_SUCCEEDED)
    )
    assert port.observe_goal(_query())['status'] == 'accepted'

    second_result = _result_response(GoalStatus.STATUS_SUCCEEDED)
    second_result._status = True
    result_future.complete(second_result)
    assert port.observe_goal(_query())['status'] == 'accepted'


def test_result_payload_must_be_exact_navigate_to_pose_result():
    """A forged result payload cannot promote only its terminal status byte."""
    result_future = _PendingFuture()
    port, node, _validator = _port()
    node.action.send_future = _ImmediateFuture(
        _GoalHandle(result_future=result_future)
    )
    assert port.ensure_started(_start())['status'] == 'accepted'
    malformed = _result_response(GoalStatus.STATUS_SUCCEEDED)
    malformed._result = object()

    result_future.complete(malformed)

    assert port.observe_goal(_query())['status'] == 'accepted'


def test_result_payload_cannot_spoof_empty_message_class():
    """A custom __class__ property cannot impersonate std_msgs/Empty."""

    class SpoofedEmpty:
        @property
        def __class__(self):
            return NavigateToPose.Result().result.__class__

    result_future = _PendingFuture()
    port, node, _validator = _port()
    node.action.send_future = _ImmediateFuture(
        _GoalHandle(result_future=result_future)
    )
    assert port.ensure_started(_start())['status'] == 'accepted'
    malformed = _result_response(GoalStatus.STATUS_SUCCEEDED)
    malformed.result._result = SpoofedEmpty()

    result_future.complete(malformed)

    assert port.observe_goal(_query())['status'] == 'accepted'


def test_terminal_result_cannot_repair_invalid_status_snapshot():
    """A prior ambiguous status snapshot keeps later result evidence unknown."""
    result_future = _PendingFuture()
    port, node, _validator = _port()
    node.action.send_future = _ImmediateFuture(
        _GoalHandle(result_future=result_future)
    )
    assert port.ensure_started(_start())['status'] == 'accepted'
    node.subscriptions[0][2](
        _status_message(
            (_GOAL_UUID, GoalStatus.STATUS_EXECUTING),
            (_GOAL_UUID, GoalStatus.STATUS_CANCELED),
        )
    )

    result_future.complete(_result_response(GoalStatus.STATUS_CANCELED))

    assert port.observe_goal(_query())['status'] == 'unknown'


def test_empty_snapshot_does_not_erase_terminal_result_history():
    """A conflicting status cannot replace a terminal result after expiry."""
    result_future = _PendingFuture()
    port, node, _validator = _port()
    node.action.send_future = _ImmediateFuture(
        _GoalHandle(result_future=result_future)
    )
    assert port.ensure_started(_start())['status'] == 'accepted'
    result_future.complete(_result_response(GoalStatus.STATUS_SUCCEEDED))
    node.subscriptions[0][2](GoalStatusArray())
    node.subscriptions[0][2](
        _status_message((_GOAL_UUID, GoalStatus.STATUS_CANCELED))
    )

    assert port.observe_goal(_query())['status'] == 'unknown'


def test_conflicting_terminal_result_invalidates_exact_goal_state():
    """Conflicting terminal channels produce unknown, never last-wins."""
    result_future = _PendingFuture()
    handle = _GoalHandle(result_future=result_future)
    port, node, _validator = _port()
    node.action.send_future = _ImmediateFuture(handle)
    port.ensure_started(_start())
    node.subscriptions[0][2](
        _status_message((_GOAL_UUID, GoalStatus.STATUS_CANCELED))
    )

    result_future.complete(_result_response(GoalStatus.STATUS_SUCCEEDED))

    assert port.observe_goal(_query())['status'] == 'unknown'


def test_tracking_bound_fails_closed_without_evicting_no_resend_record(
    monkeypatch,
):
    """A full idempotency cache refuses new sends instead of evicting."""
    monkeypatch.setattr(ros_port_module, '_MAX_TRACKED_OPERATIONS', 1)
    port, node, validator = _port()

    first = port.ensure_started(_start())
    second = port.ensure_started(_start(goal_uuid=_OTHER_GOAL_UUID))

    assert first['status'] == 'accepted'
    assert second['status'] == 'unknown'
    assert len(node.action.sent) == 1
    assert len(validator.starts) == 2


def test_cancel_uses_direct_exact_uuid_zero_stamp_and_no_resend():
    """ERROR_NONE is active while the exact CancelGoal request is stable."""
    port, node, validator = _port()
    request = _cancel()

    first = Nav2CancelReport(**port.cancel_goal(request))
    second = Nav2CancelReport(**port.cancel_goal(request))

    assert first.status == second.status == 'active'
    assert len(node.cancel.calls) == 1
    assert len(validator.cancels) == 2
    ros_request = node.cancel.calls[0]
    assert type(ros_request) is CancelGoal.Request
    assert bytes(ros_request.goal_info.goal_id.uuid) == bytes.fromhex(
        _GOAL_UUID
    )
    assert ros_request.goal_info.stamp == Time()


def test_cancel_wire_digest_matches_independent_fixed_policy():
    """The cancel contract binds exact service, UUID, and zero-stamp policy."""
    request = _cancel()
    expected = _digest(
        {
            'contract': 'malbut-nav2-cancel-goal-wire-v1',
            'service_fqn': '/navigate_to_pose/_action/cancel_goal',
            'goal_uuid': _GOAL_UUID,
            'goal_info_stamp_policy': 'zero_exact_goal',
            'runtime_mode': 'gazebo',
            'use_sim_time': True,
        }
    )

    assert request.wire_payload_digest == expected
    assert ros_port_module._cancel_wire_digest(request) == expected


def test_cancel_reauthorizes_at_fresh_side_effect_boundary():
    """An authority lease expiring after the first check prevents call_async."""
    validator = _Validator()

    def authorize(request, checked_at):
        if checked_at >= 5.0:
            raise RuntimeError('/private/expired-lease')
        return Nav2CancelAuthorization(
            operation_id=request.operation_id,
            worker_id=request.worker_id,
            cancel_request_id=request.cancel_request_id,
            goal_uuid=request.goal_uuid,
            binding_digest=request.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=_CANCEL_AUTHORITY,
        )

    validator.cancel_hook = authorize
    port, node, validator = _port(
        validator=validator,
        clock=_SequenceClock((4.9, 5.1)),
    )

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'rejected'
    assert [checked_at for _request, checked_at in validator.cancels] == [
        4.9,
        5.1,
    ]
    assert node.cancel.calls == []
    assert '/private/' not in repr(report)


@pytest.mark.parametrize(
    ('return_code', 'expected'),
    (
        (CancelGoal.Response.ERROR_REJECTED, 'rejected'),
        (CancelGoal.Response.ERROR_UNKNOWN_GOAL_ID, 'unknown'),
        (CancelGoal.Response.ERROR_GOAL_TERMINATED, 'unknown'),
    ),
)
def test_cancel_error_codes_are_conservative(return_code, expected):
    """A terminal code alone never claims that cancellation completed."""
    port, node, _validator = _port()
    node.cancel.future = _ImmediateFuture(_cancel_response(return_code))

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == expected
    assert len(node.cancel.calls) == 1


@pytest.mark.parametrize(
    'response',
    (
        _cancel_response(CancelGoal.Response.ERROR_NONE),
        _cancel_response(
            CancelGoal.Response.ERROR_NONE,
            _OTHER_GOAL_UUID,
        ),
        _cancel_response(
            CancelGoal.Response.ERROR_NONE,
            _GOAL_UUID,
            _GOAL_UUID,
        ),
        SimpleNamespace(
            return_code=CancelGoal.Response.ERROR_NONE,
            goals_canceling=[_goal_info(_GOAL_UUID)],
        ),
    ),
)
def test_cancel_error_none_requires_exact_strict_single_uuid(response):
    """A malformed or mismatched ERROR_NONE response remains unknown."""
    port, node, _validator = _port()
    node.cancel.future = _ImmediateFuture(response)

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'unknown'


def test_cancel_bool_return_code_is_unknown():
    """A bool alias cannot impersonate a generated return-code integer."""
    response = _cancel_response(
        CancelGoal.Response.ERROR_NONE,
        _GOAL_UUID,
    )
    response._return_code = False
    port, node, _validator = _port()
    node.cancel.future = _ImmediateFuture(response)

    assert port.cancel_goal(_cancel())['status'] == 'unknown'


@pytest.mark.parametrize('malformation', ('wrong_type', 'nanosec_range'))
def test_cancel_response_goal_info_requires_strict_ros_stamp(malformation):
    """Malformed acceptance-time evidence cannot authorize active status."""
    response = _cancel_response(
        CancelGoal.Response.ERROR_NONE,
        _GOAL_UUID,
    )
    if malformation == 'wrong_type':
        response.goals_canceling[0]._stamp = object()
    else:
        response.goals_canceling[0].stamp._nanosec = 2 ** 32
    port, node, _validator = _port()
    node.cancel.future = _ImmediateFuture(response)

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'unknown'


@pytest.mark.parametrize(
    'ros_status',
    (GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_ABORTED),
)
def test_cancel_does_not_call_known_terminal_goal(ros_status):
    """A known succeeded or aborted exact UUID is never canceled."""
    port, node, validator = _port()
    node.subscriptions[0][2](_status_message((_GOAL_UUID, ros_status)))

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'unknown'
    assert node.cancel.calls == []
    assert validator.cancels == []


def test_cancel_status_canceled_short_circuits_without_service_call():
    """Only exact STATUS_CANCELED proves cancellation completion."""
    port, node, validator = _port()
    node.subscriptions[0][2](
        _status_message((_GOAL_UUID, GoalStatus.STATUS_CANCELED))
    )

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'canceled'
    assert node.cancel.calls == []
    assert validator.cancels == []


def test_cancel_race_uses_canceled_status_not_error_none():
    """A status arriving during the service response is authoritative."""
    port, node, _validator = _port()
    node.cancel.future = _ImmediateFuture(
        _cancel_response(CancelGoal.Response.ERROR_NONE, _GOAL_UUID),
        result_hook=lambda: node.subscriptions[0][2](
            _status_message((_GOAL_UUID, GoalStatus.STATUS_CANCELED))
        ),
    )

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'canceled'


def test_cancel_response_cannot_override_invalid_status_snapshot():
    """A duplicate concurrent status snapshot keeps ERROR_NONE unknown."""
    port, node, _validator = _port()
    callback = node.subscriptions[0][2]
    node.cancel.future = _ImmediateFuture(
        _cancel_response(CancelGoal.Response.ERROR_NONE, _GOAL_UUID),
        result_hook=lambda: callback(
            _status_message(
                (_GOAL_UUID, GoalStatus.STATUS_EXECUTING),
                (_GOAL_UUID, GoalStatus.STATUS_CANCELED),
            )
        ),
    )

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'unknown'
    assert len(node.cancel.calls) == 1


def test_cancel_terminal_race_stays_unknown():
    """ERROR_GOAL_TERMINATED plus succeeded status is not canceled."""
    port, node, _validator = _port()
    node.cancel.future = _ImmediateFuture(
        _cancel_response(CancelGoal.Response.ERROR_GOAL_TERMINATED),
        result_hook=lambda: node.subscriptions[0][2](
            _status_message((_GOAL_UUID, GoalStatus.STATUS_SUCCEEDED))
        ),
    )

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'unknown'
    assert len(node.cancel.calls) == 1


@pytest.mark.parametrize('failure', ('call', 'timeout', 'result'))
def test_cancel_post_call_failure_is_unknown_and_not_resent(failure):
    """An ambiguous cancel attempt is never automatically submitted again."""
    port, node, validator = _port(cancel_timeout_seconds=0.001)
    if failure == 'call':
        node.cancel.call_error = RuntimeError('/private/cancel')
    elif failure == 'timeout':
        node.cancel.future = _PendingFuture()
    else:
        node.cancel.future = _ImmediateFuture(
            error=RuntimeError('/private/cancel-result')
        )

    first = Nav2CancelReport(**port.cancel_goal(_cancel()))
    second = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert first.status == second.status == 'unknown'
    assert len(node.cancel.calls) == 1
    assert len(validator.cancels) == 2
    assert '/private/' not in repr(first)


def test_cancel_status_expiry_becomes_unknown_without_resend():
    """An empty restart snapshot invalidates cached active cancel state."""
    port, node, _validator = _port()
    assert port.cancel_goal(_cancel())['status'] == 'active'
    node.subscriptions[0][2](GoalStatusArray())

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'unknown'
    assert len(node.cancel.calls) == 1


def test_cancel_authority_digest_is_bound_into_ack_evidence():
    """Cancel ACK evidence commits to the exact authority proof digest."""
    first_validator = _Validator()
    second_validator = _Validator()
    second_validator.cancel_authority_digest = 'd' * 64
    first, _node, _validator = _port(validator=first_validator)
    second, _node, _validator = _port(validator=second_validator)

    first_report = first.cancel_goal(_cancel())
    second_report = second.cancel_goal(_cancel())

    assert set(first_report) == set(second_report)
    assert first_report['status'] == second_report['status'] == 'active'
    assert (
        first_report['evidence_digest']
        != second_report['evidence_digest']
    )


def test_cancel_uses_detached_validated_authorization_snapshot():
    """A future callback cannot rewrite already validated cancel authority."""
    validator = _Validator()
    issued = []

    def authorize(request, checked_at):
        result = Nav2CancelAuthorization(
            operation_id=request.operation_id,
            worker_id=request.worker_id,
            cancel_request_id=request.cancel_request_id,
            goal_uuid=request.goal_uuid,
            binding_digest=request.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=_CANCEL_AUTHORITY,
        )
        issued.append(result)
        return result

    validator.cancel_hook = authorize
    port, node, _validator = _port(validator=validator)
    node.cancel.future = _ImmediateFuture(
        _cancel_response(CancelGoal.Response.ERROR_NONE, _GOAL_UUID),
        result_hook=lambda: object.__setattr__(
            issued[-1], 'authority_evidence_digest', 'd' * 64
        ),
    )
    request = _cancel()

    report = port.cancel_goal(request)

    assert report['evidence_digest'] == port._cancel_evidence(
        request.request_fingerprint,
        _GOAL_UUID,
        'active',
        'cancel_accepted',
        _CANCEL_AUTHORITY,
    )


def test_cancel_rejects_validator_request_mutation_without_call():
    """A validator cannot retarget cancellation after canonicalization."""
    validator = _Validator()

    def mutate(request, checked_at):
        object.__setattr__(request, 'goal_uuid', _OTHER_GOAL_UUID)
        return Nav2CancelAuthorization(
            operation_id=request.operation_id,
            worker_id=request.worker_id,
            cancel_request_id=request.cancel_request_id,
            goal_uuid=request.goal_uuid,
            binding_digest=request.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=_CANCEL_AUTHORITY,
        )

    validator.cancel_hook = mutate
    port, node, _validator = _port(validator=validator)

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'rejected'
    assert node.cancel.calls == []


def test_cancel_rechecks_sim_time_after_authorization():
    """A runtime mode change before call prevents exact-goal cancellation."""
    validator = _Validator()
    port, node, _validator = _port(validator=validator)

    def change_runtime(request, checked_at):
        node.use_sim_time = False
        return Nav2CancelAuthorization(
            operation_id=request.operation_id,
            worker_id=request.worker_id,
            cancel_request_id=request.cancel_request_id,
            goal_uuid=request.goal_uuid,
            binding_digest=request.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            wire_payload_digest=request.wire_payload_digest,
            checked_at=checked_at,
            authority_evidence_digest=_CANCEL_AUTHORITY,
        )

    validator.cancel_hook = change_runtime

    report = Nav2CancelReport(**port.cancel_goal(_cancel()))

    assert report.status == 'rejected'
    assert node.cancel.calls == []


def test_close_is_idempotent_transport_teardown_without_canceling():
    """Closing an active transport sends nothing and is not stop evidence."""
    port, node, _validator = _port()
    port.ensure_started(_start())
    sends_before = len(node.action.sent)
    cancels_before = len(node.cancel.calls)

    port.close()
    port.close()

    assert len(node.action.sent) == sends_before
    assert len(node.cancel.calls) == cancels_before
    assert node.action.destroy_calls == 1
    assert node.destroyed_clients == [node.cancel]
    assert node.destroyed_subscriptions == [node.subscription_result]
    with pytest.raises(GazeboMonitorRoomNav2RosPortError) as raised:
        port.observe_goal(_query())
    assert raised.value.code == 'nav2_ros_port_closed'
