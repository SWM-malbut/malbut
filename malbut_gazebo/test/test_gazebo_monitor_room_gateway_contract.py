"""Tests for the coordinate-free Gazebo gateway wire contract."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from malbut_gazebo.gazebo_monitor_room_gateway_contract import (
    GATEWAY_MAX_REQUEST_BYTES,
    GazeboMonitorRoomGatewayContractError,
    GazeboMonitorRoomGatewayRequest,
    GazeboMonitorRoomGatewayResponse,
)
from malbut_gazebo.gazebo_monitor_room_store import OperationObservation


_EVIDENCE = hashlib.sha256(b'evidence').hexdigest()


def _request(command='drive'):
    return GazeboMonitorRoomGatewayRequest(
        request_id='request-1',
        operation_id='operation-1',
        command=command,
    )


def _response(**changes):
    values = {
        'request_id': 'request-1',
        'operation_id': 'operation-1',
        'command': 'drive',
        'state': 'navigating',
        'current_sample_index': 0,
        'navigation_samples_total': 2,
        'navigation_samples_reached': 0,
        'terminal': False,
        'robot_blocked': True,
        'terminal_code': None,
        'evidence_digest': _EVIDENCE,
    }
    values.update(changes)
    return GazeboMonitorRoomGatewayResponse(**values)


def _observation(**changes):
    values = {
        'operation_id': 'operation-1',
        'robot_id': 'robot-1',
        'state': 'navigating',
        'current_sample_index': 0,
        'current_sample_state': 'navigating',
        'current_goal_uuid': '0' * 32,
        'navigation_samples_total': 2,
        'navigation_samples_reached': 0,
        'fence_epoch': 1,
        'lease_owner': 'worker-1',
        'lease_expires_at': 10.0,
        'deadline': 100.0,
        'terminal_code': None,
        'cancel_request_id': None,
        'created_at': 1.0,
        'updated_at': 2.0,
        'replayed': False,
    }
    values.update(changes)
    return OperationObservation(**values)


def test_request_round_trip_is_exact_minimal_and_deterministic():
    """Only an opaque operation and one command cross the wire."""
    request = _request()
    encoded = request.to_wire_bytes()

    assert encoded == (
        b'{"command":"drive","operation_id":"operation-1",'
        b'"request_id":"request-1","schema_version":1}'
    )
    assert GazeboMonitorRoomGatewayRequest.from_wire_bytes(
        encoded
    ) == request
    assert request.request_fingerprint == hashlib.sha256(encoded).hexdigest()
    assert request.to_dict() == {
        'schema_version': 1,
        'request_id': 'request-1',
        'operation_id': 'operation-1',
        'command': 'drive',
    }
    assert 'request-1' not in repr(request)
    assert 'operation-1' not in repr(request)


@pytest.mark.parametrize('command', ('drive', 'observe', 'cancel'))
def test_only_closed_commands_are_supported(command):
    """Every supported command has a distinct canonical fingerprint."""
    request = _request(command)
    assert request.command == command
    assert len(request.request_fingerprint) == 64


def test_cancel_identity_is_stable_and_server_derived():
    """The caller cannot provide a separate cancellation selector."""
    first = _request('cancel')
    replay = _request('cancel')
    changed = GazeboMonitorRoomGatewayRequest(
        request_id='request-2',
        operation_id='operation-1',
        command='cancel',
    )

    assert first.cancel_request_id == replay.cancel_request_id
    assert first.cancel_request_id.startswith('gateway-cancel-')
    assert first.cancel_request_id != changed.cancel_request_id
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        _request('drive').cancel_request_id


@pytest.mark.parametrize(
    'extra',
    (
        {'x': 1.0},
        {'goal_uuid': '0' * 32},
        {'fence_epoch': 1},
        {'lease_seconds': 30},
        {'map_id': 'map-private'},
        {'worker_id': 'worker-private'},
    ),
)
def test_request_rejects_caller_selected_execution_values(extra):
    """Coordinates and authority selectors cannot enter the request."""
    value = _request().to_dict()
    value.update(extra)
    payload = json.dumps(value).encode('utf-8')

    with pytest.raises(GazeboMonitorRoomGatewayContractError) as error:
        GazeboMonitorRoomGatewayRequest.from_wire_bytes(payload)

    assert error.value.code == 'gateway_request_invalid'
    assert not any(str(item) in str(error.value) for item in extra.values())


@pytest.mark.parametrize(
    'payload',
    (
        b'',
        b'[]',
        b'null',
        b'not-json',
        b'{"schema_version":1,"schema_version":1,'
        b'"request_id":"r","operation_id":"o","command":"drive"}',
        b'{"schema_version":true,"request_id":"r",'
        b'"operation_id":"o","command":"drive"}',
        b'{"schema_version":1,"request_id":"r",'
        b'"operation_id":"o","command":"start"}',
    ),
)
def test_request_parser_rejects_noncanonical_shapes(payload):
    """Malformed, duplicate, implicit, and unsupported values fail closed."""
    with pytest.raises(GazeboMonitorRoomGatewayContractError) as error:
        GazeboMonitorRoomGatewayRequest.from_wire_bytes(payload)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_request_parser_is_bounded_and_requires_exact_bytes():
    """Mutable buffers and oversized values are never parsed."""
    valid = _request().to_wire_bytes()
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        GazeboMonitorRoomGatewayRequest.from_wire_bytes(bytearray(valid))
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        GazeboMonitorRoomGatewayRequest.from_wire_bytes(
            b'{' + (b' ' * GATEWAY_MAX_REQUEST_BYTES) + b'}'
        )


class _String(str):
    """String subtype used to verify exact built-in type checks."""


@pytest.mark.parametrize(
    'changes',
    (
        {'schema_version': True},
        {'request_id': _String('request-1')},
        {'request_id': ' request-1'},
        {'operation_id': 'operation\nprivate'},
        {'command': _String('drive')},
        {'command': 'start'},
    ),
)
def test_direct_request_construction_rejects_weak_values(changes):
    """Direct Python construction obeys the same exact contract."""
    values = _request().to_dict()
    values.update(changes)
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        GazeboMonitorRoomGatewayRequest(**values)


def test_request_mutation_is_detected_before_wire_use():
    """Frozen-field bypasses cannot change an idempotent request."""
    request = _request()
    with pytest.raises(FrozenInstanceError):
        request.command = 'cancel'
    object.__setattr__(request, 'command', 'cancel')
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        _ = request.request_fingerprint
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        request.to_wire_bytes()


def test_response_is_coordinate_free_and_denies_stronger_claims():
    """A gateway observation never claims physical or room coverage."""
    response = _response()
    value = response.to_dict()

    assert response.to_wire_bytes() == json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode('ascii')
    assert value['runtime_mode'] == 'gazebo'
    assert value['simulation'] is True
    for name in (
        'physical_authorized',
        'physical_effects',
        'viewer_live',
        'camera_coverage_validated',
        'coverage_achieved',
    ):
        assert value[name] is False
    serialized = response.to_wire_bytes()
    for private in (
        b'x_m', b'y_m', b'goal_uuid', b'fence_epoch', b'lease', b'map_id'
    ):
        assert private not in serialized
    assert 'request-1' not in repr(response)
    assert 'operation-1' not in repr(response)
    assert _EVIDENCE not in repr(response)


def test_response_wire_round_trip_is_exact_and_deterministic():
    """A client can validate a cached response without widening its shape."""
    response = _response()
    encoded = response.to_wire_bytes()

    parsed = GazeboMonitorRoomGatewayResponse.from_wire_bytes(encoded)

    assert parsed == response
    assert parsed.response_fingerprint == response.response_fingerprint


@pytest.mark.parametrize(
    'mutation',
    (
        lambda value: value.pop('robot_blocked'),
        lambda value: value.update({'x_m': 1.0}),
        lambda value: value.update({'simulation': False}),
        lambda value: value.update({'physical_effects': True}),
        lambda value: value.update({'runtime_mode': 'physical'}),
    ),
)
def test_response_parser_rejects_changed_or_stronger_wire_shapes(mutation):
    """Stored or remote JSON cannot acquire authority by field mutation."""
    value = _response().to_dict()
    mutation(value)
    payload = json.dumps(value).encode('utf-8')

    with pytest.raises(GazeboMonitorRoomGatewayContractError) as error:
        GazeboMonitorRoomGatewayResponse.from_wire_bytes(payload)

    assert error.value.code == 'gateway_response_invalid'


def test_response_parser_rejects_duplicate_keys_and_mutable_bytes():
    """Response replay validation shares strict duplicate/type bounds."""
    encoded = _response().to_wire_bytes()
    duplicate = encoded[:-1] + b',"state":"navigating"}'

    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        GazeboMonitorRoomGatewayResponse.from_wire_bytes(duplicate)
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        GazeboMonitorRoomGatewayResponse.from_wire_bytes(bytearray(encoded))


@pytest.mark.parametrize(
    'changes',
    (
        {'schema_version': True},
        {'command': _String('drive')},
        {'state': _String('navigating')},
        {'state': 'made_up_state'},
        {'current_sample_index': True},
        {'current_sample_index': 2},
        {'navigation_samples_total': 0},
        {'navigation_samples_reached': 3},
        {'terminal': 1},
        {'terminal': True},
        {'robot_blocked': 1},
        {'robot_blocked': False},
        {'terminal_code': 'done'},
        {'terminal_code': _String('done')},
        {'evidence_digest': _String(_EVIDENCE)},
        {'evidence_digest': '0' * 63},
    ),
)
def test_response_rejects_implicit_or_inconsistent_values(changes):
    """Public observations keep exact numeric, state, and digest types."""
    with pytest.raises(GazeboMonitorRoomGatewayContractError) as error:
        _response(**changes)
    assert error.value.code == 'gateway_response_invalid'


def test_response_mutation_is_detected_before_serialization():
    """A cached response digest never blesses changed progress."""
    response = _response()
    object.__setattr__(response, 'navigation_samples_reached', 1)

    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        _ = response.response_fingerprint
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        response.to_wire_bytes()


def test_response_fingerprint_changes_with_progress_and_request():
    """Idempotent consumers can distinguish every material result."""
    base = _response()
    changed_progress = _response(
        current_sample_index=1,
        navigation_samples_reached=1,
    )
    changed_request = _response(request_id='request-2')

    assert len(base.response_fingerprint) == 64
    assert base.response_fingerprint != changed_progress.response_fingerprint
    assert base.response_fingerprint != changed_request.response_fingerprint
    assert replace(base) == base


@pytest.mark.parametrize(
    ('state', 'terminal', 'blocked', 'code', 'index', 'reached'),
    (
        ('delivery_unknown', True, True, 'nav2_unknown', 0, 0),
        ('cancel_unknown', True, True, 'nav2_cancel_unknown', 0, 0),
        ('failed', True, False, 'preflight_rejected', 0, 0),
        ('canceled', True, False, 'nav2_goal_canceled', 0, 0),
        ('succeeded', True, False, 'navigation_complete', 1, 2),
    ),
)
def test_response_terminal_and_blocking_semantics_are_exact(
    state, terminal, blocked, code, index, reached
):
    """Unknown delivery remains blocked while resolved terminals do not."""
    response = _response(
        state=state,
        current_sample_index=index,
        navigation_samples_reached=reached,
        terminal=terminal,
        robot_blocked=blocked,
        terminal_code=code,
    )

    assert response.terminal is terminal
    assert response.robot_blocked is blocked


def test_response_is_derived_from_an_exact_store_observation():
    """The gateway projection copies no caller-selected result values."""
    response = GazeboMonitorRoomGatewayResponse.from_observation(
        _request('observe'),
        _observation(),
    )

    assert response.request_id == 'request-1'
    assert response.operation_id == 'operation-1'
    assert response.command == 'observe'
    assert response.state == 'navigating'
    assert response.current_sample_index == 0
    assert response.navigation_samples_total == 2
    assert response.navigation_samples_reached == 0
    assert response.terminal is False
    assert response.robot_blocked is True
    assert len(response.evidence_digest) == 64


def test_observation_projection_binds_private_state_without_exposing_it():
    """Goal, fence, lease, and robot changes alter only the proof digest."""
    request = _request('observe')
    base = GazeboMonitorRoomGatewayResponse.from_observation(
        request,
        _observation(),
    )
    changed = GazeboMonitorRoomGatewayResponse.from_observation(
        request,
        _observation(current_goal_uuid='1' * 32),
    )

    assert base.evidence_digest != changed.evidence_digest
    for marker in ('robot-1', 'worker-1', '00000000'):
        assert marker not in base.to_wire_bytes().decode('ascii')


def test_observation_projection_rejects_mismatch_and_mutation():
    """A response cannot be projected from another or changed operation."""
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        GazeboMonitorRoomGatewayResponse.from_observation(
            _request(),
            _observation(operation_id='operation-2'),
        )

    observation = _observation()
    object.__setattr__(observation, 'current_sample_index', True)
    with pytest.raises(GazeboMonitorRoomGatewayContractError) as error:
        GazeboMonitorRoomGatewayResponse.from_observation(
            _request(), observation
        )
    assert error.value.code == 'gateway_response_invalid'
    assert error.value.__cause__ is None
    assert error.value.__context__ is None

    observation = _observation()
    object.__setattr__(observation, 'deadline', 10 ** 400)
    with pytest.raises(GazeboMonitorRoomGatewayContractError) as error:
        GazeboMonitorRoomGatewayResponse.from_observation(
            _request(), observation
        )
    assert error.value.__cause__ is None
    assert error.value.__context__ is None

    request = _request()
    object.__setattr__(request, 'operation_id', 'operation-2')
    with pytest.raises(GazeboMonitorRoomGatewayContractError):
        GazeboMonitorRoomGatewayResponse.from_observation(
            request, _observation()
        )
