# 통합 Bringup의 음성 준비와 preflight

실로봇의 정상 실행 경로는 **`build.sh` 하나로 빌드하고 `robot.launch.py` 하나로 실행**하는
방식이다. `robot.launch.py`가 Jetson용 whisper.cpp STT, Agent 대화 노드와 OpenAI TTS를
기본으로 포함한다. `speech.launch.py`는 이때 사용하는 하위 launch이며 음성만 진단할 때
별도로 실행할 수 있다. Agent의 현재 proposal-only 정책과 STT/TTS 명세는 그대로 사용한다.

## 최초 준비

실로봇에는 저장소의 **`malbut_test` 내용**을 `~/ros2_ws/src/malbut`으로 복사한다.
아래는 로봇의 Zsh 기준이다. 전체 저장소를 이 위치에 둔 경우에는 소스 명령 경로
`src/malbut`에만 `/malbut_test`를 추가한다. 빌드·설치·캐시 경로는 동일하다.

ROS 2 Humble과 제조사 환경, 현재 JetPack에 맞는 CUDA toolkit을 유지한다.
`build.sh`는 ROS와 같은 `/usr/bin/python3`의 Python 3.10, C++ 빌드 도구, Git,
CMake 3.18 이상과 `nvcc`가 필요하다. 없는 OS 도구는 최초에 준비한다:

```zsh
sudo apt install build-essential git cmake ninja-build python3-venv python3-pip libportaudio2
```

이 명령은 JetPack·CUDA·PyTorch 설치를 대신하지 않는다. CUDA toolkit은 로봇에 설치된
JetPack과 일치해야 하며 `nvcc`가 빌드 터미널에서 검색되어야 한다. `libportaudio2`는
TTS의 sounddevice 출력에 필요하다. ROS 의존성은 적용본 최상위 `README.md`를 따른다.

외부 whisper.cpp 소스를 최초 한 번 준비한다. 이미 같은 커밋의 깨끗한 checkout이 있으면
`WHISPER_CPP_SOURCE_DIR`에 그 절대 경로를 지정해 재사용할 수 있다.

```zsh
speech_cache="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_speech"
mkdir -p "$speech_cache"
git clone https://github.com/ggml-org/whisper.cpp.git "$speech_cache/whisper.cpp"
git -C "$speech_cache/whisper.cpp" checkout --detach da54572229bcf64ba367d96c7ef15770376c4280
mkdir -p "$speech_cache/models"
```

이미 확보한 다국어 `ggml-small.bin`을 `$speech_cache/models/ggml-small.bin`에 놓는다.
모델의 공식 위치·크기와 원본 근거는 [STT 네이티브 안내](../malbut_stt/native/README.md)에
있다. 아래 SHA-256이 일치하는지 확인한다:

```zsh
printf '%s  %s\n' \
  1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b \
  "$speech_cache/models/ggml-small.bin" | sha256sum --check -
```

빌드 스크립트는 OS 패키지·외부 소스·모델을 자동 다운로드하지 않는다.
CUDA 라이브러리는 로봇에서 빌드하며 Mac의 `.dylib`나 Python 가상환경을 복사하지 않는다.

## 통합 빌드

```zsh
source /opt/ros/humble/setup.zsh
source ~/ros2_ws/install/setup.zsh
cd ~/ros2_ws
bash src/malbut/build.sh
source install/malbut_test/local_setup.zsh
```

`build.sh`는 기본 `MALBUT_BUILD_SPEECH=1`로 다음을 처리한다:

1. 고정 커밋의 깨끗한 whisper.cpp 소스와 CUDA 도구를 확인한 뒤 STT 브리지를
   `Release`, `GGML_CUDA=ON`, `GGML_METAL=OFF`, CUDA architecture `87`로 빌드한다.
   빌드 대상은 `malbut_whisper`이고 네이티브 동시 빌드는 2개다.
2. 별도 음성 가상환경을 `--system-site-packages`로 생성하고 STT의
   `requirements-whisper-cpp.txt`와 TTS의 `requirements-api.txt`를 그 안에 설치한다.
3. 적용본의 음성·인식·주행 ROS 패키지를 함께 colcon 빌드한다.

음성 환경은 YOLO·ReID 환경과 분리된다. YOLO는 NumPy 1.26.4, ReID는 1.23.5를
사용하므로 음성의 NumPy `>=1.26,<2`를 ReID나 제조사 Python에 설치하지 않는다.
STT의 whisper.cpp CUDA 브리지는 Python Torch를 요구하지 않는다. Agent와 TTS는
기본 API 방식이므로 별도 GPU 모델을 적재하지 않는다.

기본 경로와 변경할 환경 변수는 다음과 같다. 아래 캐시의 기준은
`${XDG_CACHE_HOME:-$HOME/.cache}/malbut_speech`다.

| 구성 | 기본 경로 | 변경 변수 |
| --- | --- | --- |
| 음성 Python 환경 | `<캐시>/runtime` | `MALBUT_SPEECH_RUNTIME` |
| 외부 whisper.cpp 소스 | `<캐시>/whisper.cpp` | `WHISPER_CPP_SOURCE_DIR` |
| CUDA 빌드 디렉터리 | `<캐시>/whisper-cpp-build` | `MALBUT_STT_BUILD_DIR` |
| STT 모델 | `<캐시>/models/ggml-small.bin` | `MALBUT_STT_MODEL_PATH` |
| STT 라이브러리 | `<CUDA 빌드>/bin/libmalbut_whisper.so` | `MALBUT_STT_LIBRARY_PATH` |

런타임·빌드 경로를 바꾸면 빌드와 실행 터미널에서 같은 설정을 사용한다.
모델·라이브러리 환경 변수는 launch가 읽는 경로이며 파일을 만들거나 다운로드하지 않는다.
공유 whisper/ggml 라이브러리도 같은 CUDA 빌드의 `bin`에 있으므로 `.so` 하나만 옮기지
않고 빌드 디렉터리를 유지한다. 소스 checkout과 빌드 디렉터리는 분리한다.

CUDA 없는 CI나 센서 전용 빌드에서는 `MALBUT_BUILD_SPEECH=0 bash src/malbut/build.sh`로
음성 런타임·네이티브 빌드를 생략할 수 있다. 해당 실행에는 `speech:=false`를 지정한다.
이 옵션으로도 ROS 음성 패키지는 colcon 빌드 대상에 남는다.

제조사 패키지는 재빌드하지 않으며 `COLCON_IGNORE`는 유지한다. 새 터미널마다 제조사
환경과 `~/ros2_ws/install/malbut_test/local_setup.zsh`를 source한다.
`ros2 pkg prefix malbut_bringup`과 `ros2 pkg prefix malbut_stt`가
`~/ros2_ws/install/malbut_test/` 아래인지 확인한다. 인터페이스 변경을 반영하려면 기존
Agent·STT·TTS 프로세스도 종료 후 새로 시작한다.

## 실행과 장치 선택

현재 Agent의 기본 provider와 TTS backend는 `openai`이며 TTS 모델은
`gpt-4o-mini-tts`, 목소리는 `marin`이다. 실제 대화에서는 텍스트가 유료 외부 API로
전달되며 출력 음성은 AI가 생성한다. `OPENAI_API_KEY`와 필요한 Agent 환경 설정을
실행 터미널에 미리 export한다. 키를 launch 인자나 YAML에 넣지 않는다.
launch는 `.env` 파일을 자동 로드하지 않는다.

```zsh
ros2 launch malbut_bringup robot.launch.py
```

기본 센서 모드에서 차체·센서·인식·음성을 함께 시작한다. 저장 지도 주행은 같은
명령에 `mode:=navigation map:=/실제/지도.yaml`, 매핑 대기는 `mode:=mapping`을
지정한다. `speech:=true`는 모든 모드의 기본값이다. 센서·주행 모드에서는 로봇 준비
확인 뒤 음성 점검을 시작하고, 매핑 모드에서는 대기 중인 AutoSLAM 서버와 함께 시작한다.
이 실행만으로 이동 Goal을 보내지는 않는다.

장치를 따로 지정해야 할 때는 아래 목록을 로봇에서 확인한다:

```zsh
speech_python="${MALBUT_SPEECH_RUNTIME:-${XDG_CACHE_HOME:-$HOME/.cache}/malbut_speech/runtime}/bin/python"
"$speech_python" -m malbut_stt.smoke --list-devices
"$speech_python" -m malbut_tts.smoke --list-devices
ros2 launch malbut_bringup robot.launch.py --show-args
```

입력은 PvRecorder, 출력은 sounddevice의 번호이며 서로 다른 번호 체계다.
기본값 `-1`은 각 라이브러리의 기본 장치다. 노트북의 장치 번호를 그대로 적용하지 않는다.

```zsh
ros2 launch malbut_bringup robot.launch.py \
  speech_input_device:=-1 speech_output_device:=-1
```

| 음성 인자 | 기본값 / 역할 |
| --- | --- |
| `speech` | `true`; `false`면 로봇 구성만 진단 |
| `speech_python_executable` | 위 음성 환경의 `bin/python`; YOLO의 `python_executable`과 별개 |
| `stt_model_path`, `stt_library_path` | 위 환경 변수 또는 기본 캐시 경로 |
| `speech_input_device`, `speech_output_device` | 각각 `-1` |
| `stt_cpp_threads` | `6` CPU 보조 스레드 |
| `speech_input_has_aec` | `false`; 검증된 에코 제거 입력일 때만 `true` |
| `speech_agent_provider` | `openai`; `mock`으로 바꿔도 TTS는 OpenAI 사용 |
| `speech_preflight_timeout_s` | `120.0`; 모델·장치 점검 전체 제한시간 |
| `speech_peer_timeout_s` | `30.0`; ROS 연결 대기 제한시간 |

AEC 인자는 에코 제거 기능을 구현하거나 활성화하지 않는다. STT의 나머지 endpoint
설정은 `malbut_stt/config/jetson.yaml`을 사용한다. 다른 STT/TTS가 같은 마이크·출력
장치를 사용 중이면 먼저 정리한다. 통합 Bringup과 별도 음성 launch를 중복 실행하지 않는다.

## 시작 순서와 통과 의미

1. **Preflight**: 생성된 ROS 음성 타입, Agent 설정, OpenAI SDK와 키 존재,
   출력 장치의 24 kHz mono float32 스트림, STT ABI 2 모델 로딩,
   마이크 16 kHz PCM 512 samples 읽기와 20 ms VAD 입력을 점검한다.
   출력에는 100 ms 무음만 쓰며, 입력을 전사·저장·전송하지 않는다.
2. **Agent와 TTS**: 점검 프로세스가 성공 종료한 뒤 시작한다.
3. **ROS 연결 확인**: 두 Service와 타입이 맞는 Topic의 발행자·구독자가
   발견된 뒤 STT를 시작한다. 단순 프로세스 생성 시점을 준비 완료로 보지 않는다.

`speech_preflight_passed`, `speech_peers_ready` 순서로 통과 로그를 확인한다.
점검 실패·제한시간 초과·실행 중 음성 노드 종료 시 통합 Bringup 전체를 실패 코드로
종료한다. Ctrl+C도 함께 실행한 로봇·음성 구성을 정리한다.
점검에서 사용한 모델·마이크·출력 스트림은 반환 전에 해제하고,
같은 Python 실행 파일·모델·장치 설정으로 실제 노드를 시작한다.
실패 출력의 `phase`로 설정·ROS 타입·TTS 출력·STT 모델/마이크 중 실패 단계를 확인한다.
예외 원문, API 키, 마이크 샘플은 로그에 출력하지 않는다.

Preflight는 **유료 API 요청을 보내지 않는다**. 키의 유효성·API 접근 권한·네트워크·
실제 음성 합성은 검증하지 않는다. 모델 로딩 시 GPU를 요청하지만 CUDA에서 실제
추론했음을 확인하지 않는다. 성공 출력에도 `cuda_execution_verified`,
`api_request_verified`, `transcription_verified`를 `false`로 남긴다.
ROS 연결 확인은 endpoint 발견이며 Agent 대화 worker의 DB 초기화나 LLM 응답 성공의
증거가 아니다. 대화 worker 초기화가 실패하면 Agent는 종료 코드 2로 끝나고 launch가
전체 구성을 종료한다. 점검 성공 이후의 장치 분리나 네트워크 장애도 별도 실행 중 오류다.

## 음성만 진단하기

차체·Nav2·Manager를 켜지 않고 모델·장치 점검만 수행할 때 아래 하위 launch를 사용한다.
기본 경로와 달리 설치했다면 같은 환경 변수나 해당 절대 경로를 사용한다.

```zsh
speech_cache="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_speech"
ros2 launch malbut_bringup speech.launch.py \
  python_executable:="${MALBUT_SPEECH_RUNTIME:-$speech_cache/runtime}/bin/python" \
  stt_model_path:="${MALBUT_STT_MODEL_PATH:-$speech_cache/models/ggml-small.bin}" \
  stt_library_path:="${MALBUT_STT_LIBRARY_PATH:-${MALBUT_STT_BUILD_DIR:-$speech_cache/whisper-cpp-build}/bin/libmalbut_whisper.so}" \
  input_device:=-1 output_device:=-1 preflight_only:=true
```

이 진단은 성공 시 종료한다. 음성만 계속 시험하려면 같은 명령의 `preflight_only`를
`false`로 바꾼다. 정상 로봇 운용에는 위 `robot.launch.py`를 사용한다.

## 로봇에서 남겨야 할 시험 결과

- CUDA 장치 선택 로그와 고정 PCM의 실제 전사·지연·메모리 사용량.
- 마이크 호출어 → 최종 전사 1회 → Agent 응답 → 스피커 재생 → `finished`.
- TTS 완료 후 대화 종료, 실패 후 재시도, Ctrl+C 후 마이크·모델·스피커 해제.
- YOLO와 함께 실행할 때 공유 메모리, 인식 지연, 첫 음성 지연, 중간 끊김.

위 실제 시험을 기록하기 전에는 통합 launch/build/preflight 연결과 로컬 자동 검증까지만
완료한 상태다. Manager 명령 실행과 로봇 이동은 별도 검증 범위다.
