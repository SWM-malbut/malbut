"""Non-actuating semantic review of memory candidates, never storage authority.

The meaning-based extraction pattern is informed by Mem0's public examples:
https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py
This is an independent implementation; no Mem0 code or runtime is bundled.
"""

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field

from malbut_agent_server.memory_contract import MEMORY_KINDS
from malbut_agent_server.providers.base import accepts_memory_context
from malbut_agent_server.schemas import (
    AgentRequest, ProviderResult, RobotState,
)


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')).hexdigest()


def _source_digest(source):
    return _digest({key: value for key, value in source.items()
                    if key != '_semantic_review'})


def attach_source_review(source, facts):
    """Bind an internal review to exact facts and the complete source identity.
    """
    source['_semantic_review'] = {
        'version': 1,
        'source_digest': _source_digest(source),
        'fact_digests': [_digest(fact) for fact in facts],
    }


def source_review_matches(fact, source):
    """Accept only a review stored by the server for this original source."""
    stamp = source.get('_semantic_review')
    return (
        isinstance(stamp, dict)
        and stamp.get('version') == 1
        and stamp.get('source_digest') == _source_digest(source)
        and _digest(fact) in stamp.get('fact_digests', ())
    )


@dataclass
class SourceReviewOutcome:
    """Private review result; neither a public answer nor execution permission.
    """

    facts: list = field(default_factory=list)
    response: ProviderResult | None = None
    elapsed_ms: float = 0.0


class MemorySourceReviewer:
    """Reuse a memory-capable provider with no tools, history or stored facts.
    """

    def __init__(self, provider):
        """Construction does not call a model or open a database."""
        self.provider = provider

    def review(self, request, facts):
        """Review once; invalid output or failure approves no memory
        candidates.
        """
        outcome = SourceReviewOutcome()
        if not accepts_memory_context(self.provider):
            return outcome
        candidates = copy.deepcopy(facts)
        review_id = 'memory-review-' + _digest({
            'request_id': request.request_id,
            'user_id': request.user_id,
            'source': request.utterance,
            'facts': candidates,
        })[:48]
        review_request = AgentRequest(
            request_id=review_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            turn_id=review_id,
            utterance=request.utterance,
            robot_state=RobotState(),
            available_tools=(),
        )
        started = time.perf_counter()
        try:
            result = self.provider.complete(
                review_request, [], [], [], conversation_summary=None,
                memory_context={
                    'mode': 'source_review',
                    'candidate_facts': copy.deepcopy(candidates),
                    'enabled': True,
                    'pending_question': None,
                    'memories': [],
                    'allowed_kinds': list(MEMORY_KINDS),
                },
            )
            if not isinstance(result, ProviderResult):
                return outcome
            result.validate()
            outcome.response = result
            proposal = result.memory_proposal
            if (
                result.decision.type != 'message'
                or not result.memory_supported
                or proposal is None
                or proposal['operation'] != 'remember'
                or proposal['target_ids']
                or proposal['query']
                or proposal['evidence'] != request.utterance
            ):
                return outcome
            accepted = proposal['facts']
            # A reviewer may reject candidates, not rewrite or invent them.
            if any(fact not in candidates for fact in accepted):
                return outcome
            if len({_digest(fact) for fact in accepted}) != len(accepted):
                return outcome
            outcome.facts = copy.deepcopy(accepted)
        except Exception:
            # Do not expose credentials, source text or remote error payloads.
            # Storage stays blocked while the original conversation can finish.
            pass
        finally:
            outcome.elapsed_ms = (time.perf_counter() - started) * 1000
        return outcome
