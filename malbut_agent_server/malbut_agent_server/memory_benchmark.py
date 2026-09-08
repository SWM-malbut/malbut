"""Bounded memory latency experiment; never installed in the serving path.

Early JSON readiness is measured before the deferred completion transaction.
This is not a durable HTTP delivery implementation or a background job queue.
"""

import argparse
import copy
import csv
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import re
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.memory_contract import (
    MEMORY_INSTRUCTIONS, MEMORY_PROPOSAL_SCHEMA, validate_memory_proposal,
)
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.base import AgentProvider, ProviderError
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import AgentRequest, ValidationError


MODEL = 'gpt-5.6-luna'
TRIALS_PER_MODE = 10
# Each mode also receives one warmup; A/B/C need 1/1/2 calls per trial.
CALL_LIMIT = (TRIALS_PER_MODE + 1) * (1 + 1 + 2)
NO_COMPLETION_CLAIM = (
    '\n답변은 짧은 일상 대화로 작성합니다. 기억 작업은 아직 확정되지 않았으므로 '
    '기억하거나 저장했다는 완료 주장을 하지 않습니다.'
)
COMPLETION_CLAIM = re.compile(
    r'(기억|저장|삭제|정정|수정|개인화).{0,20}'
    r'(했|하였|완료|해\s*뒀|해\s*두었|됐|되었|할게|하겠|해둘|해\s*둘)'
)
CASES = (
    {'id': 'greeting', 'text': '안녕! 오늘도 만나서 반가워.',
     'expected': None},
    {'id': 'pet', 'text': '우리 강아지 이름은 두부야.',
     'expected': {'kind': 'pet', 'subject': '강아지',
                  'attribute': 'name', 'value': '두부'}},
    {'id': 'preference', 'text': '나는 커피를 좋아해.',
     'expected': {'kind': 'preference', 'subject': 'user',
                  'attribute': 'likes', 'value': '커피'}},
)


def fixture_response(payload):
    """Return declared test data, never stand in for a failed live call."""
    context = json.loads(payload['input'].split('\n', 1)[1])
    text = context['current_user_utterance']
    case = next(item for item in CASES if item['text'] == text)
    proposal = None
    if case['expected'] is not None:
        proposal = {
            'operation': 'remember',
            'facts': [dict(case['expected'], evidence=text)],
            'target_ids': [], 'query': '', 'evidence': text,
        }
    schema = payload['text']['format']['schema']['properties']
    value = {}
    if 'message' in schema:
        value.update(type='message', message='이야기해 줘서 고마워요.',
                     reason='conversation', confidence=1.0)
    if 'memory_proposal' in schema:
        value['memory_proposal'] = proposal
    return {
        'status': 'completed', 'model': MODEL, 'output': [{
            'type': 'message', 'content': [{
                'type': 'output_text',
                'text': json.dumps(value, ensure_ascii=False),
            }],
        }],
    }


def usage_cost(usage):
    """Estimate standard short-context USD; missing usage is not zero."""
    if not isinstance(usage, dict):
        return None
    incoming, outgoing = usage.get('input_tokens'), usage.get('output_tokens')
    cached = (usage.get('input_tokens_details') or {}).get('cached_tokens', 0)
    if any(type(v) is not int or v < 0
           for v in (incoming, outgoing, cached)) or cached > incoming:
        return None
    return ((incoming - cached) * 0.20 + cached * 0.02
            + outgoing * 1.20) / 1_000_000


class CallRecorder:
    """Capture timings and non-secret payloads, with one shared call cap."""

    def __init__(self, transport):
        """Wrap either the real no-retry transport or a fixed transport."""
        self.transport = transport
        self.calls = []
        self.trial_id = None
        self.phase = None

    def __call__(self, url, headers, payload, timeout):
        """Count before sending; include failures in the fixed allowance."""
        if len(self.calls) >= CALL_LIMIT:
            raise ProviderError('benchmark call limit reached')
        record = {
            'trial_id': self.trial_id, 'phase': self.phase,
            'payload': copy.deepcopy(payload), 'usage': None,
            'cost_usd': None, 'error': None, 'http_status': None,
        }
        self.calls.append(record)
        started = time.perf_counter()
        try:
            response = self.transport(url, headers, payload, timeout)
            record['response'] = copy.deepcopy(response)
            record['usage'] = response.get('usage')
            record['cost_usd'] = usage_cost(record['usage'])
            return response
        except Exception as error:
            # Do not serialize arbitrary exception text or request headers.
            record['error'] = type(error).__name__
            record['http_status'] = getattr(error.__cause__, 'code', None)
            raise
        finally:
            record['latency_ms'] = (time.perf_counter() - started) * 1000


class ExperimentProvider(AgentProvider):
    """Keep experimental prompt/schema changes outside production adapters."""

    supports_memory = True

    def __init__(self, api_key, recorder, mode):
        """Use the same inexpensive model and limits for every phase."""
        self.recorder, self.mode = recorder, mode
        self.backend = OpenAIResponsesProvider(
            api_key, MODEL, timeout_seconds=30, max_output_tokens=500,
            reasoning_effort='none', transport=self._transport,
        )

    def _transport(self, url, headers, payload, timeout):
        payload['instructions'] += NO_COMPLETION_CLAIM
        return self.recorder(url, headers, payload, timeout)

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None, *, memory_context=None):
        """A/B request a combined response; C initially requests only text."""
        self.recorder.phase = 'answer' if self.mode == 'C' else 'combined'
        result = self.backend.complete(
            request, memories, conversation_turns, tools,
            conversation_summary,
            memory_context=None if self.mode == 'C' else memory_context,
        )
        result.memory_supported = True
        return result

    def extract(self, request, snapshot):
        """Keep the original snapshot across any intervening edit."""
        self.recorder.phase = 'extraction'
        payload = self.backend.build_payload(
            request, snapshot.memories, snapshot.history, [],
            snapshot.summary, memory_context=snapshot.context,
        )
        payload['instructions'] = (
            MEMORY_INSTRUCTIONS + '\n현재 발화의 자동 기억 후보만 추출합니다. '
            'operation은 remember만 사용하고, 후보가 없으면 null입니다. '
            '대화 답변은 생성하지 않습니다.'
        )
        payload['text']['format'] = {
            'type': 'json_schema', 'name': 'malbut_memory_extraction',
            'strict': True, 'schema': {
                'type': 'object', 'properties': {
                    'memory_proposal': {'anyOf': [
                        copy.deepcopy(MEMORY_PROPOSAL_SCHEMA),
                        {'type': 'null'},
                    ]},
                }, 'required': ['memory_proposal'],
                'additionalProperties': False,
            },
        }
        response = self.recorder(
            f'{self.backend.base_url}/responses',
            {'Authorization': f'Bearer {self.backend._api_key}',
             'Content-Type': 'application/json'}, payload, 30,
        )
        if response.get('status') != 'completed':
            raise ProviderError('extraction response incomplete')
        if any(item.get('type') not in {'message', 'reasoning'}
               for item in response.get('output', [])):
            raise ProviderError('unexpected extraction output')
        messages = [item for item in response.get('output', [])
                    if item.get('type') == 'message']
        if len(messages) != 1 or len(messages[0].get('content', [])) != 1:
            raise ProviderError('invalid extraction output')
        part = messages[0]['content'][0]
        if part.get('type') != 'output_text':
            raise ProviderError('extraction was not text')
        value = json.loads(part['text'])
        if type(value) is not dict or set(value) != {'memory_proposal'}:
            raise ProviderError('invalid extraction envelope')
        proposal = value['memory_proposal']
        return None if proposal is None else validate_memory_proposal(proposal)


def consent_source():
    """Return synthetic, explicit consent used only in isolated trial DBs."""
    return {'conversation_id': 'setup', 'session_instance_id': 'setup',
            'generation': 1, 'turn_id': 'consent', 'request_id': 'consent',
            'text': '이후 일상 정보의 자동 저장과 활용에 동의합니다.'}


def run_trial(path, case, mode, recorder, api_key, *, warmup=False,
              enabled=True, setup=None, before_commit=None, on_reply=None):
    """Measure real policy/SQLite work; join deferred work before returning.

    Optional hooks are for fixed tests of races, never live timing delays.
    """
    if mode not in {'A', 'B', 'C'}:
        raise ValueError('unknown benchmark mode')
    if Path(path).exists():
        raise ValueError('each trial requires a fresh database')
    memory = SQLiteMemoryStore(str(path))
    conversations = SQLiteConversationStore(str(path))
    provider = ExperimentProvider(api_key, recorder, mode)
    runtime = AgentOrchestrator(
        provider, memory, conversations, SafetyPolicy(),
    )
    user, conversation = 'benchmark-user', 'benchmark-conversation'
    conversations.create(user, conversation)
    if enabled:
        memory.set_personalization(user, True, consent_source())
    if setup:
        setup(memory, conversations)
    request = AgentRequest.from_dict({
        'request_id': str(uuid.uuid4()), 'user_id': user,
        'conversation_id': conversation, 'turn_id': str(uuid.uuid4()),
        'utterance': case['text'], 'robot_state': {}, 'available_tools': [],
    })
    recorder.trial_id = request.request_id
    first_call = len(recorder.calls)
    row = {'trial_id': request.request_id, 'case': case['id'], 'mode': mode,
           'warmup': warmup, 'reply_ready_ms': None, 'memory_done_ms': None,
           'reply_json': None, 'postcommit_reply_json': None,
           'quality_issues': [], 'error': None}
    token = None
    started = time.perf_counter()

    def elapsed():
        return (time.perf_counter() - started) * 1000

    try:
        begin = conversations.begin_turn(
            user_id=user, conversation_id=conversation,
            turn_id=request.turn_id, request_id=request.request_id,
            request_fingerprint=runtime._request_fingerprint(request),
            user_content=request.utterance,
        )
        token = begin.token
        snapshot = runtime.personal_memory.snapshot(
            request, token, begin.history, begin.summary,
        )
        row['initial_state'] = snapshot.state
        result = runtime._handle_uncached(
            request, request, snapshot.history, snapshot.summary,
            token, None, None, snapshot,
        )
        row['model_reply'] = result.decision.message
        if COMPLETION_CLAIM.search(result.decision.message):
            row['quality_issues'].append('premature_memory_claim')
            result.decision.message = '이야기해 줘서 고마워요.'

        def finish():
            if mode == 'C':
                result.provider_result.memory_proposal = provider.extract(
                    request, snapshot,
                )
            proposal = result.provider_result.memory_proposal
            row['memory_proposal'] = copy.deepcopy(proposal)
            if proposal is not None and proposal['operation'] != 'remember':
                raise ValidationError('benchmark only permits automatic save')
            if before_commit:
                before_commit(memory, conversations)
            conversations.complete_turn(
                token, assistant_content=result.decision.message,
                response=result.to_persisted_dict(),
                commit_callback=lambda conn: runtime.personal_memory.commit(
                    request, token, snapshot, result, conn,
                ),
            )
            row['memory_done_ms'] = elapsed()

        def prepare_reply():
            if memory.policy_state(user) != snapshot.state and mode != 'A':
                raise ValidationError('memory_changed')
            session = conversations.get(user, conversation)
            expected_revision = token.revision + (1 if mode == 'A' else 0)
            if (session.status != 'active'
                    or session.session_instance_id != token.session_instance_id
                    or session.generation != token.generation
                    or session.revision != expected_revision):
                raise ValidationError('conversation_changed')
            # This exact serialized value is the early-response evidence.
            row['reply_json'] = json.dumps(
                result.to_dict(), ensure_ascii=False,
            )
            row['reply_ready_ms'] = elapsed()
            if on_reply:
                on_reply(row, memory, conversations)

        if mode == 'A':
            finish()
            prepare_reply()
        else:
            prepare_reply()
            with ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(finish).result()
        row['postcommit_reply_json'] = json.dumps(
            result.to_dict(), ensure_ascii=False,
        )
        early = json.loads(row['reply_json'])['decision']
        if early != result.decision.to_dict():
            row['quality_issues'].append('postcommit_decision_changed')
        if early['type'] != 'message':
            row['quality_issues'].append('not_a_conversation_reply')
    except Exception as error:
        row['error'] = type(error).__name__
        if token:
            conversations.fail_turn(token)
    finally:
        row['finished_ms'] = elapsed()
        row['facts'] = [record.metadata.get('fact')
                        for record in memory.list_for_user(user)]
        conversations.close()
        memory.close()
    expected = case['expected'] if enabled else None
    facts = row['facts']
    if expected is None:
        correct = not facts
    else:
        correct = len(facts) == 1 and all(
            facts[0].get(key) == value
            or (key == 'subject' and value == '강아지'
                and facts[0].get(key) == '반려견')
            for key, value in expected.items()
        )
    if not correct:
        row['quality_issues'].append('unexpected_stored_facts')
    calls = recorder.calls[first_call:]
    row['call_count'] = len(calls)
    row['cost_usd'] = (
        sum(call['cost_usd'] for call in calls)
        if calls and all(call['cost_usd'] is not None for call in calls)
        else None
    )
    row['valid'] = not row['error'] and not row['quality_issues']
    return row


def summarize(rows):
    """Only successful, correct measured trials contribute latency ranks."""
    groups = {}
    for mode in 'ABC':
        measured = [r for r in rows if r['mode'] == mode and not r['warmup']]
        valid = [r for r in measured if r['valid']]
        group = {'trials': len(measured), 'valid': len(valid)}
        for metric in ('reply_ready_ms', 'memory_done_ms', 'cost_usd'):
            values = [r[metric] for r in valid if r[metric] is not None]
            group[metric] = (
                {'median': statistics.median(values), 'min': min(values),
                 'max': max(values)} if values else None
            )
        groups[mode] = group
    return groups


def write_results(directory, rows, recorder, live, stop_reason):
    """Checkpoint raw measurements and a compact readable comparison."""
    sources = {
        str(path.relative_to(Path(__file__).parent)): hashlib.sha256(
            path.read_bytes(),
        ).hexdigest()
        for path in sorted(Path(__file__).parent.rglob('*.py'))
    }
    measured = [row for row in rows if not row['warmup']]
    result = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'live': live, 'model': MODEL, 'reasoning_effort': 'none',
        'max_output_tokens': 500, 'timeout_seconds': 30, 'retries': 0,
        'python': platform.python_version(), 'platform': platform.platform(),
        'source_sha256': hashlib.sha256(
            Path(__file__).read_bytes(),
        ).hexdigest(),
        'package_sources_sha256': sources,
        'comparison_complete': (
            len(measured) == TRIALS_PER_MODE * 3
            and all(row['valid'] for row in measured)
        ),
        'trials_per_mode': TRIALS_PER_MODE,
        'call_limit': CALL_LIMIT, 'calls_used': len(recorder.calls),
        'stop_reason': stop_reason, 'summary': summarize(rows),
        'known_cost_usd': sum(c['cost_usd'] or 0 for c in recorder.calls),
        'usage_complete': bool(recorder.calls) and all(
            c['cost_usd'] is not None for c in recorder.calls
        ),
        'rows': rows, 'calls': recorder.calls,
    }
    (directory / 'results.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8',
    )
    fields = ['trial_id', 'case', 'mode', 'warmup', 'valid', 'reply_ready_ms',
              'memory_done_ms', 'call_count', 'cost_usd', 'error',
              'quality_issues']
    with (directory / 'trials.csv').open(
        'w', newline='', encoding='utf-8',
    ) as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    call_fields = ['trial_id', 'phase', 'latency_ms', 'input_tokens',
                   'output_tokens', 'cached_tokens', 'cost_usd', 'error',
                   'http_status']
    with (directory / 'calls.csv').open(
        'w', newline='', encoding='utf-8',
    ) as f:
        writer = csv.DictWriter(f, fieldnames=call_fields)
        writer.writeheader()
        for call in recorder.calls:
            usage = call['usage'] or {}
            item = {key: call.get(key) for key in call_fields}
            item.update(input_tokens=usage.get('input_tokens'),
                        output_tokens=usage.get('output_tokens'),
                        cached_tokens=(usage.get('input_tokens_details')
                                       or {}).get('cached_tokens'))
            writer.writerow(item)
    lines = [
        '# Memory latency comparison', '',
        f'Live API: {live}; model: {MODEL}; '
        f'calls: {len(recorder.calls)}/{CALL_LIMIT}.',
        '', 'Metric: JSON readiness, not durable HTTP delivery or audio.',
        'B/C keep the turn pending until one deferred completion transaction.',
        'Fixed responses are harness checks, not model latency evidence.', '',
        f'All {TRIALS_PER_MODE * 3} measured trials valid: '
        f'{result["comparison_complete"]}.',
        'If false, use case-level results; do not rank differing subsets.', '',
        '| Mode | Valid/measured | Reply median ms | Memory median ms |',
        '|---|---:|---:|---:|',
    ]
    for mode, group in result['summary'].items():
        values = [f"{group[m]['median']:.2f}" if group[m] else 'n/a'
                  for m in ('reply_ready_ms', 'memory_done_ms')]
        lines.append(f"| {mode} | {group['valid']}/{group['trials']} | "
                     f'{values[0]} | {values[1]} |')
    lines += ['', f'Known cost USD: {result["known_cost_usd"]:.6f}; '
              f'usage complete: {result["usage_complete"]}.',
              f'Stop reason: {stop_reason or "completed"}.', '',
              'Small sample: inspect case-level ranges, outputs and failures '
              'before choosing a production architecture.']
    (directory / 'REPORT.md').write_text(
        '\n'.join(lines) + '\n', encoding='utf-8',
    )


def main(argv=None):
    """Run three warmups and ten measured trials per mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--live', action='store_true',
                        help=f'Make up to {CALL_LIMIT} paid API calls; '
                        'default is fixed.')
    args = parser.parse_args(argv)
    key = os.environ.get('OPENAI_API_KEY') if args.live else 'offline-only'
    if not key:
        parser.error('OPENAI_API_KEY is required for --live')
    directory = args.output_dir
    directory.mkdir(parents=True, exist_ok=False)
    transport = (OpenAIResponsesProvider._urllib_transport if args.live
                 else lambda _url, _headers, payload, _timeout:
                 fixture_response(payload))
    recorder = CallRecorder(transport)
    schedule = [(CASES[1], mode, True) for mode in 'ABC']
    orders = tuple(itertools.permutations('ABC'))
    case_order = (CASES[1], CASES[2], CASES[0])
    for index in range(TRIALS_PER_MODE):
        order = orders[index % len(orders)]
        case = case_order[index % len(case_order)]
        schedule.extend((case, mode, False) for mode in order)
    rows, stop_reason = [], None
    for index, (case, mode, warmup) in enumerate(schedule):
        row = run_trial(directory / f'trial-{index:02d}.sqlite3', case, mode,
                        recorder, key, warmup=warmup)
        rows.append(row)
        recent = (recorder.calls[-row['call_count']:]
                  if row['call_count'] else [])
        if args.live and any(c['http_status'] in {400, 401, 403, 404, 429}
                             for c in recent):
            stop_reason = 'api_access_or_configuration_error'
        elif warmup and not row['valid']:
            stop_reason = 'warmup_failed'
        write_results(directory, rows, recorder, args.live, stop_reason)
        print(json.dumps({k: row[k] for k in (
            'case', 'mode', 'warmup', 'valid', 'reply_ready_ms',
            'memory_done_ms', 'error',
        )}), flush=True)
        if stop_reason:
            break
    return 1 if stop_reason or any(not r['valid'] for r in rows) else 0


if __name__ == '__main__':
    raise SystemExit(main())
