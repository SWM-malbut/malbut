# Malbut TTS 텍스트 수신

Agent가 보낸 응답 텍스트를 받아 수신 로그를 남긴다. 이번 실행 모드에는
음성 합성·스피커 재생·외부 API 호출이 없으며, 수신을 재생 완료로 해석하지
않는다.

입력은 `/malbut/speech/response` Topic의 `malbut_interfaces/msg/SpeechRequest`이다.
`text`에는 Agent가 사용자에게 말할 원문 텍스트를 담는다. QoS는
`RELIABLE`, `VOLATILE`, `KEEP_LAST`, depth `10`으로 맞춘다.
필드 정의는 [SpeechRequest.msg](../malbut_interfaces/msg/SpeechRequest.msg)를
기준으로 하며, 기능 명세는 [TTS 명세](docs/tts_agent.md)에 둔다.

기존 `std_msgs/msg/String` 송·수신기와는 메시지 타입이 다르다. 전환 시
인터페이스·Agent·TTS를 함께 다시 빌드하고, 새 작업 공간 환경을 적용해 실행한다.

## 빌드와 수신 확인

Ubuntu ROS 2 Humble 환경의 저장소 루트에서 실행한다.

```bash
source /opt/ros/humble/setup.bash
colcon build --packages-select malbut_interfaces malbut_tts
source install/setup.bash
export ROS_DOMAIN_ID=192
export ROS_LOCALHOST_ONLY=1
ros2 run malbut_tts tts_receiver
```

다른 터미널에서도 같은 ROS 환경과 테스트 도메인을 설정한 뒤 발행한다.

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=192
export ROS_LOCALHOST_ONLY=1
ros2 topic pub --once /malbut/speech/response \
  malbut_interfaces/msg/SpeechRequest '{text: "안녕하세요. 말벗이에요."}'
```

유효한 텍스트는 `tts_text_received` 이벤트의 `text`로 로그에 표시한다.
개행·따옴표는 JSON 규칙에 따라 표시하고 원문 값은 유지한다. 공백뿐인
텍스트는 경고를 남기고 무시한다. 메시지에 발화 ID가 없으므로 같은 문장이
다시 들어와도 별도 수신으로 기록한다. 수신기를 먼저 실행해야 하며,
수신기가 꺼져 있을 때의 응답을 나중에 재생하는 기능은 없다.

## 시험

```bash
cd malbut_tts
PYTHONPATH=. python3 -m pytest -q test
```

단위 시험은 ROS 설치 없이 원문·빈 입력·반복 수신·종료 처리를 확인한다.
실제 ROS 메시지 수신 시험과 음성 합성·재생 시험의 결과는 별도로 기록한다.
