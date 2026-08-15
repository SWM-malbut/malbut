# SWM25-34 기본 로컬 STT (임시 one-shot)

## 문서 정보

| 항목 | 값 |
| --- | --- |
| Jira 스토리 | SWM25-34 음성 명령 수행의 임시 STT 선행 작업 |
| 대상 패키지 | `malbut_agent_server` |
| 목적 | WAV/마이크 한 건의 로컬 STT와 비실행 Agent 대화 demo |
| 구현 방식 | 선택 설치하는 `faster-whisper==1.2.1`, CPU `int8` 우선 |
| 입력 방식 | `--wav PATH`와 `--microphone` 중 정확히 하나 |
| STT CLI 기본값 | `base`, `cpu`, `int8`, `ko`, 마이크 5초 |
| 대화 demo 기본값 | `small`, `cpu`, `int8`, `ko`, confidence `0.60` |
| 현재 증명하지 못한 것 | 실제 마이크 E2E, 소음·self-echo 품질, TTS·ROS 연결 |

이 기능은 전체 음성 비서가 아니라 **한 번 녹음하고 한 번 인식하는 로컬
STT 데모**다. 모델이 완전히 내려받아진 뒤의 추론은 로컬에서 수행하며 유료
STT API를 호출하지 않는다. 그러나 아래의 모델 다운로드 단계에서는 Hugging
Face에 접속한다.

2026-08-15 개발 호스트에서 선택 의존성과 `base`/`small` 모델을
격리된 가상환경으로 준비하고, 합성 한국어 음성 24개와 실제
OpenAI 대화 provider 연결을 검증했다. 그러나 실제 사람이 마이크에
말한 E2E, 생활 소음, TV와 로봇 self-echo는 아직 시험하지 않았다.

## 1. 구성과 책임 경계

```text
사용자가 가진 PCM WAV             ALSA 마이크
        |                              |
        | --wav                        | --microphone
        |                              v
        |                  arecord(S16_LE, 16 kHz, mono)
        |                              |
        |                     권한이 제한된 임시 WAV
        +---------------+--------------+
                        |
                        v
      동일 FD → 비공개 snapshot → 형식·길이 검사
                        |
                        v
          FasterWhisperBackend(선택 의존성)
            base / CPU / int8 / ko
                        |
                        v
       LocalSTTResult(text, confidence, AudioMetadata)
                  |                    |
                  | CLI                | 명시적인 adapter 호출
                  v                    v
         stdout에 최종 텍스트     SpeechTranscriptEvent
                              (기본 origin=unknown)
                                       |
                                       v
                         SpeechConversationCoordinator
```

구현 위치와 역할은 다음과 같다.

| 구성 요소 | 책임 |
| --- | --- |
| `malbut_agent_server/local_stt.py` | WAV 검사, 마이크 1회 녹음, 로컬 추론, CLI |
| `FasterWhisperBackend` | optional dependency를 지연 import하고 최종 인식 결과를 정규화 |
| `build_transcript_event(...)` | 결과 텍스트와 제한된 메타데이터를 기존 음성 계약으로 변환 |
| `malbut_agent_server/speech.py` | 신뢰된 사용자·세션 결속과 final/confidence 정책; 오디오 처리 책임 없음 |

마이크 임시 파일은 mode `0700`인 임시 디렉터리와 mode `0600`인 WAV로
제한하고 context 종료 시 정리한다. 원시
오디오 bytes, WAV 경로와 URI는 `SpeechTranscriptEvent`, Agent 요청, 대화 DB,
audit log로 넘기지 않는다. 사용자가 직접 준 WAV 원본은 이 명령이 삭제하거나
변경하지 않는다.

## 2. 설치

### 2.1 시스템 준비

가상환경 생성과 ALSA 녹음을 먼저 확인한다. 부족한 환경에서만 다음
시스템 패키지를 한 번 설치한다.

```bash
sudo apt-get update
sudo apt-get install -y python3-venv alsa-utils
```

이 명령은 운영체제 패키지를 변경한다. 이미
`dpkg-query -W python3-venv alsa-utils`가 두 package를 모두 설치 상태로
보고하고 실제 임시 venv 생성이 성공한다면 생략해도 된다. Ubuntu에서는
`python3 -m venv --help`만 성공해도 `ensurepip`가 빠져 실제 생성이 실패할 수
있다.

### 2.2 격리된 Python 환경과 STT 선택 의존성

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server

python3 -m venv .venv-stt
source .venv-stt/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[stt]'
```

`stt` extra는 `faster-whisper==1.2.1`을 설치한다. 기본 Agent Server 설치에는
무거운 STT 런타임을 강제로 추가하지 않는다. CPU 실행은 다음 기본값을
사용한다.

```text
model=base, device=cpu, compute_type=int8, language=ko
```

`int8`은 CPU 메모리와 연산량을 줄이기 위한 실행 형식이다. 같은 음성이라도
CPU 종류, thread 수와 모델 warm-up 여부에 따라 처리 시간이 달라지며 실시간
처리를 보장하지 않는다.

## 3. 모델을 명시적으로 내려받기

CLI는 기본적으로 로컬 cache만 사용한다. 모델이 없는데도 뜻하지 않게
네트워크를 쓰지 않게 하기 위한 정책이다. 기본 `base` 모델을 먼저 명시적으로
받으려면 활성화된 가상환경에서 실행한다.

```bash
python - <<'PY'
from huggingface_hub import snapshot_download

path = snapshot_download(repo_id='Systran/faster-whisper-base')
print(path)
PY
```

이 단계는 네트워크와 디스크 공간을 사용한다. 모델 출처, 라이선스와 배포
정책을 확인한 뒤 실행해야 한다. `tiny` 또는 `small`을 쓰려면 각각
`Systran/faster-whisper-tiny`, `Systran/faster-whisper-small`을 내려받고 CLI의
`--model`도 같은 크기로 지정한다.

별도 다운로드 명령 대신 첫 인식 때 다운로드를 명시적으로 허용할 수도 있다.

```bash
malbut-stt --wav ./sample.wav --allow-model-download
```

`--allow-model-download`을 생략하면 cache에 없는 모델 때문에 안전하게 실패한다.
다운로드를 끝낸 뒤에는 이 옵션을 빼서 로컬 전용 동작을 확인하는 편이 좋다.

## 4. 실행

### 4.1 WAV 파일 한 건

```bash
malbut-stt --wav ./sample.wav
```

작은 모델로 빠르게 기능만 확인하려면 다음과 같이 실행한다.

```bash
malbut-stt \
  --wav ./sample.wav \
  --model tiny \
  --language ko \
  --device cpu \
  --compute-type int8
```

성공 시 stdout에는 **확정 transcript 텍스트만** 출력한다. shell redirect를
사용하면 그 텍스트가 파일에 저장되므로 개인정보가 포함된 음성에는 주의한다.
오류는 원시 오디오, 임시 파일 경로와 인식 문장을 포함하지 않는 정제된
메시지로 stderr에 출력하고 비정상 종료한다.

| 종료 코드 | 의미 |
| --- | --- |
| `0` | transcript 출력 성공 |
| `2` | CLI 또는 WAV 입력 검증 실패 |
| `3` | 마이크 녹음 실패 |
| `4` | faster-whisper, 모델 또는 로컬 backend 사용 불가 |
| `5` | 인식할 발화를 찾지 못함 |
| `6` | backend 추론 실패 |

지원 WAV는 16-bit PCM WAV여야 하며 최대 파일 크기는 6 MiB다. 이벤트 경계에서
허용하는 범위는 길이 1 ms~30초, sample rate 8~48 kHz, channel 1~2개다. 압축
코덱, 8/24/32-bit PCM 또는 손상된 WAV는 먼저 16-bit PCM WAV로 변환해야 한다.
symbolic link와 regular file이 아닌 입력은 거절한다. 원본을 같은 file
descriptor에서 최대 6 MiB까지만 읽어 mode `0700` 디렉터리의 mode `0600`
snapshot으로 만들고, 그 snapshot을 검증·추론한 뒤 삭제한다. 따라서 경로를
검증한 뒤 다른 파일로 바꾸는 경쟁 조건이 inference 입력을 바꾸지 못한다.
삭제는 재시도하고 경로가 실제로 사라졌는지 확인한다. cleanup이 끝내 실패하면
성공을 반환하지 않지만, 파일시스템 자체가 삭제를 거부한 residue까지 없다고
주장하지 않는다.

### 4.2 마이크에서 한 번 녹음

먼저 ALSA가 보는 장치를 확인한다.

```bash
arecord --list-devices
```

기본 장치에서 5초간 녹음하고 한 번 인식한다.

```bash
malbut-stt --microphone --seconds 5
```

특정 ALSA 장치를 선택할 때는 `arecord --list-devices` 결과의 장치 문자열을
전달한다.

```bash
malbut-stt \
  --microphone \
  --seconds 8 \
  --audio-device 'plughw:1,0'
```

`--seconds`는 정수 1~30초다. 소수는 요청보다 오래 녹음하지 않고 거절한다.
녹음은 `arecord`의 S16_LE, 16 kHz, mono 형식을
사용하고 mode `0700`인 임시 디렉터리의 mode `0600` WAV에 기록한다. CLI는
STT dependency와 모델을 먼저 준비한 뒤 녹음을 시작한다. 정상 종료, 처리 가능한
실패와 인터럽트 경로에서 임시 파일을 정리한다. 강제 전원 종료까지 완전한 삭제를
보장하는 지속성 저장소는 아니다. cleanup 재시도 뒤에도 파일이 남으면 명령은
실패로 끝난다.

### 4.3 주요 옵션

| 옵션 | 허용값·기본값 | 의미 |
| --- | --- | --- |
| `--wav PATH` | `--microphone`과 배타적 | 기존 WAV 한 건 인식 |
| `--microphone` | `--wav`와 배타적 | `arecord`로 한 번 녹음 |
| `--seconds` | 정수 1~30, 기본 5 | 마이크 녹음 길이 |
| `--language` | 기본 `ko` | 인식 언어 힌트 |
| `--model` | `tiny`, `base`, `small`; 기본 `base` | 정확도·속도·메모리 절충 |
| `--device` | `auto`, `cpu`, `cuda`; 기본 `cpu` | 추론 장치 |
| `--compute-type` | `default`, `int8`, `float16`, `int8_float16`; 기본 `int8` | 추론 숫자 형식 |
| `--audio-device` | ALSA 장치 문자열 | 마이크 입력 장치 |
| `--allow-model-download` | 기본 꺼짐 | cache miss 때 모델 다운로드 허용 |

기본 경로는 NVIDIA GPU가 설치되어 있어도 CPU `int8`이다. CUDA는 별도 CUDA,
cuDNN, CTranslate2 호환성을 사용자가 검증한 뒤 명시적으로 선택해야 한다.

## 5. confidence의 의미

Whisper 계열이 제공하는 확률은 실제 환경의 문장 정답 확률로 보정된 값이
아니다. 이 adapter의 `confidence`는 다음 순서로 계산하는 **거절용
휴리스틱**이다.

1. word probability가 있으면 모든 word probability의 산술 평균을 쓴다.
2. word probability가 없으면 각 segment의 `exp(avg_logprob)`를 segment
   길이로 가중 평균한다.
3. 결과를 0~1 범위로 제한하고, 어느 방식을 사용했는지 `confidence_basis`에
   남긴다.

- 값이 높다고 transcript가 정확하다고 보증하지 않는다.
- 서로 다른 모델, 언어, 마이크와 소음 환경의 값을 그대로 비교하면 안 된다.
- 빈 결과와 발화로 판단할 근거가 부족한 결과는 성공 transcript로 만들지 않는다.
- 기존 `SpeechConversationCoordinator`의 기본 최소값 `0.75`도 임시 정책이다.
- 운영 임계값은 한국어 실측 데이터의 WER, 의도 정확도, 오탐·미탐을 함께 보고
  다시 보정해야 한다.

따라서 데모에서 낮은 confidence로 차단된 결과를 무조건 임계값 하향으로
통과시키면 안 된다. 먼저 모델 크기, 마이크 레벨, 거리와 배경 소음을 확인한다.

## 6. 기존 `SpeechTranscriptEvent`와의 연결

`build_transcript_event(...)`는 `LocalSTTResult`를 기존
`SpeechTranscriptEvent` 경계에 맞춘다. 이 연결은 다음 원칙을 유지한다.

- one-shot 결과만 사용하므로 `is_final=true`다.
- `text`, 휴리스틱 `confidence`, 길이·sample rate·channel metadata만 넘긴다.
- audio bytes, PCM 배열, WAV 경로와 URI를 넘기지 않는다.
- 일반 WAV의 capture origin 기본값은 `unknown`이라 coordinator가 추론 전에
  차단한다. 신뢰된 마이크 adapter만 실제 capture 직후 `microphone`을 명시한다.
- `user_id`는 STT payload가 정하지 않는다.
- 인증된 로컬 계층이 만든 `TrustedSpeechBinding`에서 사용자와 대화 세션을
  결속한다.
- `utterance_id`, `sequence`, `capture_epoch`, timestamp는 adapter가 명시적으로
  제공하며 재시도·self-echo 차단에 사용한다.
- final transcript라도 confidence, binding, 순서 또는 capture epoch 검사를
  통과하지 못하면 Agent provider 앞에서 차단한다.

CLI는 사용자가 인식 결과를 확인하기 위한 도구이며 자동으로 Agent Server,
OpenAI provider 또는 Tool 실행 경로를 호출하지 않는다. 실제 대화로 보내려면
신뢰된 local/ROS adapter가 `build_transcript_event(...)`와
`SpeechConversationCoordinator`를 명시적으로 연결해야 한다.

### 6.1 지원하는 one-shot 대화 demo

`malbut-voice-demo`는 위 연결을 최소 범위로 구현한 개발용 CLI다.
모든 provenance 상태는 CLI 프로세스 내부가 소유하며, 지원하는 public
경계는 `main()` 하나다. 임의 `LocalSTTResult`를 마이크 결과로
승격시키는 public Python API는 제공하지 않는다.

```bash
# 완전 오프라인 Mock 대화
.venv-stt/bin/malbut-voice-demo --microphone --seconds 5

# 승인된 mode-0600 env 파일을 명시한 OpenAI 대화
.venv-stt/bin/malbut-voice-demo \
  --microphone \
  --provider openai \
  --env-file ./.env.local \
  --agent-model gpt-5.6-terra \
  --seconds 5
```

대화 demo의 제한은 다음과 같다.

- STT `small`/CPU `int8`, confidence `0.60`; 전역 `0.75`는 불변
- `available_tools=()`, `trusted_robot_state=false`, SQLite `:memory:`
- provider fallback 없음, retry 0회, request/total timeout 15/20초
- OpenAI reasoning `none`, output 상한 500 token
- OpenAI는 명시적 `--env-file` 하나에서만 key를 읽음
- env 파일은 regular/no-symlink/현재 사용자 소유/mode `0600`을 요구
- redirecting HTTP(S)/ALL 프록시 설정이 있으면 마이크를 열기 전에 거절;
  `NO_PROXY` bypass 목록만 있는 경우는 허용
- 실제 OpenAI provider/model과 완전한 양의 token usage를 받은 경우만
  exit `0`; reliability fallback을 성공으로 표시하지 않음
- 낮은 confidence는 provider 호출 0회, 고정된 재시도 메시지와
  exit `8`
- 응답은 현재 terminal의 stdout에만 표시; 스피커 TTS는 없음

STT dependency와 모델은 녹음 전에 준비한다. OpenAI adapter 구성도
녹음 전에 검사하지만, 실제 API 인증·모델·네트워크 readiness는
transcript를 보내는 첫 request에서만 확인된다. 따라서 key가 유효하지
않으면 녹음과 로컬 STT 뒤에 고정 오류로 종료할 수 있다. 이를
"녹음 전 OpenAI 접속 성공"으로 표현하지 않는다.

기존 계약의 자세한 내용은
[`SWM25-76 음성 대화 파이프라인`](SWM25-76_VOICE_CONVERSATION_PIPELINE.md)을
참조한다.

## 7. 개인정보와 운영 주의사항

- 모델을 cache에 받은 뒤의 faster-whisper 추론은 로컬이지만, 모델 다운로드는
  Hugging Face에 접속한다.
- 마이크 음성과 transcript에는 민감한 대화가 들어갈 수 있다. 공유 장치에서
  stdout redirect, shell session recording과 디버그 로그를 켜지 않는다.
- adapter는 원시 오디오를 Agent 경계와 audit에 넣지 않지만, 사용자가 지정한
  WAV 파일의 생성·보존·삭제 책임까지 대신하지 않는다.
- Agent 대화 파이프라인에 연결하면 **확정 transcript 텍스트는 기존 대화 보존
  정책에 따라 DB에 저장될 수 있다.** 로컬 추론과 텍스트 비저장은 같은 뜻이
  아니다.
- 자동 모델 다운로드는 기본 차단되어 있다. 오프라인 시연에서는 모델을 미리
  받고 `--allow-model-download` 없이 실행한다.
- 오류 메시지와 audit에는 transcript 원문, audio bytes와 임시 WAV 경로를
  넣지 않는다. audit projection은 내용 대신 `text_chars`처럼 제한된 수치만
  사용한다.

## 8. 검증 방법

STT 단위 시험은 실제 모델, 네트워크와 마이크를 사용하지 않고 backend와
`arecord`를 대체한 결정론적 test double로 실행해야 한다.

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server
source .venv-stt/bin/activate

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
python -m pytest -q test/test_local_stt.py
```

기존 음성 경계와 전체 Agent 회귀도 함께 확인한다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
python -m pytest -q test/test_speech_pipeline.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
python -m pytest -q test
```

ROS 2 패키지 환경에서는 lint와 package 시험을 별도로 실행한다.

```bash
cd ~/ros2_ws
colcon test --packages-select malbut_agent_server
colcon test-result --verbose
```

이 시험의 통과는 WAV 검사, backend 경계, 정제된 실패, 임시 파일 cleanup과
`SpeechTranscriptEvent` 변환이 코드 수준에서 동작한다는 근거다. 실제 모델을
설치하거나 실제 마이크·스피커·ROS 통합 품질을 증명하지 않는다.

2026-08-15 최종 소스에서 다음 결과를 확인했다.

- `test/test_local_stt.py`: **99 passed**
- `test/test_local_voice_demo.py`: **54 passed**
- 로컬 STT + one-shot demo + 음성 계약: **222 passed**
- 전체 `test/`: **740 passed**, failure·error·skip 0
- `colcon build --symlink-install --packages-select malbut_agent_server`: 성공
- `colcon test`와 package-scoped `colcon test-result --verbose`:
  **740 tests, 0 errors,
  0 failures, 0 skipped**
- 전체 package/test/script 대상 `flake8`, `pydocstyle`, `compileall`과
  `git diff --check`: 성공

위 pytest/colcon 회귀는 실제 마이크, 모델 다운로드, GPU와 유료 API를
사용하지 않았다. 별도 live 평가에서는 cached `base`/`small`과 합성
OpenAI TTS 음성, 실제 대화 provider를 사용했으며 사용자 음성은 녹음하지
않았다. 수치·비용·한계는
[`SWM25-34·35 Live Voice 평가`](../evaluations/SWM25-34_35_LIVE_VOICE_EVALUATION_2026-08-15.md)에
분리해 두었다.

## 9. 의도적으로 남긴 blocker

이번 임시 구현에는 다음 기능이 없다.

1. **스트리밍과 partial transcript**: 전체 WAV가 끝난 뒤 final 결과 한 건만
   만든다.
2. **wake word**: 항상 듣는 호출어 감지가 없다.
3. **VAD 소유 계층**: 발화 시작·종료 판단, timeout과 무음 정책을 확정하지
   않았다.
4. **화자·사용자 인식**: 목소리로 사람을 식별하지 않으며
   `speaker_id -> user_id` 신뢰 adapter가 없다.
5. **TTS와 echo cancellation**: 답변 음성 재생, AEC, 실제 barge-in과 재생
   취소가 없다.
6. **ROS 연결**: transcript Topic, QoS, clock domain, node lifecycle을 구현하지
   않았다.
7. **실시간·실장치 품질**: 합성 24개의 CER/지연은 측정했지만,
   실제 화자의 한국어 WER, 의도 정확도, 소음·거리별 품질,
   first-token/final latency와 CPU 사용량은 측정하지 않았다.
8. **실물 명령 안전성**: STT 텍스트가 Nav2, 촬영, 알림 같은 물리 Tool을 직접
   실행하지 않는다. 기존 confirmation·authorization 경계를 우회하지 않는다.
9. **운영 오디오 수명주기**: 비정상 종료, crash dump, swap, 백업까지 포함하는
   녹음 데이터 삭제 정책과 감사 절차가 없다.
10. **모델·dependency 재현성**: Python package는 `faster-whisper==1.2.1`로
    고정했지만 Hugging Face model commit과 transitive wheel hash는 고정하지
    않았다. 평가·배포 전에는 model revision, lock file과 artifact hash를 함께
    기록해야 한다.

따라서 이번 단계의 완료 기준은 “사전에 받은 로컬 모델로 WAV 또는 짧은 마이크
입력을 한 번 텍스트로 바꾸고, 그 결과를 기존 final transcript 계약으로 안전하게
변환할 수 있음”까지다. 연속 음성 대화 기능의 완료 기준으로 사용하면 안 된다.
