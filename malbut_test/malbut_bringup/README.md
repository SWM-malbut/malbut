# Malbut 실로봇 Bringup (SWM25-169)

ROSOrin / Jetson Orin NX / ROS 2 Humble용 최상위 실행 패키지다.
제조사 드라이버·TF·Nav2 설정을 재사용하고, Malbut 응용 서버를 연결한다.
Gazebo, 시나리오, 웹·AWS, LLM, 음성 서비스는 이 실행에 포함하지 않는다.
추적·순찰 알고리즘과 시스템 관리자의 정책은 변경하지 않는다.

## 실행 구성

| 모드 | 시작하는 구성 | 시작하지 않는 구성 |
| --- | --- | --- |
| `sensors` (기본) | 공식 차체·센서·TF, YOLO, ReID, RGB-D 위치 추정 | Nav2, 추적·순찰 서버, 관리자 |
| `navigation` | 위 구성 + 공식 Nav2 + 추적·순찰 서버 → 준비 확인 → 관리자 | 미션 자동 실행 |

현재 실제 저장 지도가 없으므로 기본은 `sensors`다. 시뮬레이션 지도를
실로봇에 대신 넣지 않는다. 인식 환경 준비 전에는 `perception:=false`로
센서만 확인한다. `navigation` 모드에서는 인식 파이프라인이 필요하다.

공식 실행을 다음 두 부분으로 나누어 **각각 한 번만** include한다.

- 하드웨어: `slam/launch/include/robot.launch.py`
- Nav2: `~/ros2_ws/src/navigation/launch/include/bringup.launch.py`
- Nav2 공통 설정: 이 패키지의 `config/nav2_params.yaml` (실기기 파일 복사본)

제조사 최상위 `navigation.launch.py`가 이 두 launch를 함께 실행하는
[공식 구성](https://docs.hiwonder.com/projects/ROSOrin/en/jetson-orin-nano-version/docs/5_Mapping_%26_Navigation_Course.html#single-multi-point-navigation-and-obstacle-avoidance)을
참고했고, 사용자 로봇에서 소스 경로의 하위 launch가 존재함을 확인했다.
현재 이미지의 설치 폴더에는 하위 launch가 빠져 있어 소스 파일을 사용한다.
제조사 환경 선택은 실행 전 `export need_compile=False`로 설정한다.
경로가 다르면 `hardware_launch_file`, `navigation_launch_file`,
`nav2_params_file`을 지정한다. 제조사 원본은 패치하지 않는다.
공통 YAML 복사본은 기본 BT의 Spin/Wait/BackUp, 숫자 표기 수정,
초기 위치 자동 가정 해제만 적용했다. 제조사 DWB 설정은 그대로 사용하며,
`use_teb=false`의 `FollowPath`·`general_goal_checker` ID가 추적과 일치한다.
제공받은 하위 launch까지 대조했으며, 컨테이너가 공통 YAML을 받고 controller가
제조사 DWB YAML을 별도로 받는 구성을 유지한다. 내부 costmap도 컨테이너의
공통 YAML을 사용한다. 이동 출력은 제조사 그대로
`/cmd_vel_nav → velocity_smoother → /cmd_vel`이며 임의로 차체 토픽을 재배선하지 않는다.
하드웨어 launch가 description과 카메라를 포함해야 하며, 별도
`malbut_description` 또는 카메라 드라이버를 중복 실행하지 않는다.

## 로봇에서 처음 준비

1. [실로봇 적용본 안내](../README.md)의 Git clone·선택 빌드 절차를 따른다.
   소스는 `~/ros2_ws/src/malbut/malbut_test`, 결과는 `~/ros2_ws/install/malbut_test`다.
   제조사 ROS 환경을 먼저 source하고 이 별도 설치 결과를 overlay한다.
2. ROS 2 Humble과 JetPack은 그대로 유지한다. 저장소 루트의 데스크톱용
   ROS/Gazebo 설치기를 실로봇에서 실행하지 않는다.
3. [YOLO 준비](../malbut_yolo/README.md)와 [OSNet 준비](../malbut_reid/README.md)를
   따라 **현재 JetPack에 맞는** GPU 런타임·모델을 준비한다.
   Bringup은 패키지·모델을 다운로드하거나 드라이버를 설치하지 않는다.
4. YOLO upstream 소스도 적용본에 함께 포함되어 있다.
   기본 `colcon build`로 원본을 빌드하지 않는다.

```zsh
cd ~/ros2_ws
bash src/malbut/malbut_test/build.sh
source install/malbut_test/local_setup.zsh
export need_compile=False
```

로봇의 대화형 터미널은 Zsh이므로 `.zsh`를 source한다. `bash .../build.sh`는
빌드 스크립트만 Bash로 실행하며 터미널 셸을 바꾸지 않는다.
`slam`, `navigation`은 제조사 제공 선행 패키지다. 이름만 같은 임의의
apt/pip 패키지를 설치하지 않는다. ROS 패키지 인덱스에서 찾을 수 있어야 한다.

```bash
ros2 pkg prefix slam
ros2 pkg prefix navigation
ros2 launch malbut_bringup robot.launch.py --show-args
```

공식 앱 자동실행 서비스가 하드웨어를 이미 사용한다면, 운영자가 현재 실행을
확인하고 먼저 종료해야 한다. 공식 문서의 서비스는 `start_app_node.service`다.
Bringup은 systemd 서비스·Wi-Fi·DDS 설정을 변경하거나 다른 프로세스를 죽이지 않는다.
드라이버를 유지하려는 경우 `start_hardware:=false`로 외부 실행을 재사용한다.
같은 Bringup을 두 번 실행하지 않는다.

## 1. 지도가 없을 때

처음에는 GPU 준비와 독립적으로 센서를 확인할 수 있다.

```bash
ros2 launch malbut_bringup robot.launch.py perception:=false
```

인식 환경이 준비되면 기본 전체 센서 모드:

```bash
ros2 launch malbut_bringup robot.launch.py
```

아래 topic 이름은 사용자가 제공한 실기기 `ros2 topic list -t`와 일치한다.
**Header frame·시각과 RGB-D 정렬은 아직 확인 대상**이다. 실제 frame이 다르면
해당 인자를 변경한다. 이름을 바꾸는 relay나 가짜 TF는 만들지 않는다.

| 인자 | 초기값 |
| --- | --- |
| `rgb_topic` | `/depth_cam/rgb0/image_raw` |
| `depth_topic` | `/depth_cam/depth0/image_raw` |
| `camera_info_topic` | `/depth_cam/rgb0/camera_info` |
| `scan_topic` | `/scan_raw` |
| `odom_topic` | `/odom` |
| `robot_frame` | `base_footprint` |
| `global_frame` | `map` |

예를 들어 LiDAR가 `/scan`을 제공한다면:

```bash
ros2 launch malbut_bringup robot.launch.py scan_topic:=/scan
```

RGB-D 위치 추정은 **RGB에 정렬된 Depth와 해당 RGB CameraInfo**를 사용해야
한다. 이름만 연결했다고 정렬되는 것이 아니다. 헤더의 optical frame과 실제
TF가 맞는지도 확인한다. 시뮬레이션의 frame 보정이나 fake static TF를 적용하지 않는다.
OSNet은 명시적으로 `osnet` backend를 사용하므로 로딩 실패를 HSV 대체로 숨기지 않는다.
`model_path`, `python_executable`, `reid_python_executable`, `device`, `reid_model_path`,
`inference_backend`, `dnn_target`은 기존 인식 launch에 전달한다.
디버그 영상은 기본 끔, 필요하면 `publish_debug_image:=true`를 지정한다.
YOLO와 ReID는 각각 자신의 준비 스크립트로 설치한 전용 Python runtime을 사용한다.

## 2. 실제 지도를 만든 뒤

먼저 공식 ROSOrin SLAM 실행으로 실제 공간의 지도를 작성·저장한다.
SLAM 실행과 이 Bringup의 하드웨어 실행은 겹치지 않게 종료/재사용한다.
이 패키지는 새 SLAM·자동 탐색기를 구현하지 않는다.

```bash
ros2 launch malbut_bringup robot.launch.py \
  mode:=navigation map:=/실제/저장경로/home.yaml
```

`map`은 지도 이름이 아닌 **실제 YAML 파일 경로**다. 연결된 이미지 파일도
존재해야 한다. 제조사 Nav2 설정 대신 검토한 실기기 설정을 사용하려면
`nav2_params_file:=/실제/경로/nav2_params.yaml`을 함께 지정한다.
Nav2 설정과 추적 서버의 planner/controller/goal-checker ID, 속도 제한 topic,
정적 padding 값을 대조한다. 필요하면 `following_config`와 `lidar_config`로
기존 응용 설정을 지정한다. 시뮬레이션 튜닝을 제조사 Nav2에 덮어쓰지 않는다.

Nav2가 이미 별도로 실행 중이면 지도/드라이버를 다시 실행하지 않는다.

```bash
ros2 launch malbut_bringup robot.launch.py \
  mode:=navigation start_hardware:=false start_navigation:=false
```

필요한 경우 공식 RViz를 별도로 실행하고 **실제 초기 위치**를 지정한다.
Bringup이 임의의 초기 위치나 이동 목표를 발행하지 않는다.

```bash
ros2 launch navigation rviz_navigation.launch.py
```

## 준비 확인과 미션 요청

- 모든 Malbut 노드는 `use_sim_time=false`. 제조사 include에도 이를 전달한다.
- 준비 검사기는 최근 Scan·Odometry·RGB·Depth·CameraInfo와 TF를 확인한다.
  인식을 켰다면 3D 인식 결과 수신도 확인한다. 사람이 없는 빈 검출도 정상이다.
- 주행 모드는 지도·costmap, `map ↔ base` TF, Nav2 lifecycle `ACTIVE`,
  필요한 Nav2·추적·순찰 Action 서버를 추가 확인한다.
- 준비 전에는 관리자와 `/malbut/mission/execute`를 열지 않는다.
  빠진 항목을 로그로 표시하며, 정해진 몇 초가 지났다는 이유로 시작하지 않는다.
  `sensor_timeout_s`(기본 3초)는 센서의 신선도 기준이지 부팅 제한시간이 아니다.
- 준비가 되면 검사기는 종료하고 관리자를 시작한다. 센서 모드는 준비 로그만 남긴다.
  검사기가 시스템 관리자의 `/malbut/state`를 대신 발행하지 않는다.
- 검사기는 **부팅 시점 확인용**이며 지속 안전감시·비상정지·수동 제어권 중재기가 아니다.
- 소유한 Malbut 프로세스가 종료되거나 자식 프로세스가 오류 종료하면 launch를
  종료한다. Ctrl+C도 이 launch가 실행한 프로세스에만 전달한다.
  이것만으로 모터 정지를 보장하지는 않는다. 차체 watchdog과 실제 정지 동작은
  하드웨어 검증 대상이다. 이미 외부에서 실행하던 드라이버/Nav2는 종료하지 않는다.

준비 이후 요청하기 전에는 추적/순찰을 시작하지 않는다.

```bash
ros2 action send_goal /malbut/mission/execute \
  malbut_interfaces/action/ExecuteMission \
  "{capability_id: patrol, arguments_yaml: '{thoroughness: 0}'}" --feedback
```

이것은 실제 이동 요청이므로, 최초 실행은 안전한 공간에서 운영자가 정지 수단을
확보한 뒤 한다. 수동/비상 제어권 연동은 이번 Bringup이 새로 구현하지 않는다.

## 검증 범위

로컬에서는 launch 조합·인자·패키징과 준비 조건을 검증한다. 모의 입력 검증은
실로봇 검증이 아니다. 제조사 파일 경로·하위 launch 인자·설정 전달은 제공된
파일과 대조했다. 실제 로봇의 TF/토픽 구독 연결, RGB-D 정렬,
Jetson GPU 동작, 실지도 초기 위치, Nav2 설정 호환성, 이동·정지는 실기기에서
확인해야 한다. 로컬에서 확인하지 못한 실기기 성공을 주장하지 않는다.
