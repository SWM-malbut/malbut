# 통합 Bringup의 음성 준비와 preflight

실로봇은 **최초 `setup.sh` 준비 → `build.sh` 빌드 → `cloud.launch.py` 웹 연결 → 웹의
Bringup 준비** 순서로 실행한다. 터미널에서 직접 실행할 때는 `robot.launch.py`를 사용한다.
`robot.launch.py`가 Jetson용 whisper.cpp STT, Agent 대화 노드와 OpenAI TTS를
기본으로 포함한다. `speech.launch.py`는 이때 사용하는 하위 launch이며 음성만 진단할 때
별도로 실행할 수 있다. Agent의 현재 proposal-only 정책과 STT/TTS 명세는 그대로 사용한다.

## 최초 준비

실로봇에는 저장소의 **`malbut_test` 내용**을 `~/ros2_ws/src/malbut`으로 복사한다.
아래는 로봇의 Zsh 기준이다. 전체 저장소를 이 위치에 둔 경우에는 소스 명령 경로
`src/malbut`에만 `/malbut_test`를 추가한다. 빌드·설치·캐시 경로는 동일하다.

ROS 2 Humble과 제조사 환경, 현재 JetPack에 맞는 CUDA toolkit을 유지한다.
CUDA toolkit은 로봇에 설치된 JetPack과 일치해야 하며 `nvcc`가 빌드 터미널에서
검색되어야 한다. 준비 스크립트는 필요한 ROS 패키지 의존성을 설치하며,
CUDA·JetPack·PyTorch를 재설치하지 않는다.
음성 빌드에는 ROS와 같은 `/usr/bin/python3`의 Python 3.10을 사용한다.

```zsh
source /opt/ros/humble/setup.zsh
source ~/ros2_ws/install/setup.zsh
bash ~/ros2_ws/src/malbut/setup.sh
```

`setup.sh`는 최초 준비 또는 의존성 변경 시 실행하며 다음을 처리한다:

1. 필요한 OS 개발 도구와 PortAudio, 기존 홈캠 설치 스크립트의 개발 의존성을 준비한다.
   명시한 로봇 패키지와 `homecam_media_agent`, `homecam_detector` 경로로 `rosdep`을 실행한다.
2. 외부 whisper.cpp 소스를 커밋 `da54572229bcf64ba367d96c7ef15770376c4280`에 고정한다.
   같은 커밋의 깨끗한 checkout은 재사용하고 기존 소스에 수정이 있으면 덮어쓰지 않고 중단한다.
3. 다국어 `ggml-small.bin`을 내려받고 SHA-256
   `1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b`를 확인한다.
   정상 모델은 다시 받지 않으며 부분 다운로드나 검증 실패를 완료로 처리하지 않는다.

기본 캐시는 `${XDG_CACHE_HOME:-$HOME/.cache}/malbut_speech`다.
기존 음성 소스나 모델을 사용하려면 실행 전 `WHISPER_CPP_SOURCE_DIR`,
`MALBUT_STT_MODEL_PATH`를 각각 절대 경로로 export한다. 기존 파일이 검증을 통과하지
못하면 보존한 채 중단하므로 경로와 파일을 확인한다. 모델 원본 근거는
[STT 네이티브 안내](../malbut_stt/native/README.md)에 있다.

음성 가상환경 생성과 CUDA 라이브러리 빌드는 아래 `build.sh`가 처리한다.
모델·가상환경·컴파일 결과는 Git에 넣지 않으며, CUDA 라이브러리는 로봇에서 빌드한다.
Mac의 `.dylib`나 Python 가상환경을 복사하지 않는다.

## 통합 빌드

```zsh
source /opt/ros/humble/setup.zsh
source ~/ros2_ws/install/setup.zsh
cd ~/ros2_ws
bash src/malbut/build.sh --cmake-args -DBUILD_TESTING=OFF
source install/malbut_test/local_setup.zsh
```

`build.sh`는 긴 홈캠 빌드 전에 음성 소스·모델·CUDA 도구를 확인한다. 빠졌으면
`setup.sh` 실행을 안내하며, 외부 음성 소스나 모델을 자동 다운로드하지 않는다.
기본 `MALBUT_BUILD_SPEECH=1`로 다음을 처리한다:

1. 고정 커밋의 깨끗한 whisper.cpp 소스와 CUDA 도구를 확인한 뒤 STT 브리지를
   `Release`, `GGML_CUDA=ON`, `GGML_METAL=OFF`, CUDA architecture `87`로 빌드한다.
   빌드 대상은 `malbut_whisper`이고 `nproc`으로 가용 CPU 수를 확인해 병렬 빌드한다.
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
`MALBUT_STT_MODEL_PATH`는 준비·빌드 확인·launch에서 함께 사용한다.
`MALBUT_STT_LIBRARY_PATH`는 launch가 읽을 라이브러리 경로이며 파일을 만들지 않는다.
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
웹 경로에서는 [클라우드 실행 안내](https://github.com/SWM-malbut/malbut/blob/main/malbut_test/README_CLOUD.md)에
따라 같은 터미널에서 `cloud.launch.py`를 켜고 웹에서 **Bringup 준비**를 누른다.
웹 연결만으로는 음성이 시작되지 않는다. 아래는 웹을 사용하지 않을 때의 직접 실행이다.

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
"$speech_python" -m sounddevice
ros2 launch malbut_bringup robot.launch.py --show-args
```

입력과 출력 모두 위 sounddevice 목록의 장치 번호를 그대로 사용한다.
입력 기본값은 `0`이며, 현재 로봇에서 확인한 `XFM-DP-V0.0.18: USB Audio`
(ALSA `hw:0,0`)를 선택한다. 입력 채널이 있는 장치인지 확인한다.
`-1`을 명시하면 시스템 기본 입력을 사용한다. 현장 점검에서 기본 입력은
XFM이 아닌 Jetson card 3 쪽이었으므로 XFM을 쓸 때는 `0`을 사용한다.
출력 기본값은 `-1`이다. 장비나 USB 구성을 바꾸면 목록을 다시 확인한다.
기존 노트북 `malbut_stt.smoke --list-devices`는 PvRecorder 번호이므로
ROS STT의 `device_index`에 사용하지 않는다.

```zsh
ros2 launch malbut_bringup robot.launch.py \
  speech_input_device:=0 speech_output_device:=-1
```

| 음성 인자 | 기본값 / 역할 |
| --- | --- |
| `speech` | `true`; `false`면 로봇 구성만 진단 |
| `speech_python_executable` | 위 음성 환경의 `bin/python`; YOLO의 `python_executable`과 별개 |
| `stt_model_path`, `stt_library_path` | 위 환경 변수 또는 기본 캐시 경로 |
| `speech_input_device` | `0`; 현재 로봇의 XFM 마이크, sounddevice 장치 번호 |
| `speech_output_device` | `-1`; TTS와 호출 성공음이 함께 사용하는 시스템 기본 출력 |
| `stt_cpp_threads` | `6` CPU 보조 스레드 |
| `speech_input_has_aec` | `false`; 검증된 에코 제거 입력일 때만 `true` |
| `speech_agent_provider` | `openai`; `mock`으로 바꿔도 TTS는 OpenAI 사용 |
| `speech_preflight_timeout_s` | `120.0`; 점검과 실제 STT 시작 각각의 제한시간. 재시도 대기 포함 |
| `speech_peer_timeout_s` | `30.0`; ROS 연결 대기 제한시간 |

AEC 인자는 에코 제거 기능을 구현하거나 활성화하지 않는다. STT의 나머지 endpoint
설정은 `malbut_stt/config/jetson.yaml`을 사용한다. 다른 STT/TTS가 같은 마이크·출력
장치를 사용 중이면 먼저 정리한다. 통합 Bringup과 별도 음성 launch를 중복 실행하지 않는다.

## XFM 마이크 선점 해제

현장 점검에서 `arecord -D plughw:0,0` 녹음으로 XFM의 실제 음성 입력을 확인했다.
ALSA card 1의 `USB Audio Device`는 이번에 사용할 마이크가 아니다.
제조사 `xf_mic_asr_offline/voice_control`이 `/dev/snd/pcmC0D0c`를 선점하면
입력 번호가 `0`이어도 STT가 마이크를 열 수 없다. 로봇에서 먼저 확인한다:

```bash
sudo fuser -v /dev/snd/pcmC0D0c
pgrep -af '[x]f_mic_asr_offline'
```

소유자가 아래 제조사 실행 파일인 경우 해당 음성 노드만 일회 종료한다:

```bash
pkill -TERM -f '^/home/ubuntu/ros2_ws/install/xf_mic_asr_offline/lib/xf_mic_asr_offline/voice_control([[:space:]]|$)'
sudo fuser -v /dev/snd/pcmC0D0c
```

이 종료는 재부팅 후 자동실행을 해제하지 않는다. 영구 적용은 로봇의 제조사
`startup_check`에서 `xf_mic_asr_offline/voice_control`을 실행하는 항목만
제거하거나 비활성화한다. 해당 파일은 이 저장소에 포함되어 있지 않으므로,
실제 등록부를 확인하고 수정 전 백업을 남긴다. `startup_check`의 다른 기능은 유지한다.
재부팅 후 위 프로세스가 다시 실행되지 않고, STT 시작 전 XFM 캡처 장치가
사용 가능한지 확인해야 자동실행 해제가 완료된 것이다.
Bringup이 제조사 프로세스를 자동으로 종료하지는 않는다.

## 시작 순서와 통과 의미

1. **로봇 제어 준비**: 주행 모드에서는 Manager의 Action 서버와 BOOTING 이후 상태,
   지도 작성 모드에서는 AutoSLAM Action 서버가 준비된 뒤 음성 준비를 시작한다.
   준비 확인은 이동 Goal을 보내지 않는다. 단독 음성 실행에는 이 단계를 요구하지 않는다.
2. **Preflight**: 생성된 ROS 음성 타입, Agent 설정, OpenAI SDK와 키 존재,
   출력 장치의 24 kHz mono float32 스트림, STT ABI 3 모델 로딩,
   마이크 16 kHz PCM 512 samples 읽기와 20 ms VAD 입력을 점검한다.
   출력에는 100 ms 무음만 쓰며, 입력을 전사·저장·전송하지 않는다.
3. **Agent와 TTS**: 점검 프로세스가 성공 종료한 뒤 시작한다. Agent는 대화 런타임과
   DB 세션 초기화가 끝난 뒤 음성 입력 Topic·Service를 공개한다.
4. **ROS 연결 확인**: 두 Service와 타입이 맞는 Topic의 발행자·구독자가
   발견된 뒤 STT를 시작한다.
5. **마이크 준비 완료**: STT 모델 로딩과 마이크 시작이 성공하면
   `/malbut/speech/status`에 `ready`를 발행한다. 웹은 실행 중인 Bringup의 제어 서버와
   이 준비 상태를 모두 확인해야 준비 완료로 표시한다. 단순 프로세스 생성이나
   Action 서버 발견만으로 완료 처리하지 않는다. 기존 Agent·STT·TTS가 실행 중이면
   웹의 새 Bringup 시작은 중복 실행 오류로 거부한다.

`speech_preflight_passed`, `speech_peers_ready`, `malbut_speech_capture_ready` 순서로
통과 로그를 확인한다.
점검 또는 실제 STT 시작 중 CUDA 메모리 할당 부족 로그와 함께 프로세스가 종료되면,
음성 프로세스만 5초, 10초 기다려 최대 3회 시도한다. 재시도 중에는 Bringup을 유지하고
웹은 마이크 준비를 계속 기다린다. 각 단계의 제한시간은 재시도와 대기를 모두 포함한다.
파일·설정 오류나 CUDA 메모리 부족으로 확인되지 않은 실패는 바로 보고한다.
3회 모두 실패하거나 제한시간을 넘으면 통합 Bringup 전체를 실패 코드로 종료한다.
마이크 준비 이후의 음성 노드 종료는 재시도하지 않는다. Ctrl+C 또는 웹의 Bringup
종료는 진행 중인 시도와 대기를 취소하고 함께 실행한 로봇·음성 구성을 정리한다.
점검에서 사용한 모델·마이크·출력 스트림은 반환 전에 해제하고,
같은 Python 실행 파일·모델·장치 설정으로 실제 노드를 시작한다.
실패 출력의 `phase`로 설정·ROS 타입·TTS 출력·STT 모델/마이크 중 실패 단계를 확인한다.
예외 원문, API 키, 마이크 샘플은 로그에 출력하지 않는다.

Preflight는 **유료 API 요청을 보내지 않는다**. 키의 유효성·API 접근 권한·네트워크·
실제 음성 합성은 검증하지 않는다. 모델 로딩 시 GPU를 요청하지만 CUDA에서 실제
추론했음을 확인하지 않는다. 성공 출력에도 `cuda_execution_verified`,
`api_request_verified`, `transcription_verified`를 `false`로 남긴다.
ROS 연결 확인은 초기화를 마친 Agent의 endpoint 발견이며 LLM 응답 성공의 증거는 아니다.
대화 worker 초기화가 실패하면 Agent는 종료 코드 2로 끝나고 launch가
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
  input_device:=0 output_device:=-1 preflight_only:=true
```

이 진단은 성공 시 종료한다. 음성만 계속 시험하려면 같은 명령의 `preflight_only`를
`false`로 바꾼다. 정상 로봇 운용에는 위 `robot.launch.py`를 사용한다.

현재 로봇의 XFM으로 STT·Agent·TTS만 실행하는 명령은 다음과 같다.
위 선점 해제를 마치고 ROS 및 빌드된 workspace 환경을 불러온 터미널에서 실행한다:

```bash
ros2 launch malbut_bringup speech.launch.py \
  python_executable:=/home/ubuntu/.cache/malbut_speech/runtime/bin/python \
  stt_model_path:=/home/ubuntu/.cache/malbut_speech/models/ggml-small.bin \
  stt_library_path:=/home/ubuntu/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so \
  agent_provider:=openai control_server:=none input_device:=0
```

실행 중 `ros2 param get /malbut_stt device_index`가 `0`인지 확인한다.
기본값 변경은 재빌드 후 새 launch에 적용되며, 이미 실행 중인 STT의 parameter를
자동으로 바꾸지는 않는다.

## 로봇에서 남겨야 할 시험 결과

- CUDA 장치 선택 로그와 고정 PCM의 실제 전사·지연·메모리 사용량.
- 마이크 호출어 → 최종 전사 1회 → Agent 응답 → 스피커 재생 → `finished`.
- TTS 완료 후 대화 종료, 실패 후 재시도, Ctrl+C 후 마이크·모델·스피커 해제.
- YOLO와 함께 실행할 때 공유 메모리, 인식 지연, 첫 음성 지연, 중간 끊김.

위 실제 시험을 기록하기 전에는 통합 launch/build/preflight 연결과 로컬 자동 검증까지만
완료한 상태다. Manager 명령 실행과 로봇 이동은 별도 검증 범위다.
