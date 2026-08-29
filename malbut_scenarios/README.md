# Malbut 자율주행 시연

`malbut_scenarios`는 기존 Gazebo, Nav2, 웹 지도, 자율 순회, 사람 인식과
사람 추적 기능을 하나의 시연 흐름으로 조정합니다. Nav2와 각 기능의
구현을 복사하지 않고 ROS Action과 Service로 연결합니다.

## 빌드와 실행

```bash
cd ~/ros2_ws
colcon build --symlink-install --packages-up-to malbut_scenarios
source install/local_setup.bash
ros2 launch malbut_scenarios autonomous_driving.launch.py
```

실행하면 Small House, 로봇, Nav2/RViz, 기존 웹 지도, 사람 인식과 시나리오
조정 노드가 함께 시작됩니다. 사람 모델은 자동으로 등장하지 않습니다.
웹 지도의 `사람 모델 등장` 버튼을 누르면 현관 밖에서 들어와 각 방을
포함한 고정 경로를 반복 이동하고, 같은 버튼을 다시 누르면 월드에서
제거됩니다. 웹 지도는 `http://127.0.0.1:8765`에서 확인합니다.

기존 홈캠 웹과 연결된 장치에서는 이미 발급된 장치 설정을 그대로 넘겨
기존 `cloud_robot_sync`도 같은 실행에 포함할 수 있습니다. 웹·백엔드 코드는
복사하거나 수정하지 않습니다.

```bash
ros2 launch malbut_scenarios autonomous_driving.launch.py \
  cloud_sync:=true \
  cloud_backend_url:=https://YOUR_BACKEND \
  cloud_device_id:=REGISTERED_DEVICE_ID \
  cloud_token_file:=~/.config/homecam/device.token \
  map_store:=~/.local/share/malbut/devices/REGISTERED_DEVICE_ID/maps
```

## 시연 명령

사람 모델은 웹 버튼 또는 다음 서비스로 등장·퇴장시킵니다. 생성과 삭제
후에는 Gazebo 월드의 실제 상태를 확인하므로 같은 이름의 모델이 겹쳐
남지 않습니다.

```bash
ros2 service call /scenario/toggle_person std_srvs/srv/Trigger '{}'
```

미방문 구역을 우선하는 자율 순찰을 시작합니다.

```bash
ros2 service call /scenario/start_patrol std_srvs/srv/Trigger '{}'
```

웹 지도에서 위치를 선택하고 이동을 시작하면 자율 순찰을 일시 정지하고,
선택 위치로 이동한 뒤 `config/room_routes.yaml`의 해당 구역을 순찰합니다.
구역 순찰이 끝나면 자율 순찰로 돌아갑니다.

사람 추적은 다음 한 명령으로 시작하고 중단합니다.

```bash
ros2 service call /scenario/start_person_tracking std_srvs/srv/Trigger '{}'
ros2 service call /scenario/stop_person_tracking std_srvs/srv/Trigger '{}'
```

수동 입력은 `/cmd_vel_manual`을 사용합니다. 첫 유효 입력이 들어오면 실행
중인 자율 이동을 취소하고 수동 제어권을 획득합니다. 새 자율 명령을
내리기 전까지 수동 모드가 유지됩니다.

```bash
ros2 run malbut_gazebo teleop_key_control --ros-args \
  -p cmd_vel_topic:=/cmd_vel_manual
```

현재 시연 상태는 다음으로 확인합니다.

```bash
ros2 topic echo /scenario/status
```

## SWM25-131 텍스트 요청과 승인 기록

SWM25-131 전용 server는 SWM25-130 active map에서 `거실` 이름만 해석하고,
LLM 행동 제안과 `네/아니요/취소` 확인 결과를 SQLite에 기록합니다. 이
진입점의 기본 모드는 `NamedNavigationFacade`, Robot Web, ROS ActionClient
또는 Nav2를 호출하지 않습니다. 이 기본 모드의 `approved`도 사용자 동의
기록일 뿐 이동 권한이 아닙니다.

private fixture와 Git에서 제외되는 `.env.local`을 준비한 뒤 먼저 구성을
검사합니다.

```bash
ros2 run malbut_scenarios malbut_text_agent_server -- \
  --env-file <private-path>/.env.local --check
```

필수 설정은 `MALBUT_AGENT_AUTH_TOKEN`, durable `MALBUT_AGENT_DB`,
`MALBUT_NAMED_NAVIGATION_MAP_STORE`, `MALBUT_ROBOT_DEVICE_ID`이며 Tool mode는
`proposal`이어야 합니다. `--check` 출력의
`simulation=true, physical_authorized=false, nav2=off`는 의도된 경계입니다.
전체 curl 예제와 RAI sidecar 설정은
`malbut_agent_server/docs/jira/SWM25-131_TEXT_CONFIRMATION_RAI.md`에 있습니다.

## SWM25-132 승인된 Gazebo 이동

기본 실행은 계속 `nav2=off`입니다. 승인 결과를 실제 Small House Nav2와
연결하려면 Gazebo testbed와 Robot Web을 먼저 실행한 뒤
`--execute-approved-simulation`을 명시해야 합니다. 이 모드는 승인된
`navigate(location="거실")`마다 별도 durable RobotAction을 만들고, 승인 후
새 Robot Web readiness와 현재 Safety·지도 binding을 다시 검사한 다음에만
SWM25-130 façade를 한 번 호출합니다.

Agent HTTP server와 Robot Web은 같은 포트를 사용할 수 없습니다. 아래처럼
Robot Web은 testbed 기본값 `8765`, Agent는 별도 loopback 포트 `8877`을
사용합니다.

```bash
export MALBUT_AGENT_PORT=8877
export MALBUT_ROBOT_WEB_URL=http://127.0.0.1:8765

ros2 run malbut_scenarios malbut_text_agent_server -- \
  --env-file <private-path>/.env.local \
  --execute-approved-simulation
```

`--robot-web-url http://127.0.0.1:8765`로 환경변수를 덮어쓸 수도 있습니다.
Robot Web URL은 literal loopback HTTP 주소만 허용합니다. 실행 중 사용하는
battery `100%`는 센서 측정값이 아니라 SWM25-132 Gazebo 전용 가정이며,
`simulation=true`, `physical_authorized=false`는 항상 유지됩니다.

확인만 할 때는 다음 명령을 사용합니다. execution flag를 함께 준 `--check`는
action schema, repository, worker와 executor 구성을 실제로 생성·검증한 뒤 즉시
닫습니다. 이 과정에서도 Robot Web 요청, façade start 또는 Nav2 goal은 보내지
않습니다.

```bash
ros2 run malbut_scenarios malbut_text_agent_server -- \
  --env-file <private-path>/.env.local \
  --execute-approved-simulation --check
```

재시작 시 dispatch intent 또는 started 상태였던 결과 불명 action은
`UNKNOWN`으로 봉인하며 자동 재전송하지 않습니다. 종료할 때는 dispatcher를
닫고 HTTP handler와 action worker를 join한 후 SQLite store를 닫습니다.
이 단계는 단일 named destination 실행만 다루며 로밍, Homecam, 실제 로봇
권한, 음성 입력·출력은 포함하지 않습니다.

## 기존 기능과의 연결

- 웹 지도: `malbut_gazebo/robot_web_server.py`
- 홈캠 웹 연결: `malbut_gazebo/cloud_robot_sync.py` (선택 실행)
- 금지 구역: 기존 semantic Zone GeoJSON과 Nav2 KeepoutFilter
- 자율 순찰: `malbut_roaming`
- 사람 인식: `malbut_perception`
- 사람 추적: `malbut_tracking`의 `FollowPerson` Action
- 실제 이동과 충돌 회피: ROS 2 Humble Nav2

시뮬레이션 전용 사람 모델의 생성·삭제 외의 조정 로직은 Gazebo 모델
좌표를 읽지 않습니다. 실제 로봇에서도 같은 Nav2 Action, 센서 토픽,
수동 속도 입력 인터페이스를 사용할 수 있습니다.
