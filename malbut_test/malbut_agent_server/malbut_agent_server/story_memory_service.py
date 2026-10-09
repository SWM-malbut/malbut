"""Durable, debounced story extraction with no foreground model wait."""

import hashlib
import json
import math
import re
import threading
import time

from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.story_extractor import StoryExtractionError, validate_updates
from malbut_agent_server.story_memory import StoryMemoryError
from malbut_agent_server.story_runtime_store import StoryRuntimeStore


class StoryServiceError(RuntimeError):
    """A story operation cannot currently be completed."""

    default_code = 'story_error'

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code or self.default_code


class StoryUnavailableError(StoryServiceError):
    """No authorized extractor has been configured for this runtime."""

    default_code = 'story_unavailable'


class StoryMemoryService:
    """One worker serializes durable per-user work; callers keep talking."""

    def __init__(self, conversation_store, memory_store, extractor, *,
                 debounce_seconds=2.0, max_delay_seconds=10.0,
                 batch_size=3, runtime_store=None, autostart=True):
        if type(autostart) is not bool:
            raise ValueError('autostart must be a bool')
        for name, value in (('debounce_seconds', debounce_seconds),
                            ('max_delay_seconds', max_delay_seconds)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError(name + ' must be a nonnegative finite number')
        if max_delay_seconds < debounce_seconds:
            raise ValueError('max_delay_seconds must be >= debounce_seconds')
        if type(batch_size) is not int or not 1 <= batch_size <= 3:
            raise ValueError('batch_size must be an integer between 1 and 3')
        self.store = runtime_store or StoryRuntimeStore(conversation_store, memory_store)
        self.extractor = extractor
        self.debounce_seconds = float(debounce_seconds)
        self.max_delay_seconds = float(max_delay_seconds)
        self.batch_size = batch_size
        self._condition = threading.Condition()
        self._due = {}
        self._forced = set()
        self._errors = {}
        self._last_recovery = {}
        self._stop = threading.Event()
        self._thread = None
        self._claimed_job = None
        self._started = autostart
        # Queue state is in SQLite. Recovery only starts work already permitted
        # by a persisted policy; constructing a service never grants consent.
        for user in self.store.pending_users():
            self.store.recover(user)
            self._schedule(user, immediate=True)
        if self.extractor is not None and self._due:
            self._start()

    def _open(self):
        if self._stop.is_set():
            raise StoryServiceError('기억 서비스가 종료되었습니다.')

    def _start(self):
        with self._condition:
            if self._stop.is_set() or self.extractor is None or not self._started:
                return
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name='malbut-story-memory', daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()

    def start(self):
        """Start permitted recovery only when the owning runtime is serving."""
        with self._condition:
            if self._stop.is_set():
                return
            self._started = True
        if self._due or self.store.pending_users():
            self._start()

    def _schedule(self, user, *, immediate=False):
        now = time.monotonic()
        with self._condition:
            first, _last = self._due.get(user, (now, now))
            if immediate:
                first -= self.max_delay_seconds
            self._due[user] = (first, now)
            self._condition.notify_all()

    def policy(self, user):
        stats = self.store.stats(user)
        policy = dict(stats.get('policy') or self.store.policy(user))
        for key in ('queued', 'running', 'failed'):
            policy[key] = int(stats.get(key, 0))
        policy['pending'] = policy['queued'] + policy['running']
        policy['available'] = self.extractor is not None and not self._stop.is_set()
        policy['error'] = self._errors.get(user) or stats.get('last_error') or stats.get('error')
        return policy

    def enable(self, user, include_history=False, history_scope=None, expected_revision=None):
        self._open()
        if self.extractor is None:
            raise StoryUnavailableError('기억을 정리할 AI가 연결되지 않아 기능을 켤 수 없습니다.')
        if type(include_history) is not bool:
            raise ValueError('include_history must be a bool')
        # This method is invoked only after the caller's explicit consent UI,
        # including disclosure of the configured external AI and input scope.
        self.store.set_enabled(user, True, external_consent=True,
                               include_history=include_history, history_scope=history_scope,
                               expected_revision=expected_revision)
        self.store.recover(user)
        self._errors.pop(user, None)
        self._schedule(user)
        self._start()
        return self.policy(user)

    def disable(self, user, expected_revision=None):
        self._open()
        self.store.set_enabled(user, False, expected_revision=expected_revision)
        with self._condition:
            self._due.pop(user, None)
            self._forced.discard(user)
            self._condition.notify_all()
        return self.policy(user)

    def history_preview(self, user):
        return self.store.history_preview(user)

    def after_turn(self, user, requestid):
        self._open()
        queued = self.store.enqueue_completed(user, requestid)
        if queued:
            self._schedule(user)
            self._start()
        return queued

    def note_enqueue_failure(self, user):
        """Expose a recoverable post-commit queue gap without losing the reply."""
        self._errors[user] = 'story_enqueue_failed'

    def list_stories(self, user):
        return self.store.list_stories(user)

    def evidence(self, user, story_id):
        return self.store.evidence(user, story_id)

    def validate(self, user, revision, data_revision=None):
        return not self._stop.is_set() and self.store.validate(user, revision, data_revision)

    def reply_dependency_revision(self, user, request_id):
        return self.store.reply_dependency_revision(user, request_id)

    def validate_reply_binding(self, user, revision, readset=(), request_id=None):
        """Short-context lineage remains valid when long-term reuse is off."""
        if self._stop.is_set() or type(revision) is not int:
            return False
        if readset:
            return self.validate_readset(user, revision, readset, request_id=request_id)
        return self.store.policy(user)['revision'] == revision

    @staticmethod
    def _entry_hash(entry):
        value = {key: entry[key] for key in ('text', 'kind', 'actor', 'status')}
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def validate_readset(self, user, policy_revision, readset, request_id=None):
        """Fence corrections to the claims actually read, not unrelated writes."""
        if (not isinstance(readset, (list, tuple)) or len(readset) > 20
                or not self.validate(user, policy_revision)):
            return False
        required = {}
        for item in readset:
            if (not isinstance(item, dict) or set(item) != {'story_id', 'entry_hashes'}
                    or not isinstance(item['story_id'], str)
                    or item['story_id'] in required
                    or not isinstance(item['entry_hashes'], (list, tuple))
                    or not 1 <= len(item['entry_hashes']) <= 64
                    or any(not isinstance(value, str)
                           or re.fullmatch(r'[0-9a-f]{64}', value) is None
                           for value in item['entry_hashes'])):
                return False
            required[item['story_id']] = set(item['entry_hashes'])
        if required:
            # list_stories reads valid source-backed stories in one snapshot.
            existing = {story['story_id']: {self._entry_hash(entry)
                                           for entry in story.get('current', [])}
                        for story in self.store.list_stories(user)
                        if story['story_id'] in required}
            for identifier, hashes in required.items():
                if hashes <= existing.get(identifier, set()):
                    continue
                # A completed reply can legitimately rephrase current context
                # while its own speech is still playing. Only that exact reply
                # may relax the claim guard; source and policy guards remain.
                if (identifier not in existing or not isinstance(request_id, str)
                        or not self.store.own_reply_updated_story(user, identifier, request_id)):
                    return False
        return self.validate(user, policy_revision)

    def record_reply(self, user, request_id, story_ids, revision):
        return self.store.record_reply(user, request_id, story_ids, revision)

    def record_context_reply(self, user, request_id, conversation_turns, conversation_summary):
        return self.store.record_context_reply(user, request_id, conversation_turns,
                                                conversation_summary)

    @staticmethod
    def _context_story(story):
        return {
            key: value for key, value in {
                'story_id': story['story_id'], 'title': story['title'],
                'aliases': story.get('aliases', []), 'version': story.get('version'),
                'updated_at': story.get('updated_at'),
                'current': [{key: item[key] for key in
                             ('text', 'kind', 'actor', 'status') if key in item}
                            for item in story.get('current', [])],
            }.items() if value is not None
        }

    def context(self, user, utterance, recent_turns=()):
        self._open()
        policy = self.store.policy(user)
        result = {'revision': policy['revision'], 'data_revision': policy['data_revision'],
                  'enabled': bool(policy['enabled'] and policy['external_consent']),
                  'stories': [], 'evidence': [], 'readset': [],
                  'pending': 0, 'untrusted': True, 'execution_authorized': False}
        if not result['enabled']:
            return result
        # Repair any enqueue gap after a completed turn or process crash.
        if self.store.recover(user):
            self._schedule(user)
            self._start()
        stats = self.store.stats(user)
        result['pending'] = int(stats.get('queued', 0)) + int(stats.get('running', 0))
        result['failed'] = int(stats.get('failed', 0))
        if result['pending'] or result['failed']:
            # A queued correction can invalidate the meaning of an old summary
            # even before extraction completes. Do not present it as current.
            result['status'] = 'updating' if result['pending'] else 'update_failed'
            return result
        stories = self.store.candidates(user, utterance, limit=3)
        # A short deictic follow-up may use the latest two turns to identify the
        # topic. This affects local matching, never transmits an entire archive.
        if not stories and re.search(r'그(?:거|것|때| 이야기)|이어서|계속|지난번', utterance):
            recent = list(recent_turns)[-2:]
            text = ' '.join(getattr(turn, 'user_content', '') for turn in recent)
            if text:
                stories = self.store.candidates(user, utterance + ' ' + text[-3000:], limit=3)
        result['stories'] = [self._context_story(story) for story in stories]
        result['readset'] = [
            {'story_id': story['story_id'],
             'entry_hashes': sorted({self._entry_hash(entry) for entry in story['current']})}
            for story in result['stories']
        ]
        if re.search(r'원문|정확|그대로|왜|이유|어떻게.*(?:정|결정)', utterance):
            remaining = 6000
            for story in stories:
                for source in self.store.evidence(user, story['story_id']):
                    text = source.get('text', '')
                    if not isinstance(text, str) or not text or len(text) > remaining:
                        continue
                    ref = source.get('ref', {})
                    result['evidence'].append({
                        'story_id': story['story_id'], 'text': text,
                        'role': source.get('role', ref.get('role')),
                        'created_at': source.get('created_at'),
                        'completed_at': source.get('completed_at'),
                    })
                    remaining -= len(text)
                    if remaining <= 0:
                        break
        stats = self.store.stats(user)
        result['pending'] = int(stats.get('queued', 0)) + int(stats.get('running', 0))
        result['failed'] = int(stats.get('failed', 0))
        if result['pending'] or result['failed']:
            result['stories'], result['evidence'], result['readset'] = [], [], []
            result['status'] = 'updating' if result['pending'] else 'update_failed'
        if not self.validate_readset(user, policy['revision'], result['readset']):
            # Caller validates again immediately before/after external inference.
            result['stories'], result['evidence'], result['readset'] = [], [], []
        return result

    def forget(self, user, story_id, timeout=45):
        self._open()
        # Pending new speech may paraphrase a story without exact span metadata
        # yet. Do not claim complete erasure until that deletion scope is known.
        unsettled = ('정리되지 않은 대화가 있어 삭제 범위를 확정하지 못했어요. '
                     '/stories sync 후 다시 삭제해 주세요.')
        if not self.flush(user, timeout=timeout):
            raise StoryServiceError(unsettled, code='story_not_settled')
        try:
            result = self.store.delete_story(user, story_id, require_settled=True)
        except StoryMemoryError as exc:
            if str(exc) == 'story_delete_requires_settled_sources':
                raise StoryServiceError(unsettled, code='story_not_settled') from exc
            if str(exc) == 'story_delete_requires_history_review':
                raise StoryServiceError(
                    '이전에 승인했지만 아직 정리되지 않은 원문이 있어 삭제 범위를 확정하지 못했어요. '
                    '/stories history에서 미처리 원문 범위를 확인하고 그 기록 정리에 '
                    '다시 동의한 뒤 삭제해 주세요.', code='history_review_required') from exc
            raise
        with self._condition:
            self._due.pop(user, None)
            self._condition.notify_all()
        return result

    def flush(self, user, timeout=45, retry_failed=False):
        self._open()
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise ValueError('timeout must be a nonnegative finite number')
        if type(retry_failed) is not bool:
            raise ValueError('retry_failed must be a bool')
        if retry_failed:
            self.store.retry_failed(user)
        self.store.recover(user)
        self._schedule(user, immediate=True)
        with self._condition:
            self._forced.add(user)
        self._start()
        deadline = time.monotonic() + timeout
        try:
            while not self._stop.is_set():
                stats = self.store.stats(user)
                if not stats.get('queued', 0) and not stats.get('running', 0):
                    if self.store.recover(user):
                        continue
                    return not stats.get('failed', 0)
                if self.extractor is None or time.monotonic() >= deadline:
                    return False
                with self._condition:
                    self._condition.wait(min(0.1, max(0, deadline - time.monotonic())))
            return False
        finally:
            with self._condition:
                self._forced.discard(user)

    def _run(self):
        while not self._stop.is_set():
            try:
                users = self.store.pending_users()
                now = time.monotonic()
                eligible = []
                for user in users:
                    if now - self._last_recovery.get(user, -1e9) >= 1:
                        self.store.recover(user)
                        self._last_recovery[user] = now
                    stats = self.store.stats(user)
                    with self._condition:
                        if not stats.get('queued', 0) and not stats.get('running', 0):
                            self._due.pop(user, None)
                            continue
                        first, last = self._due.setdefault(user, (now, now))
                        due = min(last + self.debounce_seconds, first + self.max_delay_seconds)
                        if user in self._forced or now >= due:
                            eligible.append((due, user))
                with self._condition:
                    if not eligible:
                        self._condition.wait(0.2)
                        continue
                job = None
                for _due, user in sorted(eligible):
                    job = self.store.claim(user, batch_size=self.batch_size)
                    if job is not None:
                        break
                if job is None:
                    with self._condition:
                        self._condition.wait(0.1)
                    continue
                self._claimed_job = job
                self._process(job)
                if not self._stop.is_set():
                    self._claimed_job = None
                stats = self.store.stats(user)
                with self._condition:
                    # Keep an existing batch eligible until the durable queue
                    # drains; subsequent new speech updates its debounce time.
                    if not stats.get('queued', 0) and not stats.get('running', 0):
                        self._due.pop(user, None)
                    self._condition.notify_all()
            except Exception:
                # Worker failures never leak raw dialogues, provider error
                # bodies, or keys, and never spin a tight retry loop.
                with self._condition:
                    self._condition.wait(0.5)

    def _process(self, job):
        user = job['user_id']
        try:
            sources = self.store.job_source(job)
            if sources is None or self._stop.is_set():
                if not self._stop.is_set():
                    self.store.fail(job, 'source_or_policy_changed')
                return
            updates = self.extractor.extract(job['stories'], sources)
            if self._stop.is_set():
                return
            # Re-check even injected extractors. The runtime store independently
            # checks the exact quotes, source revisions, consent and story CAS.
            updates = validate_updates(job['stories'], sources, updates)
            if self.store.job_source(job) is None:
                self.store.fail(job, 'source_or_policy_changed')
            elif self.store.commit(job, updates):
                self._errors.pop(user, None)
            else:
                self.store.fail(job, 'checkpoint_changed')
        except Exception as exc:
            reason = ('invalid_extraction' if isinstance(exc, StoryExtractionError)
                      else 'provider_error' if isinstance(exc, ProviderError)
                      else 'story_processing_error')
            self._errors[user] = reason
            if not self._stop.is_set():
                self.store.fail(job, reason)

    def close(self):
        with self._condition:
            if self._stop.is_set():
                return
            self._stop.set()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=45)
        if thread is not None and not thread.is_alive() and self._claimed_job is not None:
            self.store.release_claim(self._claimed_job)
            self._claimed_job = None
        # An uncooperative remote call may finish later. _process checks stop
        # before touching storage again; its durable claim can recover later.
        self.store.close()
