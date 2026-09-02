"""Offline tests for the interactive Front Route experiment CLI."""

import ast
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import urllib.request

import pytest

from malbut_agent_server.adapters.outbound.openai_front_route_candidate import (  # noqa: E501
    FrontRouteCandidateError,
    FrontRouteCandidateResult,
)
from malbut_agent_server.domain.front_route import FrontRoute
from malbut_agent_server.front_route_inspector import main, run_console


class _FakeClassifier:
    def __init__(
        self,
        route: FrontRoute = FrontRoute.ROBOT_ACTION_REQUEST,
    ) -> None:
        self.route = route
        self.requests = []

    def classify(self, request):
        self.requests.append(request)
        return FrontRouteCandidateResult(
            route=self.route,
            model='gpt-test-router',
            latency_ms=12.3456,
            input_tokens=20,
            output_tokens=5,
        )


def _run_main(
    text: str,
    classifier,
    *extra_args: str,
):
    stdout = io.StringIO()
    stderr = io.StringIO()
    factory_calls = []

    def factory(api_key, model, timeout_seconds, base_url):
        factory_calls.append(
            (api_key, model, timeout_seconds, base_url)
        )
        return classifier

    code = main(
        [
            '--allow-live-provider',
            '--env-file',
            '/dev/null',
            '--once',
            *extra_args,
        ],
        classifier_factory=factory,
        environ={'OPENAI_API_KEY': 'test-only-secret-canary'},
        stdin=io.StringIO(text),
        stdout=stdout,
        stderr=stderr,
        id_factory=lambda: 'fixed-id',
    )
    return code, stdout.getvalue(), stderr.getvalue(), factory_calls


def test_once_classifies_one_line_and_never_prints_the_utterance() -> None:
    """One direct input produces one candidate and no content echo."""
    canary = '저그 저 거실좀 가봐라 PRIVATE-CANARY'
    classifier = _FakeClassifier()

    code, output, errors, factory_calls = _run_main(
        canary + '\nignored\n',
        classifier,
    )

    assert code == 0
    assert len(classifier.requests) == 1
    assert classifier.requests[0].user_message == canary
    assert classifier.requests[0].request_id == 'front-probe-fixed-id'
    assert 'candidate_route : robot_action_request' in output
    assert 'production_route: -' in output
    assert 'promoted        : false' in output
    assert 'authority       : false' in output
    assert canary not in output
    assert canary not in errors
    assert factory_calls == [
        (
            'test-only-secret-canary',
            'gpt-4.1-mini',
            2,
            'https://api.openai.com/v1',
        )
    ]
    assert 'test-only-secret-canary' not in output + errors


def test_json_output_has_only_content_free_observation_fields() -> None:
    """Machine-readable output cannot become a transcript or authority."""
    classifier = _FakeClassifier(FrontRoute.ROBOT_STATUS_QUERY)
    code, output, errors, _factory_calls = _run_main(
        '배터리 상태 알려줘\n',
        classifier,
        '--json',
    )

    assert code == 0
    record = json.loads(output)
    assert record == {
        'schema_version': 1,
        'ordinal': 1,
        'outcome': 'candidate_observed',
        'candidate_route': 'robot_status_query',
        'production_route': None,
        'promoted': False,
        'latency_ms': 12.346,
        'model': 'gpt-4.1-mini',
        'input_tokens': 20,
        'output_tokens': 5,
        'classifier_calls': 1,
        'authority': False,
        'error_code': None,
    }
    assert '배터리 상태 알려줘' not in output + errors


def test_interactive_mode_ignores_blank_and_stops_before_quit() -> None:
    """Local commands and empty input never reach the classifier."""
    classifier = _FakeClassifier(FrontRoute.GENERAL_CONVERSATION)
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = run_console(
        classifier,
        once=False,
        json_output=True,
        model_label='gpt-test-router',
        stdin=io.StringIO('\n안녕\n오늘 날씨 어때\n/quit\n나중 문장\n'),
        stdout=stdout,
        stderr=stderr,
        id_factory=iter(('id-1', 'id-2')).__next__,
    )

    assert code == 0
    assert [item.user_message for item in classifier.requests] == [
        '안녕',
        '오늘 날씨 어때',
    ]
    assert [
        json.loads(line)['ordinal']
        for line in stdout.getvalue().splitlines()
    ] == [1, 2]
    assert '안녕' not in stdout.getvalue() + stderr.getvalue()
    assert '오늘 날씨 어때' not in stdout.getvalue() + stderr.getvalue()


def test_live_opt_in_is_checked_before_key_or_factory_use() -> None:
    """The CLI remains inert until paid traffic is explicitly enabled."""
    called = False

    def factory(*_args):
        nonlocal called
        called = True
        raise AssertionError('must not construct classifier')

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ['--env-file', '/dev/null', '--once'],
        classifier_factory=factory,
        environ={'OPENAI_API_KEY': 'test-only-secret'},
        stdin=io.StringIO('거실로 가줘\n'),
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 2
    assert called is False
    assert stdout.getvalue() == ''
    assert '--allow-live-provider' in stderr.getvalue()
    assert '거실로 가줘' not in stderr.getvalue()
    assert 'test-only-secret' not in stderr.getvalue()


def test_missing_key_fails_before_classifier_construction() -> None:
    """A missing credential is a bounded configuration error."""
    called = False

    def factory(*_args):
        nonlocal called
        called = True
        raise AssertionError('must not construct classifier')

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        [
            '--allow-live-provider',
            '--env-file',
            '/dev/null',
            '--once',
        ],
        classifier_factory=factory,
        environ={},
        stdin=io.StringIO('거실로 가줘\n'),
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 2
    assert called is False
    assert stdout.getvalue() == ''
    assert 'OPENAI_API_KEY' in stderr.getvalue()
    assert '거실로 가줘' not in stderr.getvalue()


def test_check_validates_configuration_without_classification() -> None:
    """A safe preflight creates no API request."""
    classifier = _FakeClassifier()
    code, output, errors, factory_calls = _run_main(
        '',
        classifier,
        '--check',
    )

    assert code == 0
    assert classifier.requests == []
    assert len(factory_calls) == 1
    assert output == 'configuration=ok network_calls=0 authority=false\n'
    assert errors == ''


def test_planner_model_environment_does_not_select_front_model() -> None:
    """The experiment model stays independent from OPENAI_MODEL."""
    classifier = _FakeClassifier()
    captured = []

    def factory(api_key, model, timeout_seconds, base_url):
        del api_key, timeout_seconds, base_url
        captured.append(model)
        return classifier

    code = main(
        [
            '--allow-live-provider',
            '--env-file',
            '/dev/null',
            '--check',
        ],
        classifier_factory=factory,
        environ={
            'OPENAI_API_KEY': 'test-only-key',
            'OPENAI_MODEL': 'planner-model-must-not-be-used',
        },
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert code == 0
    assert captured == ['gpt-4.1-mini']


def test_default_check_mode_cannot_open_a_network_connection(
    monkeypatch,
) -> None:
    """The concrete constructor remains lazy during configuration check."""

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError('network must remain unused')

    monkeypatch.setattr(
        urllib.request,
        'build_opener',
        forbidden_network,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        [
            '--allow-live-provider',
            '--env-file',
            '/dev/null',
            '--check',
        ],
        environ={'OPENAI_API_KEY': 'test-only-key'},
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert stdout.getvalue() == (
        'configuration=ok network_calls=0 authority=false\n'
    )
    assert stderr.getvalue() == ''


@pytest.mark.parametrize(
    ('failure', 'expected_code'),
    [
        (
            FrontRouteCandidateError('provider_timeout'),
            'provider_timeout',
        ),
        (RuntimeError('PRIVATE-CANARY'), 'candidate_classifier_failed'),
    ],
)
def test_classifier_failures_are_bounded_and_not_retried(
    failure: Exception,
    expected_code: str,
) -> None:
    """No exception text, retry, or false abstain escapes the CLI."""

    class FailingClassifier:
        def __init__(self):
            self.calls = 0

        def classify(self, request):
            del request
            self.calls += 1
            raise failure

    classifier = FailingClassifier()
    code, output, errors, _factory_calls = _run_main(
        '비밀 입력 PRIVATE-UTTERANCE\n',
        classifier,
        '--json',
    )

    assert code == 1
    assert classifier.calls == 1
    record = json.loads(output)
    assert record['outcome'] == 'error'
    assert record['candidate_route'] is None
    assert record['production_route'] is None
    assert record['classifier_calls'] == 1
    assert record['error_code'] == expected_code
    assert 'PRIVATE-CANARY' not in output + errors
    assert 'PRIVATE-UTTERANCE' not in output + errors


def test_too_long_line_is_drained_before_next_input() -> None:
    """One oversized physical line cannot become several requests."""
    classifier = _FakeClassifier(FrontRoute.GENERAL_CONVERSATION)
    stdout = io.StringIO()
    stderr = io.StringIO()
    text = ('가' * 2001) + '\n안녕\n/quit\n'

    code = run_console(
        classifier,
        once=False,
        json_output=True,
        model_label='gpt-test-router',
        stdin=io.StringIO(text),
        stdout=stdout,
        stderr=stderr,
        id_factory=lambda: 'valid-id',
    )

    assert code == 1
    assert [item.user_message for item in classifier.requests] == ['안녕']
    records = [
        json.loads(line) for line in stdout.getvalue().splitlines()
    ]
    assert records[0]['error_code'] == 'invalid_input'
    assert records[0]['classifier_calls'] == 0
    assert records[1]['candidate_route'] == 'general_conversation'


@pytest.mark.parametrize('text', ['', '   \n'])
def test_empty_once_input_has_zero_classifier_calls(text: str) -> None:
    """Silence and EOF cannot accidentally become paid requests."""
    classifier = _FakeClassifier()
    code, output, errors, _factory_calls = _run_main(
        text,
        classifier,
        '--json',
    )

    assert code == 1
    assert classifier.requests == []
    record = json.loads(output)
    assert record['error_code'] == 'invalid_input'
    assert record['classifier_calls'] == 0
    assert errors


def test_wrong_candidate_type_never_becomes_a_route() -> None:
    """Duck-typed or expanded adapter results cannot cross the probe."""

    class WrongClassifier:
        def classify(self, request):
            del request
            return {'route': 'robot_action_request'}

    code, output, _errors, _factory_calls = _run_main(
        '거실로 가줘\n',
        WrongClassifier(),
        '--json',
    )

    assert code == 1
    record = json.loads(output)
    assert record['candidate_route'] is None
    assert record['error_code'] == 'candidate_result_invalid'


def test_remote_model_metadata_cannot_escape_through_cli_output() -> None:
    """Only the configured model label may appear in a probe record."""

    class TaintedClassifier:
        def classify(self, request):
            del request
            return FrontRouteCandidateResult(
                route=FrontRoute.GENERAL_CONVERSATION,
                model='PRIVATE-REMOTE-MODEL-CANARY',
                latency_ms=1.0,
            )

    code, output, errors, _factory_calls = _run_main(
        '안녕\n',
        TaintedClassifier(),
        '--json',
    )

    assert code == 0
    assert json.loads(output)['model'] == 'gpt-4.1-mini'
    assert 'PRIVATE-REMOTE-MODEL-CANARY' not in output + errors


def test_inspector_has_no_robot_runtime_or_persistence_imports() -> None:
    """The inspector source has no direct robot or persistence imports."""
    source_path = Path(__file__).parents[1] / (
        'malbut_agent_server/front_route_inspector.py'
    )
    tree = ast.parse(source_path.read_text(encoding='utf-8'))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_fragments = {
        'factory',
        'config',
        'orchestrator',
        'conversation',
        'sqlite3',
        'http_server',
        'gateway',
        'approved_action_worker',
        'malbut_gazebo',
        'malbut_scenarios',
        'rclpy',
    }
    assert all(
        not any(fragment in name for fragment in forbidden_fragments)
        for name in imported
    )


def test_import_graph_does_not_load_agent_db_or_robot_runtime() -> None:
    """Lazy package facades keep the Inspector process lightweight."""
    package_root = Path(__file__).parents[1]
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(package_root)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    code = (
        'import json, sys; '
        'import malbut_agent_server.front_route_inspector; '
        'names=['
        '"malbut_agent_server.orchestrator",'
        '"malbut_agent_server.conversation",'
        '"malbut_agent_server.gateway",'
        '"malbut_agent_server.adapters.outbound.'
        'sqlite_action_repository"'
        ']; '
        'print(json.dumps([name for name in names if name in sys.modules]))'
    )
    completed = subprocess.run(
        [sys.executable, '-c', code],
        cwd=package_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []


def test_lazy_facades_preserve_existing_symbol_identity() -> None:
    """Import optimization does not create wrapper compatibility types."""
    import malbut_agent_server as package
    from malbut_agent_server import orchestrator
    from malbut_agent_server.adapters import outbound
    from malbut_agent_server.adapters.outbound import (
        sqlite_action_repository,
    )

    assert package.AgentOrchestrator is orchestrator.AgentOrchestrator
    assert (
        outbound.SQLiteActionRepository
        is sqlite_action_repository.SQLiteActionRepository
    )


def test_probe_does_not_persist_input_or_log_it(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    """The CLI writes no transcript file or Python log record."""
    monkeypatch.chdir(tmp_path)
    canary = 'PRIVATE-FILESYSTEM-UTTERANCE-CANARY'
    classifier = _FakeClassifier(FrontRoute.GENERAL_CONVERSATION)
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(
        [
            '--allow-live-provider',
            '--env-file',
            '/dev/null',
            '--once',
            '--json',
        ],
        classifier_factory=(
            lambda _key, _model, _timeout, _base: classifier
        ),
        environ={'OPENAI_API_KEY': 'test-only-key'},
        stdin=io.StringIO(canary + '\n'),
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert list(tmp_path.iterdir()) == []
    assert canary not in stdout.getvalue() + stderr.getvalue()
    assert canary not in caplog.text


def test_setup_declares_the_installed_inspector_entrypoint() -> None:
    """The package metadata declares the Inspector console script."""
    setup_path = Path(__file__).parents[1] / 'setup.py'
    setup_text = setup_path.read_text(encoding='utf-8')
    assert 'malbut-front-route-inspect = ' in setup_text
    assert 'malbut_agent_server.front_route_inspector:main' in setup_text
