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

## SWM25-133 텍스트부터 Gazebo terminal까지 한 명령으로 검증

SWM25-133 runner는 SWM25-131의 인증된 텍스트 요청·승인과 SWM25-132의
RobotAction·Nav2 실행 경로를 clean installed overlay에서 한 번에 검증합니다.
새 실행 경로를 만들지 않고 public Agent HTTP API, SQLite read-only observer,
Robot Web counting proxy와 read-only Nav2 status observer의 증거를 교차
확인합니다.

먼저 설치 산출물과 clean source의 결속을 검사합니다. 아래 placeholder는
검증할 환경에 맞게 바꿉니다.

```bash
cd <source-worktree>
source_tree="$(pwd -P)"
source_commit="$(git rev-parse HEAD)"
source /opt/ros/humble/setup.bash
source <isolated-overlay>/install/setup.bash

ros2 run malbut_scenarios run_text_gazebo_acceptance -- \
  --check \
  --source-commit "$source_commit" \
  --source-tree "$source_tree"
```

`--check`는 Gazebo, Agent server, HTTP listener, SQLite runtime과 Nav2 goal을
시작하지 않으며 `nav2_start_count=0`, `simulation=true`,
`physical_authorized=false`를 출력합니다. 이 검사는 source tree가 정확한 Git
toplevel·HEAD이고 untracked 파일을 포함해 clean인지 확인한다. 또한 runner가
선택한 tracked source와 installed overlay 파일이 symlink가 아닌 regular
file이며 byte-for-byte 같은지 검증한 뒤 commit과 Git tree에 결속된
`source_tree_digest`를 출력합니다.

실제 headless Small House 인수 시험은 명시적인 simulation 실행 flag와 새
owner-private evidence 파일을 함께 줘야 합니다.

```bash
evidence_root=<absolute-private-evidence-directory>
ros_domain_id=85  # 현재 사용하지 않는 1~100 범위 값으로 변경
install -d -m 0700 "$evidence_root"

ros2 run malbut_scenarios run_text_gazebo_acceptance -- \
  --run \
  --execute-approved-simulation \
  --source-commit "$source_commit" \
  --source-tree "$source_tree" \
  --ros-domain-id "$ros_domain_id" \
  --evidence "$evidence_root/run-1.json"
```

화면을 보려면 `--gui`를 추가합니다. run mode의 `--ros-domain-id`는 선택값이
아니며, 사용하지 않는 1~100 범위 값을 명시해야 합니다. runner는 mock
Provider를 사용해 인증된
`거실로 가줘 -> navigate(거실) -> 네` 입력을 결정적으로 수행합니다. 승인 전
Nav2 goal 0개와 승인 후 proposal·confirmation·RobotAction·dispatch intent·
Robot Web verified preview target·start·actual Nav2 goal·known terminal 각각
1개를 요구합니다. 승인 replay 뒤 0.25초 간격 8개 sample, 총 2초 동안 모든
effect count가 그대로인지 확인합니다. 종료는 주 실행의 성공·실패와 무관하게
각 owner에 best-effort로 수행하고, proxy의 active upstream/downstream socket도
bounded deadline 안에서 닫습니다. 성공하려면 최종 소유 process·ROS node·
socket이 모두 0개여야 합니다.

`--run`만으로는 실행되지 않으며 `--execute-approved-simulation`이 반드시
필요합니다. timeout·불명 결과를 성공으로 보정하거나 재전송하지 않습니다.
SWM25-133 완료 당시 성공 evidence v2에는 원문, token, private ID, 좌표,
fixture·DB·host 경로를
넣지 않고 `source_tree_digest`, 다른 digest, 제한된 상태, count, duration과
cleanup 결과만 기록합니다. evidence parent는 `0700`, 신규 파일은 `0600`이어야
하며 기존 파일을 덮어쓰지 않습니다. 기존 run-2는 source provenance 봉인 전
rehearsal이며 최종 인수 evidence가 아닙니다. 최종 provenance-sealed run은 이
변경을 Git에 commit하고 동일 commit으로 overlay를 다시 build한 뒤 수행합니다.
현재 SWM25-135 경로는 bounded profile과 target binding이 추가된 evidence v3을
사용하며 과거 v2 파일을 현재 성공 증거로 변환하지 않습니다.
SWM25-133 완료 당시 `malbut_scenarios/test` source suite는 298개가 통과했지만,
이것만으로 최종 clean Gazebo run이 완료됐다고 판정하지 않습니다.
상세 구조와 현재 검증 상태는
`malbut_agent_server/docs/jira/SWM25-133_GAZEBO_FULL_FLOW.md`에 있습니다.

## SWM25-134 Gazebo 반복 campaign

SWM25-134 campaign은 위 SWM25-133 runner를 case 단위의 installed child로
재사용합니다. Agent, confirmation, RobotAction, Robot Web 또는 Nav2 실행 코드를
다시 만들지 않고, case의 순서·기대 결과·격리·전체 합격 여부와 aggregate
evidence만 관리합니다. SWM25-134 완료 당시에는 legacy `happy_path` 한 종류만
허용했습니다. SWM25-135에서 같은 경계를 거실·주방·침실의 세 server-owned 정상
profile로 확장했습니다. 한 campaign에는 최대 32개의 허용 profile을 지정할 수
있습니다. 장애 profile과 그에 맞는 typed child evidence는 SWM25-136~139에서
기존 installed 경계에 확장합니다.

Agent/Gazebo/Nav2 제품 실행 효과 없이 source/install 결속과 case plan을 먼저
확인합니다. 이 검사는 provenance용 bounded child process 하나만 실행합니다.

```bash
ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --check \
  --case-profile happy_path \
  --source-commit "$source_commit" \
  --source-tree "$source_tree"
```

실제 simulation case는 기본 OFF입니다. owner-only `0700` evidence directory와
비어 있는 ROS domain을 준비하고 실행 승인을 명시해야 합니다.

```bash
campaign_evidence_root=<absolute-private-evidence-directory>
ros_domain_id=86  # 현재 사용하지 않는 1~100 값
install -d -m 0700 "$campaign_evidence_root"

ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --run \
  --execute-approved-simulation \
  --case-profile happy_path \
  --source-commit "$source_commit" \
  --source-tree "$source_tree" \
  --ros-domain-id "$ros_domain_id" \
  --evidence "$campaign_evidence_root/swm25-134-smoke.json"
```

case마다 별도 private child evidence를 만들고 SWM25-133의 독립 runtime·SQLite를
사용합니다. child failure, timeout, evidence 누락·불일치 또는 cleanup 실패가
하나라도 있으면 전체 campaign은 합격하지 않습니다. cleanup이 증명되지 않은
경우에는 다음 case를 실행하지 않습니다. aggregate에는 원문·token·좌표·private
ID·host path 대신 case 순서, 제한된 결과, digest, duration과 cleanup 집계만
기록하며 기존 파일을 덮어쓰지 않습니다. `simulation=true`,
`physical_authorized=false`는 항상 유지됩니다.

실제 Gazebo 1/1 최종 smoke는 이 변경을 Git에 commit한 뒤 같은 commit으로 clean
overlay를 다시 build해야 source/install attestation을 통과할 수 있습니다. 자세한
계약과 현재 검증 상태는
`malbut_agent_server/docs/jira/SWM25-134_GAZEBO_CAMPAIGN.md`에 있습니다.

## SWM25-135 거실·주방·침실 정상 campaign

SWM25-135는 하나의 3-stop 경로를 만드는 기능이 아닙니다. 다음 세 profile을 각각
새 SWM25-133 runtime·SQLite·Gazebo에서 한 번씩 실행하고, 세 case가 모두 실제
Nav2 `SUCCEEDED`와 clean shutdown을 증명해야 전체를 합격시킵니다.

```text
happy_living_room -> 거실
happy_kitchen     -> 주방
happy_bedroom     -> 침실
```

profile은 요청 원문이나 좌표를 받는 자유 입력이 아니라 위와 같이 코드에 고정된
allowlist입니다. 각 profile은 server-owned 요청 문장, semantic location과 현재
map target을 하나로 결속합니다. legacy `happy_path`는 기존 거실 요청과 CLI 기본값
호환을 위해 유지합니다.

실행 효과 없이 세 case plan과 installed provenance를 검사합니다.

```bash
ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --check \
  --case-profile happy_living_room \
  --case-profile happy_kitchen \
  --case-profile happy_bedroom \
  --source-commit "$source_commit" \
  --source-tree "$source_tree"
```

실제 headless campaign은 owner-private 신규 evidence 경로와 비어 있는 ROS domain,
`--run --execute-approved-simulation`을 함께 사용합니다. 각 child evidence v3에는
bounded profile과 target binding digest가 추가되며, campaign evidence v2는 서로
다른 semantic location이 같은 target binding을 재사용하면 fail-closed합니다.
요청·승인 원문, 좌표, private ID와 host path는 evidence에 기록하지 않습니다.

```bash
ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --run \
  --execute-approved-simulation \
  --case-profile happy_living_room \
  --case-profile happy_kitchen \
  --case-profile happy_bedroom \
  --source-commit "$source_commit" \
  --source-tree "$source_tree" \
  --ros-domain-id "$ros_domain_id" \
  --evidence "$campaign_evidence_root/swm25-135-three-spaces.json"
```

상세 범위와 완료 증거는
`malbut_agent_server/docs/jira/SWM25-135_MULTI_SPACE_GAZEBO_REPEAT.md`에 있습니다.

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
