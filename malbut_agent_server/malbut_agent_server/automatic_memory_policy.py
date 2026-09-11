"""Eligibility and atomic effects for delayed, non-actuating automatic saves.

The worker must hold the conversation write transaction, verify the original
session identity, generation, completed turn and current invalidation epoch,
and reject pending memory or robot questions before ``apply_automatic``.
This module never completes or rewrites the already-frozen conversation turn.
"""

import copy
import json
import re

from malbut_agent_server.memory_contract import validate_memory_proposal
from malbut_agent_server.personal_memory import (
    compact, direct_source, local_intent, management_request,
    negated_management, requested_operation,
)
from malbut_agent_server.schemas import ValidationError


_ATTRIBUTES = {
    'name': {'name'},
    'nickname': {'nickname'},
    'preference': {'likes', 'dislikes', 'preference'},
    'pet': {
        'name', 'species', 'breed', 'age', 'birthday', 'color',
        'likes', 'dislikes', 'preference',
    },
}
_SELF_SUBJECTS = {'user', 'self', '사용자', '나', '저'}
_QUESTION = re.compile(r'[?？]|뭐|누구|어떤|어떻게|일까|인가요')


def automatic_request(request, snapshot):
    """Choose response-first extraction without classifying facts up front.

    Management and pending questions stay synchronous. This is admission for
    a private extraction job, not evidence that the utterance contains a fact.
    """
    try:
        text = request.utterance
        if (
            snapshot.state['enabled'] is not True
            or snapshot.pending is not None
            or not direct_source(text)
            or management_request(text)
            or negated_management(text)
            or local_intent(text) is not None
            or any(requested_operation(text, operation)
                   for operation in ('remember', 'correct', 'forget'))
            or _QUESTION.search(text)
        ):
            return False
        return all(snapshot.source.get(key) == value for key, value in (
            ('user_id', request.user_id),
            ('conversation_id', request.conversation_id),
            ('request_id', request.request_id),
            ('turn_id', request.turn_id),
            ('text', text),
        ))
    except (AttributeError, KeyError, TypeError):
        return False


def automatic_deferred(request, snapshot, result):
    """Only an actual ordinary reply may reserve deferred extraction."""
    try:
        return (
            automatic_request(request, snapshot)
            and result.decision.type == 'message'
            and result.provider_result.decision.type == 'message'
            and result.raw_decision.type == 'message'
        )
    except AttributeError:
        return False


def automatic_candidate(request, snapshot, result):
    """Recognize only source-grounded automatic candidates, before review."""
    try:
        provider = result.provider_result
        text = request.utterance
        if (not automatic_deferred(request, snapshot, result)
                or provider.memory_supported is not True):
            return False
        proposal = validate_memory_proposal(provider.memory_proposal)
        if (
            proposal['operation'] != 'remember'
            or not proposal['facts']
            or proposal['target_ids']
            or proposal['query']
            or proposal['evidence'] not in text
        ):
            return False
        source = snapshot.source
        if any(source.get(key) != value for key, value in (
            ('user_id', request.user_id),
            ('conversation_id', request.conversation_id),
            ('request_id', request.request_id),
            ('turn_id', request.turn_id),
            ('text', text),
        )):
            return False
        for fact in proposal['facts']:
            self_subject = compact(fact['subject']) in _SELF_SUBJECTS
            if (
                fact['evidence'] not in text
                or not compact(fact['value'])
                or compact(fact['value']) not in compact(fact['evidence'])
                or fact['attribute'] not in _ATTRIBUTES[fact['kind']]
                or (fact['kind'] == 'pet' and self_subject)
                or (fact['kind'] != 'pet' and not self_subject)
            ):
                return False
        return True
    except (AttributeError, KeyError, TypeError, ValidationError):
        return False


def apply_automatic(personal, request, token, snapshot, result, conn):
    """Save eligible facts and attach lineage to their frozen origin's stamp.

    The caller owns the write transaction and original session/turn checks.
    Database failures propagate after rolling back this operation's savepoint.
    The caller's result, conversation reply and persisted response stay frozen.
    """
    discarded = {'state': 'discarded', 'memory_ids': []}
    if not conn.in_transaction:
        raise ValidationError('automatic memory requires a write transaction')
    if not automatic_candidate(request, snapshot, result):
        return discarded
    if snapshot.state != personal.memory.policy_state(
        request.user_id, connection=conn,
    ):
        return discarded
    identity = (
        request.user_id, request.request_id, token.conversation_id,
        token.session_instance_id, token.generation, token.turn_id,
    )
    if (
        token.user_id != request.user_id
        or token.request_id != request.request_id
        or token.conversation_id != request.conversation_id
        or token.turn_id != request.turn_id
        or snapshot.source.get('session_instance_id')
        != token.session_instance_id
        or snapshot.source.get('generation') != token.generation
    ):
        return discarded
    origin = conn.execute(
        '''SELECT revision, dependencies_json FROM memory_turn_state
        WHERE user_id=? AND request_id=? AND conversation_id=?
        AND session_instance_id=? AND generation=? AND turn_id=?''',
        identity,
    ).fetchone()
    if origin is None or origin['revision'] != snapshot.state['revision']:
        return discarded
    if conn.execute(
        '''SELECT 1 FROM memory_questions
        WHERE user_id=? AND conversation_id=? LIMIT 1''',
        (request.user_id, request.conversation_id),
    ).fetchone() is not None:
        return discarded
    # _apply owns meaning checks, duplicate handling and batch conflicts.
    # Give it private model output so no delayed path can mutate the reply.
    candidate = copy.copy(result)
    candidate.provider_result = copy.deepcopy(result.provider_result)
    candidate.decision = copy.deepcopy(result.decision)
    candidate.raw_decision = copy.deepcopy(result.raw_decision)
    conn.execute('SAVEPOINT automatic_memory_effects')
    try:
        decision, added = personal._apply(
            request, token, snapshot, candidate, conn,
            _automatic_insert=True,
        )
        if decision is not None or not added:
            conn.execute('ROLLBACK TO automatic_memory_effects')
            conn.execute('RELEASE automatic_memory_effects')
            return discarded
        # Even unexpected future _apply behavior must not create a question.
        if conn.execute(
            '''SELECT 1 FROM memory_questions
            WHERE user_id=? AND conversation_id=? LIMIT 1''',
            (request.user_id, request.conversation_id),
        ).fetchone() is not None:
            conn.execute('ROLLBACK TO automatic_memory_effects')
            conn.execute('RELEASE automatic_memory_effects')
            return discarded
        dependencies = set(json.loads(origin['dependencies_json']))
        dependencies.update(added)
        revision = personal.memory.policy_state(
            request.user_id, connection=conn,
        )['revision']
        updated = conn.execute(
            '''UPDATE memory_turn_state SET revision=?, dependencies_json=?
            WHERE user_id=? AND request_id=? AND conversation_id=?
            AND session_instance_id=? AND generation=? AND turn_id=?''',
            (revision, json.dumps(sorted(dependencies)), *identity),
        )
        if updated.rowcount != 1:
            conn.execute('ROLLBACK TO automatic_memory_effects')
            conn.execute('RELEASE automatic_memory_effects')
            return discarded
        # Later completed turns may have consumed the origin before the fact
        # had an ID. Backfill only their private lineage, never their frozen
        # reply or policy epoch. An in-flight descendant gets the same IDs
        # from PersonalMemory.commit's transaction-time context re-read.
        descendants = conn.execute(
            '''SELECT m.request_id, m.dependencies_json
            FROM memory_turn_state m JOIN conversation_turns t
                ON t.user_id=m.user_id AND t.request_id=m.request_id
                AND t.conversation_id=m.conversation_id
                AND t.session_instance_id=m.session_instance_id
                AND t.generation=m.generation AND t.turn_id=m.turn_id
            WHERE t.user_id=? AND t.conversation_id=?
                AND t.session_instance_id=? AND t.generation=?
                AND t.status='completed' AND t.ordinal>?''',
            (token.user_id, token.conversation_id, token.session_instance_id,
             token.generation, token.ordinal),
        ).fetchall()
        for descendant in descendants:
            inherited = set(json.loads(descendant['dependencies_json']))
            inherited.update(added)
            conn.execute(
                '''UPDATE memory_turn_state SET dependencies_json=?
                WHERE user_id=? AND request_id=?''',
                (json.dumps(sorted(inherited)), token.user_id,
                 descendant['request_id']),
            )
        conn.execute('RELEASE automatic_memory_effects')
        return {'state': 'saved', 'memory_ids': list(dict.fromkeys(added))}
    except Exception:
        conn.execute('ROLLBACK TO automatic_memory_effects')
        conn.execute('RELEASE automatic_memory_effects')
        raise
