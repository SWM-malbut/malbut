"""Interactive, observe-only inspector for Front Route candidates."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, TextIO

from malbut_agent_server.adapters.outbound.openai_front_route_candidate import (  # noqa: E501
    FrontRouteCandidateError,
    FrontRouteCandidateResult,
    OpenAIFrontRouteCandidateClient,
)
from malbut_agent_server.domain.front_route import (
    MAX_FRONT_MESSAGE_CHARS,
    FrontRouteRequest,
)
from malbut_agent_server.endpoint_policy import OFFICIAL_OPENAI_BASE_URL


DEFAULT_FRONT_ROUTER_MODEL = 'gpt-4.1-mini'
DEFAULT_FRONT_ROUTER_TIMEOUT_SECONDS = 2


class CandidateClassifier(Protocol):
    """The narrow dependency required by the manual inspector."""

    def classify(
        self,
        request: FrontRouteRequest,
    ) -> FrontRouteCandidateResult:
        """Return one unpromoted route candidate."""
        ...


@dataclass(frozen=True)
class ProbeRecord:
    """A content-free record for one manually entered utterance."""

    ordinal: int
    outcome: str
    candidate_route: str | None
    latency_ms: float | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    classifier_calls: int
    error_code: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON form without raw input or authority."""
        return {
            'schema_version': 1,
            'ordinal': self.ordinal,
            'outcome': self.outcome,
            'candidate_route': self.candidate_route,
            'production_route': None,
            'promoted': False,
            'latency_ms': self.latency_ms,
            'model': self.model,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'classifier_calls': self.classifier_calls,
            'authority': False,
            'error_code': self.error_code,
        }


ClassifierFactory = Callable[
    [str, str, int, str],
    CandidateClassifier,
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Inspect OpenAI Front Route candidates without promoting '
            'them or invoking any robot effect.'
        ),
    )
    parser.add_argument(
        '--env-file',
        default='.env.local',
        help=(
            'Load local KEY=VALUE settings without accepting a key on '
            'the command line.'
        ),
    )
    parser.add_argument(
        '--model',
        help=(
            'Override the dedicated MALBUT_FRONT_ROUTER_MODEL. '
            f'Default: {DEFAULT_FRONT_ROUTER_MODEL}.'
        ),
    )
    parser.add_argument(
        '--timeout-seconds',
        type=int,
        default=DEFAULT_FRONT_ROUTER_TIMEOUT_SECONDS,
        help=(
            'Per-I/O OpenAI timeout from 1 to 10 seconds; '
            'not a hard wall-clock deadline.'
        ),
    )
    parser.add_argument(
        '--allow-live-provider',
        action='store_true',
        help='Explicitly allow paid OpenAI requests.',
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Read at most one utterance from stdin and exit.',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Write one content-free JSON object per attempted input.',
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='Validate configuration and exit without an API request.',
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    classifier_factory: ClassifierFactory | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    id_factory: Callable[[], object] = uuid.uuid4,
    clock: Callable[[], float] = time.perf_counter,
) -> int:
    """Run a paid, explicitly enabled, observe-only route inspector."""
    args = _parser().parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    source_input = stdin if stdin is not None else sys.stdin

    if not args.allow_live_provider:
        errors.write(
            '실행 거절: 실제 API 호출에는 '
            '--allow-live-provider가 필요합니다.\n'
        )
        errors.flush()
        return 2

    environment = dict(environ if environ is not None else os.environ)
    try:
        _load_probe_env(Path(args.env_file).expanduser(), environment)
    except OSError:
        errors.write('설정 오류: env 파일을 읽을 수 없습니다.\n')
        errors.flush()
        return 2
    api_key = environment.get('OPENAI_API_KEY', '')
    if not api_key.strip():
        errors.write('설정 오류: OPENAI_API_KEY가 필요합니다.\n')
        errors.flush()
        return 2
    model = (
        args.model
        or environment.get('MALBUT_FRONT_ROUTER_MODEL')
        or DEFAULT_FRONT_ROUTER_MODEL
    )
    base_url = environment.get(
        'OPENAI_BASE_URL',
        OFFICIAL_OPENAI_BASE_URL,
    )
    factory = classifier_factory or _build_classifier
    try:
        classifier = factory(
            api_key,
            model,
            args.timeout_seconds,
            base_url,
        )
    except (TypeError, ValueError):
        errors.write('설정 오류: Front Route 실험 설정이 잘못되었습니다.\n')
        errors.flush()
        return 2

    if args.check:
        output.write(
            'configuration=ok network_calls=0 authority=false\n'
        )
        output.flush()
        return 0

    errors.write('Malbut Front Route Inspector (observe-only)\n')
    errors.write(
        '후보만 표시합니다: production route=OFF, '
        'RobotAction=0, Nav2=0\n'
    )
    errors.write(
        '입력은 OpenAI API로 전송되며 이 도구는 원문을 저장하지 않습니다.\n'
    )
    errors.write('명령: /quit\n')
    errors.flush()
    try:
        return run_console(
            classifier,
            once=args.once,
            json_output=args.json,
            model_label=model,
            stdin=source_input,
            stdout=output,
            stderr=errors,
            id_factory=id_factory,
            clock=clock,
        )
    except KeyboardInterrupt:
        errors.write('\n실험을 중단했습니다.\n')
        errors.flush()
        return 130
    except BrokenPipeError:
        return 1


def run_console(
    classifier: CandidateClassifier,
    *,
    once: bool,
    json_output: bool,
    model_label: str = '<injected>',
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    id_factory: Callable[[], object] = uuid.uuid4,
    clock: Callable[[], float] = time.perf_counter,
) -> int:
    """Read isolated utterances and display candidate-only results."""
    if not callable(getattr(classifier, 'classify', None)):
        raise TypeError('classifier must provide classify()')
    ordinal = 0
    had_error = False
    while True:
        if not once:
            stderr.write('malbut-route> ')
            stderr.flush()
        line, too_long = _read_bounded_line(stdin)
        if line is None:
            if once and ordinal == 0:
                record = _error_record(
                    ordinal=1,
                    code='invalid_input',
                    classifier_calls=0,
                )
                _write_record(stdout, record, json_output)
                return 1
            break
        text = line.strip()
        if not text and not too_long:
            if once:
                record = _error_record(
                    ordinal=1,
                    code='invalid_input',
                    classifier_calls=0,
                )
                _write_record(stdout, record, json_output)
                return 1
            continue
        if not too_long and text == '/quit':
            break
        ordinal += 1
        if too_long:
            record = _error_record(
                ordinal=ordinal,
                code='invalid_input',
                classifier_calls=0,
            )
            had_error = True
        else:
            record = _classify_once(
                classifier,
                ordinal=ordinal,
                text=text,
                model_label=_safe_model_label(model_label),
                id_factory=id_factory,
                clock=clock,
            )
            had_error = had_error or record.outcome == 'error'
        _write_record(stdout, record, json_output)
        if once:
            break
    return 1 if had_error else 0


def _classify_once(
    classifier: CandidateClassifier,
    *,
    ordinal: int,
    text: str,
    model_label: str,
    id_factory: Callable[[], object],
    clock: Callable[[], float],
) -> ProbeRecord:
    try:
        request = FrontRouteRequest(
            request_id=f'front-probe-{id_factory()}',
            user_message=text,
        )
    except (TypeError, ValueError):
        return _error_record(
            ordinal=ordinal,
            code='invalid_input',
            classifier_calls=0,
        )
    started = clock()
    try:
        result = classifier.classify(request)
    except FrontRouteCandidateError as error:
        return _error_record(
            ordinal=ordinal,
            code=error.code,
            classifier_calls=1,
            latency_ms=_elapsed_ms(started, clock()),
        )
    except Exception:
        return _error_record(
            ordinal=ordinal,
            code='candidate_classifier_failed',
            classifier_calls=1,
            latency_ms=_elapsed_ms(started, clock()),
        )
    if type(result) is not FrontRouteCandidateResult:
        return _error_record(
            ordinal=ordinal,
            code='candidate_result_invalid',
            classifier_calls=1,
            latency_ms=_elapsed_ms(started, clock()),
        )
    return ProbeRecord(
        ordinal=ordinal,
        outcome='candidate_observed',
        candidate_route=result.route.value,
        latency_ms=round(float(result.latency_ms), 3),
        model=model_label,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        classifier_calls=1,
        error_code=None,
    )


def _error_record(
    *,
    ordinal: int,
    code: str,
    classifier_calls: int,
    latency_ms: float | None = None,
) -> ProbeRecord:
    return ProbeRecord(
        ordinal=ordinal,
        outcome='error',
        candidate_route=None,
        latency_ms=latency_ms,
        model=None,
        input_tokens=None,
        output_tokens=None,
        classifier_calls=classifier_calls,
        error_code=code,
    )


def _read_bounded_line(stream: TextIO) -> tuple[str | None, bool]:
    value = stream.readline(MAX_FRONT_MESSAGE_CHARS + 2)
    if value == '':
        return None, False
    if value.endswith('\n'):
        return value, len(value.rstrip('\r\n')) > MAX_FRONT_MESSAGE_CHARS
    if len(value) <= MAX_FRONT_MESSAGE_CHARS:
        return value, False
    while True:
        remainder = stream.readline(MAX_FRONT_MESSAGE_CHARS + 2)
        if remainder == '' or remainder.endswith('\n'):
            break
    return '', True


def _write_record(
    stream: TextIO,
    record: ProbeRecord,
    json_output: bool,
) -> None:
    if json_output:
        stream.write(
            json.dumps(
                record.to_dict(),
                ensure_ascii=False,
                separators=(',', ':'),
            )
            + '\n'
        )
    else:
        route = record.candidate_route or '-'
        latency = (
            f'{record.latency_ms:.3f}'
            if record.latency_ms is not None
            else '-'
        )
        lines = (
            f'candidate_route : {route}',
            'production_route: -',
            f'outcome         : {record.outcome}',
            'promoted        : false',
            f'latency_ms      : {latency}',
            f'classifier_calls: {record.classifier_calls}',
            'authority       : false',
            f'error_code     : {record.error_code or "none"}',
        )
        for line in lines:
            stream.write(line + '\n')
    stream.flush()


def _build_classifier(
    api_key: str,
    model: str,
    timeout_seconds: int,
    base_url: str,
) -> CandidateClassifier:
    return OpenAIFrontRouteCandidateClient(
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        base_url=base_url,
    )


def _safe_model_label(value: object) -> str:
    if (
        type(value) is str
        and bool(value)
        and len(value) <= 128
        and value.isascii()
        and all(32 < ord(character) < 127 for character in value)
    ):
        return value
    return '<configured>'


def _elapsed_ms(started: float, finished: float) -> float | None:
    elapsed = (finished - started) * 1000
    if elapsed < 0:
        return None
    return round(elapsed, 3)


def _load_probe_env(path: Path, target: dict[str, str]) -> None:
    """Load simple KEY=VALUE entries without logging their values."""
    if not path.exists():
        return
    with path.open('r', encoding='utf-8') as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            if not key or not key.replace('_', '').isalnum():
                continue
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {'"', "'"}
            ):
                value = value[1:-1]
            target.setdefault(key, value)


if __name__ == '__main__':
    raise SystemExit(main())
