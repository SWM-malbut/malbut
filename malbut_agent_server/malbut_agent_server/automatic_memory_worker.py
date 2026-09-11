"""Bounded, non-actuating review of durably queued automatic memories."""

import copy
import threading
import time
from types import SimpleNamespace

from malbut_agent_server.automatic_memory_policy import (
    apply_automatic, automatic_candidate, automatic_deferred,
)
from malbut_agent_server.personal_memory import MemorySnapshot
from malbut_agent_server.schemas import (
    AgentDecision, AgentRequest, ProviderResult, RobotState, ValidationError,
)


class AutomaticMemoryWorker:
    """Use one lazy worker without SQLite or turn locks during inference."""

    def __init__(self, runtime, jobs):
        self.runtime = runtime
        self.jobs = jobs
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lifecycle = threading.Lock()
        self._thread = None

    @property
    def closed(self):
        return self._stop.is_set()

    def start(self):
        """Start one worker only once the runtime is serving requests."""
        with self._lifecycle:
            if self.closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name='malbut-automatic-memory',
                    daemon=False,
                )
                self._thread.start()
            self._wake.set()

    def close(self):
        """Stop new work and join outside the runtime/SQLite locks."""
        with self._lifecycle:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is not None:
            thread.join()

    def _transaction(self, callback):
        runtime = self.runtime
        with runtime._handle_lock, runtime.conversation_store._lock:
            conn = runtime.conversation_store._connection
            conn.execute('BEGIN IMMEDIATE')
            try:
                result = callback(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def _admit(self, conn, job):
        if self.closed or not self.jobs.valid(conn, job):
            self.jobs.finish(conn, job, 'discarded')
            return False
        state = self.runtime.memory_store.policy_state(
            job['user_id'], connection=conn,
        )
        if not state['enabled'] or state != job['payload']['state']:
            self.jobs.finish(conn, job, 'discarded')
            return False
        return True

    @staticmethod
    def _restore(job):
        """Reconstruct only memory inputs; never restore robot capabilities."""
        payload = job['payload']
        source = copy.deepcopy(payload['source'])
        request = AgentRequest(
            user_id=job['user_id'], request_id=job['request_id'],
            conversation_id=job['conversation_id'],
            turn_id=job['source_turn_id'], utterance=source['text'],
            available_tools=(), robot_state=RobotState(),
        )
        snapshot = MemorySnapshot(
            state=copy.deepcopy(payload['state']), memories=[], history=[],
            summary=None, dependencies=set(), context={'memories': []},
            pending=None, source=source,
        )
        token = SimpleNamespace(
            user_id=job['user_id'], request_id=job['request_id'],
            conversation_id=job['conversation_id'],
            session_instance_id=job['session_instance_id'],
            generation=job['generation'], turn_id=job['source_turn_id'],
            ordinal=job['ordinal'],
            revision=job['expected_session_revision'] - 1,
        )
        decision = AgentDecision(type='message', message='말씀을 들었어요.')
        provider = ProviderResult(
            decision=decision, provider='automatic-memory',
            model='private-job',
            latency_ms=0.0, memory_supported=True,
            memory_proposal=copy.deepcopy(payload['proposal']),
        )
        result = SimpleNamespace(
            decision=decision, raw_decision=decision, provider_result=provider,
        )
        return request, token, snapshot, result

    def _process(self, job):
        metrics = job['_stage_metrics'] = {}
        if not self._transaction(lambda conn: self._admit(conn, job)):
            return
        request, token, snapshot, result = self._restore(job)
        payload = job['payload']
        if 'version' in payload:
            if (
                type(payload['version']) is not int
                or payload['version'] != 2
                or payload.get('mode') != 'extract'
                or payload.get('proposal') is not None
            ):
                raise ValidationError('automatic memory payload is invalid')
            if not automatic_deferred(request, snapshot, result):
                self._transaction(lambda conn: self.jobs.finish(
                    conn, job, 'discarded',
                ))
                return
            extractor = getattr(
                self.runtime, 'automatic_memory_extractor', None,
            )
            if extractor is None:
                self._transaction(lambda conn: self.jobs.finish(
                    conn, job, 'discarded',
                ))
                return
            # Extract only after the answer is frozen, outside all turn/DB
            # locks. Restored requests have no tools or robot state, and the
            # extraction interface receives no history or stored memories.
            started = time.perf_counter()
            try:
                extracted = extractor.extract(request)
            finally:
                metrics['extraction_ms'] = (
                    time.perf_counter() - started
                ) * 1000
            if not isinstance(extracted, ProviderResult):
                raise ValidationError('automatic extraction result is invalid')
            extracted.validate()
            metrics['extraction_usage'] = {
                key: getattr(extracted.usage, key)
                for key in ('input_tokens', 'output_tokens', 'total_tokens')
            }
            # An invalidating control cancels before another remote
            # pass, not only at the eventual memory commit boundary.
            if not self._transaction(lambda conn: self._admit(conn, job)):
                return
            result = SimpleNamespace(
                decision=copy.deepcopy(extracted.decision),
                raw_decision=copy.deepcopy(extracted.decision),
                provider_result=extracted,
            )
        if not automatic_candidate(request, snapshot, result):
            self._transaction(lambda conn: self.jobs.finish(
                conn, job, 'discarded', **metrics,
            ))
            return
        # This separate provider pass cannot block a foreground turn lock.
        review = self.runtime.personal_memory.prepare_source_review(
            request, snapshot, result, self.runtime.memory_source_reviewer,
        )
        metrics['review_ms'] = review.elapsed_ms if review is not None else 0.0
        metrics['review_usage'] = None
        if review is not None and review.response is not None:
            metrics['review_usage'] = {
                key: getattr(review.response.usage, key)
                for key in ('input_tokens', 'output_tokens', 'total_tokens')
            }

        def finalize(conn):
            if not self._admit(conn, job):
                return
            outcome = apply_automatic(
                self.runtime.personal_memory, request, token, snapshot,
                result, conn,
            )
            if not self.jobs.finish(
                conn, job, outcome['state'],
                **metrics,
            ):
                # A lost claim cannot leave a partial memory mutation behind.
                raise RuntimeError('automatic memory claim changed')

        self._transaction(finalize)

    def _run(self):
        while not self.closed:
            self._wake.wait(0.25)
            self._wake.clear()
            while not self.closed:
                job = None
                try:
                    job = self.jobs.claim_next()
                    if job is None:
                        break
                    self._process(job)
                except Exception:
                    # Never log source text, proposals or provider payloads.
                    # A failed job is not retried or reported as saved.
                    if job is not None:
                        try:
                            self._transaction(lambda conn: self.jobs.finish(
                                conn, job, 'failed',
                                **job.get('_stage_metrics', {}),
                            ))
                        except Exception:
                            pass  # Expiry bounds an indeterminate DB failure.
                    break
