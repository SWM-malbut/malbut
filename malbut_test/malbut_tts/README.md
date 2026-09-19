# Malbut TTS

Agent가 보낸 답변 한 건을 하나의 요청으로 관리하고, 선택한 backend의
음성을 재생한다. 기본 backend는 OpenAI이며, backend를 생략하면 Agent 답변
텍스트를 OpenAI TTS API로 전송한다. 실험용 Qwen CUDA backend만 로컬 모델
경로와 함께 명시적으로 선택할 수 있다. 선택한 backend가 실패해도 다른
backend로 자동 전환하지 않는다. CUDA backend는 문장별로 합성하여 앞 문장
재생과 다음 문장 합성을 겹친다. 모델 내부의 토큰/음성 스트리밍과는 구분한다.
명세는 [tts_agent.md](docs/tts_agent.md)에 있다.

## 요청과 재생 규칙

- 한 답변의 여러 문장과 음성 조각은 하나의 `playback_id`를 공유한다.
- 현재 요청을 유지하고 새 요청은 대기열에 넣는다. 다음 요청은 대화 답변
  우선, 같은 종류에서는 수신한 순서대로 선택한다.
- 대기 요청은 기본 최대 32개이며, 앞선 답변의 재생·일시정지가 길어져도
  기다린 시간만으로 삭제하지 않는다. ROS parameter `max_pending_requests`
  (양의 정수)로 개수 제한을 조정한다. 가득 찬 대기열에 들어온 새 요청은
  해당 `playback_id`에 `failed`를 한 번 보내고 합성하지 않는다.
- `pending_timeout_s`는 기본 `0.0`으로 자동 만료를 끈다. 유한한 양수로
  설정하면 접수 시점부터 해당 시간이 지난 대기 요청을 만료시키고 `failed`를
  한 번 보낸다. 이 옵션을 켜면 앞선 답변이 재생·일시정지 중이어도 대기
  시간이 흐르므로, 오래 기다려도 전달해야 하는 대화에서는 기본값을 유지한다.
  현재 재생·일시정지된 요청은 만료 대상이 아니며 요청 우선순위와 FIFO는 유지된다.
- 일시정지는 이미 출력 장치에 넘긴 버퍼가 재생된 뒤 적용한다. 아직 재생하지
  않은 위치와 음성은 유지하고 재개 시 이어서 재생한다. 대기 요청은 기다린다.
- 중지는 현재 출력과 합성을 취소하고 남은 음성을 폐기한다. 모델 연산 자체가
  반환되기 전에 즉시 끊기지 않더라도, 취소 뒤 반환된 음성은 재생하지 않는다.
- `finished`는 모든 음성이 출력 장치에서 재생된 뒤 요청당 한 번만 보낸다.
  합성·장치 실패는 `failed`, 정상 중지는 `stopped`이며 서로 구분한다.
- 빈 텍스트는 무시하고 원문을 임의로 요약하거나 추가하지 않는다.

## ROS 인터페이스

| 방향 | 이름 | 타입 및 데이터 |
| --- | --- | --- |
| Agent → TTS | `/malbut/speech/response` Topic | `SpeechRequest`: `text`, `request_type` |
| STT → TTS | `/malbut/speech/playback_control` Service | `ControlSpeechPlayback`: `playback_id`, `command` → `accepted` |
| TTS → STT | `/malbut/speech/playback_status` Topic | `SpeechPlaybackStatus`: `playback_id`, `state` |

타입은 모두 `malbut_interfaces` 소속이다. 요청 종류는 `DIALOGUE=0`,
`NOTIFICATION=1`이며, 기본값은 대화 답변이다. Agent의 대화 응답은
`DIALOGUE`, 작업 상태 알림은 `NOTIFICATION`으로 발행한다.
Topic QoS는 `RELIABLE`, `VOLATILE`, `KEEP_LAST`, depth `10`이다.

제어 명령은 `pause`, `resume`, `stop`이다. `accepted`는 접수 여부이며,
실제 상태는 별도 Topic의 `playing`, `paused`, `finished`, `failed`,
`stopped`로 확인한다. 현재 요청이 아니거나 현재 상태에서 수행할 수 없는
제어는 거절한다. 생성 중인 요청은 중지할 수 있지만 일시정지는 재생 시작 후
가능하다. 메시지 필드가 추가되었으므로 Agent·인터페이스·TTS를 함께 다시
빌드하고 실행 중인 노드도 새 인터페이스로 재시작해야 한다.

## OpenAI API 스트리밍 음성

OpenAI는 CLI와 ROS node의 기본 backend다. `--backend`/`backend`를 생략하면
기존 Agent·STT를 바꾸지 않고 TTS 합성만 API로 처리한다. 로컬 모델 경로,
CUDA, PyTorch는 이 backend에 필요하지 않다. 기본 설정은
`gpt-4o-mini-tts`, `marin`, PCM이다.
공식 SDK의 스트리밍 응답을 받아 24 kHz signed 16-bit little-endian PCM을
기존 재생기가 사용하는 mono float32 조각으로 변환한다.

```mermaid
flowchart LR
    A["Agent의 답변 한 건"] --> B["OpenAI TTS 요청 한 번"]
    B --> C["도착하는 PCM 조각"]
    C --> D["기존 큐와 StreamingPlayer"]
    D --> E["스피커에서 전체 재생 완료"]
    E --> F["동일 playback_id의 finished 한 번"]
```

문장별 별도 API 요청이나 CUDA용 `SentenceSynthesizer`를 사용하지 않는다.
완성된 답변 텍스트 한 건을 보내고 음성만 스트리밍한다. LLM 답변의 텍스트
토큰 스트리밍은 아니다. 전체 음성이 도착하기 전부터 재생할 수 있지만,
첫 음성 지연과 중간 끊김은 실제 한국어·네트워크 환경에서 측정해야 한다.
초기 네트워크 조각 사이의 빈 구간을 줄이기 위해 첫 400 ms 분량의 음성을
모은 뒤 재생 큐에 전달한다. 짧은 음성은 정상 EOF에서 남은 버퍼를 전달한다.
이는 400 ms 내 응답을 보장한다는 뜻이 아니며, 이후 장시간 네트워크 지연을
완전히 숨기지는 못한다. SDK 디버그 로그는 요청 텍스트를 포함할 수 있으므로
음성 시험에서는 `openai`·`httpx`·`httpcore`의 DEBUG 로그를 켜지 않는다.
자세한 형식과 동작은 [공식 TTS 문서](https://developers.openai.com/api/docs/guides/text-to-speech)를 따른다.

- **외부 전송·비용:** 합성할 답변 텍스트를 OpenAI로 전송하고 API 비용이
  발생한다. 오프라인 기능이 아니며 Agent API 비용과 별도다.
  [현재 공식 요금](https://developers.openai.com/api/docs/pricing)을 확인한다.
- **음성 고지:** 사용자에게 사람이 아닌 AI 합성 음성임을 알린다. 터미널
  시험기와 ROS 시작 로그에도 이를 표시한다.
- **키:** 이미 승인하여 구성한 프로세스 환경의 `OPENAI_API_KEY`를 사용한다.
  CLI 인자나 ROS parameter로 키를 받지 않으며 키를 로그에 출력하지 않는다.
- **실패:** 자동 재시도·다른 backend로 fallback하지 않는다. 일부 음성이
  재생된 뒤 통신이 끊겨도 `finished`로 속이지 않고 해당 요청을 실패 처리한다.
  중지 뒤 들어오는 조각은 재생하지 않는다.
- **제어:** 기존 FIFO/우선순위, pause/resume/stop, 하나의 playback ID를
  유지한다. STT의 재생 중 입력 차단은 기존 playback 상태 연결을 재사용한다.

가상환경과 install·로그는 `ros2_ws` 밖에 둔다. 승인된 키가 현재 프로세스
환경에 준비된 상태에서 아래 명령을 사용한다. 키를 명령줄에 붙이지 않는다.
`sounddevice` 외에 운영체제의 PortAudio 공유 라이브러리가 필요하다.

```bash
/absolute/external/venv/bin/python -m pip install -r malbut_tts/requirements-api.txt
PYTHONPATH=malbut_tts /absolute/external/venv/bin/python -m malbut_tts.smoke \
  --api-model gpt-4o-mini-tts --api-voice marin \
  --api-timeout-seconds 8 --text '안녕하세요. 말벗이에요.'

# ROS 및 외부 install overlay를 적용한 같은 Python 환경:
/absolute/external/venv/bin/python -m malbut_tts.node --ros-args \
  -p api_model:=gpt-4o-mini-tts -p api_voice:=marin \
  -p api_timeout_seconds:=8.0
```

`--text`를 생략하면 직접 텍스트와 기존 재생 제어 명령을 입력할 수 있다. 일반
텍스트는 대화 답변, `/notice 텍스트`는 일반 알림으로 넣는다. `/pause`, `/resume`,
`/stop`은 현재 재생 ID를 사용하며 `/quit` 또는 Ctrl+C로 종료한다.
`--list-devices`와 `--device-index`로 출력 장치를 확인하고 선택할 수 있다.
`api_timeout_seconds`는 네트워크 요청의 timeout 설정이며, 발화 종료부터
사용자 청취까지의 종단간 지연이나 최대 재생 길이 보장이 아니다.

## ROS 노드

`tts_node`는 ROS 어댑터, `tts_smoke`는 같은 런타임을 사용하는 터미널 시험기다.
기존 `tts_receiver`는 합성 없이 수신 로그만 남기는 통신 확인용으로 유지한다.

```bash
colcon build --packages-select malbut_interfaces malbut_agent_server malbut_tts
source install/setup.bash
ros2 run malbut_tts tts_node
ros2 topic pub --once /malbut/speech/response malbut_interfaces/msg/SpeechRequest \
  '{text: "안녕하세요.", request_type: 0}'
ros2 topic echo /malbut/speech/playback_status
```

위 노드의 실제 합성에는 같은 Python 환경에 ROS와 해당 합성·오디오 의존성이
필요하다. 기본 실행에는 `requirements-api.txt`의 의존성과 `OPENAI_API_KEY`가
필요하다. 키나 SDK가 없으면 첫 음성 요청이 `failed`가 되며 로컬 backend로
자동 전환하지 않는다. 아래 CUDA 경로는 x86_64 Linux 노트북 실험용이며
Jetson 및 YOLO·STT 동시 성능은 별도 검증 대상이다.

## Linux CUDA 실험 경로

`--backend qwen-cuda` 또는 ROS `backend:=qwen-cuda`로 명시적으로 선택한다.
기존 큐·재생기·상태 Topic·중지 서비스를 재사용하며 CPU나 cloud로 자동
fallback하지 않는다. 공식 `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`의 로컬
snapshot을 사용한다.

모델과 가상환경, ROS 빌드·install·로그는 `ros2_ws` 밖에 둔다. CUDA용
PyTorch/torchaudio를 먼저 설치한 별도 Python 3.10 환경에서
`requirements-cuda-laptop.txt`를 사용한다. 기존 ROS 모듈을 사용할 환경이면
`--system-site-packages`가 필요하다. 모델은 사전에 revision을 고정하여
다운로드하며, 실행 중에는 `local_files_only=True`로만 로드한다.
`sounddevice` Python 패키지 외에 PortAudio 공유 라이브러리도 필요하다.
시스템에 없다면 외부 시험 디렉터리에 배포판 패키지를 풀고 해당 라이브러리
경로를 프로세스의 `LD_LIBRARY_PATH`로 지정한다. 모델/API 호출 전에
`sounddevice.check_output_settings(channels=1, dtype='float32', samplerate=24000)`로
장치 설정을 확인한다.

```bash
# PyTorch 2.6: whisper.cpp STT와 제한된 GPU 메모리를 공유하는 시험 환경.
# Torch를 import/CUDA 초기화하기 전에 해당 프로세스에만 적용한다.
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
PYTHONPATH=malbut_tts /absolute/venv/bin/python -m malbut_tts.smoke \
  --backend qwen-cuda --cuda-dtype float32 \
  --model-path /absolute/local/qwen-model --text '안녕하세요. 말벗이에요.'

# 같은 Python 환경에서 ROS 및 외부 overlay를 적용한 뒤:
/absolute/venv/bin/python -m malbut_tts.node --ros-args \
  -p backend:=qwen-cuda -p cuda_dtype:=float32 \
  -p model_path:=/absolute/local/qwen-model
```

기본 CUDA 정밀도는 `float32`이며 `float16`은 별도 실제 추론 시험이 필요하다.
RTX 2060 시험에서 `float16` sampling의 NaN/CUDA assertion이 재현되었으므로
이 장비의 음성 대화 시험은 `float32`로 실행한다. 정밀도를 자동 전환하지 않는다.
RTX 2060에서는 BF16이나 FlashAttention2를 요구하지 않고 SDPA를 사용한다.
GPU 메모리 부족·잘못된 PCM·합성 실패는 실패로 보고하며 다른 backend로
자동 전환하지 않는다.

RTX 2060에서 CUDA STT와 FP32 TTS를 함께 올렸을 때 긴 문장 합성 중
메모리 부족이 재현되었다. 위 `expandable_segments` 설정은 크기가 달라지는
할당에서 단편화를 줄이기 위한 PyTorch 2.6의 실험 옵션이다. 실제 메모리 용량을
늘리지 않으므로 임의 길이의 답변이나 다른 GPU 작업과의 공존까지 보장하지
않는다. 실패한 음성을 자동 재시도하거나 일부만 읽고 완료로 보고하지 않는다.
공식 설명: [PyTorch CUDA 메모리 관리](https://docs.pytorch.org/docs/2.6/notes/cuda.html#memory-management).

### 문장별 합성·재생

CUDA는 기본 `cuda_sentence_mode=true`, `sentence_max_chars=80`으로 실행한다.
기존 `SpeechRuntime`의 합성 스레드와 `StreamingPlayer`의 오디오 스레드 및
PCM 큐를 재사용하며 모델이나 ROS 요청을 문장 수만큼 만들지 않는다.

```mermaid
flowchart LR
    A["답변 한 건 / playback_id 한 개"] --> B["원문 순서대로 문장 분리"]
    B --> C["큐 여유 확인 후 한 문장 합성"]
    C --> D["PCM 큐: 재생 중 포함 최대 2개"]
    D --> E["별도 오디오 스레드에서 순차 재생"]
    E --> F["전체 재생 완료 후 finished 한 번"]
```

- 마침표·물음표·느낌표·줄바꿈을 사용해 분리한다. 80자를 넘는 문장은
  공백 등 가까운 경계로 추가 분리하며 일부를 요약하거나 생략하지 않는다.
- 분할 문자열을 다시 연결하면 원문과 같다. 지나치게 긴 공백 구간처럼
  이 계약과 길이 상한을 함께 지킬 수 없는 입력은 합성 전에 거절한다.
- 첫 문장이 완료되면 바로 재생하고, 다음 문장을 합성한다. 큐가 두 문장으로
  차 있으면 **다음 합성을 시작하기 전에** 기다려 무제한 미리 만들지 않는다.
- 문장 중간에 `finished`를 발행하지 않는다. STT의 half-duplex 차단은
  답변 전체 재생 동안 유지된다. 취소 시 남은 문장/PCM도 폐기한다.
- 뒤 문장에서 실패하면 이미 들린 앞 문장은 되돌릴 수 없으며, 답변 전체를
  `failed`로 처리한다. 남은 문장을 계속 읽거나 앞부분부터 재전송하지 않는다.
- 실험용 decoder 내부 분할 변경은 사용하지 않는다. Qwen 원본 decoder를
  사용하고, 모델에 전달하는 텍스트를 문장 단위로 제한한다.

비교 시험에만 `--no-cuda-sentence-mode` 또는 ROS
`-p cuda_sentence_mode:=false`로 답변 전체 합성을 선택할 수 있다.
`--sentence-max-chars` / ROS `sentence_max_chars`는 16..512 범위다.

**제한:** 공식 `qwen-tts==0.1.1`의 `generate_custom_voice()`는 호출 단위의
전체 PCM을 반환한다. 문장별 파이프라인은 첫 문장부터 재생하게 하지만 모델
내부 스트리밍이나 LLM 답변 토큰 스트리밍은 아니다. 다음 문장 합성이 재생보다
느리면 문장 사이에 무음이 생길 수 있고 첫 문장 합성 시간도 남는다.
모델 로드·CUDA 연산 도중 즉시 중단을 보장하지 않으며, 취소 뒤 나온 PCM은
재생하지 않는다. `finished`도 사용자가 실제로 들었다는 확인은 아니다.

공식 API: [Qwen3-TTS wrapper](https://github.com/QwenLM/Qwen3-TTS/blob/022e286b98fbec7e1e916cb940cdf532cd9f488e/qwen_tts/inference/qwen3_tts_model.py).

## 검증

```bash
PYTHONPATH=malbut_tts python3 -m pytest -q malbut_tts/test
```

코어·오디오 단위 시험은 `pytest`, `numpy`를 사용하고 실제 GPU나 스피커가
필요하지 않다. ROS 통합 시험은 ROS와 생성된 `malbut_interfaces`를 빌드해
환경을 적용한 뒤 실행한다. API 단위 시험은 가짜 SDK 응답을 사용하며
유료 API를 호출하지 않는다. 실제 스피커와 로컬 모델 또는 API 시험은 위
터미널 명령으로 별도로 확인하며, 자동 시험 통과를 실제 청취로 간주하지 않는다.
