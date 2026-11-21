"""Turn admission, freshness provenance guards, and request evidence helpers."""

import copy
import hashlib
import json
import re
import sqlite3
from typing import Any, Callable, Sequence

from malbut_agent_server.automatic_memory_jobs import AutomaticMemoryJobs
from malbut_agent_server.conversation import (
    BeginTurnToken,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestration.models import (
    MemoryChangedError,
    OrchestrationResult,
    StoryReplyBinding,
)
from malbut_agent_server.personal_memory import (
    PersonalMemory,
    direct_source,
    explicit_memory_management,
    local_intent,
    negated_management,
    requested_operation,
)
from malbut_agent_server.robot_state_source import RobotStateSource
from malbut_agent_server.schemas import (
    AgentRequest,
    RobotState,
    ValidationError,
    copy_request,
)


def admit_memory_turn(
    automatic_memory_jobs: AutomaticMemoryJobs,
    memory_store: SQLiteMemoryStore,
    connection: sqlite3.Connection,
    request: AgentRequest,
) -> None:
    """Ordinary turns preserve jobs; explicit memory intent fences them.

    This runs after request-cache/conflict checks in the turn reservation
    transaction. Bump even for no-match deletion so an older in-flight
    provider cannot enqueue its stale source after this request finishes.
    """
    text = request.utterance
    # Existing extraction eligibility is deliberately broad. Its
    # "기억하" stem also matches read-only questions like "기억하니?";
    # those must not become cancellation merely by mentioning memory.
    remember_instruction = re.sub(
        r'(기억|저장)(하고\s*있|하니|하나요|하는지|했니|했나요|했는지'
        r'|해(?:요)?\s*(?=[?？]))',
        '', text,
    )
    if direct_source(text) and (
        local_intent(text) is not None
        or (negated_management(text) and re.search(
            r'(기억|저장)(을|은|는)?\s*(하지|말아|말고|말라|않|금지)'
            r'|안\s*(기억|저장)', text,
        ))
        or any(requested_operation(text, operation)
               for operation in ('correct', 'forget'))
        or requested_operation(remember_instruction, 'remember')
    ):
        automatic_memory_jobs.invalidate_user(
            connection, request.user_id, reason='memory_control',
        )
        memory_store.invalidate_answers(
            request.user_id, connection=connection,
        )


def assert_story_binding(
    story_memory: Any,
    user_id: str,
    request_id: str,
    binding: StoryReplyBinding | None,
) -> None:
    """Verify that story provenance still matches the persisted readset."""
    if binding is None:
        return
    if story_memory is None:
        raise MemoryChangedError('story memory is unavailable; submit a new turn')
    value = binding.to_dict()
    if not story_memory.validate_reply_binding(
        user_id, value['policy_revision'], value['readset'], request_id=request_id,
    ):
        raise MemoryChangedError('memory changed; submit a new turn')


def assert_reply_fresh(
    *,
    closed: bool,
    conversation_store: SQLiteConversationStore,
    personal_memory: PersonalMemory,
    story_memory: Any,
    user_id: str,
    request_id: str,
) -> None:
    """Check durable fact and story provenance, including delayed/cache use."""
    if closed:
        raise MemoryChangedError('orchestrator is closed')
    try:
        with conversation_store.locked():
            personal_memory.assert_fresh(user_id, request_id)
            if story_memory is not None:
                record = story_memory.store.reply_record(user_id, request_id)
            else:
                row = conversation_store.database.connection.execute(
                    'SELECT response_json FROM conversation_turns '
                    "WHERE user_id=? AND request_id=? AND status='completed'",
                    (user_id, request_id),
                ).fetchone()
                record = (
                    {'response': json.loads(row[0]), 'has_story_dependencies': False}
                    if row is not None and row[0] is not None
                    else None
                )
            if record is None:
                raise MemoryChangedError('reply is unavailable; submit a new turn')
            restored = OrchestrationResult.from_persisted_dict(record['response'])
            if record['has_story_dependencies'] and restored.story_binding is None:
                raise MemoryChangedError(
                    'legacy story reply cannot be verified; submit a new turn'
                )
            assert_story_binding(story_memory, user_id, request_id, restored.story_binding)
    except (ValidationError, ValueError, RuntimeError) as error:
        if isinstance(error, MemoryChangedError):
            raise
        raise MemoryChangedError('memory changed; submit a new turn') from error


def can_deliver_before_save(
    request: AgentRequest,
    memory_snapshot: Any,
    result: OrchestrationResult,
    confirmation_factory: Any,
) -> bool:
    """Return True when a conversational reply may stream to TTS before commit."""
    # Memory management/configuration and action receipts must
    # never claim success before persistence. commit_memory may
    # also replace a model's unsupported completion claim.
    return not (
        result.raw_decision.type == 'tool_call'
        or result.decision.type not in {'message', 'clarification', 'refusal'}
        or result.decision.reason == 'personal_memory'
        or result.decision.clarification_for == 'mapping_name'
        or result.safety.code == 'navigation_confirmation_required'
        or result.provider_result.memory_proposal is not None
        or memory_snapshot.pending is not None
        or memory_snapshot.source.get('settings_edit')
        or explicit_memory_management(request.utterance)
        or confirmation_factory is not None
    )


def effective_request(
    request: AgentRequest,
    conversation_turns: Sequence[ConversationTurn],
    token: BeginTurnToken,
    utterance_resolver: Callable[
        [AgentRequest, Sequence[ConversationTurn], BeginTurnToken],
        str | None,
    ] | None,
) -> AgentRequest:
    """Resolve one server-owned utterance without changing authority."""
    if utterance_resolver is None:
        return request
    resolved = utterance_resolver(
        copy.deepcopy(request),
        copy.deepcopy(tuple(conversation_turns)),
        copy.deepcopy(token),
    )
    if resolved is None:
        return request
    if type(resolved) is not str:
        raise TypeError('utterance_resolver must return str or None')
    value = request.to_dict()
    value['utterance'] = resolved
    return copy_request(request, value)


def fresh_safety_request(
    robot_state_source: RobotStateSource | None,
    trusted_robot_state: bool,
    robot_state_max_age_seconds: float,
    state_clock: Callable[[], float],
    request: AgentRequest,
) -> tuple[AgentRequest, bool, str | None, float | None]:
    """Read independent server-owned evidence, or preserve legacy mode."""
    if robot_state_source is None:
        return request, trusted_robot_state, None, None
    try:
        evidence = robot_state_source.read()
        now = float(state_clock())
        age = now - float(evidence.observed_at)
        trusted = (
            evidence.trusted
            and age >= 0
            and age <= robot_state_max_age_seconds
        )
        state = evidence.state
        if age < 0:
            evidence_id: str | None = None
            observed_at: float | None = None
        else:
            evidence_id = evidence.evidence_id
            observed_at = float(evidence.observed_at)
    except Exception:
        trusted = False
        state = RobotState()
        evidence_id = None
        observed_at = None
    value = request.to_dict()
    value['robot_state'] = state.to_dict()
    return (
        copy_request(request, value),
        trusted,
        evidence_id,
        observed_at,
    )


def request_fingerprint(request: AgentRequest) -> str:
    """Return a canonical SHA-256 fingerprint for idempotency conflict detection."""
    encoded = json.dumps(
        request.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
