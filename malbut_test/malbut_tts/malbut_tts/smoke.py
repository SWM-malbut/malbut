"""Try streaming speech and queue controls from a terminal without ROS."""

import argparse
from pathlib import Path
from threading import Condition

from malbut_tts.runtime import NOTIFICATION, SpeechRuntime, TERMINAL_STATES


def main(argv=None):
    """Speak one text, or accept texts and playback commands interactively."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-path', type=Path)
    parser.add_argument('--backend', choices=('qwen-cuda', 'openai'),
                        default='openai')
    parser.add_argument('--api-model', default='gpt-4o-mini-tts')
    parser.add_argument('--api-voice', default='marin')
    parser.add_argument('--api-timeout-seconds', type=float, default=8.0)
    parser.add_argument('--cuda-dtype', choices=('float32', 'float16'),
                        default='float32')
    parser.add_argument('--cuda-sentence-mode', action=argparse.BooleanOptionalAction,
                        default=True, help='Pipeline CUDA sentences before playback.')
    parser.add_argument('--sentence-max-chars', type=int, default=80)
    parser.add_argument('--device-index', type=int)
    parser.add_argument('--list-devices', action='store_true')
    parser.add_argument('--text', help='Speak one request, then exit.')
    args = parser.parse_args(argv)
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return 0
    if args.backend != 'openai' and (
        args.model_path is None or not args.model_path.is_dir()
    ):
        parser.error('--model-path must point to an existing local model')
    if args.text is not None and not args.text.strip():
        parser.error('--text must not be blank')
    if args.backend == 'openai':
        print('OpenAI TTS: 입력 텍스트를 유료 외부 API로 전송합니다. '
              '재생되는 목소리는 사람이 아닌 AI 합성 음성입니다.', flush=True)

    from malbut_tts.audio import StreamingPlayer
    from malbut_tts.backends import create_synthesizer

    condition = Condition()
    terminal = {}
    active_id = None

    def on_status(playback_id, state):
        nonlocal active_id
        with condition:
            if state in ('playing', 'paused'):
                active_id = playback_id
            if state in TERMINAL_STATES:
                terminal[playback_id] = state
                if active_id == playback_id:
                    active_id = None
            print(f'[{state}] {playback_id}', flush=True)
            condition.notify_all()

    runtime = SpeechRuntime(
        create_synthesizer(args.model_path, backend=args.backend,
                           cuda_dtype=args.cuda_dtype,
                           cuda_sentence_mode=args.cuda_sentence_mode,
                           sentence_max_chars=args.sentence_max_chars,
                           api_model=args.api_model, api_voice=args.api_voice,
                           api_timeout_seconds=args.api_timeout_seconds),
        lambda **kwargs: StreamingPlayer(device=args.device_index, **kwargs),
        on_status,
    )
    try:
        if args.text is not None:
            playback_id = runtime.submit(args.text)
            with condition:
                condition.wait_for(lambda: playback_id in terminal)
                return 0 if terminal[playback_id] == 'finished' else 1

        print('텍스트 → 대화 답변 /notice 텍스트 → 일반 알림')
        print('/pause /resume /stop [재생 ID] /quit')
        if args.backend == 'openai':
            print('각 요청을 한 번 전송하고 도착하는 음성부터 재생합니다. '
                  '실패 시 자동 재전송하지 않습니다.', flush=True)
        else:
            print('첫 요청에서 로컬 모델을 로드합니다.', flush=True)
        while True:
            try:
                line = input('> ')
            except EOFError:
                break
            if line.strip() == '/quit':
                break
            command, _, target = line.partition(' ')
            if command in ('/pause', '/resume', '/stop'):
                with condition:
                    playback_id = target.strip() or active_id
                accepted = runtime.control(playback_id, command[1:])
                print(f'accepted={str(accepted).lower()}', flush=True)
            elif command == '/notice':
                playback_id = runtime.submit(target, NOTIFICATION)
                if playback_id:
                    print(f'[queued notification] {playback_id}', flush=True)
            elif line.startswith('/'):
                print('명령: /notice 텍스트 /pause /resume /stop /quit')
            else:
                playback_id = runtime.submit(line)
                if playback_id:
                    print(f'[queued dialogue] {playback_id}', flush=True)
    except KeyboardInterrupt:
        return 130
    finally:
        runtime.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
