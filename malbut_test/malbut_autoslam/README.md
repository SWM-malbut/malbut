# 자동 지도 만들기

기존 웹 지도 만들기의 **프론티어 탐색 로직**을 분리한 패키지다.
실시간 SLAM 지도의 알려진 공간과 미확인 공간 사이 경계를 찾아 Nav2로
이동한다. 더 탐색할 유효한 경계가 없으면
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
환경이 다르면 `map_topic`, `base_frame`, `navigation_action`, `map_directory`를
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
드라이버 토픽이며, 자동 기동 SLAM/Nav2는 `normalized_scan_topic`을 사용한다.
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
- 탐색 종료는 유효한 경계가 없는 상태가 유지될 때 판단한다. 성공은 탐색 절차와
  지도 저장 완료이며, 모든 방에 접근했거나 전체 집을 완벽히 복원했다는 뜻은 아니다.
- 갈 수 없는 경계, 반복 방문해도 변하지 않는 경계는 기존 방식대로 제외한다.
- 같은 서버의 중복 요청은 거부한다. Manifest는 `FOREGROUND/NORMAL/[BASE]`다.
- 취소·선점·Ctrl+C 시 하위 Nav2 Goal을 취소하고 **실제 종료까지 기다린다**.
  외부 Nav2의 응답이 불명확하면 이동이 끝났다고 간주해 새 작업을 받지 않는다.
  직접 기동한 Nav2가 취소에 응답하지 않으면 준비 제한 시간 후 소유한 프로세스
  그룹만 종료한다. 종료는 SIGINT → SIGTERM → SIGKILL 순으로 제한 시간을 두며,
  다른 터미널의 프로세스에는 신호를 보내지 않는다.
- 저장 요청은 취소 불가능한 Service이므로 이미 저장 중이면 응답 후 취소를
  완료한다. 이때 저장된 파일은 남고 `map_yaml`로 반환한다.
  서버 자체를 Ctrl+C로 종료하면 지도 저장 서버도 함께 종료될 수 있으므로,
  응답이 없을 때는 저장 성공을 보고하지 않고 저장 결과 미확인으로 종료·정리한다.
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

탐색 주기는 `exploration_period_s=1.0`, 종료 확인 시간은 기존처럼
`completion_delay_s=12.0`이다. `robot_clearance_m=0.30`,
`minimum_goal_distance_m=0.45`, `minimum_frontier_cells=8`도 기존 탐색 기준이다.
통신 준비·지도/TF 신선도·개별 이동 제한은 각각 `ready_timeout_s`,
`map_timeout_s`, `tf_timeout_s`, `navigation_timeout_s`로 조정한다.
`ready_timeout_s`는 launch 인자로도 설정할 수 있다. 부모 launch의 종료 유예도
같은 값에 프로세스 정리 시간을 더해 적용하므로, Ctrl+C가 Action 서버를 먼저
강제 종료해 자동 기동한 매핑 프로세스를 남기지 않도록 한다.
전체 탐색 시간에는 제한이 없다. 대기 중에는 프론티어 계산이나 이동을 하지 않는다.

단위·모의 ROS 테스트는 실제 로봇의 탐색 품질 검증이 아니다. 실제 운행 전에는
주행 설정·TF·LiDAR와 정지 수단을 확인하고 안전한 공간에서 직접 확인해야 한다.
