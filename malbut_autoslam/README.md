# 자동 지도 만들기

기존 웹 지도 만들기의 **프론티어 탐색 로직**을 분리한 패키지다.
실시간 SLAM 지도의 알려진 공간과 미확인 공간 사이 경계를 찾아 Nav2로
이동한다. 접근 불가능하거나 관측이 늘지 않는 곳은 건너뛰고, 탐색이 끝나면
[Nav2 Map Saver](https://github.com/ros-navigation/navigation2/blob/humble/nav2_map_server/src/map_saver/map_saver.cpp)로
YAML·PGM을 저장한다.
고정 순회 좌표, Gazebo 위치 정보, 웹 또는 시스템 관리자에 의존하지 않는다.

## 실행

실기기에서는 **아래 서버를 켜고 Goal을 요청하면 필요한 구성도 준비한다.**
Goal을 받으면 ROS 그래프를 확인하고, 드라이버·실시간 SLAM·Nav2 중 없는 구성만
`malbut_bringup/mapping_backend.launch.py`로 실행한다. 이미 실행 중인 구성은
재사용한다. 센서·지도·TF 수신 및 Nav2 활성 상태를 확인한 뒤 탐색한다.
기존 웹 자동 탐색기는 동시에 시작하지 않는다.

```bash
ros2 launch malbut_autoslam autoslam.launch.py
```

이 명령은 대기 중인 Action 서버와 지도 저장 서버만 켠다. 실제 로봇이 움직이는
것은 아래 요청 이후다. 완료·취소·실패하면 **이번 요청이 켠 구성만 종료**한다.
시뮬레이션에서는 `use_sim_time:=true`를 지정한다. 이때는 자동 기동이 기본으로
꺼지고 기존 시뮬레이션의 SLAM·Nav2를 그대로 사용한다.
환경이 다르면 `map_topic`, `base_frame`, `navigation_action`, `planning_action`, `map_directory`를
launch 인자로 지정한다. 기본 저장 폴더는 `~/.ros/malbut/maps`다.

이미 별도로 준비한 매핑 구성을 그대로 사용할 때는 다음과 같이 실행한다.

```bash
ros2 launch malbut_autoslam autoslam.launch.py auto_start:=false
```

자동 기동은 실기기의 제조사 드라이버와 `slam_toolbox`/Nav2 구성용이다.
AMCL·저장 지도 서버가 켜져 있으면 매핑으로 임의 전환하지 않고 오류를 반환한다.
기존 주행 Bringup을 먼저 종료해야 한다. 알 수 없는 지도 발행자, 중복 발행자,
일부만 켜진 하드웨어도 임의로 덧붙이지 않는다. 사용자 정의 매핑 시스템은
`auto_start:=false`로 외부에서 준비한다. `scan_topic`과 `odom_topic`은 실제
드라이버 토픽이며, 자동 기동 SLAM/Nav2도 같은 `scan_topic`을 직접 사용한다.
자동 기동 로그는 `~/.ros/malbut/autoslam/mapping-*.log`에 남는다.

관리자 없이 직접 실행:

```bash
ros2 action send_goal /autoslam malbut_interfaces/action/AutoSlam \
  '{map_name: home}' --feedback
```

관리자를 사용하는 경우에도 **같은 서버**에 요청한다. 관리자 코드는 변경하지
않으며 중앙 Manifest의 `autoslam` 항목을 사용한다.

```bash
ros2 launch malbut_system_manager system_manager.launch.py
ros2 action send_goal /malbut/mission/execute \
  malbut_interfaces/action/ExecuteMission \
  "{capability_id: autoslam, arguments_yaml: '{map_name: home}'}" --feedback
```

자동 탐색 없이 조이스틱으로 지도만 작성하려면 이 Action을 요청하지 않아도 된다.

## 명세

| 구분 | 필드 | 의미 |
| --- | --- | --- |
| Goal | `map_name: string` | 파일 이름, 기본 `home`. 경로·확장자 없이 입력 |
| Feedback | `state: string` | `WAITING`, `EXPLORING`, `NAVIGATING`, `SAVING`, `CANCELING` |
| Feedback | `frontier_count: uint32` | 마지막 계획에서 선택 가능한 탐색 경계 수 |
| Feedback·Result | `known_area_m2: float32` | 현재 지도에서 알려진 면적. 전체 집 면적 대비 관측률이 아님 |
| Result | `success: bool`, `message: string` | 완료 여부와 사유 |
| Result | `map_yaml: string` | 저장한 지도 YAML의 절대 경로. 실패·저장 전 취소 시 빈 문자열 |

- 기존 지도 파일은 덮어쓰지 않는다. 다시 작성하려면 다른 `map_name`을 사용한다.
- `home` 요청은 `map_directory/home.yaml`과 `home.pgm`을 생성한다.
  저장 직후 매핑 구성을 종료하기 전에 신선한 `map` → 로봇 TF를 읽어
  `home.pose.yaml`에 초기 위치·방향도 원자적으로 저장한다. 지도 YAML·이미지의
  SHA256이 일치할 때만 주행 Bringup이 이 값을 초기 위치로 복원한다.
  TF에는 공분산이 없으므로 0.5 m·15도 표준편차의 초기 추정 불확실성을 사용하며,
  SLAM 측정 정확도를 뜻하지 않는다. 위치 저장에 실패해도 지도는 보존하고
  성공 Result의 `message`에 경고한다. 이 경우 RViz의 `2D Pose Estimate`가 필요하다.
  저장 후 로봇을 다른 곳으로 옮겼다면 저장 위치를 믿지 말고 실제 위치를 지정한다.
- 종료 사유는 `message`로 구분한다: 남은 접근 가능 경계 없음, 남은 경계 접근 불가/
  관측 증가 없음, 무진행 시간 초과, 전체 탐색 예산 도달. 이 경우 확보한 지도를
  저장하고 정상 종료한다. 성공은 사용 가능한 지도 저장까지 마쳤다는 뜻이며,
  모든 방에 접근했거나 미확인 공간이 전혀 없다는 뜻은 아니다.
- 목표는 로봇과 알려진 빈 공간으로 연결된 탐색 경계에서만 고른다. 경계에서
  접근점을 찾을 때도 벽·미확인 공간을 넘어 확장하지 않는다. 실패한 위치 주변은
  제외하되 같은 경계의 다른 접근점은 사용할 수 있다. 지도에서 연결되지 않은
  공간은 방문하지 않는다. 이동 전 Nav2 `ComputePathToPose`로 실제 costmap 기반
  경로를 확인하고, 경로가 미확인 공간·벽을 통과하거나 목표 반대편에서 끝나면 제외한다.
  접근점 여유는 장애물 셀의 중심이 아닌 셀 영역과 지도 바깥까지 고려한다.
  계획 응답 후 최신 SLAM 지도에서 목표와 실제 경로 끝점의 여유를 다시 확인한다.
  이 30cm 접근점 여유를 경로 전체에 강제해 좁은 통로나 벽 근처 출발을 막지는 않는다.
  이동 중 경로 재계획·충돌 회피는 Nav2가 담당한다.
  로봇 위치가 지도 밖이거나 빈 공간이 아니면 탐색 완료로 오인하지 않고 실패한다.
- 긴 경계의 전체 중심 대신 가까운 안전 접근점과 인근 미확인 공간을 기준으로
  위치·방향을 고른다. 먼 후보가 없으면 45cm 이내의 가까운 후보도 확인한다.
- 도착 후 새 SLAM 지도를 기다리고, 알려진 지도 셀 증가량으로 진전을 확인한다.
  실패하거나 새 공간을 관측하지 못한 지점은 제외한다. 전체 제외 목록 재시도는
  다른 후보가 소진된 뒤 한 번만 하며, 오래된 실패를 삭제해 무한 순회하지 않는다.
- 이동 Goal 수락 후 **Nav2의 유효한 이동 명령이 지속되는데도** 5초 동안
  5cm 이동이 없으면 진행 불가로 추정하고 Nav2 취소 완료를 기다린다.
  제자리 회전 명령은 별도로 0.15rad(약 9도) 회전 여부를 본다. 계획 대기·정지
  명령·명령 미수신은 이 장애물 추정에서 제외하며, 기존 전체 이동 제한은 유지한다.
  얇은 물체를 밟는 IMU 충격만을 이유로 중단하지 않는다.
  실패 목표와 사전 경로의
  현재 위치 앞쪽 접근 구간은 **이번 요청이 끝날 때까지** 제외한다. 다른 후보의
  사전 경로도 이 구간을 통과하면 보내지 않으며, 새 요청에서는 기록을 초기화한다.
  이 기록은 저장 지도나 costmap을 바꾸지 않는다. Nav2 자체 재계획의 강제 금지구역은
  아니며, TF 기반 추정이므로 충돌 센서나 비상 정지를 대체하지 않는다.
- 같은 서버의 중복 요청은 거부한다. Manifest는 `FOREGROUND/NORMAL/[BASE]`다.
- 취소·선점·Ctrl+C 시 하위 Nav2 Goal을 취소하고 **실제 종료까지 기다린다**.
  외부 Nav2의 응답이 불명확하면 이동이 끝났다고 간주해 새 작업을 받지 않는다.
  직접 기동한 Nav2가 취소에 응답하지 않으면 준비 제한 시간 후 소유한 프로세스
  그룹만 종료한다. 종료는 SIGINT → SIGTERM → SIGKILL 순으로 제한 시간을 두며,
  다른 터미널의 프로세스에는 신호를 보내지 않는다.
- 저장 요청은 취소 불가능한 Service이므로 이미 저장 중이면 응답 후 취소를
  완료한다. 이때 저장된 파일은 남고 `map_yaml`로 반환한다.
  서버 자체를 Ctrl+C로 종료하면 지도 저장 서버도 함께 종료될 수 있으므로,
  `ready_timeout_s` 안에 응답이 없거나 응답 전송 오류가 나면 저장 성공을 보고하지
  않고 요청을 결과 미확인으로 종료·정리한다. 새 요청은 거부한다. 지연된 저장이
  파일을 쓸 수 있으므로 파일을 확인한 뒤 서버를 재시작한다.
- 지도 작성과 저장이 끝나도 외부에서 실행한 SLAM·Nav2는 이 Action이 끄지 않는다.
  외부 매핑 구성이 남았다면 종료하고 반환받은 `map_yaml`로 주행 Bringup을 실행한다.
- 저장 대상은 주행용 2D 지도이며 웹 방 라벨 생성·클라우드 업로드·pose graph 저장은
  포함하지 않는다. 기존 웹 지도 작성 기능은 별도로 유지한다.

## 파라미터와 내부 구성

- `frontier.py`: 기존 탐색 경계 추출·정렬과 지도 통계. 기존 Gazebo 웹 코드도
  이 공통 구현을 재사용한다.
- `autoslam_node.py`: Action 수명주기, Nav2 호출·취소, 지도 저장.
- `runtime.py`: 중복·저장 지도 충돌 검사, 요청 소유 프로세스 기동·정리.
- `launch/autoslam.launch.py`: Action 서버와 공식 Nav2 지도 저장 서버 실행.

탐색 주기는 `exploration_period_s=1.0`, 경계가 없을 때 재확인 시간은
`completion_delay_s=12.0`이다. `robot_clearance_m=0.30`은 접근점 여유,
`minimum_goal_distance_m=0.45`는 먼 후보 우선 기준(근거리 후보의 절대 금지 아님),
`minimum_frontier_cells=8`은 작은 경계 잡음을 제외하는 기준이다.
통신 준비·지도/TF 신선도·개별 이동 제한은 각각 `ready_timeout_s`,
`map_timeout_s`, `tf_timeout_s`, `navigation_timeout_s`로 조정한다.
짧은 정체 판단의 ROS parameter는 `progress_timeout_s=5.0`,
`progress_distance_m=0.05`, `progress_angle_rad=0.15`다. Goal 수락 대기 시간은
정체 시간에 포함하지 않는다. `cmd_vel_topic=/cmd_vel`은 제조사 Nav2 smoother와
behavior 서버의 출력이며, 별도 조이스틱 입력 `/controller/cmd_vel`과 다르다.
기존 드라이버의 `progress_odom_topic=/odom_rf2o`가 신선하고 유효하면 그 이동량을
우선 사용한다. 수신하지 못하면 기존 SLAM TF로 대체하며, 이때 바퀴 미끄러짐을
독립적으로 구분할 수 있다고 보장하지 않는다. 입력 전환 시 기준 위치를 초기화하고
로그에 사용 소스를 표시한다. 토픽 이름은 launch 인자로 변경할 수 있다.
RF2O를 새로 기동하거나 LiDAR 정합 알고리즘을 추가하지 않는다.
`ready_timeout_s`는 launch 인자로도 설정할 수 있다. 부모 launch의 종료 유예도
같은 값에 프로세스 정리 시간을 더해 적용하므로, Ctrl+C가 Action 서버를 먼저
강제 종료해 자동 기동한 매핑 프로세스를 남기지 않도록 한다.
`navigation_timeout_s=90.0` 동안 새로 알려진 공간이 늘지 않으면 확보한 지도를
저장하고 종료한다. 전체 탐색 예산은 `max_exploration_time_s=1200.0`(20분)이며,
계속 생기는 센서 잡음으로 종료가 미뤄지는 것을 제한한다. 큰 공간은 아래처럼
launch 인자로 조절한다. 시작 준비·안전 정지·저장 시간은 탐색 예산과 별도다.
대기 중에는 프론티어 계산이나 이동을 하지 않는다.

```bash
ros2 launch malbut_autoslam autoslam.launch.py max_exploration_time_s:=1800.0
```

설계 참고: [Nav2 Humble 경로 계획 Action](https://github.com/ros-navigation/navigation2/blob/humble/nav2_msgs/action/ComputePathToPose.action),
[explore_lite의 프론티어·무진행 처리](https://github.com/robo-friends/m-explore-ros2/blob/main/explore/src/explore.cpp).
해당 구현을 복사하지 않고 기존 Malbut Action의 실행·종료 규칙에 맞춰 적용했다.

단위·모의 ROS 테스트는 실제 로봇의 탐색 품질 검증이 아니다. 실제 운행 전에는
주행 설정·TF·LiDAR와 정지 수단을 확인하고 안전한 공간에서 직접 확인해야 한다.
