# Malbut Patrol

저장된 지도에서 Nav2 목적지를 순서대로 방문하고 정해진 간격으로
순찰을 반복하는 ROS 2 패키지입니다.

## 책임 범위

`malbut_patrol`이 담당하는 기능:

- 순찰 지점의 순서와 도착 방향
- 지점별 대기 시간
- 순찰 반복 주기
- 실패 시 재시도, 건너뛰기 또는 중단
- 시작, 일시정지, 재개, 중지와 상태 보고

이 패키지는 `/cmd_vel`을 발행하거나 SLAM, AMCL, Nav2
planner/controller 설정을 변경하지 않습니다. 경로 계획, 장애물 회피와
실제 이동은 기존 Nav2의 `navigate_to_pose` 액션에 위임합니다.

## 사전 조건

순찰 실행 전에 다음 항목이 준비되어 있어야 합니다.

- 저장된 지도와 해당 지도에 맞는 순찰 좌표
- `map` 좌표계에서 동작하는 위치 추정
- 활성화된 Nav2 `navigate_to_pose` 액션
- 시뮬레이션 또는 실차의 정상적인 Nav2 단일 목적지 이동

## 빌드

```bash
cd ~/ros2_ws
source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-select malbut_patrol
source ~/ros2_ws/install/local_setup.bash
```

## 실행

시뮬레이션에서는 Nav2와 위치 추정을 먼저 실행한 뒤 다음을 실행합니다.

```bash
ros2 launch malbut_patrol patrol.launch.py use_sim_time:=true
```

실차에서는 시스템 시계를 사용합니다.

```bash
ros2 launch malbut_patrol patrol.launch.py use_sim_time:=false
```

기본값은 안전을 위해 자동 출발하지 않습니다. 시작:

```bash
ros2 service call /patrol/start std_srvs/srv/Trigger "{}"
```

제어:

```bash
ros2 service call /patrol/pause std_srvs/srv/Trigger "{}"
ros2 service call /patrol/resume std_srvs/srv/Trigger "{}"
ros2 service call /patrol/stop std_srvs/srv/Trigger "{}"
```

주행 중 일시정지나 중지를 요청하면 상태가 먼저 `pausing` 또는
`stopping`이 됩니다. 기존 Nav2 목표가 실제 종료된 뒤에만 `paused`
또는 `idle`로 바뀌며, 그전에는 재개나 새 순찰을 허용하지 않습니다.
RViz나 다른 노드가 목표를 외부에서 취소한 경우에는 자동 재출발하지
않고 `aborted` 상태가 됩니다.

상태 확인:

```bash
ros2 topic echo /patrol/status
```

노드 실행과 동시에 순찰을 시작하고 예약 반복을 활성화하려면:

```bash
ros2 launch malbut_patrol patrol.launch.py \
  use_sim_time:=true autostart:=true
```

## 경로 설정

기본 경로는 Small House 전용 지도와 짝을 이루는
`config/routes/small_house_patrol.yaml`입니다.

```yaml
schema_version: 1

route:
  name: household_patrol
  map_id: saved_map_name
  frame_id: map
  cycles_per_run: 1
  defaults:
    dwell_seconds: 3.0
    max_retries: 1
    retry_backoff_seconds: 2.0
    on_failure: skip
  waypoints:
    - name: living_room
      pose: {x: 1.0, y: 2.0, yaw: 0.0}

schedule:
  mode: interval
  interval_seconds: 300.0
```

- `yaw` 단위는 라디안입니다.
- `cycles_per_run`은 예약 한 번에 경로 전체를 도는 횟수입니다.
- `manual` 모드는 한 번 실행하고 종료합니다.
- `interval` 모드는 한 번 실행을 마친 시점부터
  `interval_seconds`만큼 기다린 뒤 다음 실행을 시작합니다.
- `max_retries`는 첫 시도 이후 추가 재시도 횟수입니다.
- `on_failure`는 `skip` 또는 `abort`입니다.
- 일시정지한 대기 단계는 재개할 때 남아 있던 시간부터 계속됩니다.

`use_sim_time:=true`이면 지점 대기, 실패 재시도 대기와 순찰 반복
간격은 Gazebo 시뮬레이션 시간을 사용합니다. Gazebo를 일시정지하면
이 시간도 함께 멈춥니다. Nav2 서버 연결 제한 시간과 종료 제한 시간은
시뮬레이션 시간과 별개의 단조 증가 시계를 사용합니다.

다른 경로 파일을 사용할 수 있습니다.

```bash
ros2 launch malbut_patrol patrol.launch.py \
  route_file:=/absolute/path/to/route.yaml
```

지도와 경로 좌표는 한 세트입니다. 새로 지도를 만들면 RViz에서 안전한
목표를 하나씩 검증한 뒤 별도의 경로 YAML을 만들어야 합니다.

## 시뮬레이션과 실차

동일하게 사용하는 항목:

- `malbut_patrol` 코드
- Nav2 `navigate_to_pose` 액션
- 순찰 상태와 실패 처리

환경마다 교체하는 항목:

- 지도와 경로 YAML
- `use_sim_time`
- Nav2 및 로봇 bringup
