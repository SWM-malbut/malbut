# Malbut TTS

Agent가 보낸 답변 한 건을 하나의 요청으로 관리하고, 로컬 모델에서 생성한
음성을 준비되는 부분부터 재생한다. 앞부분을 재생하는 동안 뒷부분을 생성한다.
명세는 [tts_agent.md](docs/tts_agent.md)에 있다.

## 요청과 재생 규칙

- 한 답변의 여러 문장과 음성 조각은 하나의 `playback_id`를 공유한다.
- 현재 요청을 유지하고 새 요청은 대기열에 넣는다. 다음 요청은 대화 답변
  우선, 같은 종류에서는 수신한 순서대로 선택한다.
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

## 노트북에서 로컬 음성 시험

현재 실제 합성 어댑터는 Apple Silicon용 MLX이다. 선택한 모델은
`mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit`, 목소리는 `Sohee`,
언어는 `Korean`이다. 기존 로컬 모델 디렉터리를 사용하며, 외부 음성 API를
호출하거나 모델을 자동으로 내려받지 않는다. 첫 요청에서 모델을 한 번 로드한다.

ROS 없이 저장소 루트에서 실행한다. `--model-path`에는 이미 준비된 모델의
실제 디렉터리를 전달한다.

```bash
python3 -m venv .runtime/tts-mlx
.runtime/tts-mlx/bin/python -m pip install -r malbut_tts/requirements-mlx-laptop.txt
PYTHONPATH=malbut_tts .runtime/tts-mlx/bin/python -m malbut_tts.smoke \
  --model-path /absolute/path/to/local/model
```

일반 텍스트는 대화 답변, `/notice 순찰이 끝났어요.`는 일반 알림을 추가한다.
재생 중에도 다음 텍스트와 명령을 입력할 수 있다.

```text
첫 번째 답변입니다. 잠시 멈췄다가 이어서 읽어 보겠습니다.
/pause
두 번째 답변입니다.
/notice 순찰이 끝났어요.
/resume
/stop
/quit
```

`/pause`, `/resume`, `/stop`은 현재 재생 ID를 사용한다. 뒤에 출력된 ID를
직접 지정할 수도 있다. `/quit` 또는 Ctrl+C는 현재 재생과 대기 요청을 종료한다.
`--list-devices`로 장치를 확인하고 `--device-index 1`처럼 출력 장치 번호를
지정할 수 있다. 한 문장만 시험하려면 `--text '안녕하세요. 말벗이에요.'`를 쓴다.

## ROS 노드

`tts_node`는 ROS 어댑터, `tts_smoke`는 같은 런타임을 사용하는 터미널 시험기다.
기존 `tts_receiver`는 합성 없이 수신 로그만 남기는 통신 확인용으로 유지한다.

```bash
colcon build --packages-select malbut_interfaces malbut_agent_server malbut_tts
source install/setup.bash
ros2 run malbut_tts tts_node --ros-args -p model_path:=/absolute/model/path
ros2 topic pub --once /malbut/speech/response malbut_interfaces/msg/SpeechRequest \
  '{text: "안녕하세요.", request_type: 0}'
ros2 topic echo /malbut/speech/playback_status
```

위 노드의 실제 합성에는 같은 Python 환경에 ROS와 해당 합성·오디오 의존성이
필요하다. 현재 MLX 어댑터는 Jetson/Linux용이 아니다. Linux에서는 생성된 ROS
인터페이스와 가짜 오디오를 사용한 통합 시험을 실행할 수 있다. Jetson 실제
합성 어댑터와 YOLO·STT 동시 실행 성능은 별도 검증 대상이다.

## 검증

```bash
PYTHONPATH=malbut_tts python3 -m pytest -q malbut_tts/test
```

코어·오디오 단위 시험은 `pytest`, `numpy`를 사용하고 실제 MLX나 스피커가
필요하지 않다. ROS 통합 시험은 ROS와 생성된 `malbut_interfaces`를 빌드해
환경을 적용한 뒤 실행한다. 실제 스피커와 로컬 모델 시험은 위 터미널 명령으로
별도로 확인한다.
