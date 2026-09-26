"""One background semantic compaction; foreground turns never join it."""

import logging
import json
import threading
import time
from dataclasses import replace

from malbut_agent_server.semantic_summary import count_tokens


LOGGER = logging.getLogger(__name__)


def conversation_tokens(summary, turns, utterance='', counter=count_tokens):
    """Estimate the dialogue portion, including its serialized metadata."""
    from malbut_agent_server.prompting import _history_payload, _summary_payload

    return counter({
        'conversation_history_untrusted': _history_payload(
            turns, set(), preserve=True,
        ),
        'conversation_summary_untrusted': _summary_payload(
            summary, set(), preserve=True,
        ),
        'current_user_utterance': utterance,
    })


class BackgroundContextCompactor:
    """Keep raw records intact and publish only a still-current summary."""

    def __init__(self, conversations, memory, summarizer, *,
                 token_budget=16384, token_counter=count_tokens):
        if type(token_budget) is not int or token_budget < 1024:
            raise ValueError('conversation token budget must be >= 1024')
        self.conversations = conversations
        self.memory = memory
        self.summarizer = summarizer
        self.token_budget = token_budget
        self.counter = token_counter
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = None
        self._retry_after = 0.0

    def schedule(self, token, expected_summary, history, summary, state,
                 utterance=''):
        """Snapshot already-redacted context; never enqueue unbounded work."""
        with self._lock:
            # ponytail: one robot, one summarizer. Busy sessions try next turn;
            # add a bounded fair queue only if concurrent users need it.
            if (self._closed.is_set()
                    or (self._thread and self._thread.is_alive())
                    or time.monotonic() < self._retry_after):
                return False
            estimate = conversation_tokens(
                summary, history, utterance, self.counter,
            )
            if estimate < self.token_budget * 0.9 or len(history) < 2:
                return False
            # Keep whole recent exchanges near 10% of the dialogue budget,
            # always retaining the last exchange even if it alone is larger.
            split = len(history) - 1
            while split > 1 and conversation_tokens(
                None, history[split - 1:], counter=self.counter,
            ) <= self.token_budget * 0.1:
                split -= 1
            source, recent = tuple(history[:split]), tuple(history[split:])
            target = max(256, int(self.token_budget * 0.3)
                         - conversation_tokens(
                             None, recent, counter=self.counter,
                         ))
            self._thread = threading.Thread(
                target=self._run,
                args=(token, expected_summary, source, recent, summary,
                      dict(state), target),
                name='malbut-context-compaction', daemon=True,
            )
            self._thread.start()
            return True

    def _fresh(self, token, state, connection=None):
        return (not self._closed.is_set()
                and self.memory.policy_state(
                    token.user_id, connection=connection,
                ) == state)

    def _run(self, token, expected_summary, source, recent, summary,
             state, target):
        started = time.monotonic()
        try:
            if not self._fresh(token, state):
                return
            result = self.summarizer.summarize(summary, source, recent, target)
            if not self._fresh(token, state):
                return
            before = self.counter({
                'summary': summary.content if summary else '',
                'turns': [{'user': t.user_content, 'assistant': t.assistant_content}
                          for t in source],
            })
            after = self.counter(result.content)
            if not result.content.strip() or after >= before:
                raise ValueError('compaction did not reduce context')
            metadata = json.loads(result.state_json)
            metadata['memory_policy_state'] = state
            result = replace(result, state_json=json.dumps(metadata))
            applied = self.conversations.apply_compaction(
                token, expected_summary, source, result,
                guard=lambda conn: self._fresh(token, state, conn),
            )
            LOGGER.info(
                'Context compaction applied=%s estimated_tokens=%s->%s '
                'target=%s elapsed=%.3fs',
                applied, before, after, target, time.monotonic() - started,
            )
        except Exception:
            # Keep the original context on every failure. Do not log dialogue,
            # provider error bodies, or credentials, or bill a tight retry loop.
            with self._lock:
                self._retry_after = time.monotonic() + 60
            LOGGER.warning('Context compaction failed; original context retained')

    def close(self):
        """Fence late responses before shared SQLite handles are closed."""
        with self._lock:
            self._closed.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=1)
