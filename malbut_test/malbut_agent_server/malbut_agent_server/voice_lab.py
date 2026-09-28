"""Interactive terminal for isolated voice-to-Manager experiments."""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import shlex
import subprocess
import sys
from threading import Thread
import time


HELP = """
문장을 입력하면 STT의 최종 발화로 전달합니다.
  자유 대화 모드에서는 일상 대화와 질문을 이어서 할 수 있습니다.
  거실로 가줘 / 따라와 / 순찰해 / 꼼꼼히 순찰해 / 멈춰

/scenarios                   시나리오 목록
/run ID                      시나리오 하나 실행 (목록 번호도 가능)
/all                         모든 시나리오 실행, 결과 JSON 저장
/regression                  만료·대화 변경·TTS 실패 등 집중 회귀 실행
/status                      현재 Manager 상태와 하위 Goal 확인
/behavior 기능 결과 [초] [취소지연초]
                             기능: nav, follow, patrol
                             결과: success, hold, abort, reject
                             예: /behavior patrol hold
/map home|other|none|mapping|switching|error
                             시험 지도/상태 변경
/repeat                      마지막 발화 ID를 그대로 재전송
/cancel                      '멈춰' 발화 전송
/new                         새 대화 시작
/reset                       새 임시 DB와 시험 그래프로 초기화
/help                        도움말
/quit                        시험 작업을 정리하고 종료
""".strip()

REGRESSION_FILES = (
    'test_speech_mission_policy.py', 'test_speech_navigation.py',
    'test_speech_missions.py', 'test_speech_mission_dialogue.py',
    'test_mock_speech_missions.py', 'test_ros_speech_missions.py',
)
CAPABILITIES = {
    'nav': 'navigate_to_pose', 'navigate_to_pose': 'navigate_to_pose',
    'follow': 'follow_person', 'follow_person': 'follow_person',
    'patrol': 'patrol',
}


def _now():
    return datetime.now(timezone.utc).isoformat()


class Report:
    """Keep raw observations and a separate scenario result document."""

    def __init__(self, path, *, provider='mock', model=None):
        """Create a report directory before starting any ROS node."""
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = self.path.with_name(self.path.name + '.events.jsonl')
        self.results = []
        self.provider = provider
        self.model = model
        self._last_progress = {}
        self._log = self.log_path.open('a', encoding='utf-8')

    def event(self, item):
        """Record every observation, suppressing repetitive console feedback."""
        if not isinstance(item, dict):
            item = {'event': 'system', 'message': str(item)}
        record = {'observed_at': _now(), **item}
        self._log.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        self._log.flush()
        category = item.get('event', 'system')
        if category == 'mission':
            if item.get('kind') == 'progress':
                key = item.get('request_id')
                state = item.get('state')
                if self._last_progress.get(key) == state:
                    return
                self._last_progress[key] = state
            print('[Manager]', item.get('capability_id', ''),
                  item.get('kind', ''), item.get('state', ''),
                  item.get('reason', ''), flush=True)
        elif category == 'speech':
            print('[응답]', item.get('text', item.get('message', '')), flush=True)
        elif category == 'reply':
            # The corresponding speech Topic carries the actual published text.
            return
        elif category == 'goal':
            print('[하위 Goal]', item.get('capability_id', ''),
                  json.dumps(item.get('arguments', {}), ensure_ascii=False), flush=True)
        elif category == 'scenario_start':
            print('\n[시나리오]', item.get('id', ''), item.get('title', ''), flush=True)
        elif category == 'scenario_result':
            self.results.append({key: value for key, value in item.items() if key != 'event'})
            self.save()
            print('[PASS]' if item.get('passed') else '[FAIL]',
                  item.get('id', ''), item.get('error', item.get('cleanup_error', '')),
                  flush=True)
        elif category == 'system' and item.get('kind') == 'transcript':
            print('[입력]', item.get('text', ''), flush=True)
        elif category == 'system' and item.get('kind') == 'ready':
            print('[준비]', item.get('provider', ''), item.get('model', ''),
                  item.get('namespace', ''), flush=True)
        elif category == 'system' and item.get('kind') == 'map':
            print('[지도]', item.get('mode', ''), item.get('map'), flush=True)
        elif category == 'system' and item.get('kind') == 'actuator_terminal':
            print('[하위 종료]', item.get('capability_id', ''), item.get('status', ''),
                  flush=True)
        else:
            print('[시험]', item.get('message', json.dumps(item, ensure_ascii=False,
                                                         default=str)), flush=True)

    def save(self):
        """Persist completed cases even when the interactive session continues."""
        payload = {
            'recorded_at': _now(), 'mode': 'isolated_ros',
            'interactive_provider': self.provider, 'interactive_model': self.model,
            'scenario_provider': 'mock',
            'results': self.results,
            'passed': sum(item.get('passed') is True for item in self.results),
            'failed': sum(item.get('passed') is False for item in self.results),
            'event_log': str(self.log_path),
        }
        temporary = self.path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                        default=str) + '\n')
        temporary.replace(self.path)

    def close(self):
        """Flush final observations and close the log."""
        self.save()
        self._log.close()


def list_scenarios():
    """Print stable scenario IDs without starting ROS."""
    from malbut_agent_server.voice_lab_scenarios import SCENARIOS
    for index, scenario in enumerate(SCENARIOS, 1):
        print(f'{index:2}. {scenario.id:24} {scenario.title}')
        print(f'    {scenario.description}')


def resolve_scenario(value):
    """Accept a stable ID or its displayed one-based menu number."""
    from malbut_agent_server.voice_lab_scenarios import SCENARIOS
    if value.isdecimal():
        index = int(value) - 1
        if 0 <= index < len(SCENARIOS):
            return SCENARIOS[index].id
    if value in {item.id for item in SCENARIOS}:
        return value
    raise ValueError('없는 시나리오입니다. /scenarios로 ID를 확인하세요.')


def run_regression(report):
    """Execute source-tree checks for dispatch races that need precise timing."""
    package = Path(__file__).resolve().parents[1]
    paths = [package / 'test' / name for name in REGRESSION_FILES]
    if not all(path.is_file() for path in paths):
        raise ValueError('집중 회귀는 소스 checkout의 scripts/voice_lab.sh에서 실행하세요.')
    log = report.path.parent / 'regression.log'
    junit = report.path.parent / 'regression.xml'
    print(f'[회귀] 전송 조건·중복·지도·정책·ROS 통합 검사 실행 중: {log}', flush=True)
    started = time.monotonic()
    with log.open('w') as stream:
        try:
            completed = subprocess.run(
                [sys.executable, '-m', 'pytest', '-q', *map(str, paths),
                 '--junitxml=' + str(junit)],
                cwd=package.parent, stdout=stream, stderr=subprocess.STDOUT,
                timeout=180, check=False,
            )
            result = {'id': 'regression', 'title': '집중 회귀',
                      'passed': completed.returncode == 0,
                      'evidence': {'exit_code': completed.returncode,
                                   'log': str(log), 'junit': str(junit)}}
        except subprocess.TimeoutExpired:
            result = {'id': 'regression', 'title': '집중 회귀', 'passed': False,
                      'error': '180초 시간 초과', 'evidence': {'log': str(log)}}
    result['elapsed_s'] = round(time.monotonic() - started, 3)
    print('\n'.join(log.read_text().splitlines()[-4:]), flush=True)
    report.event({'event': 'scenario_result', **result})
    return result


class Terminal:
    """Own ROS on the main thread while input waits independently."""

    def __init__(self, factory, report, *, scenario_factory=None):
        """Bind the graph factory without starting it."""
        self.factory = factory
        self.scenario_factory = scenario_factory or factory
        self.report = report
        self.graph = None
        self.last_utterance = None

    def start(self):
        """Start a clean graph with a selected fixture map."""
        self.graph = self.factory(output=self.report.event)
        try:
            self.graph.start()
        except BaseException:
            self.stop()
            raise
        self.last_utterance = None

    def stop(self):
        """Cancel fixture work and release its ROS context."""
        if self.graph is not None:
            graph, self.graph = self.graph, None
            graph.close()

    def scenario(self, identifier):
        """Run an independent case, then restore the interactive playground."""
        from malbut_agent_server.voice_lab_scenarios import run_all, run_scenario
        if identifier != 'all':
            identifier = resolve_scenario(identifier)
        self.stop()
        try:
            if identifier == 'all':
                results = run_all(self.scenario_factory, self.report.event)
            else:
                results = [run_scenario(identifier, self.scenario_factory,
                                        self.report.event)]
            passed = sum(item['passed'] for item in results)
            print(f'[결과] {passed}/{len(results)} PASS — {self.report.path}', flush=True)
        finally:
            self.start()

    def command(self, line):
        """Execute one terminal command; return False to exit."""
        line = line.strip()
        if not line:
            return True
        if not line.startswith('/'):
            uid = self.graph.send(line)
            self.last_utterance = (uid, line)
            self.graph.wait_reply(uid)
            return True
        parts = shlex.split(line)
        command, args = parts[0], parts[1:]
        if command in {'/quit', '/exit'} and not args:
            return False
        if command == '/help' and not args:
            print(HELP)
        elif command == '/scenarios' and not args:
            list_scenarios()
        elif command == '/all' and not args:
            self.scenario('all')
        elif command == '/run' and len(args) == 1:
            self.scenario(args[0])
        elif command == '/regression' and not args:
            self.stop()
            try:
                run_regression(self.report)
            finally:
                self.start()
        elif command == '/status' and not args:
            print(json.dumps(self.graph.status(), ensure_ascii=False, indent=2, default=str))
        elif command == '/reset' and not args:
            self.stop()
            self.start()
        elif command == '/cancel' and not args:
            return self.command('멈춰')
        elif command == '/new' and not args:
            return self.command('새 대화 시작해줘')
        elif command == '/repeat' and not args:
            if self.last_utterance is None:
                raise ValueError('먼저 발화를 입력하세요.')
            uid, text = self.last_utterance
            self.graph.send(text, utterance_id=uid)
            print('[재수신] 같은 utterance_id로 보냈습니다. /status에서 Goal 수를 확인하세요.')
        elif command == '/map' and len(args) == 1:
            selection = args[0]
            if selection in {'home', 'other', 'none'}:
                self.graph.set_map(mode='LOCALIZATION', variant=selection)
            elif selection in {'mapping', 'switching', 'error'}:
                self.graph.set_map(mode=selection.upper(), variant='home')
            else:
                raise ValueError('/map home|other|none|mapping|switching|error')
        elif command == '/behavior' and 2 <= len(args) <= 4:
            capability = CAPABILITIES.get(args[0])
            if capability is None or args[1] not in {'success', 'hold', 'abort', 'reject'}:
                raise ValueError('/behavior nav|follow|patrol success|hold|abort|reject [초]')
            delays = [float(value) for value in args[2:]]
            if any(not math.isfinite(value) or value < 0 or value > 30 for value in delays):
                raise ValueError('지연 시간은 0~30초여야 합니다.')
            self.graph.set_behavior(capability, outcome=args[1],
                                    delay_s=delays[0] if delays else 0.3,
                                    cancel_delay_s=delays[1] if len(delays) > 1 else 0.0)
            print(f'[설정] 다음 {capability} Goal: {args[1]}', flush=True)
        else:
            raise ValueError('명령 형식을 확인하세요. /help')
        return True

    def interact(self, stream):
        """Keep spinning ROS while waiting for the next complete input line."""
        pending = Queue()

        def read_lines():
            for line in stream:
                pending.put(line)
            pending.put(None)

        Thread(target=read_lines, name='voice-lab-input', daemon=True).start()
        print(HELP)
        print('\nvoice-lab> ', end='', flush=True)
        while True:
            self.graph.spin(timeout=0.02)
            try:
                line = pending.get_nowait()
            except Empty:
                continue
            if line is None:
                return
            try:
                if not self.command(line):
                    return
            except (ValueError, TimeoutError, AssertionError) as error:
                self.report.event({'event': 'input_error', 'message': str(error)})
            print('voice-lab> ', end='', flush=True)


def _dialogue_settings(args):
    """Read explicit chat configuration without modifying process environment."""
    from malbut_agent_server.config import Settings, load_env_file
    if args.chat and args.provider == 'mock':
        raise ValueError('--chat과 --provider mock은 함께 사용할 수 없습니다.')
    if args.model and args.provider == 'mock':
        raise ValueError('--model은 --chat 또는 --provider openai와 함께 사용하세요.')
    selected = 'openai' if args.chat else args.provider
    if selected == 'mock' or not (selected or args.env_file or args.model):
        return Settings(provider='mock')
    source = dict(os.environ)
    env_file = args.env_file
    if args.chat and env_file is None:
        conventional = Path.home() / '.config/malbut/agent.env'
        if conventional.is_file():
            env_file = str(conventional)
    if env_file:
        path = Path(env_file).expanduser()
        if not path.is_file():
            raise ValueError('지정한 환경 설정 파일을 찾을 수 없습니다.')
        load_env_file(path, target=source)
    if selected is not None:
        source['MALBUT_AGENT_PROVIDER'] = selected
    if args.model:
        source['OPENAI_MODEL'] = args.model
    settings = replace(Settings.from_env(source), database_path=':memory:',
                       user_id='voice-lab-user', tool_mode='proposal')
    if settings.provider not in {'mock', 'openai'}:
        raise ValueError('실험실은 mock 또는 openai Provider를 지원합니다.')
    if args.model and settings.provider != 'openai':
        raise ValueError('--model은 --chat 또는 --provider openai와 함께 사용하세요.')
    settings.validate_for_dialogue()
    return settings


def main(argv=None):
    """Run interactive experiments or a repeatable batch of scenarios."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--all', action='store_true', help='모든 독립 시나리오 실행')
    mode.add_argument('--scenario', action='append', help='선택한 ID 실행, 반복 지정 가능')
    mode.add_argument('--list', action='store_true', help='시나리오 목록만 표시')
    mode.add_argument('--regression', action='store_true', help='집중 회귀 검사')
    parser.add_argument('--domain-id', type=int, default=197)
    parser.add_argument('--report', help='결과 JSON 경로')
    parser.add_argument('--chat', action='store_true', help='기존 OpenAI 설정으로 자유 대화')
    parser.add_argument('--provider', choices=('mock', 'openai'),
                        help='직접 입력에 사용할 Provider; 자동 시나리오는 항상 mock')
    parser.add_argument('--env-file', help='기존 Agent 환경 설정 파일')
    parser.add_argument('--model', help='직접 입력의 OpenAI 대화 모델')
    args = parser.parse_args(argv)
    if not 0 <= args.domain_id <= 232:
        parser.error('--domain-id는 0~232 범위여야 합니다.')
    if args.list:
        list_scenarios()
        return 0
    batch = bool(args.all or args.scenario or args.regression)
    try:
        settings = None if batch else _dialogue_settings(args)
    except (ValueError, OSError) as error:
        print(f'대화 설정 오류: {error}', file=sys.stderr)
        return 2
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    report = Report(args.report or str(
        Path.home() / '.local/state/malbut/voice-lab' / stamp / 'results.json'),
        provider=settings.provider if settings else 'mock',
        model=settings.openai_model if settings and settings.provider == 'openai' else None)
    terminal = None
    try:
        if settings and settings.provider == 'openai':
            print(f'자유 대화: OpenAI {settings.openai_model} + 실제 Agent/Manager')
            print('일상 대화와 작업 요청을 같은 대화에서 이어갈 수 있습니다.')
        else:
            print('음성 명령 실험실: mock LLM + 실제 Agent/Manager + 시험용 동작 서버')
        print('/all·/run 자동 시나리오는 항상 mock Provider로 검증합니다.')
        print(f'localhost · 고유 ROS 경로 · domain {args.domain_id} · 실물 이동 없음')
        print(f'관측 로그: {report.log_path}', flush=True)
        if args.regression:
            return 0 if run_regression(report)['passed'] else 1
        from malbut_agent_server.voice_lab_graph import VoiceLabGraph
        from malbut_agent_server.voice_lab_scenarios import run_all, run_scenario

        def factory(**kwargs):
            return VoiceLabGraph(domain_id=args.domain_id, **kwargs)

        if args.all or args.scenario:
            if args.all:
                run_all(factory, report.event)
            else:
                for identifier in args.scenario:
                    run_scenario(resolve_scenario(identifier), factory, report.event)
            passed = sum(item['passed'] for item in report.results)
            print(f'\n결과: {passed}/{len(report.results)} PASS')
            return 0 if passed == len(report.results) else 1

        def dialogue_factory(**kwargs):
            return VoiceLabGraph(domain_id=args.domain_id, dialogue_settings=settings, **kwargs)

        terminal = Terminal(dialogue_factory, report, scenario_factory=factory)
        terminal.start()
        terminal.interact(sys.stdin)
        return 0
    except KeyboardInterrupt:
        print('\n시험 작업을 정리합니다.', flush=True)
        return 130
    except (ImportError, RuntimeError, ValueError, OSError) as error:
        print(f'실험실 실행 실패: {error}', file=sys.stderr)
        print('소스의 scripts/voice_lab.sh를 사용해 ROS와 의존성을 준비하세요.',
              file=sys.stderr)
        return 2
    finally:
        try:
            if terminal is not None:
                terminal.stop()
        finally:
            report.close()
            print(f'결과 파일: {report.path}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
