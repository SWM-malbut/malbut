"""Contract tests for server-owned text decision routing."""

import pytest

from malbut_agent_server.gateway import (
    PROPOSAL_ONLY,
    READ_ONLY,
    production_registry,
    simulation_registry,
)
from malbut_agent_server.schemas import AgentDecision
from malbut_agent_server.text_decision_policy import (
    TextDecisionPolicy,
    TextDecisionRoute,
)


def _policy(*confirmable_tool_names: str) -> TextDecisionPolicy:
    return TextDecisionPolicy(
        production_registry(),
        confirmable_tool_names=confirmable_tool_names,
    )


@pytest.mark.parametrize(
    'decision_type',
    ['message', 'refusal'],
)
def test_non_tool_decisions_are_direct_replies(
    decision_type: str,
) -> None:
    """Speaking never gains Tool or execution authority."""
    result = _policy('navigate').classify(
        AgentDecision(
            type=decision_type,
            message='응답',
        ),
        available_tools=('navigate',),
    )

    assert result.route is TextDecisionRoute.DIRECT_REPLY
    assert result.code == 'direct_reply'
    assert result.tool_name is None
    assert result.capability_mode is None
    assert result.risk_level is None
    assert result.confirmable is False


def test_clarification_is_a_distinct_non_authorizing_route() -> None:
    """A question is speaking, but it expects one bounded follow-up turn."""
    result = _policy('navigate').classify(
        AgentDecision(
            type='clarification',
            message='어느 공간으로 이동할까요?',
        ),
        available_tools=('navigate',),
    )

    assert result.route is TextDecisionRoute.CLARIFICATION_REQUIRED
    assert result.code == 'clarification_required'
    assert result.tool_name is None
    assert result.capability_mode is None
    assert result.risk_level is None
    assert result.confirmable is False


def test_registered_status_query_is_read_only() -> None:
    """The server registry, not the model, assigns read-only authority."""
    result = _policy('navigate').classify(
        AgentDecision(
            type='tool_call',
            message='로봇 상태를 확인하겠습니다.',
            tool_name='get_robot_status',
            arguments={},
        ),
        available_tools=('get_robot_status', 'navigate'),
    )

    assert result.route is TextDecisionRoute.READ_ONLY_QUERY
    assert result.code == 'read_only_query'
    assert result.tool_name == 'get_robot_status'
    assert result.capability_mode == READ_ONLY
    assert result.risk_level == 'L0'
    assert result.confirmable is False


def test_navigation_is_only_a_confirmable_proposal() -> None:
    """A valid navigation proposal still carries no execution authority."""
    result = _policy('navigate').classify(
        AgentDecision(
            type='tool_call',
            message='거실로 이동할까요?',
            tool_name='navigate',
            arguments={'location': '거실'},
        ),
        available_tools=('navigate',),
    )

    assert (
        result.route
        is TextDecisionRoute.CONFIRMABLE_ACTION_PROPOSAL
    )
    assert result.code == 'confirmation_required'
    assert result.tool_name == 'navigate'
    assert result.capability_mode == PROPOSAL_ONLY
    assert result.risk_level == 'L3'
    assert result.confirmable is True
    assert not hasattr(result, 'execution_authorized')
    assert not hasattr(result, 'physical_authorized')


@pytest.mark.parametrize(
    ('decision', 'available_tools', 'expected_code'),
    [
        (
            AgentDecision(
                type='tool_call',
                message='실행합니다.',
                tool_name='execute_shell',
                arguments={},
            ),
            ('execute_shell',),
            'unknown_tool',
        ),
        (
            AgentDecision(
                type='tool_call',
                message='상태를 확인합니다.',
                tool_name='get_robot_status',
                arguments={},
            ),
            ('navigate',),
            'tool_unavailable',
        ),
        (
            AgentDecision(
                type='tool_call',
                message='거실로 이동합니다.',
                tool_name='navigate',
                arguments={'location': '거실', 'x': 1},
            ),
            ('navigate',),
            'invalid_arguments',
        ),
        (
            AgentDecision(
                type='tool_call',
                message='알림을 보냅니다.',
                tool_name='send_notification',
                arguments={'message': '확인', 'image_id': None},
            ),
            ('send_notification',),
            'tool_not_routable',
        ),
    ],
)
def test_unknown_unavailable_malformed_and_unroutable_are_rejected(
    decision: AgentDecision,
    available_tools: tuple[str, ...],
    expected_code: str,
) -> None:
    """Every authority mismatch fails closed before any adapter exists."""
    result = _policy('navigate').classify(
        decision,
        available_tools=available_tools,
    )

    assert result.route is TextDecisionRoute.REJECTED
    assert result.code == expected_code
    assert result.confirmable is False


def test_invalid_decision_and_invalid_allowlist_fail_closed() -> None:
    """Malformed model and caller values cannot widen the Tool set."""
    policy = _policy('navigate')
    malformed = AgentDecision(
        type='tool_call',
        message='이동합니다.',
        tool_name='navigate',
        arguments=[],
    )
    invalid_decision = policy.classify(
        malformed,
        available_tools=('navigate',),
    )
    invalid_allowlist = policy.classify(
        AgentDecision(
            type='tool_call',
            message='이동합니다.',
            tool_name='navigate',
            arguments={'location': '거실'},
        ),
        available_tools='navigate',
    )

    assert invalid_decision.route is TextDecisionRoute.REJECTED
    assert invalid_decision.code == 'decision_invalid'
    assert invalid_allowlist.route is TextDecisionRoute.REJECTED
    assert invalid_allowlist.code == 'tool_unavailable'


def test_runtime_mode_cannot_reclassify_simulation_as_confirmation() -> None:
    """A simulation-only binding is not a physical action proposal."""
    policy = TextDecisionPolicy(
        simulation_registry(),
        confirmable_tool_names=('navigate',),
    )
    result = policy.classify(
        AgentDecision(
            type='tool_call',
            message='거실로 이동합니다.',
            tool_name='navigate',
            arguments={'location': '거실'},
        ),
        available_tools=('navigate',),
    )

    assert result.route is TextDecisionRoute.REJECTED
    assert result.code == 'tool_not_routable'
    assert result.confirmable is False
