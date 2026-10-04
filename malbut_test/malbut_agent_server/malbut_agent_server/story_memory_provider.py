"""Attach consented story context to dialogue without granting tool authority."""

from collections import OrderedDict
import copy
import hashlib
import json
import threading

from malbut_agent_server.providers.base import (
    accepts_memory_context, accepts_weather_context,
)
from malbut_agent_server.prompting import MAX_CONVERSATION_CONTEXT_CHARS
from malbut_agent_server.schemas import ValidationError


class StoryMemoryProvider:
    """Wrap only foreground dialogue; extraction and robot approval stay separate.

    The service validates policy before network submission and after inference.
    Console delivery and delayed voice playback repeat that validation. A small
    in-memory receipt cache contains revisions only, never recalled text.
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

    def validate_reply(self, user_id, request_id):
        revision = self.reply_revision(user_id, request_id)
        if revision is None:
            raise ValidationError('memory_changed')
        self._validate(user_id, revision, self.reply_readset(user_id, request_id), request_id)

    def _validate(self, user_id, revision, readset=(), request_id=None):
        try:
            valid = self.service.validate(user_id, revision)
            if valid is not False and readset:
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
        # Fact extraction has its own source-only contract. This wrapper is
        # installed only around the foreground provider, but fail closed if a
        # future caller routes an extraction request through it.
        mode = context.get('mode')
        story_context = None
        if self.supports_memory and mode not in {'source_review', 'extract_only'}:
            story_context = self.service.context(
                request.user_id, request.utterance, conversation_turns,
            )
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
                readset.append({'story_id': story['story_id'], 'entry_hashes': hashes})
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
        result = self.provider.complete(
            request, memories, conversation_turns, tools,
            conversation_summary=conversation_summary, **arguments,
        )
        if revision is not None:
            self._validate(request.user_id, revision, readset, request.request_id)
            with self._lock:
                self._revisions[(request.user_id, request.request_id)] = (revision, readset)
                self._revisions.move_to_end((request.user_id, request.request_id))
                while len(self._revisions) > 256:
                    self._revisions.popitem(last=False)
        return result
