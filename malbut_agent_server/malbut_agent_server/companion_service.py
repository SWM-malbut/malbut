"""App-confirmed, encounter-bound access to expiring daily summaries."""

import json
import math
import threading
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from malbut_agent_server.companion_memory import CATEGORIES, CompanionMemory
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


class CompanionService:
    """No identity survives end/restart; stale/offline confirmations grant no access."""

    def __init__(self, memory, client, extractor=None, *, clock=time.monotonic, wall=time.time):
        self.memory, self.client, self.extractor = memory, client, extractor
        self.clock, self.wall = clock, wall
        self.lock, self._sync_lock = threading.RLock(), threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.on_change = lambda: None
        self.session = None
        self.owner = None
        self.revision = -1
        self.epoch = 0
        self._verified_until = -math.inf
        self._fresh_until = -math.inf

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='companion-policy', daemon=True)
            self._thread.start()

    def _run(self):
        next_purge = -math.inf
        while not self._stop.is_set():
            if self.clock() >= next_purge:
                try:
                    self.memory.purge()
                except Exception:
                    pass
                next_purge = self.clock() + 60
            try:
                self.sync()
            except Exception:
                # No network response, transcript, credential or summary in logs.
                with self.lock:
                    self._fresh_until = -math.inf
            self._stop.wait(3)

    def begin(self, session):
        with self.lock:
            if session != self.session:
                self.session = session
                self.epoch += 1
                self._verified_until = -math.inf
                self._fresh_until = -math.inf

    def end(self, session=None):
        with self.lock:
            if session is None or session == self.session:
                self.session = None
                self.epoch += 1
                self._verified_until = self._fresh_until = -math.inf

    def sync(self):
        try:
            return self._sync_once()
        except Exception:
            with self.lock:
                self._fresh_until = -math.inf
            raise

    def _sync_once(self):
        if self.client is None:
            return
        with self._sync_lock:
            with self.lock:
                session = self.session
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
                if session != self.session:
                    return
                self.memory.apply_policy(owner, revision, data['eraseRevision'], categories,
                                         data['externalProcessing'])
                changed = (owner, revision) != (self.owner, self.revision)
                if changed:
                    self.epoch += 1
                self.owner, self.revision = owner, revision
                self._fresh_until = started + 10
                confirmed = session and data['confirmedSessionId'] == session
                self._verified_until = started + remaining if confirmed else -math.inf
            if changed:
                self.on_change()

    def binding(self, session):
        with self.lock:
            now = self.clock()
            if (not session or self.session != session or self.owner is None
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

    def remember(self, binding, turn_id, text, observed_at):
        with self.lock:
            if not self.valid(binding) or self.extractor is None:
                return 0
            categories = self.memory.policy(binding[1])['categories']
        summaries = self.extractor.extract(text, observed_at, categories)
        # Refresh consent after external work; a withdrawal fences late results.
        self.sync()
        with self.lock:
            if not self.valid(binding):
                return 0
            return self.memory.save(binding[1], binding[2], turn_id, observed_at, summaries)

    def forget(self, binding, *, latest=False):
        with self.lock:
            if not self.valid(binding):
                return None
            count = self.memory.forget(binding[1], latest=latest)
            self.epoch += 1
            return count

    def close(self):
        self.end()
        self._stop.set()
        if self._thread:
            self._thread.join()
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
