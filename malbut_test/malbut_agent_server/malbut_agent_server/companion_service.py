"""App-confirmed, encounter-bound access to expiring daily summaries."""

from collections import deque
from dataclasses import dataclass, field
import json
import math
import threading
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from malbut_agent_server.companion_extractor import trivial_utterance
from malbut_agent_server.companion_memory import CATEGORIES, CompanionMemory, expires_at
from malbut_agent_server.homecam_query import HomecamQueryClient


class CompanionCloudClient(HomecamQueryClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._url = self._url.removesuffix('/agent/operate') + '/companion'

    def sync(self, report):
        from malbut_agent_server.fall_upload_worker import _read_token
        token = _read_token(self._token_file)
        if not token or any(c.isspace() for c in token):
            raise ValueError('invalid device credential')
        request = urllib.request.Request(self._url, data=json.dumps(report).encode(), method='POST',
                                         headers={'Authorization': 'Bearer ' + token,
                                                  'Content-Type': 'application/json'})
        with self._opener.open(request, timeout=5) as response:
            body = response.read(8193)
        if len(body) > 8192:
            raise ValueError('companion response too large')
        return json.loads(body)


@dataclass(frozen=True)
class _MemoryJob:
    binding: tuple
    turn_id: str
    text: str = field(repr=False)
    observed_at: float
    deadline: float


class CompanionService:
    """No identity survives end/restart; stale/offline confirmations grant no access."""

    def __init__(self, memory, client, extractor=None, *, clock=time.monotonic, wall=time.time):
        self.memory, self.client, self.extractor = memory, client, extractor
        self.clock, self.wall = clock, wall
        self.lock, self._sync_lock = threading.RLock(), threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._memory_condition = threading.Condition(self.lock)
        self._memory_thread = None
        self._memory_jobs = deque()
        self._memory_active = None
        self._memory_seen = deque(maxlen=128)
        self.on_change = lambda: None
        self.session = None
        self.owner = None
        self.revision = -1
        self.epoch = 0
        self._verified_until = -math.inf
        self._fresh_until = -math.inf

    def start(self):
        with self.lock:
            if not self._stop.is_set() and self._thread is None:
                self._thread = threading.Thread(target=self._run, name='companion-policy', daemon=True)
                self._thread.start()

    def _poll_delay(self, failures=0):
        if failures:
            return min(30, 3 * 2 ** min(failures - 1, 4))
        with self.lock:
            # Preserve 3 s erasure delivery whenever data exists, including idle.
            # An empty, inactive robot can poll at 6 s within the 10 s heartbeat.
            active = self.session is not None or self._memory_active or self._memory_jobs
            return 3 if active or self.memory.has_summaries() else 6

    def _run(self):
        next_purge = -math.inf
        failures = 0
        while not self._stop.is_set():
            self._wake.clear()
            if self.clock() >= next_purge:
                try:
                    self.memory.purge()
                except Exception:
                    pass
                next_purge = self.clock() + 60
            try:
                self.sync()
                failures = 0
            except Exception:
                failures += 1
            with self.lock:
                self._prune_jobs()
            self._wake.wait(self._poll_delay(failures))

    def _discard_jobs(self):
        self._memory_jobs.clear()
        self._memory_seen.clear()
        self._memory_condition.notify_all()

    def _prune_jobs(self):
        count = len(self._memory_jobs)
        self._memory_jobs = deque(job for job in self._memory_jobs
                                  if self._job_valid(job))
        if len(self._memory_jobs) != count:
            self._memory_condition.notify_all()

    def _job_valid(self, job):
        return (not self._stop.is_set() and self.clock() < job.deadline
                and expires_at(job.observed_at) > self.wall() and self.valid(job.binding))

    def begin(self, session):
        with self.lock:
            if session != self.session:
                self.session = session
                self.epoch += 1
                self._verified_until = -math.inf
                self._fresh_until = -math.inf
                self._discard_jobs()
                self._wake.set()  # Do not wait for an idle/backoff interval to report an encounter.

    def end(self, session=None):
        with self.lock:
            if session is None or session == self.session:
                self.session = None
                self.epoch += 1
                self._verified_until = self._fresh_until = -math.inf
                self._discard_jobs()
                self._wake.set()

    def sync(self):
        try:
            return self._sync_once()
        except Exception:
            with self.lock:
                self._fresh_until = -math.inf
                self.epoch += 1
                self._discard_jobs()
            raise

    def _sync_once(self):
        if self.client is None:
            return
        with self._sync_lock:
            with self.lock:
                if self._stop.is_set():
                    return
                session = self.session
                epoch = self.epoch
                report = {'sessionId': session, 'appliedRevision': max(self.revision, 0)}
            started = self.clock()
            data = self.client.sync(report)
            if not isinstance(data, dict) or set(data) != {
                    'ownerId', 'revision', 'eraseRevision', 'categories', 'externalProcessing',
                    'confirmedSessionId', 'remainingSeconds'}:
                raise ValueError('invalid companion response')
            owner, revision, categories = data['ownerId'], data['revision'], data['categories']
            remaining = data['remainingSeconds']
            if (not isinstance(categories, list) or any(type(c) is not str for c in categories)
                    or not set(categories) <= CATEGORIES
                    or type(remaining) not in (int, float) or not math.isfinite(remaining)
                    or not 0 <= remaining <= 300
                    or data['confirmedSessionId'] is not None
                    and (not isinstance(data['confirmedSessionId'], str)
                         or len(data['confirmedSessionId']) > 128)):
                raise ValueError('invalid companion confirmation')
            with self.lock:
                if self._stop.is_set() or session != self.session or epoch != self.epoch:
                    return
                self.memory.apply_policy(owner, revision, data['eraseRevision'], categories,
                                         data['externalProcessing'])
                changed = (owner, revision) != (self.owner, self.revision)
                if changed:
                    self.epoch += 1
                    self._discard_jobs()
                self.owner, self.revision = owner, revision
                self._fresh_until = started + 10
                confirmed = session and data['confirmedSessionId'] == session
                self._verified_until = started + remaining if confirmed else -math.inf
                if not confirmed or remaining <= 0:
                    self._discard_jobs()
            if changed:
                self.on_change()

    def binding(self, session):
        with self.lock:
            now = self.clock()
            if (self._stop.is_set() or not session or self.session != session or self.owner is None
                    or now >= min(self._verified_until, self._fresh_until)):
                return None
            policy = self.memory.policy(self.owner)
            if not policy or 'daily' not in policy['categories']:
                return None
            return (session, self.owner, self.revision, self.epoch)

    def valid(self, binding):
        return binding is not None and self.binding(binding[0]) == binding

    def context(self, binding, query=''):
        with self.lock:
            rows = self.memory.recall(binding[1], query=query) if self.valid(binding) else []
            return [dict(row, observed_at_local=datetime.fromtimestamp(
                row['observed_at'], ZoneInfo('Asia/Seoul')).isoformat()) for row in rows]

    def enqueue_remember(self, binding, turn_id, text, observed_at):
        """Bounded, ordered, RAM-only work; overload never delays a spoken answer."""
        if (not isinstance(text, str) or not 1 <= len(text) <= 8000 or trivial_utterance(text)
                or not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 256
                or type(observed_at) not in (int, float) or not math.isfinite(observed_at)
                or observed_at > self.wall() + 1 or expires_at(observed_at) <= self.wall()):
            return False
        with self._memory_condition:
            if self._stop.is_set() or not self.valid(binding) or self.extractor is None:
                return False
            self._prune_jobs()
            current = ([self._memory_active] if self._memory_active else []) + list(self._memory_jobs)
            key = binding, turn_id
            if (key in self._memory_seen or len(current) >= 4
                    or sum(len(job.text) for job in current) + len(text) > 16000):
                return False
            self._memory_seen.append(key)
            self._memory_jobs.append(_MemoryJob(binding, turn_id, text, observed_at,
                                                self.clock() + 60))
            if self._memory_thread is None:
                self._memory_thread = threading.Thread(target=self._run_memory,
                                                       name='companion-memory', daemon=True)
                self._memory_thread.start()
            self._memory_condition.notify_all()
            return True

    def _run_memory(self):
        while True:
            with self._memory_condition:
                self._memory_condition.wait_for(lambda: self._stop.is_set() or self._memory_jobs)
                if self._stop.is_set():
                    return
                self._prune_jobs()
                if not self._memory_jobs:
                    continue
                job = self._memory_active = self._memory_jobs.popleft()
            try:
                self.remember(job.binding, job.turn_id, job.text, job.observed_at, job=job)
            except Exception:
                pass  # No durable retry, original text, provider response or error logging.
            finally:
                with self._memory_condition:
                    self._memory_active = None
                    self._memory_condition.notify_all()
                job = None  # Do not retain the last source in an idle worker frame.

    def remember(self, binding, turn_id, text, observed_at, *, job=None):
        if job is not None:
            # A queued source may have waited behind another extraction. Refresh
            # permission before sending it to the external provider.
            with self.lock:
                if not self._job_valid(job):
                    return 0
            self.sync()
        with self.lock:
            if (not self.valid(binding) or self.extractor is None or trivial_utterance(text)
                    or job is not None and not self._job_valid(job)):
                return 0
            categories = self.memory.policy(binding[1])['categories']
        summaries = self.extractor.extract(text, observed_at, categories)
        with self.lock:
            if not self.valid(binding) or job is not None and not self._job_valid(job):
                return 0
        # Refresh consent after external work; a withdrawal fences late results.
        self.sync()
        with self.lock:
            if not self.valid(binding) or job is not None and not self._job_valid(job):
                return 0
            return self.memory.save(binding[1], binding[2], turn_id, observed_at, summaries)

    def forget(self, binding, *, latest=False):
        with self.lock:
            if not self.valid(binding):
                return None
            jobs = list(self._memory_jobs) + ([self._memory_active] if self._memory_active else [])
            jobs = [job for job in jobs if job.binding == binding and self._job_valid(job)]
            newest = max(jobs, key=lambda job: job.observed_at, default=None)
            stored = self.memory.latest_turn(binding[1]) if latest else None
            if latest and newest and (stored is None or newest.observed_at >= stored['observed_at']):
                # "The last memory" may still be waiting for extraction. Do not delete
                # an unrelated older summary while allowing the new one to arrive later.
                count = max(1, self.memory.forget(binding[1], turn_id=newest.turn_id))
            else:
                count = self.memory.forget(binding[1], latest=latest) + int(bool(jobs) and not latest)
            self.epoch += 1
            self._discard_jobs()
            return count

    def close(self):
        with self.lock:
            if self._stop.is_set():
                return
            self._stop.set()
            self.end()
            self._memory_condition.notify_all()
        if self._thread:
            self._thread.join()
        if self._memory_thread:
            self._memory_thread.join()  # Existing extractor/HTTP timeouts bound active work.
        with self._sync_lock:
            self.memory.close()


def build_companion_service(settings):
    client = CompanionCloudClient.from_env()
    extractor = None
    if client is not None and settings.provider == 'openai':
        from malbut_agent_server.companion_extractor import CompanionExtractor
        from malbut_agent_server.factory import openai_key_for
        from malbut_agent_server.semantic_summary import OpenAISemanticSummarizer
        extractor = CompanionExtractor(OpenAISemanticSummarizer(
            openai_key_for(settings), model=settings.openai_summary_model or settings.openai_model,
            base_url=settings.openai_base_url, timeout_seconds=30,
            reasoning_effort=settings.openai_summary_reasoning_effort or settings.openai_reasoning_effort))
    path = ':memory:' if settings.database_path == ':memory:' else settings.database_path + '.companion.sqlite3'
    return CompanionService(CompanionMemory(path), client, extractor)
