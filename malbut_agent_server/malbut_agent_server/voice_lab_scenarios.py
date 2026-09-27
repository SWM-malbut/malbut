"""Run declared voice scenarios against an isolated Agent/Manager graph.

The graph supplies the real dialogue, Manager, and fake actuator observations.
This module never fabricates successful outcomes, imports ROS, or contacts a
model service. Each scenario constructs and closes its own graph.
"""

from copy import deepcopy
from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class Scenario:
    """One stable terminal-lab selection and its advertised coverage."""

    id: str
    title: str
    description: str


SCENARIOS = (
    Scenario('navigation', '목적지 이동', '거실 좌표 Goal과 Manager 성공 결과를 확인합니다.'),
    Scenario('follow', '사람 따라가기', '보이는 사람 추적의 실행 상태를 확인하며 완료를 가정하지 않습니다.'),
    Scenario('cancel-follow', '따라가기 취소', '지속 중인 따라가기를 음성으로 취소하고 종료를 확인합니다.'),
    Scenario('patrol-light', '가벼운 순찰', '가벼운 순찰이 thoroughness=0으로 전달됩니다.'),
    Scenario('patrol-normal', '기본 순찰', '기본 순찰이 thoroughness=1로 전달됩니다.'),
    Scenario('patrol-thorough', '꼼꼼한 순찰', '꼼꼼한 순찰이 thoroughness=2로 전달됩니다.'),
    Scenario('cancel-navigation', '이동 취소', '진행 중인 목적지 이동을 음성으로 취소합니다.'),
    Scenario('cancel-patrol', '순찰 취소', '진행 중인 순찰을 음성으로 취소합니다.'),
    Scenario('cancel-delayed', '취소 접수와 종료 분리', '늦은 취소 완료를 접수와 구분하고 실제 종료를 기다립니다.'),
    Scenario('abort-navigation', '이동 실행 실패', '이동 Action 실패가 Manager 실패 결과로 전달됩니다.'),
    Scenario('abort-follow', '추적 실행 실패', '추적 Action 실패가 Manager 실패 결과로 전달됩니다.'),
    Scenario('abort-patrol', '순찰 실행 실패', '순찰 Action 실패가 Manager 실패 결과로 전달됩니다.'),
    Scenario('reject-navigation', '이동 Goal 거절', '하위 이동 Action의 Goal 거절을 성공으로 알리지 않습니다.'),
    Scenario('reject-follow', '추적 Goal 거절', '하위 추적 Action의 Goal 거절을 성공으로 알리지 않습니다.'),
    Scenario('reject-patrol', '순찰 Goal 거절', '하위 순찰 Action의 Goal 거절을 성공으로 알리지 않습니다.'),
    Scenario('preempt-base', 'BASE 작업 선점', '따라가기 종료 후 새 순찰 Goal이 전달되는지 확인합니다.'),
    Scenario('unknown-place', '등록되지 않은 목적지', '미등록 베란다 요청이 Goal을 만들지 않습니다.'),
    Scenario('map-not-selected', '저장 지도 미선택', '매핑 상태에서 목적지 이동을 보내지 않습니다.'),
    Scenario('map-mismatch', '목적지와 지도 불일치', '목적지 설정과 다른 저장 지도에서는 이동하지 않습니다.'),
    Scenario('map-switching', '지도 전환 중', '지도 전환 중에는 목적지 이동을 보내지 않습니다.'),
    Scenario('map-error', '지도 상태 오류', '지도 오류 상태에서는 목적지 이동을 보내지 않습니다.'),
    Scenario('negation', '부정 명령', '이동·추적·순찰을 하지 말라는 말을 실행 요청으로 바꾸지 않습니다.'),
    Scenario('quotation', '인용문', '인용된 이동·추적·순찰·취소 명령을 실행하지 않습니다.'),
    Scenario('multiple-tasks', '복합 요청', '여러 작업 요청에서 일부 행동을 임의로 실행하지 않습니다.'),
    Scenario('relative-destination', '불명확한 목적지', '저기·뒤로 같은 표현으로 지도 좌표를 생성하지 않습니다.'),
    Scenario('duplicate', '중복 발화', '같은 발화 ID를 다시 보내도 Goal과 답변이 늘지 않습니다.'),
    Scenario('cancel-empty', '취소할 작업 없음', '작업 없는 취소 요청이 새 Goal을 만들지 않습니다.'),
    Scenario('manager-unavailable', 'Manager 연결 없음', '이동·추적·순찰 요청이 하위 Action으로 우회하지 않습니다.'),
    Scenario('navigation-disabled', '목적지 설정 없음', '목적지 기능 미설정 상태에서 이동 Goal을 보내지 않습니다.'),
)

COVERAGE_NOTE = (
    '이 목록은 명시된 명령과 장애 조건을 mock LLM·실제 Agent/Manager·가짜 Action으로 '
    '검증합니다. 실물 이동, 실제 음성 인식, 모든 자연어 표현의 이해를 검증하지 않습니다. '
    '전송 직전 TTL·대화/기억 변경·상황 대응 선점·TTS 발행 실패 등의 경쟁 조건은 '
    '별도 /regression 회귀 테스트 범위입니다.'
)

_MOTION = frozenset({'navigate_to_pose', 'follow_person', 'patrol'})
_COMMANDS = {
    'navigate_to_pose': '거실로 가',
    'follow_person': '나 따라와',
    'patrol': '순찰해 줘',
}
_TIMEOUT = 8.0


def _wait(graph, predicate, description, *, timeout=_TIMEOUT):
    deadline = time.monotonic() + timeout
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f'시간 초과: {description}')
        graph.spin(timeout=min(0.02, remaining))


def _observe(graph, seconds=0.15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        graph.spin(timeout=min(0.02, max(0.0, deadline - time.monotonic())))


def _events(graph, capability=None, *, kind=None, state=None):
    return [event for event in graph.events
            if (capability is None or event.get('capability_id') == capability)
            and (kind is None or event.get('kind') == kind)
            and (state is None or event.get('state') == state)]


def _check(evidence, condition, description):
    evidence.append({'check': description, 'passed': bool(condition)})
    if not condition:
        raise AssertionError(description)


def _send(graph, text):
    utterance_id = graph.send(text)
    reply = graph.wait_reply(utterance_id, timeout=_TIMEOUT)
    if (reply.get('utterance_id') != utterance_id
            or not isinstance(reply.get('text'), str) or not reply['text'].strip()
            or reply.get('kind') == 'progress'):
        raise AssertionError('해당 발화의 최종 응답이 필요합니다.')
    return utterance_id, reply


def _terminal(graph, capability, kind):
    _wait(graph, lambda: any(event.get('terminal') is True
                             for event in _events(graph, capability, kind=kind)),
          f'{capability} Manager {kind}')
    return next(event for event in reversed(_events(graph, capability, kind=kind))
                if event.get('terminal') is True)


def _goal(graph, capability):
    _wait(graph, lambda: any(item['capability_id'] == capability for item in graph.goals),
          f'{capability} 하위 Goal')
    return next(item for item in graph.goals if item['capability_id'] == capability)


def _payload(checks, goal, capability, level=1):
    args = goal['arguments']
    if capability == 'navigate_to_pose':
        pose = args.get('pose', {})
        position = pose.get('pose', {}).get('position', {})
        orientation = pose.get('pose', {}).get('orientation', {})
        correct = (
            pose.get('header', {}).get('frame_id') == 'map'
            and math.isclose(position.get('x', float('nan')), 1.0)
            and math.isclose(position.get('y', float('nan')), 1.0)
            and math.isclose(orientation.get('z', float('nan')), 0.0)
            and math.isclose(orientation.get('w', float('nan')), 1.0)
            and args.get('behavior_tree') == ''
        )
        _check(checks, correct, '실제 거실 fixture의 map 좌표와 방향을 전달함')
    elif capability == 'follow_person':
        _check(checks, args == {
            'target_mode': 0, 'target_person_id': '', 'desired_distance_m': 1.0,
        }, '화자 ID를 만들지 않고 보이는 사람·기본 거리로 추적 요청함')
    else:
        _check(checks, args == {'thoroughness': level}, f'순찰 꼼꼼함 {level}을 전달함')


def _motion(graph, checks, capability, *, outcome='success', text=None, level=1):
    graph.set_behavior(capability, outcome=outcome)
    _send(graph, text or _COMMANDS[capability])
    goal = _goal(graph, capability)
    _payload(checks, goal, capability, level)
    if outcome == 'hold':
        _check(checks, goal.get('accepted') is True, '하위 지속 Action이 Goal을 접수함')
        _wait(graph, lambda: bool(_events(graph, capability, state='RUNNING')),
              f'{capability} 실행 중')
        _observe(graph)
        _check(checks, not any(event.get('terminal') for event in _events(graph, capability)),
               '지속 실행 중인 작업을 완료로 간주하지 않음')
    else:
        terminal = _terminal(graph, capability, 'succeeded' if outcome == 'success' else 'failed')
        _check(checks, terminal.get('terminal') is True, 'Manager의 확정된 종료 결과를 관찰함')
        if outcome == 'reject':
            _check(checks, goal.get('accepted') is False, '하위 Action 서버가 Goal을 거절함')
        else:
            _check(checks, goal.get('accepted') is True, '하위 Action 서버가 Goal을 접수함')
    _check(checks, len(graph.goals) == 1, '하위 Goal을 정확히 한 번 전달함')


def _cancel(graph, checks, capability, *, delayed=False, timeline=()):
    graph.set_behavior(capability, outcome='hold', cancel_delay_s=0.6 if delayed else 0.0)
    _send(graph, _COMMANDS[capability])
    _wait(graph, lambda: bool(_events(graph, capability, state='RUNNING')),
          f'{capability} 실행 중')
    _, reply = _send(graph, '취소해 줘')
    _check(checks, '종료 여부' in reply['text'], '취소 요청 답변은 종료 확인 전임을 알림')
    terminal = _terminal(graph, capability, 'canceled')
    request_id = terminal.get('request_id')
    _check(checks, any(event.get('request_id') == request_id
                       for event in _events(graph, capability, kind='cancel_requested')),
           '동일한 음성 소유 작업에 취소 요청과 최종 취소 결과가 연결됨')
    _check(checks, len(graph.goals) == 1, '취소는 새 실행 Goal을 만들지 않음')
    _check(checks, not _events(graph, capability, kind='succeeded'),
           '취소된 작업을 성공 완료로 알리지 않음')
    if delayed:
        requested = [event for event in timeline
                     if event.get('event') == 'mission'
                     and event.get('request_id') == request_id
                     and event.get('kind') == 'cancel_requested']
        accepted = [event for event in timeline
                    if event.get('event') == 'mission'
                    and event.get('request_id') == request_id
                    and event.get('kind') == 'cancel_accepted']
        finished = [event for event in timeline
                    if event.get('event') == 'mission'
                    and event.get('request_id') == request_id
                    and event.get('kind') == 'canceled']
        _check(checks, bool(requested and accepted and finished),
               '취소 요청·Manager 접수 확인·최종 종료를 각각 관찰함')
        _check(checks, accepted[0]['observed_at'] < finished[-1]['observed_at'],
               'Manager 취소 접수 확인과 실제 최종 종료가 별도 이벤트임')
        _check(checks, finished[-1]['observed_at'] - requested[0]['observed_at'] >= 0.45,
               '취소 요청 이후 지연된 최종 종료를 별도로 확인함')


def _no_motion(graph, checks, texts):
    for text in texts:
        _, reply = _send(graph, text)
        _check(checks, reply.get('kind') != 'error', f'요청을 비실행 응답으로 처리함: {text}')
    _observe(graph)
    _check(checks, not graph.goals, '하위 이동·추적·순찰 Goal이 없음')
    _check(checks, not any(event.get('capability_id') in _MOTION for event in graph.events),
           'Manager에도 이동·추적·순찰 실행 요청을 보내지 않음')


def _preempt(graph, checks, timeline):
    graph.set_behavior('follow_person', outcome='hold', cancel_delay_s=0.2)
    graph.set_behavior('patrol', outcome='success')
    _send(graph, '따라와')
    _wait(graph, lambda: bool(_events(graph, 'follow_person', state='RUNNING')), '추적 시작')
    _send(graph, '순찰해 줘')
    previous = _terminal(graph, 'follow_person', 'failed')
    _terminal(graph, 'patrol', 'succeeded')
    _check(checks, 'preempted' in previous.get('reason', ''),
           'Manager가 서버 주도 교체를 사용자 취소와 구분하여 보고함')
    _check(checks, [goal['capability_id'] for goal in graph.goals] == ['follow_person', 'patrol'],
           '같은 BASE를 쓰는 추적 다음에 순찰이 전달됨')
    markers = [event for event in timeline
               if event.get('kind') == 'actuator_terminal'
               and event.get('capability_id') == 'follow_person'
               and event.get('status') == 'canceled']
    _check(checks, bool(markers), '하위 추적 Action의 취소 종료를 관찰함')
    _check(checks, graph.goals[1]['received_at'] >= markers[-1]['observed_at'],
           '기존 하위 Action이 종료된 후 새 BASE Goal이 도착함')


def _quotation(graph, checks):
    # An empty graph cannot distinguish a correctly ignored quoted cancel from
    # a wrongly selected cancellation tool. Keep a real observed task active.
    graph.set_behavior('follow_person', outcome='hold')
    _send(graph, '따라와')
    _goal(graph, 'follow_person')
    _wait(graph, lambda: bool(_events(graph, 'follow_person', state='RUNNING')), '추적 시작')
    for text in ('"거실로 가"라고 말했어', '"따라와"라는 문장을 설명해',
                 '"순찰해"라고 말했어', '"취소해"라고 말해'):
        _, reply = _send(graph, text)
        _check(checks, reply.get('kind') != 'error', f'인용문을 비실행 응답으로 처리함: {text}')
    _observe(graph)
    _check(checks, [goal['capability_id'] for goal in graph.goals] == ['follow_person'],
           '인용된 이동·추적·순찰 명령이 새 Goal을 만들지 않음')
    _check(checks, not _events(graph, kind='cancel_requested'),
           '인용된 취소 명령이 실제 진행 중인 작업에 취소를 보내지 않음')
    _check(checks, not any(event.get('terminal') for event in graph.events),
           '인용문 처리 뒤에도 원래 작업이 계속 실행 중임')


def _execute(scenario_id, graph, checks, timeline):
    if scenario_id == 'navigation':
        return _motion(graph, checks, 'navigate_to_pose')
    if scenario_id == 'follow':
        return _motion(graph, checks, 'follow_person', outcome='hold')
    if scenario_id.startswith('patrol-'):
        level, text = {
            'patrol-light': (0, '가볍게 순찰해 줘'),
            'patrol-normal': (1, '순찰해 줘'),
            'patrol-thorough': (2, '꼼꼼히 순찰해 줘'),
        }[scenario_id]
        return _motion(graph, checks, 'patrol', level=level, text=text)
    if scenario_id in {'cancel-follow', 'cancel-navigation', 'cancel-patrol', 'cancel-delayed'}:
        capability = {
            'cancel-follow': 'follow_person', 'cancel-delayed': 'follow_person',
            'cancel-navigation': 'navigate_to_pose', 'cancel-patrol': 'patrol',
        }[scenario_id]
        return _cancel(graph, checks, capability, delayed=scenario_id == 'cancel-delayed',
                       timeline=timeline)
    if scenario_id.startswith(('abort-', 'reject-')):
        outcome, name = scenario_id.split('-', 1)
        capability = {'navigation': 'navigate_to_pose', 'follow': 'follow_person',
                      'patrol': 'patrol'}[name]
        return _motion(graph, checks, capability, outcome=outcome)
    if scenario_id == 'preempt-base':
        return _preempt(graph, checks, timeline)
    if scenario_id == 'quotation':
        return _quotation(graph, checks)
    if scenario_id.startswith('map-'):
        mode, variant = {
            'map-not-selected': ('MAPPING', 'none'),
            'map-mismatch': ('LOCALIZATION', 'other'),
            'map-switching': ('SWITCHING', 'home'),
            'map-error': ('ERROR', 'home'),
        }[scenario_id]
        graph.set_map(mode=mode, variant=variant)
        return _no_motion(graph, checks, ['거실로 가'])
    noncommands = {
        'unknown-place': ['베란다로 가'],
        'negation': ['거실로 가지 마', '따라오지 말아줘', '순찰하지 말아줘'],
        'multiple-tasks': ['거실로 가고 주방으로 이동해', '따라와 그리고 순찰해'],
        'relative-destination': ['저기로 가', '뒤로 가'],
        'navigation-disabled': ['거실로 가'],
    }
    if scenario_id in noncommands:
        return _no_motion(graph, checks, noncommands[scenario_id])
    if scenario_id == 'duplicate':
        graph.set_behavior('follow_person', outcome='hold')
        uid, _ = _send(graph, '따라와')
        _goal(graph, 'follow_person')
        before = len([reply for reply in graph.replies
                      if reply.get('utterance_id') == uid and reply.get('kind') != 'progress'])
        graph.send('따라와', utterance_id=uid)
        _observe(graph, 0.35)
        after = len([reply for reply in graph.replies
                     if reply.get('utterance_id') == uid and reply.get('kind') != 'progress'])
        _check(checks, len(graph.goals) == 1, '같은 발화 ID가 새 Goal을 만들지 않음')
        _check(checks, before == after == 1, '같은 발화 ID의 최종 답변을 반복하지 않음')
        return
    if scenario_id == 'cancel-empty':
        _, reply = _send(graph, '취소해 줘')
        _check(checks, '동작이 없어요' in reply['text'], '취소할 음성 작업이 없음을 사실대로 안내함')
        _check(checks, not graph.goals and not graph.events, '작업 없는 취소가 Manager 실행을 만들지 않음')
        return
    if scenario_id == 'manager-unavailable':
        for capability, text in _COMMANDS.items():
            _send(graph, text)
            event = _terminal(graph, capability, 'unavailable')
            _check(checks, event.get('accepted') is not True,
                   f'Manager 없는 {capability} 요청을 접수됐다고 표시하지 않음')
        _check(checks, not graph.goals, 'Manager가 없어도 하위 Action으로 직접 우회하지 않음')
        return
    raise ValueError('scenario has no runner')


def run_scenario(scenario_id, graph_factory, output):
    """Run one independent scenario and return inspectable observed evidence."""
    scenario = next((item for item in SCENARIOS if item.id == scenario_id), None)
    if scenario is None:
        raise ValueError(f'unknown scenario: {scenario_id}')
    output({'event': 'scenario_start', 'id': scenario.id, 'title': scenario.title,
            'description': scenario.description})
    started = time.monotonic()
    timeline, checks = [], []
    graph = None
    result = {'id': scenario.id, 'title': scenario.title, 'passed': False, 'evidence': {}}

    def observe(event):
        value = deepcopy(event)
        value.setdefault('observed_at', time.monotonic())
        timeline.append(value)
        output(value)

    try:
        graph = graph_factory(output=observe)
        graph.start(manager_enabled=scenario_id != 'manager-unavailable',
                    navigation_enabled=scenario_id != 'navigation-disabled')
        _execute(scenario_id, graph, checks, timeline)
        result['passed'] = True
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
    finally:
        # Capture before cleanup so its automatic cancellation cannot turn a
        # missing user cancellation or terminal result into passing evidence.
        result['evidence'] = {
            'checks': deepcopy(checks),
            'goals': deepcopy(graph.goals) if graph is not None else [],
            'events': deepcopy(graph.events) if graph is not None else [],
            'replies': deepcopy(graph.replies) if graph is not None else [],
            'timeline': deepcopy(timeline),
        }
        if graph is not None:
            try:
                graph.close()
            except Exception as error:
                result['passed'] = False
                result['cleanup_error'] = f'{type(error).__name__}: {error}'
        result['duration_s'] = round(time.monotonic() - started, 3)
    output({'event': 'scenario_result', **result})
    return result


def run_all(graph_factory, output):
    """Run the declared catalog independently, preserving failures for review."""
    return [run_scenario(scenario.id, graph_factory, output) for scenario in SCENARIOS]
