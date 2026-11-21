"""Conversation, memory, provider, and deterministic safety orchestration."""

import copy
import math
import threading
import time
import uuid
from concurrent.futures import CancelledError
from contextlib import nullcontext
from dataclasses import replace
from typing import Any, Callable, Dict, Sequence

from malbut_agent_server.automatic_memory_jobs import AutomaticMemoryJobs
from malbut_agent_server.automatic_memory_policy import (
    automatic_candidate,
    automatic_deferred,
    automatic_request,
)
from malbut_agent_server.automatic_memory_worker import AutomaticMemoryWorker
from malbut_agent_server.conversation import (
    BeginTurnToken,
    ConversationSummary,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.conversation_ownership import (
    owned_conversation_request,
    recover_abandoned_turns,
)
from malbut_agent_server.conversation_progress import conversation_request
from malbut_agent_server.gateway import (
    CapabilityRegistry,
    production_registry,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.offline_fallback import (
    DEFAULT_OFFLINE_STOP_TIMEOUT_SECONDS,
    is_provider_outage_result,
    is_urgent_stop_utterance,
    offline_stop_budget_scope,
    provider_circuit_summary,
    resolve_offline_speech_fallback,
)
from malbut_agent_server.orchestration import (
    ExpiredDecisionError,
    MemoryChangedError,
    OrchestrationResult,
    ServerClarification,
    StoryReplyBinding,
    admit_memory_turn,
    answer_homecam,
    answer_weather,
    assert_reply_fresh,
    assert_story_binding,
    can_deliver_before_save,
    effective_request as resolve_effective_request,
    fresh_safety_request,
    request_fingerprint,
    thanks_after_answer,
)
from malbut_agent_server.personal_memory import (
    PersonalMemory,
    explicit_memory_management,
    reply as memory_reply,
)
from malbut_agent_server.providers.base import (
    AgentProvider,
    ProviderError,
    accepts_memory_context,
    accepts_weather_context,
)
from malbut_agent_server.robot_state_source import RobotStateSource
from malbut_agent_server.safety import SafetyPolicy, SafetyResult
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    RobotState,
    SpeechAgentRequest,
    ValidationError,
    copy_request,
)
from malbut_agent_server.tools import HOMECAM_QUERY_TOOLS, PATROL_QUERY_TOOLS
from malbut_agent_server.turn_metrics import (
    TurnWaterfallRecorder,
    TurnWaterfallTrace,
    current_turn_trace,
    turn_trace_scope,
)

__all__ = [
    'AgentOrchestrator',
    'ExpiredDecisionError',
    'MemoryChangedError',
    'OrchestrationResult',
    'ServerClarification',
    'StoryReplyBinding',
]


class AgentOrchestrator:
    """Keep model selection separate from conversation and authorization."""

    def __init__(
        self,
        provider: AgentProvider,
        memory_store: SQLiteMemoryStore,
        conversation_store: SQLiteConversationStore,
        safety_policy: SafetyPolicy,
        memory_limit: int = 5,
        trusted_robot_state: bool = False,
        capability_registry: CapabilityRegistry | None = None,
        robot_state_source: RobotStateSource | None = None,
        robot_state_max_age_seconds: float = 2.0,
        state_clock: Callable[[], float] = time.time,
        memory_source_reviewer=None,
        background_memory: bool = False,
        automatic_memory_extractor=None,
        weather_executor: Callable[[str], dict] | None = None,
        weather_location_executor: Callable[[str, str], dict] | None = None,
        context_compactor=None,
        story_memory=None,
        story_provider=None,
        turn_metrics: TurnWaterfallRecorder | None = None,
        offline_safety_fallback_enabled: bool = True,
        offline_stop_timeout_seconds: float = DEFAULT_OFFLINE_STOP_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize provider, memory, session, and safety services."""
        if memory_limit < 1 or memory_limit > 10:
            raise ValueError('memory_limit must be between 1 and 10')
        self.provider = provider
        self.memory_store = memory_store
        self.conversation_store = conversation_store
        self.safety_policy = safety_policy
        self.memory_limit = memory_limit
        self.trusted_robot_state = trusted_robot_state
        if (
            isinstance(robot_state_max_age_seconds, bool)
            or not isinstance(robot_state_max_age_seconds, (int, float))
            or robot_state_max_age_seconds <= 0
            or robot_state_max_age_seconds > 60
        ):
            raise ValueError(
                'robot_state_max_age_seconds must be from 0 to 60'
            )
        self.robot_state_source = robot_state_source
        self.robot_state_max_age_seconds = float(
            robot_state_max_age_seconds
        )
        if not callable(state_clock):
            raise TypeError('state_clock must be callable')
        self._state_clock = state_clock
        self.capability_registry = (
            capability_registry or production_registry()
        )
        self._handle_lock = threading.RLock()
        self.personal_memory = PersonalMemory(memory_store, conversation_store)
        self.memory_source_reviewer = memory_source_reviewer
        self.automatic_memory_extractor = automatic_memory_extractor
        self.context_compactor = context_compactor
        self.story_memory = story_memory
        self.story_provider = story_provider
        self.weather_executor = weather_executor
        self.weather_location_executor = weather_location_executor
        if type(background_memory) is not bool:
            raise TypeError('background_memory must be a boolean')
        self.automatic_memory_jobs = AutomaticMemoryJobs(conversation_store)
        self.automatic_memory_worker = (
            AutomaticMemoryWorker(self, self.automatic_memory_jobs)
            if background_memory else None
        )
        self.turn_metrics = turn_metrics or TurnWaterfallRecorder()
        if type(offline_safety_fallback_enabled) is not bool:
            raise TypeError('offline_safety_fallback_enabled must be a boolean')
        if (
            isinstance(offline_stop_timeout_seconds, bool)
            or not isinstance(offline_stop_timeout_seconds, (int, float))
            or not math.isfinite(float(offline_stop_timeout_seconds))
            or not (0.5 <= float(offline_stop_timeout_seconds) <= 15.0)
        ):
            raise ValueError(
                'offline_stop_timeout_seconds must be between 0.5 and 15.0'
            )
        self.offline_safety_fallback_enabled = offline_safety_fallback_enabled
        self.offline_stop_timeout_seconds = float(offline_stop_timeout_seconds)
        self._closed = False
        self._deferred_conversation_recovery = recover_abandoned_turns(conversation_store)

    def start_background_memory(self):
        """Recover queued work when serving, not during construction."""
        if self.automatic_memory_worker is not None and not self._closed:
            self.automatic_memory_worker.start()
        if self.story_memory is not None and not self._closed:
            self.story_memory.start()

    def provider_health(self) -> Dict[str, Any]:
        """Return content-free circuit states for observability boundaries."""
        return provider_circuit_summary(self.provider)

    def stop_background_memory(self):
        """Drain the owned worker before callers close the shared stores."""
        if self.automatic_memory_worker is not None:
            self.automatic_memory_worker.close()
        if self.story_memory is not None:
            self.story_memory.close()

    def checkpoint_story_memory(self, user_id, timeout=0.25):
        """Try a short checkpoint; reset retains originals for durable recovery."""
        if self.story_memory is None:
            return True
        self.start_background_memory()
        return self.story_memory.flush(user_id, timeout=timeout)

    def delete_conversation(self, user_id, conversation_id):
        """Delete a session through shared story/source lineage when installed."""
        with self._handle_lock:
            if self._closed:
                raise RuntimeError('orchestrator is closed')
            if self.story_memory is not None:
                return self.story_memory.store.delete_conversation(user_id, conversation_id)
            return self.conversation_store.delete(user_id, conversation_id)

    def close(self):
        """Reject new turns, join the worker, then close SQLite handles."""
        with self._handle_lock:
            if self._closed:
                return
            self._closed = True
        patrol_worker = getattr(self, 'patrol_observation_worker', None)
        if patrol_worker is not None:
            patrol_worker.close()
        self.stop_background_memory()
        if self.context_compactor is not None:
            self.context_compactor.close()
        try:
            from malbut_agent_server.providers.base import close_providers

            close_providers(
                self.provider,
                getattr(self.memory_source_reviewer, 'provider', None),
                getattr(self.automatic_memory_extractor, 'provider', None),
                getattr(getattr(self, 'speech_addressee', None), 'provider', None),
                getattr(self, 'companion_provider', None),
                getattr(self, 'homecam_executor', None),
            )
        finally:
            try:
                self.conversation_store.close()
            finally:
                self.memory_store.close()

    def _memory_guard(self, user_id, request_id):
        """Bind a response to its durable user-specific memory version."""
        def validate():
            self.assert_reply_fresh(user_id, request_id)
        return validate

    def _assert_story_binding(self, user_id, request_id, binding):
        assert_story_binding(self.story_memory, user_id, request_id, binding)

    def assert_reply_fresh(self, user_id, request_id):
        """Check durable fact and story provenance, including delayed/cache use."""
        assert_reply_fresh(
            closed=self._closed,
            conversation_store=self.conversation_store,
            personal_memory=self.personal_memory,
            story_memory=self.story_memory,
            user_id=user_id,
            request_id=request_id,
        )

    def _admit_memory_turn(self, connection, request):
        """Ordinary turns preserve jobs; explicit memory intent fences them."""
        admit_memory_turn(
            self.automatic_memory_jobs,
            self.memory_store,
            connection,
            request,
        )

    @conversation_request
    @owned_conversation_request
    def handle(
        self,
        request: AgentRequest,
        *,
        utterance_resolver: Callable[
            [AgentRequest, Sequence[ConversationTurn], BeginTurnToken],
            str | None,
        ] | None = None,
        proposal_verifier: Callable[
            [AgentDecision], SafetyResult | None
        ] | None = None,
        confirmation_factory: Callable[
            [OrchestrationResult, BeginTurnToken], Any
        ] | None = None,
        server_clarification: ServerClarification | None = None,
    ) -> OrchestrationResult:
        """Process one turn and optionally bind a non-authorizing intent."""
        if (
            confirmation_factory is not None
            and not callable(confirmation_factory)
        ):
            raise TypeError('confirmation_factory must be callable')
        if proposal_verifier is not None and not callable(proposal_verifier):
            raise TypeError('proposal_verifier must be callable')
        if utterance_resolver is not None and not callable(
            utterance_resolver
        ):
            raise TypeError('utterance_resolver must be callable')
        if (
            server_clarification is not None
            and not isinstance(server_clarification, ServerClarification)
        ):
            raise TypeError(
                'server_clarification must be a ServerClarification'
            )
        fingerprint = self._request_fingerprint(request)
        outer_trace = current_turn_trace()
        owns_trace = outer_trace is None
        trace = (
            outer_trace
            if outer_trace is not None
            else TurnWaterfallTrace(
                channel='orchestrator',
                request_id=request.request_id,
                conversation_id=request.conversation_id,
            )
        )
        trace.set_metadata(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
        )
        with turn_trace_scope(trace), self._handle_lock:
            if self._closed:
                if owns_trace:
                    trace.finalize(self.turn_metrics, outcome='error')
                raise RuntimeError('orchestrator is closed')
            try:
                with trace.measure('begin_turn_ms'):
                    begin = self.conversation_store.begin_turn(
                        user_id=request.user_id,
                        conversation_id=request.conversation_id,
                        turn_id=request.turn_id,
                        request_id=request.request_id,
                        request_fingerprint=fingerprint,
                        user_content=request.utterance,
                        max_user_content_chars=request.max_utterance_chars,
                        before_new_turn=lambda conn: self._admit_memory_turn(
                            conn, request,
                        ),
                    )
            except Exception as error:
                if owns_trace:
                    trace.finalize(
                        self.turn_metrics,
                        outcome=(
                            'cancelled'
                            if isinstance(error, CancelledError)
                            else 'error'
                        ),
                    )
                raise
            if begin.cached_response is not None:
                guard = self._memory_guard(request.user_id, request.request_id)
                guard()
                result = OrchestrationResult.from_persisted_dict(
                    begin.cached_response
                )
                result.clock = self._state_clock
                result.memory_validator = guard
                self.start_background_memory()
                trace.set_flag('cached_response', True)
                trace.set_metadata(
                    outcome='cached',
                    decision_type=result.decision.type,
                    tool_name=(
                        result.raw_decision.tool_name
                        or result.decision.tool_name
                    ),
                    safety_code=result.safety.code,
                )
                trace.attach_provider_result(result.provider_result)
                if owns_trace:
                    trace.finalize(self.turn_metrics, outcome='cached')
                return result
            token = begin.token
            if token is None:
                if owns_trace:
                    trace.finalize(self.turn_metrics, outcome='error')
                raise RuntimeError(
                    'conversation begin returned no token'
                )
            committed = False
            try:
                with trace.measure('memory_snapshot_ms'):
                    memory_snapshot = self.personal_memory.snapshot(
                        request, token, begin.history, begin.summary,
                        memory_limit=self.memory_limit,
                        defer_facts=(isinstance(request, SpeechAgentRequest)
                                     and self.provider is self.story_provider
                                     and getattr(self.story_provider, 'on_demand_speech', False)),
                    )
                    effective_request = self._effective_request(
                        request,
                        memory_snapshot.history,
                        token,
                        utterance_resolver,
                    )
                    if self.context_compactor is not None:
                        self.context_compactor.schedule(
                            token, begin.summary, memory_snapshot.history,
                            memory_snapshot.summary, memory_snapshot.state,
                            effective_request.utterance,
                        )
                    from malbut_agent_server.conversation_recall import recall_originals

                    recall_originals(self.personal_memory, request, token, memory_snapshot)
                result = self._handle_uncached(
                    request,
                    effective_request,
                    memory_snapshot.history,
                    memory_snapshot.summary,
                    token,
                    proposal_verifier,
                    server_clarification,
                    memory_snapshot,
                    trace=trace,
                )
                worker = self.automatic_memory_worker
                extract = (
                    self._separate_memory_extraction(request, memory_snapshot)
                    and server_clarification is None
                    and automatic_deferred(request, memory_snapshot, result)
                )
                deferred = (
                    worker is not None and not worker.closed
                    and (extract or automatic_candidate(
                        request, memory_snapshot, result,
                    ))
                )
                if not deferred:
                    self.personal_memory.prepare_source_review(
                        request, memory_snapshot, result,
                        self.memory_source_reviewer,
                    )
                completion_arguments: Dict[str, Any] = {}
                if confirmation_factory is not None:
                    safety_provenance = (
                        result.state_evidence_id,
                        result.state_observed_at,
                        result.safety_policy_revision,
                    )
                    completion_arguments['confirmation_draft'] = (
                        confirmation_factory(result, token)
                    )
                    if safety_provenance != (
                        result.state_evidence_id,
                        result.state_observed_at,
                        result.safety_policy_revision,
                    ):
                        raise RuntimeError(
                            'confirmation factory modified safety provenance'
                        )

                def commit_memory(connection):
                    if deferred and (extract or automatic_candidate(
                        request, memory_snapshot, result,
                    )):
                        # The response and private reservation commit together.
                        # Saturation skips autosave, not the conversation.
                        self.automatic_memory_jobs.enqueue(
                            connection, request, token, memory_snapshot,
                            result, extract=extract,
                        )
                        result.provider_result.memory_proposal = None
                    return self.personal_memory.commit(
                        request, token, memory_snapshot, result, connection,
                    )

                from malbut_agent_server.speech_delivery import (
                    deliver_before_save, delivery_available,
                )

                def deliver_committing_reply():
                    if not can_deliver_before_save(
                        request, memory_snapshot, result, confirmation_factory,
                    ):
                        return
                    self.personal_memory.assert_fresh(request.user_id, request.request_id)
                    self._assert_story_binding(request.user_id, request.request_id,
                                               result.story_binding)
                    result.speech_published = deliver_before_save(result)

                publish = delivery_available()
                read_version = None
                lookup_version = getattr(self.story_provider, 'direct_story_read_version', None)
                if (lookup_version is not None and result.story_binding is not None
                        and request.utterance == effective_request.utterance):
                    read_version = lookup_version(request.user_id, request.request_id)

                def before_commit():
                    if (read_version is not None and result.raw_decision.type == result.decision.type == 'message'
                            and result.safety.allowed and result.safety.code == 'not_an_action'
                            and result.provider_result.memory_proposal is None):
                        self.story_memory.store.checkpoint_source_read(
                            self.conversation_store.database.connection,
                            request.user_id,
                            request.request_id,
                            result.story_binding.policy_revision,
                            result.story_binding.readset,
                            read_version,
                        )
                    if publish:
                        deliver_committing_reply()

                if publish or read_version is not None:
                    completion_arguments['before_commit'] = before_commit

                with trace.measure('commit_ms'):
                    with self.conversation_store.locked():
                        # Policy/erasure writes use this same lock. Keep validation
                        # and the completed response commit in one protected window.
                        self._assert_story_binding(request.user_id, request.request_id,
                                                   result.story_binding)
                        session, _turn = self.conversation_store.complete_turn(
                            token,
                            assistant_content=result.decision.message,
                            response=result.to_persisted_dict(),
                            commit_callback=commit_memory,
                            **completion_arguments,
                        )
                    committed = True
                    if (
                        session.generation
                        != result.conversation_generation
                        or session.revision
                        != result.conversation_revision
                    ):
                        raise RuntimeError(
                            'conversation commit metadata did not match'
                        )
                    result.memory_validator = self._memory_guard(
                        request.user_id, request.request_id,
                    )
                    self.start_background_memory()
                    if self.story_memory is not None:
                        try:
                            self.story_memory.after_turn(request.user_id, request.request_id)
                        except Exception:
                            # The completed reply already owns durable raw sources.
                            # Recovery fills an enqueue gap without replaying inference.
                            self.story_memory.note_enqueue_failure(request.user_id)
                    if not result.speech_published:
                        result.memory_validator()
                if owns_trace:
                    trace.finalize(self.turn_metrics, outcome='ok')
                return result
            except Exception as error:
                if not committed:
                    self.conversation_store.fail_turn(token)
                if owns_trace:
                    trace.finalize(
                        self.turn_metrics,
                        outcome=(
                            'cancelled'
                            if isinstance(error, CancelledError)
                            else 'error'
                        ),
                    )
                if (
                    isinstance(error, ValidationError)
                    and str(error) == 'memory_changed'
                ):
                    raise MemoryChangedError(
                        'memory changed; submit a new turn'
                    ) from error
                raise

    def _separate_memory_extraction(self, request, snapshot):
        worker = self.automatic_memory_worker
        return (
            self.automatic_memory_extractor is not None
            and worker is not None and not worker.closed
            and accepts_memory_context(self.provider)
            and automatic_request(request, snapshot)
        )

    def _thanks_after_answer(self, request, snapshot, token):
        """A bare thank-you after a settled voice answer keeps the session open."""
        return thanks_after_answer(self.conversation_store, request, snapshot, token)

    def _handle_uncached(
        self,
        request: AgentRequest,
        effective_request: AgentRequest,
        conversation_turns: Sequence[ConversationTurn],
        conversation_summary: ConversationSummary | None,
        token: BeginTurnToken,
        proposal_verifier: Callable[
            [AgentDecision], SafetyResult | None
        ] | None,
        server_clarification: ServerClarification | None,
        memory_snapshot,
        trace: TurnWaterfallTrace | None = None,
    ) -> OrchestrationResult:
        """Call one provider without holding a SQLite transaction."""
        effective_value = effective_request.to_dict()
        effective_value['available_tools'] = (
            self.capability_registry.effective_names(
                effective_request.available_tools
            )
        )
        if self.weather_executor is None:
            effective_value['available_tools'] = [
                name for name in effective_value['available_tools'] if name != 'get_weather'
            ]
        if self.weather_location_executor is None:
            effective_value['available_tools'] = [
                name for name in effective_value['available_tools']
                if name != 'set_weather_location'
            ]
        safety_request = copy_request(effective_request, effective_value)
        model_request = copy_request(effective_request, effective_value)
        local_memory = self.personal_memory.local_decision(
            request, memory_snapshot,
        )
        memories = memory_snapshot.memories[:self.memory_limit]
        memory_revision = memory_snapshot.state['revision']
        if local_memory is not None:
            provider_result = ProviderResult(
                decision=local_memory, provider='malbut-memory-policy',
                model='consent-v1', latency_ms=0.0, memory_supported=True,
            )
        elif (server_clarification is None and request.utterance == effective_request.utterance
              and (thanks := self._thanks_after_answer(request, memory_snapshot, token)) is not None):
            provider_result = thanks
        elif server_clarification is None:
            if self.robot_state_source is not None and set(model_request.available_tools) - {
                'get_weather', 'set_weather_location',
            }:
                # Advisory context only; Safety reads again after inference.
                model_request, trusted, _, _ = self._fresh_safety_request(model_request)
                if not trusted:
                    model_request = replace(model_request, robot_state=RobotState())
            tool_specs = self.capability_registry.select_specs(
                model_request.available_tools
            )
            memory_arguments: Dict[str, Any] = {}
            if accepts_memory_context(self.provider):
                memory_context = copy.deepcopy(memory_snapshot.context)
                if set(PATROL_QUERY_TOOLS).intersection(model_request.available_tools):
                    from datetime import datetime, timezone

                    memory_context['patrol_query_clock'] = {
                        'now': datetime.now(timezone.utc).isoformat(), 'timezone': 'Asia/Seoul',
                    }
                if self._separate_memory_extraction(request, memory_snapshot):
                    memory_context['mode'] = 'answer_only'
                memory_arguments['memory_context'] = memory_context
            if memory_snapshot.facts_deferred:
                def recall_facts(focus, *, previous_name_id=None):
                    return self.personal_memory.recall_deferred_facts(
                        token, memory_snapshot, focus, self.memory_limit, previous_name_id=previous_name_id,
                    )
                memory_arguments['fact_lookup'] = recall_facts
            allow_offline_fallback = (
                self.offline_safety_fallback_enabled
                and isinstance(model_request, SpeechAgentRequest)
                and not model_request.mapping_name_requested
                and memory_snapshot.pending is None
                and not memory_snapshot.source.get('settings_edit')
                and not explicit_memory_management(request.utterance)
            )
            use_stop_budget = (
                allow_offline_fallback
                and 'cancel_voice_mission' in model_request.available_tools
                and not model_request.navigation_confirmation
                and is_urgent_stop_utterance(model_request.utterance)
            )
            budget_ctx = (
                offline_stop_budget_scope(self.offline_stop_timeout_seconds)
                if use_stop_budget
                else nullcontext()
            )
            with (trace.measure('provider_ms') if trace is not None else nullcontext()), budget_ctx:
                try:
                    provider_result = self.provider.complete(
                        model_request,
                        memories,
                        copy.deepcopy(list(conversation_turns)),
                        tool_specs,
                        conversation_summary=copy.deepcopy(
                            conversation_summary
                        ),
                        **memory_arguments,
                    )
                except (ProviderError, TimeoutError, OSError):
                    if not allow_offline_fallback:
                        raise
                    offline_result = resolve_offline_speech_fallback(
                        model_request,
                        model_request.available_tools,
                    )
                    if offline_result is None:
                        raise
                    provider_result = offline_result
                    if trace is not None:
                        trace.set_flag('offline_fallback', True)
                else:
                    if allow_offline_fallback and is_provider_outage_result(provider_result):
                        offline_result = resolve_offline_speech_fallback(
                            model_request,
                            model_request.available_tools,
                        )
                        if offline_result is not None:
                            provider_result = replace(
                                offline_result,
                                latency_ms=provider_result.latency_ms,
                            )
                            if trace is not None:
                                trace.set_flag('offline_fallback', True)
            if memory_snapshot.facts_deferred:
                memories = memory_snapshot.memories
        else:
            memories = []
            provider_result = ProviderResult(
                decision=server_clarification.to_decision(),
                provider='malbut-server-policy',
                model=server_clarification.policy_revision,
                latency_ms=0.0,
                input_chars=0,
            )
        try:
            provider_result.validate()
        except (ValidationError, TypeError) as error:
            raise ProviderError(
                'provider returned invalid metadata'
            ) from error
        if trace is not None:
            trace.attach_provider_result(provider_result)
        if self._separate_memory_extraction(request, memory_snapshot):
            # No foreground proposal can bypass the C extraction path,
            # even when an injected/older provider ignores the mode hint.
            provider_result.memory_proposal = None
        provider_decision = provider_result.decision
        raw_decision = AgentDecision(
            type=provider_decision.type,
            message=provider_decision.message,
            tool_name=provider_decision.tool_name,
            arguments=copy.deepcopy(provider_decision.arguments),
            reason=provider_decision.reason,
            confidence=provider_decision.confidence,
            expires_in_ms=provider_decision.expires_in_ms,
            clarification_for=provider_decision.clarification_for,
        )
        if raw_decision.type == 'tool_call' and (
            provider_result.memory_proposal is not None
            or (explicit_memory_management(request.utterance)
                and raw_decision.tool_name != 'set_weather_location')
        ):
            raw_decision = memory_reply(
                '로봇 실행과 기억 관리 중 먼저 처리할 요청을 말씀해 주세요.',
                clarification=True,
            )
            provider_result.memory_proposal = None
        provider_result.decision = raw_decision
        if (
            self.memory_store.policy_state(request.user_id)
            != memory_snapshot.state
        ):
            raise MemoryChangedError(
                'memory changed during model inference; retry the request'
            )
        try:
            raw_decision.validate()
        except (ValidationError, TypeError) as error:
            raise ProviderError(
                'provider returned an invalid decision'
            ) from error
        with (trace.measure('safety_ms') if trace is not None else nullcontext()):
            early_rejection = (
                proposal_verifier(copy.deepcopy(raw_decision))
                if proposal_verifier is not None
                else None
            )
            if early_rejection is not None:
                if not isinstance(early_rejection, SafetyResult):
                    raise TypeError(
                        'proposal_verifier must return SafetyResult or None'
                    )
                if early_rejection.allowed is not False:
                    raise ValueError(
                        'proposal_verifier may only return a rejection'
                    )
                state_trusted = False
                state_evidence_id = None
                state_observed_at = None
                safety = early_rejection
            else:
                if server_clarification is None and raw_decision.tool_name not in {
                    'get_weather', 'set_weather_location',
                }:
                    (
                        safety_request,
                        state_trusted,
                        state_evidence_id,
                        state_observed_at,
                    ) = self._fresh_safety_request(safety_request)
                else:
                    state_trusted = False
                    state_evidence_id = None
                    state_observed_at = None
                safety = self.safety_policy.evaluate(
                    safety_request,
                    raw_decision,
                    state_trusted=state_trusted,
                )
        decision = raw_decision
        if not safety.allowed:
            decision = AgentDecision(
                type=('clarification' if safety.code == 'navigation_confirmation_required'
                      else 'refusal'),
                message=safety.reason,
                reason=f'safety:{safety.code}',
                confidence=1.0,
                expires_in_ms=raw_decision.expires_in_ms,
            )
        elif raw_decision.type == 'tool_call' and raw_decision.tool_name in {
            'get_weather', 'set_weather_location',
        }:
            with (trace.measure('tool_followup_ms') if trace is not None else nullcontext()):
                provider_result = self._answer_weather(
                    model_request, memories, conversation_turns, conversation_summary, provider_result,
                    memory_context={
                        'mode': 'answer_only',
                        'enabled': memory_snapshot.state['enabled'],
                        'response_settings': memory_snapshot.context['response_settings'],
                    },
                )
            decision = provider_result.decision
        elif raw_decision.type == 'tool_call' and safety.code == 'manager_request':
            # Persist the proposal as a request, never a model-invented completion.
            decision = replace(raw_decision, message=(
                '전면 작업의 취소를 요청할게요.'
                if raw_decision.tool_name == 'cancel_voice_mission'
                else 'Manager에 작업 실행을 요청할게요.'
            ))
        elif raw_decision.type == 'tool_call' and raw_decision.tool_name in PATROL_QUERY_TOOLS:
            from malbut_agent_server.patrol_query import answer_patrol

            with (trace.measure('tool_followup_ms') if trace is not None else nullcontext()):
                provider_result = answer_patrol(
                    self, model_request, memories, conversation_turns, conversation_summary,
                    provider_result, memory_context={
                        'mode': 'answer_only', 'enabled': memory_snapshot.state['enabled'],
                        'response_settings': memory_snapshot.context['response_settings'],
                    })
            decision = provider_result.decision
        elif raw_decision.type == 'tool_call' and raw_decision.tool_name in HOMECAM_QUERY_TOOLS:
            with (trace.measure('tool_followup_ms') if trace is not None else nullcontext()):
                provider_result = self._answer_homecam(
                    model_request, memories, conversation_turns, conversation_summary,
                    provider_result,
                    memory_context={
                        'mode': 'answer_only',
                        'enabled': memory_snapshot.state['enabled'],
                        'response_settings': memory_snapshot.context['response_settings'],
                    },
                )
            decision = provider_result.decision
        if trace is not None:
            trace.attach_provider_result(provider_result)
            trace.set_metadata(
                decision_type=decision.type,
                tool_name=raw_decision.tool_name or decision.tool_name,
                safety_code=safety.code,
            )
        issued_at = float(self._state_clock())
        expires_at = (
            issued_at + decision.expires_in_ms / 1000.0
        )
        memory_expirations = [
            memory.expires_at
            for memory in memories
            if memory.expires_at is not None
        ]
        if memory_expirations:
            expires_at = min(
                expires_at,
                min(memory_expirations),
            )
        return OrchestrationResult(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            turn_id=request.turn_id,
            conversation_generation=token.generation,
            conversation_revision=token.revision + 1,
            conversation_ordinal=token.ordinal,
            raw_decision=raw_decision,
            decision=decision,
            safety=safety,
            provider_result=provider_result,
            memory_ids=[memory.id for memory in memories],
            decision_id=str(uuid.uuid4()),
            issued_at=issued_at,
            expires_at=expires_at,
            state_trusted=state_trusted,
            memory_revision=memory_revision,
            story_binding=(StoryReplyBinding.from_dict(binding)
                           if (self.story_provider is not None
                               and (binding := self.story_provider.reply_binding(
                                   request.user_id, request.request_id)) is not None)
                           else None),
            state_evidence_id=state_evidence_id,
            state_observed_at=state_observed_at,
            safety_policy_revision=(
                self.safety_policy.policy_revision
                if state_evidence_id is not None
                else None
            ),
            clock=self._state_clock,
        )

    def _answer_homecam(self, request, memories, turns, summary, first_result, *, memory_context):
        """Read cloud metadata directly, then answer with no executable Tools."""
        return answer_homecam(
            self.provider,
            getattr(self, 'homecam_executor', None),
            request,
            memories,
            turns,
            summary,
            first_result,
            memory_context=memory_context,
        )

    def _answer_weather(
        self, request, memories, conversation_turns, conversation_summary, first_result,
        *, memory_context=None,
    ) -> ProviderResult:
        """Execute one Manager read after the model's Tool choice, then answer without Tools."""
        return answer_weather(
            self.provider,
            self.weather_executor,
            self.weather_location_executor,
            request,
            memories,
            conversation_turns,
            conversation_summary,
            first_result,
            memory_context=memory_context,
            weather_context_checker=accepts_weather_context,
        )

    @staticmethod
    def _effective_request(
        request: AgentRequest,
        conversation_turns: Sequence[ConversationTurn],
        token: BeginTurnToken,
        utterance_resolver: Callable[
            [AgentRequest, Sequence[ConversationTurn], BeginTurnToken],
            str | None,
        ] | None,
    ) -> AgentRequest:
        """Resolve one server-owned utterance without changing authority."""
        return resolve_effective_request(
            request, conversation_turns, token, utterance_resolver,
        )

    def _fresh_safety_request(
        self,
        request: AgentRequest,
    ) -> tuple[AgentRequest, bool, str | None, float | None]:
        """Read independent server-owned evidence, or preserve legacy mode."""
        return fresh_safety_request(
            self.robot_state_source,
            self.trusted_robot_state,
            self.robot_state_max_age_seconds,
            self._state_clock,
            request,
        )

    @staticmethod
    def _request_fingerprint(request: AgentRequest) -> str:
        return request_fingerprint(request)
