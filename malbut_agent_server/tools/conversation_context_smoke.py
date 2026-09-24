#!/usr/bin/env python3
"""Opt-in production context test at the default 16,384-token budget.

Synthetic fixture turns are committed locally; summaries and probes use the
configured OpenAI model. Two compactions and a restart are exercised. This is
not a microphone/TTS test or a claim of naturally occurring long conversation.
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from malbut_agent_server.config import DEFAULT_OPENAI_MODEL, Settings
from malbut_agent_server.context_compaction import conversation_tokens
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.schemas import AgentDecision, AgentRequest, ProviderResult


USER = 'synthetic-context-user'
CASES = Path(__file__).parent / 'context_compression_pilot' / 'cases.json'


class FixtureProvider(AgentProvider):
    """Seed a supplied synthetic transcript without charging for filler turns."""

    message = '예시 자료를 확인했어요.'

    def complete(self, *args, **kwargs):
        return ProviderResult(AgentDecision('message', self.message),
                              'synthetic-fixture', 'no-model', 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live or not os.environ.get('OPENAI_API_KEY', '').strip():
        parser.error('--live and an existing OPENAI_API_KEY are required')
    cases = json.loads(CASES.read_text())
    source = next(case for case in cases if case['id'] == 'long_style_scope')
    distractors = [case for case in cases if case is not source]
    model = os.environ.get('OPENAI_MODEL', DEFAULT_OPENAI_MODEL)
    effort = os.environ.get('OPENAI_REASONING_EFFORT', Settings.openai_reasoning_effort)
    result = {'created_at': datetime.now(timezone.utc).isoformat(), 'model': model,
              'synthetic_only': True, 'fixture_turns_use_model': False,
              'conversation_token_budget': 16384, 'reasoning_effort': effort, 'source_case': source,
              'seeded_turns': [], 'api_calls': [], 'compactions': [], 'probes': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')

    def observe(kind, transport):
        def call(url, headers, payload, timeout):
            entry = {'kind': kind, 'started': time.monotonic(),
                     'reasoning': payload.get('reasoning'),
                     'timeout_seconds': timeout,
                     'max_output_tokens': payload.get('max_output_tokens')}
            entry['model_input'] = payload['input']
            entry['instructions'] = payload['instructions']
            result['api_calls'].append(entry)
            try:
                response = transport(url, headers, payload, timeout)
                entry.update(status=response.get('status'), model=response.get('model'),
                             usage=response.get('usage'), output=response.get('output'),
                             incomplete_details=response.get('incomplete_details'))
                return response
            except Exception as error:
                entry['error_type'] = type(error).__name__
                code = getattr(error.__cause__, 'code', None)
                if isinstance(code, int):
                    entry['http_status'] = code
                raise
            finally:
                entry['finished'] = time.monotonic()
        return call

    with tempfile.TemporaryDirectory(prefix='malbut-context-eval-') as directory:
        settings = Settings.from_env({
            **{key: os.environ[key] for key in (
                'MALBUT_AGENT_TIMEOUT_SECONDS',
                'MALBUT_AGENT_PROVIDER_TOTAL_TIMEOUT_SECONDS',
                'OPENAI_SUMMARY_MODEL', 'OPENAI_SUMMARY_REASONING_EFFORT',
            ) if key in os.environ},
            'MALBUT_AGENT_PROVIDER': 'openai',
            'OPENAI_API_KEY': os.environ['OPENAI_API_KEY'],
            'OPENAI_MODEL': model,
            'OPENAI_REASONING_EFFORT': effort,
            'MALBUT_AGENT_DB': str(Path(directory) / 'context.db'),
        })
        result['request_timeout_seconds'] = settings.request_timeout_seconds
        result['provider_total_timeout_seconds'] = settings.provider_total_timeout_seconds
        runtime = build_orchestrator(settings, http_server=False)
        result['summary_model'] = runtime.context_compactor.summarizer.model
        result['summary_reasoning_effort'] = runtime.context_compactor.summarizer.reasoning_effort
        fixture = FixtureProvider()
        session = runtime.conversation_store.create(USER)

        def instrument():
            adapter = runtime.provider._providers[0]
            adapter.transport = observe('answer', adapter.transport)
            summarizer = runtime.context_compactor.summarizer
            summarizer.transport = observe('summary', summarizer.transport)

        def say(text, *, fixture_answer=None):
            request = AgentRequest.from_dict({
                'request_id': str(uuid.uuid4()), 'turn_id': str(uuid.uuid4()),
                'user_id': USER, 'conversation_id': session.conversation_id,
                'utterance': text, 'robot_state': {}, 'available_tools': [],
            })
            provider, compactor = runtime.provider, runtime.context_compactor
            if fixture_answer is not None:
                fixture.message = fixture_answer
                runtime.provider, runtime.context_compactor = fixture, None
            try:
                answer = runtime.handle(request)
            finally:
                runtime.provider, runtime.context_compactor = provider, compactor
            record = {'user': text, 'decision': answer.decision.to_dict(),
                      'usage': asdict(answer.provider_result.usage),
                      'finished': time.monotonic()}
            result['seeded_turns' if fixture_answer is not None else 'probes'].append(record)
            return answer

        def context():
            snapshot = runtime.conversation_store.snapshot(USER, session.conversation_id, limit=500)
            assert len(snapshot.turns) == len(result['seeded_turns']) + len(result['probes'])
            covered = snapshot.summary.source_end_ordinal if snapshot.summary else 0
            recent = [turn for turn in snapshot.turns if turn.ordinal > covered]
            return snapshot, recent, conversation_tokens(snapshot.summary, recent)

        def fill(cycle):
            # Deliberately synthetic distractors, not thousands of duplicate
            # greetings. Each quoted sample remains separate from the real plan.
            for index in range(80):
                snapshot, recent, estimate = context()
                if estimate >= 16384 * .9:
                    return estimate
                case = distractors[index % len(distractors)]
                for part, turn in enumerate(case['turns']):
                    say(f'읽기 자료 {cycle}-{index}-{part}: {case["title"]}\n'
                        '다음은 가상 대화 예문이며 나나 은서의 실제 사실 또는 모임 결정이 아니에요.\n'
                        + '예시 사용자: ' + turn['user'] + '\n예시 응답: ' + turn['assistant'],
                        fixture_answer='가상 대화 예문으로 구분했어요.')
                    estimate = context()[2]
                    if estimate >= 16384 * .9:
                        return estimate
            raise AssertionError('fixture did not reach the default compaction threshold')

        def finish_compaction(before, expected_revision):
            thread = runtime.context_compactor._thread
            assert thread is not None, 'foreground turn did not start compaction'
            thread.join(timeout=runtime.context_compactor.summarizer.timeout_seconds + 10)
            assert not thread.is_alive(), 'specific summary worker still running'
            snapshot, recent, estimate = context()
            assert snapshot.summary is not None, 'summary failed or was not applied'
            assert snapshot.summary.summary_revision == expected_revision
            entry = {'before_estimated_tokens': before, 'after_estimated_tokens': estimate,
                     'summary': asdict(snapshot.summary),
                     'recent_ordinals': [turn.ordinal for turn in recent],
                     'raw_turn_count': len(snapshot.turns)}
            result['compactions'].append(entry)
            save()
            print('compaction', expected_revision, before, '->', estimate, flush=True)

        try:
            instrument()
            for turn in source['turns']:
                say(turn['user'], fixture_answer=turn['assistant'])
            before = fill(1)
            say('읽기 자료 속 가상 인물과 실제 준비하던 모임을 구분해 주세요. 초대문은 다음에 이어갈게요.')
            finish_compaction(before, 1)
            say('이제 느린 취미회 최종 문구 주세요.')
            say('모임 이름 후보 중 포기한 것들을 내가 썼던 정확한 문구로 알려 주세요.')
            before = fill(2)
            # This correction is absent from the source snapshot being compacted.
            say('실제 느린 취미회는 일요일 그대로이고 시작 시간만 오후 세 시로 바꿨어요. '
                '끝나는 시간은 오후 네 시 그대로예요. 나머지 조건은 바꾸지 않아요.')
            finish_compaction(before, 2)
            say('이제 확정된 느린 취미회 초대문을 앞서 정한 문체와 형식으로 써 주세요.')
            snapshot, _, _ = context()
            raw = [(turn.ordinal, turn.user_content, turn.assistant_content) for turn in snapshot.turns]
            raw_digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()
            saved_summary = snapshot.summary
            runtime.close()
            runtime = build_orchestrator(settings, http_server=False)
            instrument()
            resumed = runtime.conversation_store.resume_or_create(USER)
            assert resumed.conversation_id == session.conversation_id
            reopened, _, _ = context()
            assert reopened.summary == saved_summary
            assert [(t.ordinal, t.user_content, t.assistant_content) for t in reopened.turns] == raw
            result['restart'] = {'same_session': True, 'same_summary': True,
                                 'raw_turn_count': len(raw), 'raw_sha256': raw_digest}
            say('은서와 제가 각각 어떤 취미를 한다고 했고, 귤은 누가 준비하기로 했나요? '
                '취미 소개 순서는 정했나요?')
            result['final_raw_turn_count'] = len(context()[0].turns)
            save()
            print('restart and final probe completed', flush=True)
        except Exception as error:
            result['error_type'] = type(error).__name__
            save()
            raise SystemExit('context evaluation failed: ' + type(error).__name__) from None
        finally:
            runtime.close()


if __name__ == '__main__':
    main()
