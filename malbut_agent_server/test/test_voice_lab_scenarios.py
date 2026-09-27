"""Check lab verdicts against observations and keep scenario cleanup independent."""

from copy import deepcopy

import pytest

from malbut_agent_server import voice_lab_scenarios as scenarios


class ObservedNavigationGraph:
    """Supply minimal graph observations without making runner assertions pass itself."""

    def __init__(self, output, *, position=1.0, terminal=True, start_error=False):
        self.output = output
        self.position = position
        self.terminal = terminal
        self.start_error = start_error
        self.events, self.goals, self.replies = [], [], []
        self.started, self.closed = False, False
        self.options = None

    def start(self, **options):
        self.options = options
        self.started = True
        if self.start_error:
            raise RuntimeError('test startup failed')

    def set_behavior(self, capability, **behavior):
        self.behavior = (capability, behavior)

    def send(self, text, utterance_id=None):
        uid = utterance_id or 'utterance'
        self.replies.append({'utterance_id': uid, 'kind': 'answer', 'text': '요청을 보냈어요.'})
        self.goals.append({
            'capability_id': 'navigate_to_pose', 'accepted': True,
            'arguments': {
                'pose': {'header': {'frame_id': 'map'}, 'pose': {
                    'position': {'x': self.position, 'y': 1.0, 'z': 0.0},
                    'orientation': {'x': 0.0, 'y': 0.0, 'z': 0.0, 'w': 1.0},
                }},
                'behavior_tree': '',
            },
        })
        if self.terminal:
            event = {'capability_id': 'navigate_to_pose', 'kind': 'succeeded',
                     'state': 'SUCCEEDED', 'terminal': True}
            self.events.append(event)
            self.output({'event': 'mission', **event})
        return uid

    def wait_reply(self, uid, timeout):
        return next(reply for reply in self.replies if reply['utterance_id'] == uid)

    def spin(self, timeout=0.02):
        pass

    def close(self):
        self.closed = True
        # Cleanup must not become evidence that the user's scenario succeeded.
        self.events.append({'kind': 'cleanup_only'})


def test_catalog_is_unique_and_advertises_advanced_tests_separately():
    names = [item.id for item in scenarios.SCENARIOS]
    assert len(names) == len(set(names)) == 29
    assert {
        'navigation', 'follow', 'cancel-follow', 'patrol-light', 'patrol-normal',
        'patrol-thorough', 'preempt-base', 'cancel-delayed', 'manager-unavailable',
    } <= set(names)
    assert all(item.title and item.description for item in scenarios.SCENARIOS)
    assert '/regression' in scenarios.COVERAGE_NOTE
    assert '실물' in scenarios.COVERAGE_NOTE


def test_passing_result_contains_actual_observations_and_closes_graph():
    graphs, output = [], []

    def factory(**kwargs):
        graph = ObservedNavigationGraph(**kwargs)
        graphs.append(graph)
        return graph

    result = scenarios.run_scenario('navigation', factory, output.append)
    assert result['passed']
    assert graphs[0].closed
    assert graphs[0].options == {'manager_enabled': True, 'navigation_enabled': True}
    assert result['evidence']['goals'] == graphs[0].goals
    assert result['evidence']['events'] == graphs[0].events[:-1]
    assert result['evidence']['replies'] == graphs[0].replies
    assert result['evidence']['checks']
    assert output[0]['event'] == 'scenario_start'
    assert output[-1]['event'] == 'scenario_result'
    assert any(item['event'] == 'mission' for item in result['evidence']['timeline'])
    graphs[0].goals.clear()
    assert result['evidence']['goals']  # Evidence is detached from subsequent graph cleanup.


def test_wrong_destination_cannot_pass_even_with_a_success_event():
    graph = None

    def factory(**kwargs):
        nonlocal graph
        graph = ObservedNavigationGraph(position=9.0, **kwargs)
        return graph

    result = scenarios.run_scenario('navigation', factory, lambda event: None)
    assert not result['passed']
    assert '거실' in result['error']
    assert graph.closed
    assert result['evidence']['checks'][-1]['passed'] is False


def test_missing_manager_result_does_not_become_success(monkeypatch):
    def require_now(graph, predicate, description, **kwargs):
        if not predicate():
            raise AssertionError('missing terminal observation')

    monkeypatch.setattr(scenarios, '_wait', require_now)
    graphs = []

    def factory(**kwargs):
        graph = ObservedNavigationGraph(terminal=False, **kwargs)
        graphs.append(graph)
        return graph

    result = scenarios.run_scenario('navigation', factory, lambda event: None)
    assert not result['passed']
    assert 'missing terminal observation' in result['error']
    assert not result['evidence']['events']
    assert graphs[0].closed


def test_startup_failure_is_reported_and_cleanup_still_runs():
    graph = ObservedNavigationGraph(lambda event: None, start_error=True)
    result = scenarios.run_scenario('navigation', lambda **kwargs: graph, lambda event: None)
    assert not result['passed']
    assert 'startup failed' in result['error']
    assert graph.closed


def test_invalid_id_never_constructs_a_graph():
    def forbidden(**kwargs):
        raise AssertionError('should not start a graph')

    with pytest.raises(ValueError, match='unknown scenario'):
        scenarios.run_scenario('not-a-scenario', forbidden, lambda event: None)


def test_run_all_keeps_independent_failures_and_uses_every_catalog_item(monkeypatch):
    calls = []

    def run(identifier, factory, output):
        result = {'id': identifier, 'passed': identifier != 'navigation'}
        calls.append((identifier, factory, output))
        return deepcopy(result)

    factory, output = object(), object()
    monkeypatch.setattr(scenarios, 'run_scenario', run)
    results = scenarios.run_all(factory, output)
    assert [item['id'] for item in results] == [item.id for item in scenarios.SCENARIOS]
    assert all(call[1:] == (factory, output) for call in calls)
    assert not results[0]['passed'] and results[-1]['passed']


@pytest.mark.parametrize('incorrect_cancel', [False, True])
def test_quotation_scenario_detects_wrong_cancel_with_an_active_task(
    monkeypatch, incorrect_cancel,
):
    class FollowingGraph(ObservedNavigationGraph):
        def send(self, text, utterance_id=None):
            uid = 'utterance-' + str(len(self.replies))
            self.replies.append({'utterance_id': uid, 'kind': 'answer', 'text': '응답'})
            if text == '따라와':
                self.goals.append({'capability_id': 'follow_person'})
                self.events.append({'capability_id': 'follow_person',
                                    'kind': 'progress', 'state': 'RUNNING', 'terminal': False})
            elif '취소' in text and incorrect_cancel:
                self.events.append({'capability_id': 'follow_person',
                                    'kind': 'cancel_requested', 'terminal': False})
            return uid

    monkeypatch.setattr(scenarios, '_observe', lambda *args, **kwargs: None)
    result = scenarios.run_scenario('quotation', FollowingGraph, lambda event: None)
    assert result['passed'] is not incorrect_cancel
    assert len(result['evidence']['goals']) == 1
    if incorrect_cancel:
        assert '취소' in result['error']
