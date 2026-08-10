"""Tests for the SWM25-73 server-owned Tool Gateway boundary."""

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from malbut_agent_server.gateway import (
    PRODUCTION,
    PROPOSAL_ONLY,
    READ_ONLY,
    SIMULATION,
    SIMULATION_ONLY,
    CapabilityRegistry,
    GatewayConflictError,
    MockToolAdapter,
    ReadOnlyToolAdapter,
    SimulationToolAdapter,
    ToolCapability,
    ToolGateway,
    ToolQuery,
    production_registry,
    simulation_registry,
)


def _query(
    tool_name: str = 'get_robot_status',
    arguments: Dict[str, Any] | None = None,
    *,
    request_id: str = 'gateway-request-1',
) -> ToolQuery:
    return ToolQuery.from_dict(
        {
            'request_id': request_id,
            'user_id': 'test-user',
            'tool_name': tool_name,
            'arguments': arguments or {},
        }
    )


class CountingStatusAdapter(ReadOnlyToolAdapter):
    """Return a fresh status and record dispatch count."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert arguments == {}
        self.calls += 1
        return {
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'source': 'trusted-test-state',
            'battery_percent': 75,
            'emergency_stop': False,
        }


def test_registry_is_authoritative_and_rejects_unsafe_bindings() -> None:
    """Client names cannot widen policy or reclassify side effects."""

    class UntrustedSideEffectAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
            del arguments
            self.calls += 1
            return {'simulated': True}

    registry = CapabilityRegistry(
        [ToolCapability('get_robot_status')]
    )
    assert registry.effective_names(
        [
            'navigate',
            'unlock_door',
            'get_robot_status',
            'get_robot_status',
        ]
    ) == ['get_robot_status']
    assert [
        item.name
        for item in registry.select_specs(
            ['navigate', 'get_robot_status']
        )
    ] == ['get_robot_status']

    with pytest.raises(ValueError, match='unknown Tool'):
        ToolCapability('unlock_door')
    with pytest.raises(ValueError, match='read-only'):
        ToolCapability('navigate', mode=READ_ONLY)
    with pytest.raises(ValueError, match='cannot bind'):
        ToolCapability(
            'navigate',
            mode=PROPOSAL_ONLY,
            adapter=MockToolAdapter('navigate'),
        )
    side_effect_spy = UntrustedSideEffectAdapter()
    with pytest.raises(ValueError, match='SimulationToolAdapter'):
        ToolCapability(
            'navigate',
            mode=SIMULATION_ONLY,
            adapter=side_effect_spy,
        )
    assert side_effect_spy.calls == 0
    with pytest.raises(ValueError, match='duplicate'):
        CapabilityRegistry(
            [
                ToolCapability('navigate'),
                ToolCapability('navigate'),
            ]
        )


def test_capability_snapshot_never_exposes_adapter_objects() -> None:
    """Discovery reports mode and block reason, never implementation data."""
    snapshot = production_registry().to_dict()
    assert snapshot['source'] == 'server_owned_registry'
    navigate = next(
        item
        for item in snapshot['capabilities']
        if item['name'] == 'navigate'
    )
    assert navigate['executable'] is False
    assert navigate['blocked_by'] == 'confirmation_required'
    status = next(
        item
        for item in snapshot['capabilities']
        if item['name'] == 'get_robot_status'
    )
    assert status['mode'] == 'read_only'
    assert status['executable'] is False
    assert status['blocked_by'] == 'executor_unavailable'
    assert status['timeout_ms'] == 1000
    detect = next(
        item
        for item in snapshot['capabilities']
        if item['name'] == 'detect_pet'
    )
    assert detect['timeout_ms'] == 3000
    assert 'adapter' not in str(snapshot)


def test_read_only_query_is_validated_and_idempotent() -> None:
    """A duplicate safe query invokes its trusted adapter exactly once."""
    adapter = CountingStatusAdapter()
    gateway = ToolGateway(
        CapabilityRegistry(
            [
                ToolCapability(
                    'get_robot_status',
                    mode=READ_ONLY,
                    adapter=adapter,
                )
            ]
        )
    )
    try:
        query = _query()
        first, first_cached = gateway.query_with_cache_state(query)
        second, second_cached = gateway.query_with_cache_state(query)
        assert first.status == 'succeeded'
        assert first.result_id == second.result_id
        assert first_cached is False
        assert second_cached is True
        assert adapter.calls == 1
        assert 'tool_call_id' not in first.to_dict()

        with pytest.raises(GatewayConflictError):
            gateway.query(
                ToolQuery.from_dict(
                    {
                        'request_id': query.request_id,
                        'user_id': 'another-user',
                        'tool_name': 'get_robot_status',
                        'arguments': {},
                    }
                )
            )
        assert adapter.calls == 1
    finally:
        gateway.close()


def test_concurrent_duplicate_query_calls_adapter_once() -> None:
    """The process-local lock prevents a concurrent duplicate dispatch."""

    class SlowAdapter(CountingStatusAdapter):
        def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
            time.sleep(0.02)
            return super().invoke(arguments)

    adapter = SlowAdapter()
    gateway = ToolGateway(
        CapabilityRegistry(
            [
                ToolCapability(
                    'get_robot_status',
                    mode=READ_ONLY,
                    adapter=adapter,
                )
            ]
        )
    )
    results = []
    try:
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    gateway.query(_query())
                )
            )
            for _index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)
        assert len(results) == 2
        assert results[0].result_id == results[1].result_id
        assert adapter.calls == 1
    finally:
        gateway.close()


def test_unrelated_queries_do_not_share_one_execution_lock() -> None:
    """Different IDs can occupy separate bounded adapter workers."""

    class ParallelStatusAdapter(ReadOnlyToolAdapter):
        def __init__(self) -> None:
            self.barrier = threading.Barrier(2)

        def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
            assert arguments == {}
            self.barrier.wait(timeout=0.5)
            return {
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'source': 'parallel-test-state',
                'battery_percent': 75,
                'emergency_stop': False,
            }

    gateway = ToolGateway(
        CapabilityRegistry(
            [
                ToolCapability(
                    'get_robot_status',
                    mode=READ_ONLY,
                    adapter=ParallelStatusAdapter(),
                    timeout_seconds=1,
                )
            ]
        ),
        max_workers=2,
    )
    results = []
    try:
        threads = [
            threading.Thread(
                target=lambda request_id=request_id: results.append(
                    gateway.query(_query(request_id=request_id))
                )
            )
            for request_id in ('parallel-1', 'parallel-2')
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert len(results) == 2
        assert all(result.status == 'succeeded' for result in results)
    finally:
        gateway.close()


def test_side_effects_and_unknown_tools_fail_closed() -> None:
    """SWM25-73 never issues a real action or tool_call_id."""
    gateway = ToolGateway(production_registry())
    try:
        navigate = gateway.query(
            _query('navigate', {'location': '거실'})
        ).to_dict()
        assert navigate['status'] == 'rejected'
        assert navigate['error']['code'] == 'confirmation_required'
        assert 'tool_call_id' not in navigate

        unknown = gateway.query(
            _query('cmd_vel', {'linear': 1}, request_id='unknown-1')
        ).to_dict()
        assert unknown['error']['code'] == 'unknown_tool'
    finally:
        gateway.close()


def test_invalid_arguments_never_reach_adapter() -> None:
    """Strict registered schemas reject missing and additional fields."""
    adapter = MockToolAdapter('navigate')
    gateway = ToolGateway(
        CapabilityRegistry(
            [
                ToolCapability(
                    'navigate',
                    mode=SIMULATION_ONLY,
                    adapter=adapter,
                )
            ],
            runtime_mode=SIMULATION,
        )
    )
    try:
        result = gateway.query(
            _query(
                'navigate',
                {'location': '거실', 'speed': 10},
            )
        )
        assert result.error['code'] == 'invalid_arguments'
        assert adapter.calls == 0
    finally:
        gateway.close()


def test_simulation_is_explicit_and_has_no_real_sink() -> None:
    """Simulation results identify themselves and publish no Nav2 goal."""
    production_adapter = MockToolAdapter('navigate')
    production = ToolGateway(
        CapabilityRegistry(
            [
                ToolCapability(
                    'navigate',
                    mode=SIMULATION_ONLY,
                    adapter=production_adapter,
                )
            ],
            runtime_mode=PRODUCTION,
        )
    )
    simulation = ToolGateway(simulation_registry())
    try:
        blocked = production.query(
            _query('navigate', {'location': '거실'})
        )
        assert blocked.error['code'] == 'confirmation_required'
        assert production_adapter.calls == 0

        result = simulation.query(
            _query(
                'navigate',
                {'location': '거실'},
                request_id='simulation-navigation',
            )
        )
        assert result.status == 'succeeded'
        assert result.result['simulated'] is True
        assert result.result['nav2_goal_published'] is False
    finally:
        production.close()
        simulation.close()


def test_simulation_adapter_must_identify_its_result() -> None:
    """A result without simulated=true cannot become a success."""

    class MislabelledAdapter(SimulationToolAdapter):
        def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
            return {'destination': arguments['location']}

    gateway = ToolGateway(
        CapabilityRegistry(
            [
                ToolCapability(
                    'navigate',
                    mode=SIMULATION_ONLY,
                    adapter=MislabelledAdapter(),
                )
            ],
            runtime_mode=SIMULATION,
        )
    )
    try:
        result = gateway.query(
            _query('navigate', {'location': '거실'})
        )
        assert result.status == 'failed'
        assert result.error['code'] == 'adapter_failed'
    finally:
        gateway.close()


def test_pet_detection_requires_privacy_evidence() -> None:
    """A camera result without local privacy evidence fails closed."""

    class UncheckedCameraAdapter(SimulationToolAdapter):
        def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
            assert arguments == {}
            return {'simulated': True, 'detected': True}

    gateway = ToolGateway(
        CapabilityRegistry(
            [
                ToolCapability(
                    'detect_pet',
                    mode=SIMULATION_ONLY,
                    adapter=UncheckedCameraAdapter(),
                )
            ],
            runtime_mode=SIMULATION,
        )
    )
    try:
        result = gateway.query(_query('detect_pet'))
        assert result.status == 'failed'
        assert result.error['code'] == 'adapter_failed'
    finally:
        gateway.close()


def test_timeout_stale_state_and_bad_results_are_normalized() -> None:
    """Adapter faults cannot leak tracebacks or become false successes."""

    class SlowAdapter(ReadOnlyToolAdapter):
        def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
            del arguments
            time.sleep(0.03)
            return {}

    class StaleAdapter(ReadOnlyToolAdapter):
        def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
            del arguments
            return {
                'observed_at': (
                    datetime.now(timezone.utc) - timedelta(seconds=10)
                ).isoformat(),
                'source': 'stale-test-state',
                'battery_percent': 75,
                'emergency_stop': False,
            }

    class ExplodingAdapter(ReadOnlyToolAdapter):
        def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
            del arguments
            raise RuntimeError('secret-token-and-traceback')

    class BadTypedStatusAdapter(ReadOnlyToolAdapter):
        def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
            del arguments
            return {
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'source': 'bad-typed-test-state',
                'battery_percent': 'secret',
                'emergency_stop': 'false',
            }

    scenarios = [
        (SlowAdapter(), 0.005, 'timed_out'),
        (StaleAdapter(), 1.0, 'stale_state'),
        (ExplodingAdapter(), 1.0, 'adapter_failed'),
        (BadTypedStatusAdapter(), 1.0, 'adapter_failed'),
    ]
    for index, (adapter, timeout, code) in enumerate(scenarios):
        gateway = ToolGateway(
            CapabilityRegistry(
                [
                    ToolCapability(
                        'get_robot_status',
                        mode=READ_ONLY,
                        adapter=adapter,
                        timeout_seconds=timeout,
                    )
                ]
            )
        )
        try:
            result = gateway.query(
                _query(request_id=f'failure-{index}')
            ).to_dict()
            assert result['status'] != 'succeeded'
            assert result['error']['code'] == code
            assert 'secret-token' not in str(result)
        finally:
            gateway.close()
