"""Terminal controls preserve evidence and never interpret invalid input as motion."""

import io
import json
from types import SimpleNamespace

import pytest

from malbut_agent_server.voice_lab import Report, Terminal


class Graph:
    def __init__(self, output):
        self.output = output
        self.sent = []
        self.behaviors = []
        self.closed = False

    def start(self):
        pass

    def spin(self, timeout=0.02):
        pass

    def send(self, text, utterance_id=None):
        uid = utterance_id or 'utterance-' + str(len(self.sent))
        self.sent.append((uid, text))
        return uid

    def wait_reply(self, uid):
        return {'utterance_id': uid, 'text': 'accepted'}

    def set_behavior(self, *args, **kwargs):
        self.behaviors.append((args, kwargs))

    def close(self):
        self.closed = True


@pytest.fixture
def terminal(tmp_path):
    report = Report(tmp_path / 'results.json')
    runner = Terminal(Graph, report)
    runner.start()
    yield runner
    runner.stop()
    report.close()


@pytest.mark.parametrize('command', [
    '/behavior nav hold nan', '/behavior nav hold -1',
    '/behavior nav hold 31', '/behavior unknown success',
    '/behavior follow launch', '/map guessed_place', '/cancel all',
    '/run', '/something 따라와',
])
def test_invalid_controls_never_become_transcripts(terminal, command):
    with pytest.raises(ValueError):
        terminal.command(command)
    assert terminal.graph.sent == []
    assert terminal.graph.behaviors == []


def test_repeat_reuses_identity_and_does_not_wait_for_a_second_reply(terminal):
    terminal.command('따라와')
    terminal.graph.wait_reply = lambda uid: pytest.fail('duplicate must not wait for new reply')
    terminal.command('/repeat')
    assert terminal.graph.sent == [('utterance-0', '따라와')] * 2


def test_scripted_input_recovers_from_invalid_control_then_cancels(terminal):
    terminal.interact(io.StringIO('/behavior nav hold nan\n따라와\n/cancel\n/quit\n'))
    assert [text for _, text in terminal.graph.sent] == ['따라와', '멈춰']


def test_report_keeps_all_feedback_but_deduplicates_console(tmp_path, capsys):
    report = Report(tmp_path / 'custom.jsonl')
    item = {'event': 'mission', 'kind': 'progress', 'state': 'RUNNING',
            'request_id': 'one', 'capability_id': 'patrol'}
    report.event(item)
    report.event(item)
    report.results.append({'id': 'case', 'passed': False, 'evidence': {'goals': 0}})
    report.close()
    assert capsys.readouterr().out.count('[Manager]') == 1
    assert report.path != report.log_path
    observations = [json.loads(line) for line in report.log_path.read_text().splitlines()]
    assert len(observations) == 2
    summary = json.loads(report.path.read_text())
    assert summary['failed'] == 1 and summary['passed'] == 0


def test_failed_start_closes_partially_constructed_graph(tmp_path):
    graph = SimpleNamespace(close=lambda: None)
    closed = []

    def start():
        raise RuntimeError('startup failed')

    graph.start = start
    graph.close = lambda: closed.append(True)
    report = Report(tmp_path / 'results.json')
    terminal = Terminal(lambda **_: graph, report)
    with pytest.raises(RuntimeError, match='startup failed'):
        terminal.start()
    assert closed == [True]
    assert terminal.graph is None
    report.close()


def test_finished_case_is_persisted_before_next_case_can_be_interrupted(tmp_path):
    report = Report(tmp_path / 'results.json')
    report.event({'event': 'scenario_result', 'id': 'done', 'passed': True,
                  'evidence': {'confirmed_terminal': 'CANCELED'}})
    report.event({'event': 'scenario_start', 'id': 'pending'})
    on_disk = json.loads(report.path.read_text())
    assert [result['id'] for result in on_disk['results']] == ['done']
    assert on_disk['passed'] == 1 and on_disk['failed'] == 0
    report.close()
