"""Attach consented story context to dialogue without granting tool authority."""

from collections import OrderedDict
from dataclasses import replace
import copy
import hashlib
import json
import threading
from malbut_agent_server.conversation_recall import (
    event_scoped_attribute, named_story_read_only, previous_self_name_id, self_name_lookup,
    speech_recall_focus, speech_recall_prohibited,
)
from malbut_agent_server.memory_trace import observe_inputs
from malbut_agent_server.personal_memory import requested_operation, self_name_correction

from malbut_agent_server.providers.base import (
    AgentProvider, ProviderError, accepts_memory_context, accepts_weather_context,
)
from malbut_agent_server.prompting import MAX_CONVERSATION_CONTEXT_CHARS
from malbut_agent_server.schemas import ProviderUsage, SpeechAgentRequest, ValidationError
from malbut_agent_server.tools import PERSONAL_RECALL_TOOL, STORY_RECALL_TOOL


class StoryMemoryProvider(AgentProvider):
    """Wrap only foreground dialogue; extraction and robot approval stay separate.

    The service validates policy before network submission and after inference.
    The common runtime persists the resulting binding and repeats validation
    on cache replay and delayed delivery. This cache never contains recalled text.
    """

    def __init__(self, provider, service, *, on_demand_speech=False):
        self.provider = provider
        self.service = service
        self.on_demand_speech = on_demand_speech
        self._revisions = OrderedDict()
        self._lock = threading.RLock()

    def __getattr__(self, name):
        return getattr(self.provider, name)

    @property
    def supports_memory(self):
        return accepts_memory_context(self.provider)

    def reply_revision(self, user_id, request_id):
        with self._lock:
            receipt = self._revisions.get((user_id, request_id))
            return receipt[0] if receipt else None

    def reply_readset(self, user_id, request_id):
        with self._lock:
            receipt = self._revisions.get((user_id, request_id))
            return copy.deepcopy(receipt[1]) if receipt else []

    def direct_story_read_version(self, user_id, request_id):
        with self._lock:
            receipt = self._revisions.get((user_id, request_id))
            return receipt[2] if receipt else None

    def reply_binding(self, user_id, request_id):
        with self._lock:
            receipt = self._revisions.get((user_id, request_id))
            return ({'policy_revision': receipt[0], 'readset': copy.deepcopy(receipt[1])}
                    if receipt else None)

    def validate_reply(self, user_id, request_id):
        revision = self.reply_revision(user_id, request_id)
        if revision is None:
            raise ValidationError('memory_changed')
        self._validate(user_id, revision, self.reply_readset(user_id, request_id), request_id)

    def _validate(self, user_id, revision, readset=(), request_id=None):
        try:
            if hasattr(self.service, 'validate_reply_binding'):
                valid = self.service.validate_reply_binding(
                    user_id, revision, readset, request_id=request_id)
            else:
                valid = self.service.validate(user_id, revision)
            if valid is not False and readset and not hasattr(self.service, 'validate_reply_binding'):
                valid = self.service.validate_readset(
                    user_id, revision, readset, request_id=request_id,
                )
        except (ValueError, RuntimeError) as error:
            raise ValidationError('memory_changed') from error
        if valid is False:
            raise ValidationError('memory_changed')

    def complete(
        self, request, memories, conversation_turns, tools,
        conversation_summary=None, *, memory_context=None, weather_context=None, fact_lookup=None,
    ):
        context = memory_context or {}
        if context.get('pet_name_evidence_only'):
            return self._answer_recall(request, memories, conversation_turns, None, context,
                                       None, None, 'pet')
        if context.get('self_name_evidence_only'):
            return self._answer_recall(request, memories, conversation_turns, None, context,
                                       None, None, 'name')
        deferred = (self.on_demand_speech and isinstance(request, SpeechAgentRequest)
                    and context.get('mode') not in {'source_review', 'extract_only', 'automatic_extraction'})
        recall_tool = PERSONAL_RECALL_TOOL if fact_lookup is not None else STORY_RECALL_TOOL
        phase = None
        revision = None
        first_request, first_tools = request, tools
        restricted_operations = ()
        if deferred:
            revision = self.service.recall_revision(request.user_id)
            # An evidence answer has already selected a read-only function.
            followup = (weather_context is not None or 'patrol_observations_untrusted' in context
                        or 'homecam_query_result_untrusted' in context)
            phase = 'result' if followup else 'deferred'
            if speech_recall_prohibited(request.utterance):
                phase = 'restricted'
                restricted_operations = tuple(op for op in ('remember', 'correct', 'forget')
                                              if requested_operation(request.utterance, op))
                if not restricted_operations:
                    memory_context = dict(context, mode='answer_only')
            elif (not followup and context.get('enabled') and not context.get('pending_question')
                  and self_name_correction(request.utterance)):
                # The correction already identifies its slot. No lookup or
                # robot actuation belongs to this one foreground decision.
                first_tools = []
                first_request = replace(request, available_tools=())
            elif (revision is not None or fact_lookup is not None) and not followup:
                if not context.get('pending_question') and (not context.get('enabled') or fact_lookup):
                    focus = speech_recall_focus(request.utterance, conversation_turns, conversation_summary)
                    previous_name_id = None
                    if focus is None and fact_lookup is not None:
                        previous_name_id = previous_self_name_id(
                            request.utterance, conversation_turns, conversation_summary)
                        if previous_name_id is not None:
                            focus = 'name'
                    if focus is not None:
                        answer = self._answer_recall(
                            request, memories, conversation_turns, conversation_summary, context,
                            revision, fact_lookup, focus, previous_name_id=previous_name_id,
                            direct_story_read=focus in {'related', 'story_repeat'})
                        if answer is not None:
                            return answer
                first_tools = [*tools, recall_tool]
                first_request = replace(
                    request, available_tools=(*request.available_tools, recall_tool.name),
                )
        first = self._complete(
            first_request, memories, conversation_turns, first_tools, conversation_summary,
            memory_context, weather_context, phase, revision,
        )
        if (phase == 'restricted' and first.memory_proposal is not None
                and first.memory_proposal.get('operation') not in restricted_operations):
            raise ProviderError('unexpected memory operation on a restricted turn')
        name = getattr(getattr(first, 'decision', None), 'tool_name', None)
        if name not in (STORY_RECALL_TOOL.name, PERSONAL_RECALL_TOOL.name):
            return first
        first.validate()
        if (phase != 'deferred' or name != recall_tool.name or first.decision.type != 'tool_call'
                or (revision is None and fact_lookup is None)):
            raise ProviderError('invalid story lookup request')
        arguments = first.decision.arguments
        if fact_lookup is None:
            if arguments:
                raise ProviderError('invalid story lookup request')
            focus = 'related'
        else:
            if set(arguments) != {'focus'} or arguments['focus'] not in (
                    'related', 'name', 'nickname', 'pet', 'preference'):
                raise ProviderError('invalid personal recall focus')
            focus = arguments['focus']
        answer = self._answer_recall(request, memories, conversation_turns, conversation_summary,
                                     context, revision, fact_lookup, focus)
        usage = {}
        for key in ProviderUsage.__dataclass_fields__:
            a, b = getattr(first.usage, key), getattr(answer.usage, key)
            usage[key] = a + b if a is not None and b is not None else None
        return replace(
            answer, latency_ms=first.latency_ms + answer.latency_ms, usage=ProviderUsage(**usage),
            input_chars=(first.input_chars + answer.input_chars
                         if first.input_chars is not None and answer.input_chars is not None else None),
        )

    def _answer_recall(self, request, memories, conversation_turns, conversation_summary,
                       context, revision, fact_lookup, focus, *, previous_name_id=None, direct_story_read=False):
        fact_guard = None
        answer_phase = 'requested'
        if focus == 'story_repeat' or previous_name_id is not None:
            # A prior assistant answer or its paraphrases may predate a correction.
            # The user's own statements still carry corrections and temporary names.
            conversation_turns = [replace(turn, assistant_content='', response={})
                                  for turn in conversation_turns]
            if focus == 'story_repeat':
                focus = 'related'
        if fact_lookup is not None:
            if previous_name_id is None:
                memories, candidates, fact_guard = fact_lookup(focus)
            else:
                current = fact_lookup(focus, previous_name_id=previous_name_id)
                if current is None:
                    return None
                memories, candidates, fact_guard = current
            context = dict(context, memories=candidates)
            if focus == 'name' and self_name_lookup(request.utterance):
                # A model-selected self-name lookup also uses only owned facts.
                # A lookup miss must not fall through to another person's story.
                conversation_turns, conversation_summary, revision = [], None, None
            if event_scoped_attribute(request.utterance):
                context['event_attribute_evidence'] = (
                    '개인 사실은 보통 쓰는 기본값이며 행사 당시의 값으로 삼을 수 없습니다. '
                    '질문한 바로 그 행사·장소와 값을 연결하는 대화나 이야기 근거로만 당시 값을 답하세요. '
                    '다른 행사 기록이나 기본값만 있으면 당시 값은 확인할 수 없다고 답하세요.'
                )
            elif any(record.kind == focus for record in memories):
                answer_phase = 'facts'
        # Evidence can support an answer, never a new action or memory mutation.
        answer = self._complete(
            replace(request, available_tools=()), memories, conversation_turns, [], conversation_summary,
            dict(context, mode='answer_only'), None, answer_phase, revision, fact_guard,
            direct_story_read=direct_story_read,
        )
        answer.validate()
        if answer.decision.type == 'tool_call' or answer.memory_proposal is not None:
            raise ProviderError('unexpected tool or memory operation after story lookup')
        return answer

    def _complete(self, request, memories, conversation_turns, tools, conversation_summary,
                  memory_context, weather_context, phase, revision, fact_guard=None, *, direct_story_read=False):
        context = copy.deepcopy(memory_context or {})

        def trace(event, data):
            writer = getattr(self.service, 'trace', None)
            if writer is not None:
                try:
                    if phase is not None:
                        data = dict(data, story_lookup=phase)
                    writer(request, event, data)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning('Memory trace unavailable')
        # Fact extraction has its own source-only contract. This wrapper is
        # installed only around the foreground provider, but fail closed if a
        # future caller routes an extraction request through it.
        mode = context.get('mode')
        story_context = None
        if ((phase is None or (phase == 'requested' and revision is not None)) and self.supports_memory
                and mode not in {'source_review', 'extract_only', 'automatic_extraction'}):
            story_context = self.service.context(
                request.user_id, request.utterance, conversation_turns,
            )
            if phase == 'requested' and (
                    story_context['revision'] != revision or not story_context.get('enabled', True)):
                raise ValidationError('memory_changed')
        trace('memory_retrieved', {
            'fact_ids': [item.id for item in memories],
            'stories': [{'story_id': item['story_id'], 'version': item.get('version')}
                        for item in (story_context or {}).get('stories', [])],
            'story_enabled': revision is not None or bool((story_context or {}).get('enabled')),
            'fact_enabled': bool(context.get('enabled')),
            'status': (story_context or {}).get('status',
                'ready' if (story_context or {}).get('enabled') else phase if revision is not None
                else 'disabled'),
            'pending': (story_context or {}).get('pending', 0),
            'failed': (story_context or {}).get('failed', 0),
        })
        readset = []
        read_version = None
        if story_context is not None and story_context.get('enabled', True):
            revision = story_context['revision']
            attachment = {
                key: copy.deepcopy(value) for key, value in story_context.items()
                if key not in {'revision', 'data_revision', 'enabled', 'readset'}
            }
            attachment['execution_authorized'] = False
            # Drop complete lowest-ranked stories rather than slicing claims
            # or their status mid-sentence. The service also limits raw excerpts.
            context['story_memory_untrusted'] = attachment
            while len(json.dumps(context, ensure_ascii=False)) > MAX_CONVERSATION_CONTEXT_CHARS:
                if attachment.get('evidence'):
                    attachment['evidence'] = []
                elif attachment.get('source_excerpts'):
                    attachment['source_excerpts'] = []
                elif attachment.get('pending_sources'):
                    attachment['pending_sources'] = []
                elif attachment.get('stories'):
                    attachment['stories'].pop()
                else:
                    context.pop('story_memory_untrusted', None)
                    break
            attached = attachment.get('stories', [])
            if direct_story_read and phase == 'requested' and named_story_read_only(request.utterance, attached):
                read_version = attached[0].get('version')
            story_ids = [item['story_id'] for item in attachment.get('stories', [])]
            for story in attachment.get('stories', []):
                hashes = []
                for entry in story.get('current', []):
                    value = {key: entry[key] for key in ('text', 'kind', 'actor', 'status')}
                    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':'))
                    hashes.append(hashlib.sha256(canonical.encode('utf-8')).hexdigest())
                readset.append({'story_id': story['story_id'], 'entry_hashes': sorted(set(hashes))})
            self._validate(request.user_id, revision, readset, request.request_id)
            if story_ids:
                registered = self.service.record_reply(
                    request.user_id, request.request_id, story_ids, revision,
                )
                if registered is False:
                    raise ValidationError('memory_changed')
        if phase == 'result' and self.reply_revision(request.user_id, request.request_id) is not None:
            self.validate_reply(request.user_id, request.request_id)
        arguments = {}
        if self.supports_memory:
            arguments['memory_context'] = context
        if accepts_weather_context(self.provider):
            arguments['weather_context'] = weather_context
        registered = self.service.record_context_reply(
            request.user_id, request.request_id, conversation_turns,
            conversation_summary,
        )
        if registered is False:
            raise ValidationError('memory_changed')
        if revision is None and hasattr(self.service, 'reply_dependency_revision'):
            revision = self.service.reply_dependency_revision(request.user_id, request.request_id)
        if revision is not None:
            self._validate(request.user_id, revision, readset, request.request_id)
        if fact_guard is not None:
            fact_guard()
        with observe_inputs(trace):
            try:
                result = self.provider.complete(
                    request, memories, conversation_turns, tools,
                    conversation_summary=conversation_summary, **arguments,
                )
            except Exception as error:
                trace('provider_failed', {'error_type': type(error).__name__})
                raise
        if fact_guard is not None:
            fact_guard()
        event = ('story_recall_requested'
                 if phase == 'deferred' and result.decision.tool_name in (STORY_RECALL_TOOL.name, PERSONAL_RECALL_TOOL.name)
                 else 'provider_completed')
        trace(event, {'provider': getattr(result, 'provider', None),
                      'model': getattr(result, 'model', None),
                      'latency_ms': getattr(result, 'latency_ms', None)})
        if revision is not None:
            self._validate(request.user_id, revision, readset, request.request_id)
            with self._lock:
                prior = self._revisions.get((request.user_id, request.request_id))
                if prior is not None:
                    if prior[0] != revision:
                        raise ValidationError('memory_changed')
                    merged = {}
                    for item in [*prior[1], *readset]:
                        merged.setdefault(item['story_id'], set()).update(item['entry_hashes'])
                    readset = [{'story_id': identifier, 'entry_hashes': sorted(hashes)}
                               for identifier, hashes in merged.items()]
                if prior is not None or result.decision.type != 'message' or result.memory_proposal is not None:
                    read_version = None
                self._revisions[(request.user_id, request.request_id)] = (revision, readset, read_version)
                self._revisions.move_to_end((request.user_id, request.request_id))
                while len(self._revisions) > 256:
                    self._revisions.popitem(last=False)
        return result
