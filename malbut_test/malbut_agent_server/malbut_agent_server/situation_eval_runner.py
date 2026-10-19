"""Small text-only situation suite; mock results are not live semantic validation."""

import argparse
from collections import Counter
from dataclasses import dataclass
from importlib import resources
import json
import os
from pathlib import Path
import re
import sys

from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.ros_situation import build_situation_factory
from malbut_agent_server.schemas import MAX_SPEECH_TRANSCRIPT_LENGTH
from malbut_agent_server.situation_dialogue import SituationRequest, SituationResult


@dataclass(frozen=True)
class SituationEvaluationCase:
    """Fixed answers follow generated questions; null explicitly simulates silence."""

    id: str
    situation_type: str
    summary: str
    answers: tuple
    providers: tuple
    expected: SituationResult
    question_count: int

    @classmethod
    def parse(cls, value):
        """Reject malformed fixtures without printing their contents."""
        fields = {'id', 'situation_type', 'summary', 'answers', 'providers', 'expected'}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError('invalid evaluation case')
        identifier = value['id']
        if not isinstance(identifier, str) or not re.fullmatch(
                r'[a-z][a-z0-9_-]{0,79}', identifier):
            raise ValueError('invalid evaluation case ID')
        SituationRequest(identifier, value['situation_type'], value['summary'])
        answers, providers, expected = value['answers'], value['providers'], value['expected']
        if (not isinstance(answers, list) or not 1 <= len(answers) <= 6
                or any(answer is not None and (
                    not isinstance(answer, str) or not answer.strip()
                    or len(answer) > MAX_SPEECH_TRANSCRIPT_LENGTH) for answer in answers)
                or not isinstance(providers, list) or not providers
                or any(provider not in ('mock', 'openai') for provider in providers)
                or 'openai' not in providers or len(set(providers)) != len(providers)
                or not isinstance(expected, dict)
                or set(expected) != {'situation_assessment', 'help_needed', 'question_count'}
                or type(expected['question_count']) is not int
                or not 1 <= expected['question_count'] <= 6):
            raise ValueError('invalid evaluation expectation')
        outcome = SituationResult(expected['situation_assessment'], expected['help_needed'])
        return cls(identifier, value['situation_type'], value['summary'], tuple(answers),
                   tuple(providers), outcome, expected['question_count'])


def load_cases(path=None):
    """Load the packaged Korean JSON suite using the existing evaluation convention."""
    source = Path(path) if path is not None else resources.files(
        'malbut_agent_server').joinpath('data/situation_eval_cases.json')
    text = source.read_text(encoding='utf-8')
    if len(text) > 1024 * 1024:
        raise ValueError('evaluation suite is too large')
    data = json.loads(text)
    if not isinstance(data, list) or not 1 <= len(data) <= 1000:
        raise ValueError('invalid evaluation suite')
    cases = [SituationEvaluationCase.parse(case) for case in data]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError('duplicate evaluation case IDs')
    return cases


def run_evaluation(cases, engine_factory, *, provider='mock'):
    """Return bounded judgments/counts only, never questions, answers, or errors."""
    if provider not in ('mock', 'openai'):
        raise ValueError('unsupported evaluation provider')
    rows = []
    for case in cases:
        row = dict(id=case.id, status='skipped_live_only', situation_assessment=None,
                   help_needed=None, question_count=0)
        if provider not in case.providers:
            rows.append(row)
            continue
        try:
            engine = engine_factory()
            turn = engine.start(case.id, case.situation_type, case.summary)
            row['question_count'] = 1
            consumed = 0
            for answer in case.answers:
                if turn.result is not None:
                    break
                turn = engine.no_response() if answer is None else engine.answer(answer)
                consumed += 1
                if turn.result is None:
                    row['question_count'] += 1
            result = turn.result
            if result is not None:
                row.update(situation_assessment=result.situation_assessment,
                           help_needed=result.help_needed)
            passed = (result == case.expected and consumed == len(case.answers)
                      and row['question_count'] == case.question_count)
            row['status'] = 'passed' if passed else 'failed'
        except Exception:
            # A model/transport failure is not a user no-response. Exception
            # messages can contain request text or secrets, so omit them.
            row['status'] = 'error'
        rows.append(row)
    counts = Counter(row['status'] for row in rows)
    return {
        'evaluation': 'mock_state_flow_only' if provider == 'mock' else 'openai_semantic_cases',
        'counts': {status: counts[status]
                   for status in ('passed', 'failed', 'error', 'skipped_live_only')},
        'cases': rows,
    }


def main(argv=None):
    """Require explicit --provider openai before loading live credentials or calling it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('mock', 'openai'), default='mock')
    parser.add_argument('--cases', type=Path)
    parser.add_argument('--case-id', action='append', default=[])
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.cases)
        selected = set(args.case_id)
        if selected - {case.id for case in cases}:
            raise ValueError('unknown evaluation case ID')
        if selected:
            cases = [case for case in cases if case.id in selected]
        # Mock mode does not inspect the user's OpenAI environment or key file.
        environment = dict(os.environ) if args.provider == 'openai' else {}
        if args.provider == 'openai' and args.env_file is not None:
            load_env_file(args.env_file.expanduser(), target=environment)
        environment['MALBUT_AGENT_PROVIDER'] = args.provider
        settings = Settings.from_env(environment)
        settings.validate_for_dialogue()
        report = run_evaluation(cases, build_situation_factory(settings), provider=args.provider)
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
        if args.output is not None:
            args.output.write_text(encoded, encoding='utf-8')
        else:
            sys.stdout.write(encoded)
        return int(bool(report['counts']['failed'] or report['counts']['error']))
    except Exception:
        print('situation evaluation failed: check configuration and fixture format',
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
