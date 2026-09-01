"""Exact revision lookup tests for durable text-turn request claims."""

import hashlib

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _claim(
    store: SQLiteConversationStore,
    *,
    user_id: str = 'user-a',
    conversation_id: str = 'conversation-a',
):
    session = store.create(user_id, conversation_id)
    claim, record = store.claim_text_turn_response(
        user_id,
        conversation_id,
        request_id='response-request-1',
        turn_id='response-turn-1',
        request_fingerprint=_digest('response-envelope-1'),
        outcome='confirmation_not_pending',
        now=1000.0,
    )
    assert record is None
    assert claim.session_instance_id == session.session_instance_id
    return claim


def test_exact_claim_revision_is_found_and_other_namespaces_are_not(
    tmp_path,
) -> None:
    store = SQLiteConversationStore(
        str(tmp_path / 'claim-revision.sqlite3'),
        clock=lambda: 1000.0,
    )
    try:
        claim = _claim(store)

        assert store.has_text_turn_claim_at_revision(
            claim.user_id,
            claim.conversation_id,
            claim.session_instance_id,
            claim.generation,
            claim.revision,
        )
        assert not store.has_text_turn_claim_at_revision(
            claim.user_id,
            claim.conversation_id,
            claim.session_instance_id,
            claim.generation,
            claim.revision + 1,
        )
        assert not store.has_text_turn_claim_at_revision(
            claim.user_id,
            claim.conversation_id,
            claim.session_instance_id,
            claim.generation + 1,
            claim.revision,
        )
        assert not store.has_text_turn_claim_at_revision(
            claim.user_id,
            claim.conversation_id,
            'different-session-instance',
            claim.generation,
            claim.revision,
        )
        assert not store.has_text_turn_claim_at_revision(
            claim.user_id,
            'different-conversation',
            claim.session_instance_id,
            claim.generation,
            claim.revision,
        )
        assert not store.has_text_turn_claim_at_revision(
            'different-user',
            claim.conversation_id,
            claim.session_instance_id,
            claim.generation,
            claim.revision,
        )
    finally:
        store.close()


def test_claim_revision_lookup_survives_store_restart(tmp_path) -> None:
    path = tmp_path / 'claim-revision-restart.sqlite3'
    first = SQLiteConversationStore(str(path), clock=lambda: 1000.0)
    claim = _claim(first)
    first.close()

    second = SQLiteConversationStore(str(path), clock=lambda: 1000.0)
    try:
        assert second.has_text_turn_claim_at_revision(
            claim.user_id,
            claim.conversation_id,
            claim.session_instance_id,
            claim.generation,
            claim.revision,
        )
    finally:
        second.close()


@pytest.mark.parametrize(
    ('field', 'value'),
    (
        ('user_id', ''),
        ('conversation_id', ''),
        ('session_instance_id', ''),
        ('session_instance_id', 'x' * 129),
        ('generation', True),
        ('generation', 0),
        ('generation', 2 ** 63),
        ('revision', False),
        ('revision', -1),
        ('revision', 2 ** 63),
    ),
)
def test_claim_revision_lookup_rejects_invalid_inputs(
    tmp_path,
    field,
    value,
) -> None:
    store = SQLiteConversationStore(
        str(tmp_path / 'invalid-claim-revision.sqlite3'),
        clock=lambda: 1000.0,
    )
    values = {
        'user_id': 'user-a',
        'conversation_id': 'conversation-a',
        'session_instance_id': 'session-a',
        'generation': 1,
        'revision': 0,
    }
    values[field] = value
    try:
        with pytest.raises(ValueError):
            store.has_text_turn_claim_at_revision(**values)
    finally:
        store.close()
