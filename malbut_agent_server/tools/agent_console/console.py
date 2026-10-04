"""One local terminal for the current Agent, real speech and explicit robot simulations."""

import argparse
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for package in ('malbut_agent_server', 'malbut_stt', 'malbut_tts'):
    sys.path.insert(0, str(ROOT / package))

from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.ros_situation import build_situation_factory
from console_core import ConsoleCore
from console_robot import RobotProbe


def OpenAISynthesizer(*args, **kwargs):
    """Load optional audio dependencies only when voice output is requested."""
    from malbut_tts.api_synthesis import OpenAISynthesizer as implementation
    return implementation(*args, **kwargs)


def ConsoleAudio(*args, **kwargs):
    """Text-only startup needs no microphone, model, or audio dependency."""
    from console_audio import ConsoleAudio as implementation
    return implementation(*args, **kwargs)


HELP = '''
문장 입력       실제 Agent와 대화 · 기억 · 설정 변경
Enter / /voice  마이크로 한 번 말하기 → 같은 대화로 답변
/fall           낙상 확인 음성 체험
/fall text      낙상 확인 텍스트 체험 (/silence로 무응답 입력)
/robot          이동 제안 · 목적지 확인 · 승인/취소 체험 [모의 로봇]
/tools          도구별 연결 상태와 입력 형식
/tool 이름 JSON 도구 직접 시험 [날씨 조회·지역 설정 / 로봇 모의 실행]
/memory         사실 기억·이야기 기억과 각각의 동의 상태
/stories        같은 이야기로 묶인 장기기억 목록·처리 상태
/stories on     설명 확인 후 장기 이야기 기억 켜기 (별도 동의)
/stories off    새 장기 저장·재사용 끄기 (기존 기억 보관)
/stories sync   진행 중인 이야기 정리 기다리기
/stories sources ID   이야기 원문 근거 보기
/stories delete ID    이야기와 관련 원문·파생 기억 삭제
/stories history      과거 대화 범위 확인 후 별도 동의하여 포함
/settings       현재 대화/기본 응답 설정 확인
/status         현재 대화 · DB · 실행 환경
/last           마지막 처리의 상세 결과
/new            새 대화 (장기기억은 유지)
/tts on|off     음성 출력 켜기/끄기 (낙상 음성 체험 포함)
/checks         모의 입력·오류·기억·음성 연결 자동검사
/help           이 안내
/quit           종료

이야기 체험: /stories on → 네 → “오늘 전시를 보고 마음이 편해졌어”
             /stories sync → /new → “그 전시 이야기를 이어가자”
사실 기억 예시: 개인화에 동의해 / 내 강아지 이름은 초코야. 기억해줘
설정 예시: 답변은 짧게 해줘 / 나를 현재라고 불러줘
도구 예시: /tool navigate {"location":"거실"}
날씨 예시: 여기는 서울 강남구야 → 오늘 날씨 어때?
'''


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str), flush=True)



def show_story_data(value):
    """Keep the everyday memory view readable; /status retains full diagnostics."""
    if isinstance(value, dict) and 'items' in value:
        policy = value['policy']
        pending, failed = policy.get('pending', 0), policy.get('failed', 0)
        if pending:
            print(f'정리 중: {pending}턴 · /stories sync로 기다릴 수 있어요.', flush=True)
        if failed or policy.get('error'):
            print('일부 이야기를 정리하지 못했어요. /stories sync로 다시 시도할 수 있어요.', flush=True)
        if not value['items']:
            print('아직 정리된 이야기가 없어요.', flush=True)
        for item in value['items']:
            print(f"\n{item['story_id'][:8]} · {item['title']}", flush=True)
            for entry in item.get('current', ()):
                actor = {'user': '내가 말한 내용', 'assistant': '말벗의 제안',
                         'inference': '말벗의 추측'}.get(entry.get('actor'), '기록')
                status = {'confirmed': '확정', 'open': '미해결', 'proposed': '제안',
                          'superseded': '이전 내용', 'stated': '발언'}.get(entry.get('status'), '')
                print(f"  - {entry['text']} ({actor} · {status})", flush=True)
    elif isinstance(value, list):
        if not value:
            print('현재 확인할 수 있는 원문 근거가 없어요.', flush=True)
        for source in value:
            role = source.get('role', source.get('ref', {}).get('role'))
            print(('나' if role == 'user' else '말벗') + ': ' + source.get('text', ''), flush=True)


def text_fall(engine_factory):
    engine = engine_factory()
    turn = engine.start(uuid4().hex, 'fall', '거실 바닥에 사람이 누워 있어 낙상이 의심됩니다.')
    print('낙상 확인 텍스트 체험: /silence 무응답 입력 · /back 돌아가기')
    while True:
        print(f'\n🤖 {turn.text}', flush=True)
        if turn.result is not None:
            show(asdict(turn.result))
            return asdict(turn.result)
        answer = input('답변 > ').strip()
        if answer in ('/back', '/cancel', '/quit'):
            return {'status': 'canceled'}
        if not answer:
            continue
        turn = engine.no_response() if answer == '/silence' else engine.answer(answer)


def robot_console(settings):
    print('\n[모의 로봇] 이동 제안과 승인/취소 논리를 시험합니다. 실제 주행은 없습니다.')
    print('모의 장소: 거실 · 주방 · 침실 · 현관 · 충전소')
    print('예: 거실로 가줘 → 네 / 취소. /last 상세 결과 · /back 일반 대화 복귀.')
    database = Path(settings.database_path)
    probe = RobotProbe(replace(settings, database_path=str(database.with_name(
                               database.stem + '-robot.sqlite3')),
                               tool_mode='proposal'))
    result = None
    try:
        while True:
            text = input('로봇(모의) > ').strip()
            if text in ('/back', '/quit'):
                return result
            if not text:
                continue
            if text == '/new':
                probe.reset()
                continue
            if text == '/last':
                show(result or {'message': '아직 처리 결과가 없습니다.'})
                continue
            print('판단 중…', flush=True)
            result = probe.turn(text)
            message = result.get('message') or result.get('decision', {}).get('message', '')
            print(f'\n🤖 {message}', flush=True)
            print(f'[{result["status"]} · {result.get("result_code", "proposal")} · 실제 주행 없음]',
                  flush=True)
    finally:
        probe.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('openai', 'mock'), default='openai')
    parser.add_argument('--database', type=Path, default=ROOT / '.runtime/agent-console/agent.sqlite3')
    parser.add_argument('--no-tts', action='store_true', help='음성 출력 끄기; 낙상은 /fall text 사용')
    parser.add_argument('--device-index', type=int, default=-1)
    parser.add_argument('--output-device', type=int)
    parser.add_argument('--check', action='store_true', help='음성 장치·모델·라이브러리 준비 검사')
    parser.add_argument('--stt-model', type=Path, help='로컬 ggml Whisper 모델 파일')
    parser.add_argument('--stt-library', type=Path, help='ABI 3 libmalbut_whisper 라이브러리')
    args = parser.parse_args()
    os.umask(0o077)
    env = dict(os.environ)
    load_env_file(ROOT / '.env', target=env)
    def voice_asset(argument, key, legacy, portable):
        if argument is not None:
            return argument.expanduser()
        if env.get(key):
            return Path(env[key]).expanduser()
        return legacy if legacy.is_file() else portable
    model_path = voice_asset(
        args.stt_model, 'MALBUT_CONSOLE_STT_MODEL',
        ROOT / '.runtime/stt-engine-comparison-20260916/whisper-cpp/models/ggml-small.bin',
        ROOT / '.runtime/agent-console/models/ggml-small.bin',
    )
    library_path = voice_asset(
        args.stt_library, 'MALBUT_CONSOLE_STT_LIBRARY',
        ROOT / '.runtime/fall-voice-20260925/native-build/bin/libmalbut_whisper.dylib',
        ROOT / '.runtime/agent-console/native-build/bin' / (
            'libmalbut_whisper.dylib' if sys.platform == 'darwin' else 'libmalbut_whisper.so'),
    )
    args.database = args.database.expanduser().resolve()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings.from_env(env), provider=args.provider,
                       database_path=str(args.database.expanduser()),
                       user_id='terminal-demo-user', tool_mode='simulation')
    settings._validate_runtime(http_server=False)
    read_aloud = not args.no_tts
    synth = None
    if read_aloud:
        synth = OpenAISynthesizer(api_key=settings.openai_api_key)
        synth.load()
    print('\nMalbut 통합 체험기', flush=True)
    print(f'Agent: {args.provider}' + (
        f' / 대화 {settings.openai_model} / 낙상 {settings.openai_general_model or settings.openai_model}'
        if args.provider == 'openai' else ''), flush=True)
    print('대화·기억·설정: 실제 코드 / 선택 음성: 로컬 STT + OpenAI TTS', flush=True)
    print('날씨: 기상청 조회·지역 저장 / 로봇 도구: 모의 실행 / 실제 로봇·카메라 감지: 미연결', flush=True)
    if not env.get('KMA_SERVICE_KEY', '').strip():
        print('날씨 조회에는 .env의 KMA_SERVICE_KEY 설정이 필요합니다.', flush=True)
    print(f'체험 DB: {settings.database_path}', flush=True)
    print('대화·기억은 이 DB에 유지됩니다. 녹음 파일은 저장하지 않습니다.', flush=True)
    print('OpenAI 대화 모드는 대화 텍스트를 외부 API로 전송합니다.', flush=True)
    print('음성 출력은 mock 모드에서도 OpenAI TTS API를 사용합니다. 음성은 AI 합성입니다.', flush=True)
    if args.check:
        import sounddevice as sd
        from malbut_stt.cpp_transcription import _load_library
        if not model_path.is_file():
            raise FileNotFoundError('local Whisper model')
        _load_library(library_path)
        input_device = None if args.device_index == -1 else args.device_index
        sd.check_input_settings(device=input_device, samplerate=16000,
                                channels=1, dtype='int16')
        sd.check_output_settings(device=args.output_device, samplerate=24000,
                                 channels=1, dtype='float32')
        show({'input_device': sd.query_devices(
                  input_device, kind='input')['name'],
              'output_device': sd.query_devices(args.output_device, kind='output')['name'],
              'api_key_present': bool(settings.openai_api_key),
              'check': 'ready; no capture, inference or API call'})
        return 0

    print('대화 엔진 준비 중…', flush=True)
    state_path = args.database.expanduser().with_suffix('.status.json')
    core = ConsoleCore(settings, weather_service_key=env.get('KMA_SERVICE_KEY', ''))
    audio = None
    last = None
    def get_audio():
        nonlocal audio
        if audio is None:
            audio = ConsoleAudio(synth, device_index=args.device_index,
                                 output_device=args.output_device,
                                 emit=lambda text: print(text, flush=True),
                                 model_path=model_path, library_path=library_path)
        return audio
    try:
        situation_factory = build_situation_factory(settings)
        print(HELP, flush=True)
        policy = core.stories()['policy']
        if policy['enabled']:
            print('장기 이야기 기억이 켜져 있어요. /stories로 확인하고 /stories off로 끌 수 있어요.', flush=True)
        elif sys.stdin.isatty() and sys.stdout.isatty():
            print(core.story_command('/stories on')['text'], flush=True)
        else:
            print('장기 이야기 기억은 꺼져 있어요. /stories on으로 설명을 확인하고 동의하면 켜집니다.', flush=True)
        while True:
            state_path.write_text(json.dumps({'state': 'waiting_for_input', 'pid': os.getpid(),
                                              'provider': settings.provider, 'updated_at': time.time()}))
            try:
                line = input('나 > ').strip()
            except EOFError:
                break
            if line in ('/quit', '/exit', 'q'):
                break
            state_path.write_text(json.dumps({'state': 'processing', 'pid': os.getpid(),
                                              'updated_at': time.time()}))
            try:
                if line == '/help':
                    print(HELP)
                elif line == '/status':
                    status = core.status()
                    core._assert_management_policy(status['story_memory']['policy'])
                    show(status)
                elif line == '/memory':
                    memories = core.memories()
                    core._assert_management_policy(memories['stories']['policy'])
                    show(memories)
                elif line == '/stories' or line.startswith('/stories '):
                    if line.strip() == '/stories sync':
                        print('이야기를 정리하는 중…', flush=True)
                    last = core.story_command(line)
                    core.validate_reply(last)
                    print(last['text'], flush=True)
                    if 'data' in last:
                        show_story_data(last['data'])
                elif line == '/settings':
                    status = core.status()
                    show({name: status[name] for name in (
                        'default_preferences', 'session_preferences')})
                elif line == '/tools':
                    show(core.tools())
                elif line == '/last':
                    if last and 'metadata' in last and 'conversation_id' in last:
                        core.validate_reply(last)
                    show(last or {'message': '아직 처리 결과가 없습니다.'})
                elif line == '/new':
                    print('이야기 정리를 확인하고 새 대화를 여는 중…', flush=True)
                    print(f'새 대화: {core.new_conversation()} · 장기기억 유지')
                    story_policy = core.stories()['policy']
                    if story_policy.get('pending') or story_policy.get('error'):
                        print('이야기 정리가 아직 끝나지 않았어요. /stories sync로 처리 상태를 확인하세요.')
                        show(story_policy)
                elif line in ('/tts on', '/tts off'):
                    if line.endswith('on'):
                        if synth is None:
                            synth = OpenAISynthesizer(api_key=settings.openai_api_key)
                        synth.load()
                        if audio is not None:
                            audio.synthesizer = synth
                    read_aloud = line.endswith('on')
                    print('음성 출력 ' + ('켬' if read_aloud else '끔'))
                elif line == '/fall text':
                    last = text_fall(situation_factory)
                elif line == '/fall':
                    if not read_aloud:
                        print('음성 출력이 꺼져 있어요. /fall text로 시험하거나 /tts on으로 켜세요.')
                        continue
                    status, result = get_audio().fall(situation_factory)
                    last = {'status': status, 'result': asdict(result) if result else None}
                elif line == '/robot':
                    last = robot_console(settings)
                elif line.startswith('/tool '):
                    _, name, *payload = line.split(maxsplit=2)
                    last = core.query_tool(name, json.loads(payload[0]) if payload else {})
                    show(last)
                elif line == '/checks':
                    tests = ROOT / 'malbut_agent_server/test'
                    command = [sys.executable, '-m', 'pytest', '-q', *(
                        str(tests / name) for name in (
                            'test_agent_console_core.py', 'test_agent_console_audio.py',
                            'test_agent_console_robot.py', 'test_agent_console_cli.py',
                            'test_agent_console_story.py', 'test_agent_console_fall.py',
                            'test_agent_console_weather.py',
                        )), str(ROOT / 'malbut_bringup/test/test_confirmation_audio.py')]
                    test_env = dict(os.environ, PYTHONPATH=os.pathsep.join(
                        str(ROOT / p) for p in ('malbut_agent_server', 'malbut_stt', 'malbut_tts')))
                    subprocess.run(command, cwd=ROOT, env=test_env, check=False)
                elif line.startswith('/') and line != '/voice':
                    print('알 수 없는 명령입니다. /help로 확인하세요.')
                else:
                    if line in ('', '/voice'):
                        line = get_audio().listen_once()
                        if line is None:
                            print('발화가 시작되지 않아 텍스트 입력으로 돌아왔어요.')
                            continue
                        print(f'🗣 인식: {line}', flush=True)
                    print('생각 중…', flush=True)
                    last = core.chat(line)
                    core.validate_reply(last)
                    print(f'\n🤖 {last["text"]}', flush=True)
                    print(f'[{last["decision_type"]} · {last["elapsed_s"]:.2f}초]', flush=True)
                    if 'data' in last:
                        show_story_data(last['data'])
                    if read_aloud and not get_audio().speak(
                            last['text'], validate=lambda: core.validate_reply(last)):
                        print('음성 재생에 실패했습니다. 위 텍스트 답변은 확인할 수 있어요.')
            except KeyboardInterrupt:
                print('\n현재 체험 중단. 일반 입력으로 돌아옵니다. /quit으로 종료하세요.', flush=True)
            except Exception as error:
                traceback.print_tb(error.__traceback__, file=sys.stderr)
                print(type(error).__name__, file=sys.stderr, flush=True)
                print(f'처리 실패 ({type(error).__name__}). /status 또는 runtime.log를 확인하세요.', flush=True)
    finally:
        try:
            if audio is not None:
                audio.close()
        finally:
            core.close()
            state_path.write_text(json.dumps({'state': 'closed', 'pid': os.getpid()}))
    print('체험을 종료했습니다. 대화와 기억은 체험 DB에 유지됩니다.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\n종료했습니다.', flush=True)
    except Exception as error:
        traceback.print_tb(error.__traceback__, file=sys.stderr)
        print(f'시작 실패 ({type(error).__name__}). --check와 runtime.log를 확인하세요.',
              file=sys.stderr, flush=True)
        raise SystemExit(1)
