#!/usr/bin/env python3
"""Record readable, offline-only synthetic agent conversation traces."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


from malbut_agent_server.conversation import (  # noqa: E402
    ConversationSummary,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.gateway import (  # noqa: E402
    ToolGateway,
    ToolQuery,
    production_registry,
)
from malbut_agent_server.memory import (  # noqa: E402
    MemoryRecord,
    SQLiteMemoryStore,
)
from malbut_agent_server.orchestrator import (  # noqa: E402
    AgentOrchestrator,
)
from malbut_agent_server.prompting import (  # noqa: E402
    MAX_CONVERSATION_TURNS,
    SYSTEM_INSTRUCTIONS,
    prepare_model_input,
)
from malbut_agent_server.providers.base import (  # noqa: E402
    AgentProvider,
)
from malbut_agent_server.providers.mock import (  # noqa: E402
    MockProvider,
)
from malbut_agent_server.safety import SafetyPolicy  # noqa: E402
from malbut_agent_server.schemas import (  # noqa: E402
    AgentRequest,
    ProviderResult,
    ValidationError,
)
from malbut_agent_server.tools import TOOL_SPECS, ToolSpec  # noqa: E402


DEFAULT_JSON_OUTPUT = (
    PACKAGE_ROOT
    / 'docs/evaluations/artifacts/'
    'SYNTHETIC_CONVERSATION_TRACE_2026-08-13.json'
)
DEFAULT_MARKDOWN_OUTPUT = (
    PACKAGE_ROOT
    / 'docs/evaluations/'
    'SYNTHETIC_CONVERSATION_TRACE_2026-08-13.md'
)
RUNTIME_FILES = (
    'malbut_agent_server/conversation.py',
    'malbut_agent_server/gateway.py',
    'malbut_agent_server/memory.py',
    'malbut_agent_server/orchestrator.py',
    'malbut_agent_server/prompting.py',
    'malbut_agent_server/providers/mock.py',
    'malbut_agent_server/safety.py',
    'malbut_agent_server/schemas.py',
    'malbut_agent_server/tools.py',
)
SECRET_PATTERNS = (
    re.compile(r'\bsk-[A-Za-z0-9_-]{8,}\b'),
    re.compile(r'Bearer\s+[A-Za-z0-9._~-]+', re.IGNORECASE),
    re.compile(
        r'(?:api[_ -]?key|password|비밀번호)\s*[:=]\s*'
        r'["\']?[^\s,"\']+',
        re.IGNORECASE,
    ),
    re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
)


class RecordingProvider(AgentProvider):
    """Capture exact synthetic context before delegating to MockProvider."""

    def __init__(self, delegate: MockProvider) -> None:
        """Wrap one network-free deterministic provider."""
        self.delegate = delegate
        self.name = delegate.name
        self.model = delegate.model
        self.captures: List[Dict[str, Any]] = []

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        """Capture provider inputs, then return the MockProvider result."""
        prepared = prepare_model_input(
            request,
            memories,
            conversation_turns,
            conversation_summary,
            self.delegate.max_model_input_chars,
            MAX_CONVERSATION_TURNS,
        )
        context_text = prepared.text.split('\n', 1)[1]
        capture: Dict[str, Any] = {
            'request_received_by_provider': request.to_dict(),
            'retrieved_memories': [
                memory.to_dict()
                for memory in memories
            ],
            'conversation_history': [
                _turn_context(turn)
                for turn in conversation_turns
            ],
            'conversation_summary': (
                conversation_summary.to_dict()
                if conversation_summary is not None
                else None
            ),
            'effective_tool_schemas': [
                tool.to_openai_dict()
                for tool in tools
            ],
            'system_instructions': SYSTEM_INSTRUCTIONS,
            'system_instructions_sha256': _text_sha256(
                SYSTEM_INSTRUCTIONS
            ),
            'prepared_model_input': prepared.text,
            'prepared_context_json': json.loads(context_text),
            'context_metrics': prepared.metrics.to_dict(),
        }
        result = self.delegate.complete(
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary=conversation_summary,
        )
        capture['normalized_provider_decision'] = (
            result.decision.to_dict()
        )
        capture['provider_metadata'] = result.to_dict()
        self.captures.append(capture)
        return result


def _text_sha256(value: str) -> str:
    """Return the SHA-256 digest of one UTF-8 string."""
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _file_sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_sha256() -> str:
    """Hash the ordered source files that implement the traced flow."""
    digest = hashlib.sha256()
    for relative in RUNTIME_FILES:
        path = PACKAGE_ROOT / relative
        digest.update(relative.encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def _git_value(*arguments: str) -> Optional[str]:
    """Read one content-free Git identifier when it exists."""
    result = subprocess.run(
        ['git', *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _turn_context(turn: ConversationTurn) -> Dict[str, Any]:
    """Return the exact recent-turn fields supplied to a provider."""
    return {
        'turn_id': turn.turn_id,
        'ordinal': turn.ordinal,
        'user': turn.user_content,
        'assistant': turn.assistant_content,
    }


def _snapshot_dict(snapshot: Any) -> Dict[str, Any]:
    """Return a synthetic session snapshot including persisted responses."""
    turns = []
    for turn in snapshot.turns:
        turns.append({
            **_turn_context(turn),
            'request_id': turn.request_id,
            'generation': turn.generation,
            'persisted_response': turn.response,
        })
    return {
        'session': snapshot.session.to_dict(),
        'turns': turns,
        'summary': (
            snapshot.summary.to_dict()
            if snapshot.summary is not None
            else None
        ),
    }


def _contains_key(value: Any, expected_key: str) -> bool:
    """Return whether a nested JSON value contains one exact object key."""
    if isinstance(value, dict):
        if expected_key in value:
            return True
        return any(
            _contains_key(item, expected_key)
            for item in value.values()
        )
    if isinstance(value, list):
        return any(
            _contains_key(item, expected_key)
            for item in value
        )
    return False


def _request(
    request_id: str,
    conversation_id: str,
    turn_id: str,
    utterance: str,
    *,
    user_id: str = 'synthetic-user',
    tools: Sequence[str] = (),
    robot_state: Optional[Dict[str, Any]] = None,
) -> AgentRequest:
    """Build one strictly validated synthetic AgentRequest."""
    return AgentRequest.from_dict({
        'request_id': request_id,
        'user_id': user_id,
        'conversation_id': conversation_id,
        'turn_id': turn_id,
        'utterance': utterance,
        'robot_state': robot_state or {},
        'available_tools': list(tools),
    })


def _orchestrator(
    provider: AgentProvider,
    memory_store: SQLiteMemoryStore,
    conversation_store: SQLiteConversationStore,
) -> AgentOrchestrator:
    """Create the same non-actuating safety boundary used at runtime."""
    return AgentOrchestrator(
        provider=provider,
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
        trusted_robot_state=False,
        capability_registry=production_registry(),
    )


def _assertion(
    name: str,
    expected: Any,
    actual: Any,
) -> Dict[str, Any]:
    """Build one transparent equality assertion record."""
    return {
        'name': name,
        'expected': expected,
        'actual': actual,
        'passed': actual == expected,
    }


def _trace_turn(
    orchestrator: AgentOrchestrator,
    provider: RecordingProvider,
    conversation_store: SQLiteConversationStore,
    request: AgentRequest,
) -> Dict[str, Any]:
    """Run one turn and record its full synthetic decision timeline."""
    before = conversation_store.snapshot(
        request.user_id,
        request.conversation_id,
        limit=500,
    )
    capture_count = len(provider.captures)
    result = orchestrator.handle(request)
    if len(provider.captures) != capture_count + 1:
        raise RuntimeError('provider capture count did not advance once')
    capture = provider.captures[-1]
    public = result.to_dict(include_raw_decision=True)
    after = conversation_store.snapshot(
        request.user_id,
        request.conversation_id,
        limit=500,
    )
    latest = after.turns[-1]
    timeline = [
        {
            'stage': 'request_validated',
            'request': request.to_dict(),
        },
        {
            'stage': 'session_before',
            **_snapshot_dict(before),
        },
        {
            'stage': 'context_selected',
            'request_received_by_provider': (
                capture['request_received_by_provider']
            ),
            'conversation_history': capture['conversation_history'],
            'conversation_summary': capture['conversation_summary'],
            'retrieved_memories': capture['retrieved_memories'],
            'effective_tool_schemas': (
                capture['effective_tool_schemas']
            ),
        },
        {
            'stage': 'model_input_prepared',
            'system_instructions': capture['system_instructions'],
            'system_instructions_sha256': (
                capture['system_instructions_sha256']
            ),
            'prepared_model_input': capture['prepared_model_input'],
            'prepared_context_json': capture['prepared_context_json'],
            'context_metrics': capture['context_metrics'],
        },
        {
            'stage': 'provider_raw_decision',
            'decision': public['raw_decision'],
            'normalized_provider_decision': (
                capture['normalized_provider_decision']
            ),
            'provider_metadata': capture['provider_metadata'],
            'raw_transport_response': None,
            'raw_transport_note': (
                'MockProvider has no HTTP transport response.'
            ),
        },
        {
            'stage': 'safety_evaluated',
            **public['safety'],
        },
        {
            'stage': 'final_response',
            'decision': public['decision'],
            'memory': public['memory'],
            'execution': public['execution'],
        },
        {
            'stage': 'persisted_after',
            **_snapshot_dict(after),
            'latest_turn_persisted_response': latest.response,
            'raw_decision_persisted': _contains_key(
                latest.response,
                'raw_decision',
            ),
        },
    ]
    return {
        'request': request.to_dict(),
        'timeline': timeline,
        'result': public,
    }


def _scenario_follow_up() -> Dict[str, Any]:
    """Trace a real two-turn history lookup in one synthetic session."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    provider = RecordingProvider(MockProvider())
    orchestrator = _orchestrator(provider, memories, conversations)
    conversation_id = 'synthetic-follow-up'
    conversations.create('synthetic-user', conversation_id)
    try:
        first = _trace_turn(
            orchestrator,
            provider,
            conversations,
            _request(
                'synthetic-follow-up-request-1',
                conversation_id,
                'turn-1',
                '내 이름은 사용자A야',
            ),
        )
        second = _trace_turn(
            orchestrator,
            provider,
            conversations,
            _request(
                'synthetic-follow-up-request-2',
                conversation_id,
                'turn-2',
                '아까 내가 뭐라고 했지?',
            ),
        )
        history = second['timeline'][2]['conversation_history']
        assertions = [
            _assertion(
                'second turn received one prior completed turn',
                1,
                len(history),
            ),
            _assertion(
                'prior user utterance reached untrusted history',
                '내 이름은 사용자A야',
                history[0]['user'],
            ),
            _assertion(
                'MockProvider resolved the follow-up from history',
                '아까 “내 이름은 사용자A야”라고 말했어.',
                second['result']['decision']['message'],
            ),
            _assertion(
                'non-action response passed SafetyPolicy',
                'not_an_action',
                second['result']['safety']['code'],
            ),
        ]
        return {
            'story_ids': ['SWM25-69', 'SWM25-70', 'SWM25-71'],
            'scenario_id': 'multi_turn_follow_up',
            'title': '같은 세션의 이전 사용자 발화 참조',
            'purpose': (
                '두 번째 요청에 첫 번째 턴이 어떤 형태로 들어가고 '
                '응답에 사용되는지 보여준다.'
            ),
            'kind': 'agent_flow',
            'turns': [first, second],
            'assertions': assertions,
            'passed': all(item['passed'] for item in assertions),
        }
    finally:
        conversations.close()
        memories.close()


def _seed_summary_turns(
    conversations: SQLiteConversationStore,
    memories: SQLiteMemoryStore,
    conversation_id: str,
) -> None:
    """Create twelve genuine Mock orchestration turns for summary input."""
    seed = _orchestrator(MockProvider(), memories, conversations)
    for number in range(1, 13):
        seed.handle(_request(
            f'synthetic-summary-seed-request-{number:02d}',
            conversation_id,
            f'seed-turn-{number:02d}',
            f'합성 대화 {number:02d}: 오늘 기록 {number:02d}',
        ))


def _scenario_summary_and_memory() -> Dict[str, Any]:
    """Trace recent turns, rolling summary, and retrieved memory together."""
    conversations = SQLiteConversationStore(
        ':memory:',
        history_limit=10,
    )
    memories = SQLiteMemoryStore(':memory:')
    conversation_id = 'synthetic-summary-memory'
    conversations.create('synthetic-user', conversation_id)
    memories.add(
        'synthetic-user',
        '강아지 이름은 초코야',
        memory_id='synthetic-memory-pet-name',
        metadata={'synthetic': True},
    )
    _seed_summary_turns(conversations, memories, conversation_id)
    provider = RecordingProvider(MockProvider())
    orchestrator = _orchestrator(provider, memories, conversations)
    try:
        flow = _trace_turn(
            orchestrator,
            provider,
            conversations,
            _request(
                'synthetic-summary-memory-request-13',
                conversation_id,
                'turn-13',
                '강아지 이름이 뭐였지?',
            ),
        )
        selected = flow['timeline'][2]
        summary = selected['conversation_summary']
        assertions = [
            _assertion(
                'provider received latest ten raw turns',
                10,
                len(selected['conversation_history']),
            ),
            _assertion(
                'summary covered exactly the two older turns',
                2,
                summary['source_turn_count'] if summary else None,
            ),
            _assertion(
                'one user-isolated memory was retrieved',
                1,
                len(selected['retrieved_memories']),
            ),
            _assertion(
                'retrieved fact was used in the final answer',
                '기억해 둔 내용은 “강아지 이름은 초코야”이야.',
                flow['result']['decision']['message'],
            ),
        ]
        return {
            'story_ids': ['SWM25-70', 'SWM25-71'],
            'scenario_id': 'summary_history_memory_context',
            'title': '최근 10턴 + 이전 요약 + 장기 기억 결합',
            'purpose': (
                '모델에 전달되는 세 컨텍스트 영역과 최종 기억 답변을 '
                '한 흐름으로 보여준다.'
            ),
            'kind': 'agent_flow',
            'turns': [flow],
            'assertions': assertions,
            'passed': all(item['passed'] for item in assertions),
        }
    finally:
        conversations.close()
        memories.close()


def _scenario_navigation_safety() -> Dict[str, Any]:
    """Trace a raw navigation proposal being replaced by a safe refusal."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    provider = RecordingProvider(MockProvider())
    orchestrator = _orchestrator(provider, memories, conversations)
    conversation_id = 'synthetic-navigation-safety'
    conversations.create('synthetic-user', conversation_id)
    try:
        flow = _trace_turn(
            orchestrator,
            provider,
            conversations,
            _request(
                'synthetic-navigation-request-1',
                conversation_id,
                'turn-1',
                '거실로 가줘',
                tools=('navigate',),
                robot_state={
                    'battery_percent': 80,
                    'navigation_available': True,
                    'localization_ok': True,
                    'emergency_stop': False,
                },
            ),
        )
        result = flow['result']
        assertions = [
            _assertion(
                'provider proposed the high-level navigate Tool',
                'tool_call',
                result['raw_decision']['type'],
            ),
            _assertion(
                'provider bound the named destination',
                {'location': '거실'},
                result['raw_decision']['arguments'],
            ),
            _assertion(
                'local SafetyPolicy rejected untrusted request state',
                'untrusted_robot_state',
                result['safety']['code'],
            ),
            _assertion(
                'final decision became refusal',
                'refusal',
                result['decision']['type'],
            ),
            _assertion(
                'physical execution stayed unauthorized',
                False,
                result['execution']['authorized'],
            ),
            _assertion(
                'no tool_call_id was created',
                None,
                result['execution']['tool_call_id'],
            ),
        ]
        return {
            'story_ids': ['SWM25-69', 'SWM25-72', 'SWM25-74'],
            'scenario_id': 'navigation_raw_vs_final',
            'title': 'navigate 제안과 Safety 이후 최종 거절 비교',
            'purpose': (
                '모델의 Tool 제안이 곧 실행이 아니며 로컬 SafetyPolicy가 '
                '최종 응답을 바꾸는 것을 보여준다.'
            ),
            'kind': 'agent_flow',
            'turns': [flow],
            'assertions': assertions,
            'passed': all(item['passed'] for item in assertions),
        }
    finally:
        conversations.close()
        memories.close()


def _scenario_gateway_boundary() -> Dict[str, Any]:
    """Trace the production Gateway and absent SWM25-74 confirmation."""
    registry = production_registry()
    gateway = ToolGateway(registry)
    query_value = {
        'request_id': 'synthetic-gateway-request-1',
        'user_id': 'synthetic-user',
        'tool_name': 'navigate',
        'arguments': {'location': '거실'},
    }
    try:
        query = ToolQuery.from_dict(query_value)
        result, cached = gateway.query_with_cache_state(query)
    finally:
        gateway.close()
    fake_confirmation = {
        **query_value,
        'request_id': 'synthetic-fake-confirmation',
        'confirmation': {'confirmed': True},
    }
    validation_error = None
    try:
        ToolQuery.from_dict(fake_confirmation)
    except ValidationError as error:
        validation_error = str(error)
    snapshot = registry.to_dict()
    executable_count = sum(
        1
        for capability in snapshot['capabilities']
        if capability['executable']
    )
    assertions = [
        _assertion(
            'production registry exposes zero executable Tools',
            0,
            executable_count,
        ),
        _assertion(
            'navigate query is blocked pending SWM25-74 confirmation',
            'confirmation_required',
            result.error['code'] if result.error else None,
        ),
        _assertion(
            'fake confirmation field is rejected by strict schema',
            True,
            validation_error is not None,
        ),
    ]
    return {
        'story_ids': ['SWM25-73', 'SWM25-74'],
        'scenario_id': 'production_gateway_execution_boundary',
        'title': 'Production Gateway의 실제 실행 차단 경계',
        'purpose': (
            '현재 Gateway는 confirmation·tool_call_id·실제 ROS 실행을 '
            '구현한 것처럼 보이지 않도록 negative evidence를 남긴다.'
        ),
        'kind': 'gateway_boundary',
        'implementation_status': 'SWM25-74 absent',
        'evidence_type': 'negative_only',
        'timeline': [
            {
                'stage': 'production_registry',
                'snapshot': snapshot,
                'executable_count': executable_count,
            },
            {
                'stage': 'tool_query_validated',
                'query': query_value,
            },
            {
                'stage': 'gateway_result',
                'result': result.to_dict(cached=cached),
            },
            {
                'stage': 'fake_confirmation_rejected',
                'request': fake_confirmation,
                'validation_error': validation_error,
            },
        ],
        'assertions': assertions,
        'passed': all(item['passed'] for item in assertions),
    }


def _secret_hits(value: str) -> List[str]:
    """Return names of secret-like patterns found in rendered output."""
    hits = []
    for index, pattern in enumerate(SECRET_PATTERNS, start=1):
        if pattern.search(value):
            hits.append(f'pattern-{index}')
    return hits


def _atomic_write(
    path: Path,
    content: str,
    mode: int,
) -> None:
    """Atomically write one artifact with explicit permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f'refusing to replace symlink: {path}')
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.',
        dir=str(path.parent),
        text=True,
    )
    try:
        os.fchmod(file_descriptor, mode)
        with os.fdopen(
            file_descriptor,
            'w',
            encoding='utf-8',
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _json_block(value: Any) -> str:
    """Render one Markdown JSON code block."""
    return (
        '```json\n'
        + json.dumps(value, ensure_ascii=False, indent=2)
        + '\n```\n'
    )


def _timeline_stage(flow: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Return one named timeline stage from an agent flow."""
    for stage in flow['timeline']:
        if stage['stage'] == name:
            return stage
    raise KeyError(name)


def _render_agent_flow(lines: List[str], scenario: Dict[str, Any]) -> None:
    """Append readable agent-flow details to Markdown lines."""
    for index, flow in enumerate(scenario['turns'], start=1):
        request = flow['request']
        selected = _timeline_stage(flow, 'context_selected')
        prepared = _timeline_stage(flow, 'model_input_prepared')
        raw = _timeline_stage(flow, 'provider_raw_decision')
        safety = _timeline_stage(flow, 'safety_evaluated')
        final = _timeline_stage(flow, 'final_response')
        persisted = _timeline_stage(flow, 'persisted_after')
        lines.extend([
            f'### 턴 {index}: `{request["turn_id"]}`',
            '',
            f'- 사용자: **{request["utterance"]}**',
            '- Provider: `mock / malbut-korean-rules-v1`',
            '- 외부 API·ROS 호출: 없음',
            '',
            '선택된 최근 대화:',
            '',
            _json_block(selected['conversation_history']).rstrip(),
            '',
            '선택된 이전 요약:',
            '',
            _json_block(selected['conversation_summary']).rstrip(),
            '',
            '검색된 장기 기억:',
            '',
            _json_block(selected['retrieved_memories']).rstrip(),
            '',
            '모델에 전달된 정확한 컨텍스트 JSON:',
            '',
            _json_block(prepared['prepared_context_json']).rstrip(),
            '',
            'MockProvider 원결정:',
            '',
            _json_block(raw['decision']).rstrip(),
            '',
            '로컬 SafetyPolicy 결과:',
            '',
            _json_block({
                'allowed': safety['allowed'],
                'code': safety['code'],
                'reason': safety['reason'],
            }).rstrip(),
            '',
            '최종 응답과 실행 경계:',
            '',
            _json_block({
                'decision': final['decision'],
                'execution': final['execution'],
            }).rstrip(),
            '',
            '영속 저장 확인:',
            '',
            f'- 저장된 턴 수: `{len(persisted["turns"])}`',
            '- `raw_decision` 저장 여부: '
            f'`{str(persisted["raw_decision_persisted"]).lower()}`',
            '',
        ])


def _render_gateway_flow(
    lines: List[str],
    scenario: Dict[str, Any],
) -> None:
    """Append readable Gateway-boundary details to Markdown lines."""
    for stage in scenario['timeline']:
        lines.extend([
            f'### `{stage["stage"]}`',
            '',
            _json_block({
                key: value
                for key, value in stage.items()
                if key != 'stage'
            }).rstrip(),
            '',
        ])


def _render_markdown(report: Dict[str, Any]) -> str:
    """Render a human-readable companion to the full JSON trace."""
    summary = report['summary']
    lines = [
        '# 합성 대화·컨텍스트 전체 흐름 기록',
        '',
        '> SYNTHETIC / OFFLINE / NON-ACTUATING / NOT PRODUCTION DATA',
        '',
        '기존 300회 stress JSON에는 발화·응답·prompt가 저장되지 않았다. '
        '이 문서는 과거 로그를 복구한 것이 아니라, 현재 코드를 Mock으로 '
        '새로 실행해 사람이 읽을 수 있도록 남긴 별도 증거다.',
        '',
        f'- 생성 시각: `{report["generated_at"]}`',
        f'- 시나리오: `{summary["scenario_count"]}`개',
        f'- 통과: `{summary["passed"]}/{summary["scenario_count"]}`',
        '- 실제 OpenAI 호출: `false`',
        '- 실제 ROS·파일·카메라·알림 부작용: `false`',
        '- JSON 원본 권한: `0600`',
        '',
        '## 읽는 순서',
        '',
        '`요청 → 이전 대화/요약/기억 → 모델 입력 JSON → Mock 원결정 '
        '→ SafetyPolicy → 최종 응답 → DB 저장` 순서로 보면 된다.',
        '',
    ]
    for number, scenario in enumerate(report['scenarios'], start=1):
        status = '통과' if scenario['passed'] else '실패'
        lines.extend([
            f'## {number}. {scenario["title"]} — {status}',
            '',
            scenario['purpose'],
            '',
            f'- 관련 story: `{", ".join(scenario["story_ids"])}`',
            '',
        ])
        if scenario['kind'] == 'agent_flow':
            _render_agent_flow(lines, scenario)
        else:
            _render_gateway_flow(lines, scenario)
        lines.extend([
            '검증 결과:',
            '',
        ])
        for assertion in scenario['assertions']:
            marker = 'x' if assertion['passed'] else ' '
            lines.append(f'- [{marker}] {assertion["name"]}')
        lines.append('')
    lines.extend([
        '## 해석할 때 주의할 점',
        '',
        '- 이 결과는 규칙 기반 `MockProvider`의 코드 흐름 증거이며 실제 '
        'OpenAI 모델 품질 증거가 아니다.',
        '- exact prompt와 raw decision은 이 합성 trace에만 기록했다. 운영 '
        '대화에서는 개인정보 때문에 기본적으로 저장하지 않는다.',
        '- `execution.authorized=false`는 의도된 현재 경계다. SWM25-74의 '
        'confirmation·영속 1회 소비·ROS 실행·feedback/cancel은 아직 없다.',
        '- 전체 system instructions와 모든 중간 필드는 같은 이름의 JSON '
        '원본에서 확인할 수 있다.',
        '',
    ])
    return '\n'.join(lines)


def _build_report() -> Dict[str, Any]:
    """Run every synthetic scenario and return one full trace report."""
    scenarios = [
        _scenario_follow_up(),
        _scenario_summary_and_memory(),
        _scenario_navigation_safety(),
        _scenario_gateway_boundary(),
    ]
    return {
        'schema_version': 1,
        'title': 'Malbut synthetic conversation and context trace',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'offline_synthetic_full_trace',
        'labels': [
            'SYNTHETIC',
            'OFFLINE',
            'NON_ACTUATING',
            'NOT_PRODUCTION_DATA',
        ],
        'source': {
            'git_head': _git_value('rev-parse', 'HEAD'),
            'origin_main': _git_value('rev-parse', 'origin/main'),
            'agent_tree_at_head': _git_value(
                'rev-parse',
                'HEAD:malbut_agent_server',
            ),
            'runtime_source_sha256': _runtime_sha256(),
            'harness_sha256': _file_sha256(Path(__file__)),
            'system_instructions_sha256': _text_sha256(
                SYSTEM_INSTRUCTIONS
            ),
            'tool_schema_sha256': _text_sha256(json.dumps(
                {
                    name: spec.to_openai_dict()
                    for name, spec in TOOL_SPECS.items()
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            )),
        },
        'guarantees': {
            'synthetic_only': True,
            'live_api_calls': False,
            'physical_actions': False,
            'external_side_effects': False,
            'persistent_product_database_read': False,
            'environment_credentials_read': False,
            'raw_transport_fixture': False,
            'secrets_scanned_before_write': True,
        },
        'limitations': [
            (
                'This does not recover the missing content from the earlier '
                '300x stress artifact.'
            ),
            (
                'MockProvider is deterministic and does not measure live '
                'OpenAI quality or latency.'
            ),
            (
                'The production Gateway remains non-executing; SWM25-74 '
                'evidence is negative-only.'
            ),
        ],
        'scenarios': scenarios,
        'summary': {
            'scenario_count': len(scenarios),
            'passed': sum(1 for item in scenarios if item['passed']),
            'failed': sum(1 for item in scenarios if not item['passed']),
            'all_passed': all(item['passed'] for item in scenarios),
        },
    }


def _parse_arguments() -> argparse.Namespace:
    """Parse output paths for the synthetic trace artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--output',
        type=Path,
        default=DEFAULT_JSON_OUTPUT,
        help='private full JSON output path',
    )
    parser.add_argument(
        '--markdown-output',
        type=Path,
        default=DEFAULT_MARKDOWN_OUTPUT,
        help='human-readable Markdown output path',
    )
    return parser.parse_args()


def main() -> int:
    """Run the trace, scan it for secrets, and write both artifacts."""
    arguments = _parse_arguments()
    report = _build_report()
    rendered_json = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + '\n'
    hits = _secret_hits(rendered_json)
    if hits:
        raise RuntimeError(
            'secret-like content blocked artifact write: '
            + ', '.join(hits)
        )
    rendered_markdown = _render_markdown(report)
    markdown_hits = _secret_hits(rendered_markdown)
    if markdown_hits:
        raise RuntimeError(
            'secret-like content blocked Markdown write: '
            + ', '.join(markdown_hits)
        )
    _atomic_write(arguments.output.resolve(), rendered_json, 0o600)
    _atomic_write(
        arguments.markdown_output.resolve(),
        rendered_markdown,
        0o644,
    )
    print(json.dumps({
        'json_output': str(arguments.output.resolve()),
        'markdown_output': str(arguments.markdown_output.resolve()),
        'summary': report['summary'],
    }, ensure_ascii=False))
    return 0 if report['summary']['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
