"""Attach consented story context to dialogue without granting tool authority."""

from collections import OrderedDict
import copy
import hashlib
import json
import threading
from malbut_agent_server.memory_trace import observe_inputs

from malbut_agent_server.providers.base import (
    AgentProvider, accepts_memory_context, accepts_weather_context,
)
from malbut_agent_server.prompting import MAX_CONVERSATION_CONTEXT_CHARS
from malbut_agent_server.schemas import ValidationError


class StoryMemoryProvider(AgentProvider):
    """Wrap only foreground dialogue; extraction and robot approval stay separate.

    The service validates policy before network submission and after inference.
    The common runtime persists the resulting binding and repeats validation
    on cache replay and delayed delivery. This cache never contains recalled text.
    """

    def __init__(self, provider, service):
        self.provider = provider
        self.service = service
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
        conversation_summary=None, *, memory_context=None, weather_context=None,
    ):
        context = copy.deepcopy(memory_context or {})

        def trace(event, data):
            writer = getattr(self.service, 'trace', None)
            if writer is not None:
                try:
                    writer(request, event, data)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning('Memory trace unavailable')
        # Fact extraction has its own source-only contract. This wrapper is
        # installed only around the foreground provider, but fail closed if a
        # future caller routes an extraction request through it.
        mode = context.get('mode')
        story_context = None
        if self.supports_memory and mode not in {'source_review', 'extract_only'}:
            story_context = self.service.context(
                request.user_id, request.utterance, conversation_turns,
            )
        trace('memory_retrieved', {
            'fact_ids': [item.id for item in memories],
            'stories': [{'story_id': item['story_id'], 'version': item.get('version')}
                        for item in (story_context or {}).get('stories', [])],
            'story_enabled': bool((story_context or {}).get('enabled')),
            'fact_enabled': bool(context.get('enabled')),
            'status': (story_context or {}).get('status',
                'ready' if (story_context or {}).get('enabled') else 'disabled'),
            'pending': (story_context or {}).get('pending', 0),
            'failed': (story_context or {}).get('failed', 0),
        })
        revision = None
        readset = []
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
        with observe_inputs(trace):
            try:
                result = self.provider.complete(
                    request, memories, conversation_turns, tools,
                    conversation_summary=conversation_summary, **arguments,
                )
            except Exception as error:
                trace('provider_failed', {'error_type': type(error).__name__})
                raise
        trace('provider_completed', {'provider': getattr(result, 'provider', None),
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
                self._revisions[(request.user_id, request.request_id)] = (revision, readset)
                self._revisions.move_to_end((request.user_id, request.request_id))
                while len(self._revisions) > 256:
                    self._revisions.popitem(last=False)
        return result
