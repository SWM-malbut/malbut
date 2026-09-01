"""Inspect free-form text turns through Malbut's non-actuating runtime."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, replace
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable, List, Optional, Sequence, TextIO
import uuid

from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.conversation import ConversationTurn
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.providers.base import AgentProvider, ProviderError
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    ValidationError,
)
from malbut_agent_server.tools import ToolSpec
from malbut_gazebo.named_navigation_facade import ActiveMapCatalogSource
from malbut_scenarios.simulation_text_runtime import (
    build_simulation_text_runtime,
)


LOCAL_AUTH_MARKER = 'malbut-text-inspector-local-only'
STATEFUL = 'stateful'
ISOLATED = 'isolated'
MODES = (STATEFUL, ISOLATED)
LIVE_PROVIDERS = frozenset({'openai', 'rai-sidecar'})
_PUBLIC_PROVIDER_LABELS = frozenset({
    'malbut-server-policy',
    'mock',
    'openai',
    'rai-sidecar',
})
_PUBLIC_MODEL_LABELS = frozenset({
    'malbut-korean-rules-v1',
    'navigation-clarification-v1',
})
_PUBLIC_ARGUMENT_KEYS = frozenset({'location'})


@dataclass(frozen=True)
class ProviderObservation:
    """One in-memory snapshot at the actual Provider boundary."""

    effective_utterance: str
    available_tools: tuple[str, ...]
    decision: AgentDecision
    provider: str
    model: str


class InspectingProvider(AgentProvider):
    """Delegate unchanged while retaining only the latest local snapshot."""

    def __init__(self, delegate: AgentProvider) -> None:
        """Wrap one concrete provider without changing its behavior."""
        if not isinstance(delegate, AgentProvider):
            raise TypeError('delegate must be an AgentProvider')
        self.delegate = delegate
        self.last_observation: ProviderObservation | None = None

    def clear(self) -> None:
        """Forget the previous turn before another local inspection."""
        self.last_observation = None

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary=None,
    ) -> ProviderResult:
        """Record the effective request and normalized model decision."""
        result = self.delegate.complete(
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary=conversation_summary,
        )
        self.last_observation = ProviderObservation(
            effective_utterance=request.utterance,
            available_tools=tuple(request.available_tools),
            decision=copy.deepcopy(result.decision),
            provider=result.provider,
            model=result.model,
        )
        return result


@dataclass(frozen=True)
class InspectionReport:
    """Safe terminal projection of one real TextTurnService result."""

    input_chars: int
    effective_changed: bool | None
    provider_called: bool
    provider: str | None
    model: str | None
    model_decision_type: str | None
    model_tool_name: str | None
    model_argument_keys: tuple[str, ...]
    route: str
    route_code: str
    final_decision_type: str | None
    safety_code: str | None
    status: str
    result_code: str | None
    response_chars: int
    proposal_present: bool
    execution: dict

    def compact_history(self) -> dict:
        """Return a bounded in-memory history entry for explicit display."""
        return {
            'input_chars': self.input_chars,
            'route': self.route,
            'status': self.status,
            'tool': self.model_tool_name,
        }


class TextAgentInspector:
    """Drive the real text-turn service without any execution adapter."""

    def __init__(
        self,
        orchestrator,
        service,
        *,
        user_id: str,
        mode: str = STATEFUL,
    ) -> None:
        """Bind one private session and enforce the non-actuating contract."""
        if mode not in MODES:
            raise ValueError('inspector mode is unsupported')
        if service.create_robot_actions is not False:
            raise ValueError('inspector refuses RobotAction creation')
        provider = InspectingProvider(orchestrator.provider)
        orchestrator.provider = provider
        self.orchestrator = orchestrator
        self.service = service
        self.provider = provider
        self.user_id = user_id
        self.mode = mode
        self.conversation_id = ''
        self._reports: list[InspectionReport] = []
        self._start_new_conversation()

    def _start_new_conversation(self) -> None:
        """Replace the current session without accumulating durable data."""
        if self.conversation_id:
            self.orchestrator.conversation_store.delete(
                self.user_id,
                self.conversation_id,
            )
        self.conversation_id = f'inspector-{uuid.uuid4()}'
        self.orchestrator.conversation_store.create(
            self.user_id,
            self.conversation_id,
        )

    def new_conversation(self) -> None:
        """Start a clean stateful conversation and clear visible history."""
        self._start_new_conversation()
        self._reports.clear()

    def set_mode(self, mode: str) -> None:
        """Select stateful or one-conversation-per-input inspection."""
        if mode not in MODES:
            raise ValueError('inspector mode is unsupported')
        self.mode = mode
        self.new_conversation()

    def history(self) -> tuple[dict, ...]:
        """Return only compact process-memory summaries on demand."""
        return tuple(report.compact_history() for report in self._reports)

    def submit(self, utterance: str) -> InspectionReport:
        """Submit one raw utterance and project its trusted public outcome."""
        if self.mode == ISOLATED:
            self._start_new_conversation()
        self.provider.clear()
        request_id = f'inspector-request-{uuid.uuid4()}'
        turn_id = f'inspector-turn-{uuid.uuid4()}'
        try:
            response = self.service.handle(
                user_id=self.user_id,
                value={
                    'request_id': request_id,
                    'conversation_id': self.conversation_id,
                    'turn_id': turn_id,
                    'text': utterance,
                },
            )
            _assert_non_actuating(response)
            stored_public = self._stored_public(turn_id)
            observation = self.provider.last_observation
            if observation is None:
                route_source = stored_public or response
                route, route_code = _non_provider_route(route_source)
                model_decision_type = None
                model_tool_name = None
                model_argument_keys = ()
                effective_changed = None
                provider_metadata = _mapping_value(
                    route_source,
                    'provider',
                )
                provider_name = (
                    str(provider_metadata['provider'])
                    if provider_metadata is not None
                    and provider_metadata.get('provider') is not None
                    else None
                )
                model = (
                    str(provider_metadata['model'])
                    if provider_metadata is not None
                    and provider_metadata.get('model') is not None
                    else None
                )
            else:
                classification = self.service.decision_policy.classify(
                    observation.decision,
                    available_tools=observation.available_tools,
                )
                route = classification.route.value
                route_code = classification.code
                model_decision_type = observation.decision.type
                model_tool_name = _safe_tool_label(
                    observation.decision.tool_name,
                    observation.available_tools,
                )
                model_argument_keys = _safe_argument_keys(
                    observation.decision.arguments,
                )
                effective_changed = (
                    observation.effective_utterance != utterance.strip()
                )
                provider_name = observation.provider
                model = observation.model
            final_decision = _mapping_value(
                stored_public or response,
                'decision',
            )
            safety = _mapping_value(stored_public or response, 'safety')
            response_message = str(
                response.get('message')
                or (final_decision or {}).get('message')
                or ''
            )
            report = InspectionReport(
                input_chars=len(utterance.strip()),
                effective_changed=effective_changed,
                provider_called=observation is not None,
                provider=_safe_provider_label(provider_name),
                model=_safe_model_label(model),
                model_decision_type=model_decision_type,
                model_tool_name=model_tool_name,
                model_argument_keys=model_argument_keys,
                route=route,
                route_code=route_code,
                final_decision_type=(
                    str(final_decision['type'])
                    if final_decision is not None
                    and final_decision.get('type') is not None
                    else None
                ),
                safety_code=(
                    str(safety['code'])
                    if safety is not None
                    and safety.get('code') is not None
                    else None
                ),
                status=str(response.get('status', 'unknown')),
                result_code=(
                    str(response['result_code'])
                    if response.get('result_code') is not None
                    else None
                ),
                response_chars=len(response_message),
                proposal_present=isinstance(response.get('proposal'), dict),
                execution={
                    'execution_authorized': False,
                    'physical_authorized': False,
                    'nav2_start_count': response['execution'][
                        'nav2_start_count'
                    ],
                    'nav2_cancel_count': response['execution'][
                        'nav2_cancel_count'
                    ],
                },
            )
        finally:
            # The full Provider request/decision is needed only while forming
            # this one content-free report.  Never retain it in history.
            self.provider.clear()
        self._reports.append(report)
        return report

    def _stored_public(self, turn_id: str) -> dict | None:
        """Read only the public envelope for this just-completed turn."""
        turns = self.orchestrator.conversation_store.list_turns(
            self.user_id,
            self.conversation_id,
        )
        for turn in reversed(turns):
            if turn.turn_id != turn_id:
                continue
            value = turn.response.get('public')
            return dict(value) if isinstance(value, dict) else None
        return None


def _mapping_value(value: dict, key: str) -> dict | None:
    item = value.get(key)
    return dict(item) if isinstance(item, dict) else None


def _non_provider_route(response: dict) -> tuple[str, str]:
    result_code = response.get('result_code')
    if result_code is not None:
        return 'confirmation_response', str(result_code)
    decision = _mapping_value(response, 'decision')
    if decision is not None and decision.get('type') == 'clarification':
        code = 'clarification_required'
        if decision.get('reason') == (
            'server:navigation_destination_missing'
        ):
            code = 'navigation_destination_missing'
        return 'clarification_required', code
    if response.get('cached') is True:
        return 'cached_response', 'cached_response'
    return 'provider_not_called', 'provider_not_called'


def _assert_non_actuating(response: dict) -> None:
    """Abort if the supposedly safe runtime exposes any execution effect."""
    execution = response.get('execution')
    if not isinstance(execution, dict):
        raise RuntimeError('inspector response lost its execution contract')
    false_fields = (
        'authorized',
        'execution_authorized',
        'physical_authorized',
        'consume_once',
    )
    if any(execution.get(name) is not False for name in false_fields):
        raise RuntimeError('inspector observed execution authority')
    if execution.get('proposal_authorized', False) is not False:
        raise RuntimeError('inspector observed proposal authority')
    if execution.get('tool_call_id') is not None:
        raise RuntimeError('inspector observed an executable Tool call')
    for name in ('nav2_start_count', 'nav2_cancel_count'):
        if execution.get(name) != 0:
            raise RuntimeError('inspector observed a Nav2 side effect')


def _safe_provider_label(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in _PUBLIC_PROVIDER_LABELS else '<configured>'


def _safe_model_label(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in _PUBLIC_MODEL_LABELS else '<configured>'


def _safe_tool_label(
    value: str | None,
    available_tools: Sequence[str],
) -> str | None:
    if value is None:
        return None
    return value if value in available_tools else '<unsupported>'


def _safe_argument_keys(arguments: dict) -> tuple[str, ...]:
    keys = sorted(key for key in arguments if key in _PUBLIC_ARGUMENT_KEYS)
    if len(keys) != len(arguments):
        keys.append('<unexpected>')
    return tuple(keys)


def format_report(report: InspectionReport) -> tuple[str, ...]:
    """Format one report without prompt, credentials, or private evidence."""
    effective = (
        'Provider 호출 없음'
        if report.effective_changed is None
        else ('변환됨' if report.effective_changed else '입력과 동일')
    )
    lines = [
        '',
        '[해석 결과]',
        f'내부 effective 발화 : {effective}',
        f'Provider 호출       : {str(report.provider_called).lower()}',
        (
            'Provider / model    : '
            f'{report.provider or "-"} / {report.model or "-"}'
        ),
        f'서버 route           : {report.route}',
        f'route code           : {report.route_code}',
        f'모델 decision        : {report.model_decision_type or "-"}',
        f'Tool                 : {report.model_tool_name or "-"}',
        (
            'argument keys        : '
            + (', '.join(report.model_argument_keys) or '-')
        ),
        f'최종 decision        : {report.final_decision_type or "-"}',
        f'Safety               : {report.safety_code or "-"}',
        f'status               : {report.status}',
        f'result code          : {report.result_code or "-"}',
        f'응답                  : 있음({report.response_chars}자)',
        (
            '실행 효과           : '
            'RobotAction=0, '
            f'Nav2 start={report.execution["nav2_start_count"]}, '
            f'cancel={report.execution["nav2_cancel_count"]}'
        ),
    ]
    return tuple(lines)


def format_history(history: Sequence[dict]) -> tuple[str, ...]:
    """Format explicit in-memory history without private persisted fields."""
    if not history:
        return ('[history] 비어 있음',)
    lines = ['[history]']
    for ordinal, item in enumerate(history, start=1):
        lines.append(
            f'{ordinal}. chars={int(item["input_chars"])} '
            f'-> {item["route"]} / {item["status"]}'
        )
    return tuple(lines)


def run_console(
    inspector: TextAgentInspector,
    *,
    read: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
) -> None:
    """Run a bounded interactive loop with local-only slash commands."""
    _write_lines(output, (
        'Malbut Text Inspector',
        '기본 안전 경계: RobotAction=off, Nav2=off, physical=false',
        '명령: /new /history /stateful /isolated /quit',
        f'현재 모드: {inspector.mode}',
    ))
    while True:
        try:
            value = read('malbut> ')
        except (EOFError, KeyboardInterrupt):
            output.write('\n')
            break
        text = value.strip()
        if not text:
            continue
        if text == '/quit':
            break
        if text == '/new':
            inspector.new_conversation()
            _write_lines(output, ('새 대화를 시작했습니다.',))
            continue
        if text == '/history':
            _write_lines(output, format_history(inspector.history()))
            continue
        if text in {'/stateful', '/isolated'}:
            mode = text[1:]
            inspector.set_mode(mode)
            _write_lines(output, (f'모드: {mode}',))
            continue
        if text.startswith('/'):
            _write_lines(output, ('알 수 없는 명령입니다.',))
            continue
        try:
            report = inspector.submit(value)
        except ValidationError:
            _write_lines(output, ('입력 거절: ValidationError',))
            continue
        except ProviderError as error:
            _write_lines(
                output,
                (f'Provider 실패: {type(error).__name__}',),
            )
            continue
        _write_lines(output, format_report(report))


def _write_lines(output: TextIO, lines: Sequence[str]) -> None:
    for line in lines:
        output.write(line + '\n')
    output.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Interactively inspect Malbut text decisions with all '
            'RobotAction and Nav2 effects disabled.'
        ),
    )
    parser.add_argument('--env-file', default='.env.local')
    parser.add_argument('--map-store')
    parser.add_argument('--device-id')
    parser.add_argument(
        '--provider',
        choices=('mock', 'openai', 'rai-sidecar'),
        default='mock',
        help='Explicit provider; defaults to offline deterministic mock.',
    )
    parser.add_argument(
        '--allow-live-provider',
        action='store_true',
        help='Required for paid/external OpenAI or RAI provider calls.',
    )
    parser.add_argument('--mode', choices=MODES, default=STATEFUL)
    return parser


def _load_settings(args: argparse.Namespace) -> tuple[Settings, Path, str]:
    environment = dict(os.environ)
    load_env_file(Path(args.env_file).expanduser(), environment)
    environment['MALBUT_AGENT_PROVIDER'] = args.provider
    environment['MALBUT_AGENT_TOOL_MODE'] = 'proposal'
    environment['MALBUT_AGENT_HOST'] = '127.0.0.1'
    environment['MALBUT_AGENT_AUTH_TOKEN'] = LOCAL_AUTH_MARKER
    if args.provider in LIVE_PROVIDERS and not args.allow_live_provider:
        raise ValueError(
            'live provider requires --allow-live-provider'
        )
    settings = Settings.from_env(environment)
    map_store = (
        args.map_store
        or environment.get('MALBUT_NAMED_NAVIGATION_MAP_STORE', '')
    )
    device_id = (
        args.device_id
        or environment.get('MALBUT_ROBOT_DEVICE_ID', '')
    )
    if not map_store:
        raise ValueError('named-navigation map store is required')
    if not device_id:
        raise ValueError('robot device ID is required')
    return settings, Path(map_store).expanduser(), device_id


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Compose an ephemeral inspector and never bind HTTP or execution."""
    args = _parser().parse_args(argv)
    settings, map_store, device_id = _load_settings(args)
    with tempfile.TemporaryDirectory(
        prefix='malbut-text-inspector-',
    ) as temporary:
        os.chmod(temporary, 0o700)
        runtime_settings = replace(
            settings,
            database_path=str(Path(temporary) / 'inspector.sqlite3'),
            auth_token=LOCAL_AUTH_MARKER,
            host='127.0.0.1',
            tool_mode='proposal',
        )
        runtime_settings.validate_for_server()
        source = ActiveMapCatalogSource(map_store, device_id)
        source.load()
        orchestrator, service = build_simulation_text_runtime(
            runtime_settings,
            source.load,
        )
        try:
            inspector = TextAgentInspector(
                orchestrator,
                service,
                user_id=runtime_settings.user_id,
                mode=args.mode,
            )
            if args.provider in LIVE_PROVIDERS:
                print(
                    '주의: 외부 Provider 호출과 비용이 발생할 수 있습니다.',
                    file=sys.stderr,
                )
            run_console(inspector)
        finally:
            orchestrator.conversation_store.close()
            orchestrator.memory_store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
