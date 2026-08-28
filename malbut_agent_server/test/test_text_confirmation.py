"""Unit tests for non-authorizing deterministic text confirmation."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from malbut_agent_server.named_target import BoundNamedTarget
from malbut_agent_server.schemas import AgentDecision, ValidationError
from malbut_agent_server.text_confirmation import (
    ConfirmationDomainConflictError,
    ConfirmationDraft,
    ConfirmationRecord,
    ConfirmationResolution,
    classify_confirmation_text,
)


def _draft() -> ConfirmationDraft:
    decision = AgentDecision(
        type='tool_call',
        message='거실로 이동할게.',
        tool_name='navigate',
        arguments={'location': '거실'},
        confidence=1.0,
        expires_in_ms=5000,
    )
    result = SimpleNamespace(
        request_id='request-1',
        conversation_id='conversation-1',
        turn_id='turn-1',
        conversation_generation=1,
        conversation_revision=2,
        conversation_ordinal=1,
        decision=decision,
        safety=SimpleNamespace(allowed=True),
        state_trusted=True,
        decision_id='decision-1',
        issued_at=100.0,
        expires_at=105.0,
        state_evidence_id='state-evidence-1',
        state_observed_at=99.5,
        safety_policy_revision='malbut-safety-v1',
    )
    token = SimpleNamespace(
        user_id='user-1',
        conversation_id='conversation-1',
        session_instance_id='session-instance-1',
        generation=1,
        revision=1,
        ordinal=1,
        turn_id='turn-1',
        request_id='request-1',
    )
    target = BoundNamedTarget(
        room_name='거실',
        room_category='living_room',
        binding_digest='a' * 64,
    )
    return ConfirmationDraft.from_orchestration(
        result,
        token,
        target,
        confirmation_request_id='confirmation-1',
    )


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('  네! ', 'approve'),
        ('YES.', 'approve'),
        ('아니요', 'deny'),
        ('취소해', 'cancel'),
        ('네 근데 잠시만', None),
        ('아마도', None),
        ('', None),
    ],
)
def test_classifier_uses_only_exact_bounded_phrases(
    text: str,
    expected: str | None,
) -> None:
    assert classify_confirmation_text(text) == expected


def test_draft_round_trip_hides_private_target_and_has_no_authority() -> None:
    draft = _draft()
    restored = ConfirmationDraft.from_private_dict(
        draft.to_private_dict()
    )

    assert restored == draft
    public = restored.to_public_dict()
    assert public['proposal']['arguments'] == {'location': '거실'}
    assert public['proposal']['target'] == {
        'room_name': '거실',
        'room_category': 'living_room',
        'execution_authorized': False,
    }
    assert 'binding_digest' not in str(public)
    assert 'state-evidence-1' not in str(public)
    assert 'malbut-safety-v1' not in str(public)
    assert public['execution']['authorized'] is False
    assert public['execution']['tool_call_id'] is None
    assert public['execution']['nav2_start_count'] == 0


def test_private_tampering_is_rejected() -> None:
    value = deepcopy(_draft().to_private_dict())
    value['arguments']['location'] = '주방'
    with pytest.raises(ValidationError, match='digest mismatch'):
        ConfirmationDraft.from_private_dict(value)

    value = deepcopy(_draft().to_private_dict())
    value['state_evidence_id'] = 'forged-state-evidence'
    with pytest.raises(ValidationError, match='digest mismatch'):
        ConfirmationDraft.from_private_dict(value)

    value = deepcopy(_draft().to_private_dict())
    value['safety_policy_revision'] = 'forged-policy'
    with pytest.raises(ValidationError, match='digest mismatch'):
        ConfirmationDraft.from_private_dict(value)

    for field_name, forged in (
        ('message', '주방으로 이동할까요?'),
        ('target_room_name', '주방'),
        ('target_room_category', 'kitchen'),
    ):
        value = deepcopy(_draft().to_private_dict())
        value[field_name] = forged
        with pytest.raises(ValidationError, match='digest mismatch'):
            ConfirmationDraft.from_private_dict(value)


@pytest.mark.parametrize(
    ('text', 'disposition', 'code'),
    [
        ('네', 'approved', 'confirmation_approved'),
        ('아니요', 'denied', 'confirmation_denied'),
        ('취소', 'canceled', 'confirmation_canceled'),
    ],
)
def test_terminal_response_never_grants_execution_authority(
    text: str,
    disposition: str,
    code: str,
) -> None:
    record = ConfirmationRecord.pending(_draft())
    resolution = ConfirmationResolution.create(
        record,
        caller_user_id='user-1',
        caller_conversation_id='conversation-1',
        caller_session_instance_id='session-instance-1',
        caller_generation=1,
        response_id='response-1',
        response_turn_id='response-turn-1',
        response_text=text,
    )
    terminal = record.resolve(resolution, resolved_at=101.0)

    assert terminal.disposition == disposition
    assert terminal.result_code == code
    assert terminal.execution_authorized is False
    assert terminal.consume_once is False
    assert terminal.to_public_dict()['execution'] == {
        'authorized': False,
        'execution_authorized': False,
        'consume_once': False,
        'tool_call_id': None,
        'physical_authorized': False,
        'nav2_start_count': 0,
        'nav2_cancel_count': 0,
    }
    assert ConfirmationRecord.from_private_dict(
        terminal.to_private_dict()
    ) == terminal


def test_wrong_session_and_ambiguous_response_fail_closed() -> None:
    record = ConfirmationRecord.pending(_draft())
    with pytest.raises(ValidationError, match='ambiguous'):
        ConfirmationResolution.create(
            record,
            caller_user_id='user-1',
            caller_conversation_id='conversation-1',
            caller_session_instance_id='session-instance-1',
            caller_generation=1,
            response_id='response-ambiguous',
            response_turn_id='response-turn-ambiguous',
            response_text='글쎄',
        )

    with pytest.raises(ConfirmationDomainConflictError):
        ConfirmationResolution.create(
            record,
            caller_user_id='user-1',
            caller_conversation_id='conversation-1',
            caller_session_instance_id='another-session',
            caller_generation=1,
            response_id='response-wrong-session',
            response_turn_id='response-turn-wrong-session',
            response_text='네',
        )
