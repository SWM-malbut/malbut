# Malbut 실로봇 Bringup (SWM25-169)

ROSOrin / Jetson Orin NX / ROS 2 Humble용 최상위 실행 패키지다.
제조사 드라이버·TF·Nav2 설정을 재사용하고, Malbut 응용 서버를 연결한다.
Gazebo, 시나리오, 기존 홈캠·AWS, LLM, 음성 서비스는 이 실행에 포함하지 않는다.
추적·순찰 알고리즘과 시스템 관리자의 정책은 변경하지 않는다.

## 실행 구성

| 모드 | 시작하는 구성 | 시작하지 않는 구성 |
| --- | --- | --- |
| `sensors` (기본) | 공식 차체·센서·TF, 스캔 정규화, YOLO, ReID, RGB-D 위치 추정 | Nav2, 추적·순찰 서버, 관리자 |
| `mapping` | AutoSLAM 대기 서버. Goal 수신 후 없는 매핑 구성만 기동 | YOLO, 추적·순찰, 저장 지도 AMCL, 미션 자동 실행 |
| `navigation` | 센서 구성 + Nav2 + 위치 저장·복원 + 추적·순찰 서버 → 준비 확인 → 관리자 | 미션 자동 실행 |

기본은 `sensors`다. 시뮬레이션 지도를
실로봇에 대신 넣지 않는다. 인식 환경 준비 전에는 `perception:=false`로
센서만 확인한다. `navigation` 모드에서는 인식 파이프라인이 필요하다.

공식 실행을 다음 두 부분으로 나누어 **각각 한 번만** include한다.

- 하드웨어: `slam/launch/include/robot.launch.py`
- Nav2: `navigation/launch/include/bringup.launch.py`
- Nav2 기본 설정: `malbut_bringup/config/nav2_params.yaml`

제조사 최상위 `navigation.launch.py`가 이 두 launch를 함께 실행하는
[공식 구성](https://docs.hiwonder.com/projects/ROSOrin/en/jetson-orin-nano-version/docs/5_Mapping_%26_Navigation_Course.html#single-multi-point-navigation-and-obstacle-avoidance)을
참고했다. 실기기에 설치된 이미지 버전의 경로·인자는 반드시 확인해야 한다.
경로가 다르면 `hardware_launch_file`, `navigation_launch_file`,
`nav2_params_file`을 지정한다. 제조사 원본은 수정하지 않는다. Malbut의 Nav2
YAML은 로봇에서 제공된 설정에 아래의 확인된 보정을 적용한 프로젝트 소유 파일이다.
하드웨어 launch가 description과 카메라를 포함해야 하며, 별도
`malbut_description` 또는 카메라 드라이버를 중복 실행하지 않는다.

## 로봇에서 처음 준비

1. 제조사 ROS 환경을 먼저 source하고 별도의 Malbut workspace를 overlay한다.
   `~/ros2_ws` 등 제조사 workspace를 Malbut로 덮어쓰거나 다시 빌드하지 않는다.
2. ROS 2 Humble과 JetPack은 그대로 유지한다. 저장소 루트의 데스크톱용
   ROS/Gazebo 설치기를 실로봇에서 실행하지 않는다.
3. [YOLO 준비](../malbut_yolo/README.md)와 [OSNet 준비](../malbut_reid/README.md)를
   따라 **현재 JetPack에 맞는** GPU 런타임·모델을 준비한다.
   Bringup은 패키지·모델을 다운로드하거나 드라이버를 설치하지 않는다.
4. 아래는 YOLO upstream을 포함한 Malbut 소스가
   `~/malbut_ws/src/malbut`에 이미 준비되어 있다는 전제의 빌드 명령이다.

```bash
cd ~/malbut_ws
PATH=/usr/bin:/bin colcon build --symlink-install \
  --base-paths src src/malbut/malbut_yolo/vendor/yolo_ros/{yolo_ros,yolo_msgs} \
  --packages-up-to malbut_bringup
source install/local_setup.bash
```

위 명령은 Bash 기준이다. 로봇의 Zsh에서는 대응하는 `local_setup.zsh`를
사용한다. 실제로 source할 제조사 setup 경로는 로봇 설치 상태를 따른다.
`slam`, `navigation`은 제조사 제공 선행 패키지다. 이름만 같은 임의의
apt/pip 패키지를 설치하지 않는다. ROS 패키지 인덱스에서 찾을 수 있어야 한다.

빌드·source 후 인식 런타임이나 모델이 아직 없다면 기존 준비 도구를 한 번 실행한다.
설치된 패키지 경로를 사용하므로 소스의 복사 위치와 무관하다.

```bash
bash "$(ros2 pkg prefix malbut_yolo)/share/malbut_yolo/scripts/prepare_runtime.sh"
bash "$(ros2 pkg prefix malbut_reid)/share/malbut_reid/scripts/prepare_inference_runtime.sh"
bash "$(ros2 pkg prefix malbut_reid)/share/malbut_reid/scripts/prepare_osnet_model.sh"
```

Bringup은 인식용 Python 실행 파일·모델이 없으면 누락 경로와 위 준비 명령을
알려주고 시작을 거부한다. 자동 설치는 하지 않으며, 파일 사전 검사가 실제 GPU
추론 성공까지 보증하지는 않는다. 지도 만들기에는 이 인식 준비가 필요 없다.

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

기본 토픽 이름은 사용자가 제공한 ROSOrin/Aurora 실기기 목록과 일치한다.
센서 Header·TF·RGB-D 정렬은 별도 실측 대상이다. 원본 `/scan_raw`는 유지하며
SLAM·Nav2용 `/scan_normalized`를 별도로 발행한다.

| 인자 | 초기값 |
| --- | --- |
| `rgb_topic` | `/depth_cam/rgb0/image_raw` |
| `depth_topic` | `/depth_cam/depth0/image_raw` |
| `camera_info_topic` | `/depth_cam/rgb0/camera_info` |
| `raw_scan_topic` | `/scan_raw` |
| `scan_topic` | `/scan_normalized` |
| `odom_topic` | `/odom` |
| `robot_frame` | `base_footprint` |
| `global_frame` | `map` |

예를 들어 LiDAR가 `/scan`을 제공한다면:

```bash
ros2 launch malbut_bringup robot.launch.py raw_scan_topic:=/scan
```

RGB-D 위치 추정은 **RGB에 정렬된 Depth와 해당 RGB CameraInfo**를 사용해야
한다. 이름만 연결했다고 정렬되는 것이 아니다. 헤더의 optical frame과 실제
TF가 맞는지도 확인한다. 시뮬레이션의 frame 보정이나 fake static TF를 적용하지 않는다.
OSNet은 명시적으로 `osnet` backend를 사용하므로 로딩 실패를 HSV 대체로 숨기지 않는다.
`model_path`, `python_executable`, `device`, `reid_model_path`,
`inference_backend`, `dnn_target`은 기존 인식 launch에 전달한다.
디버그 영상은 기본 끔, 필요하면 `publish_debug_image:=true`를 지정한다.

## 2. 자동 지도 만들기와 저장 지도 주행

기존에 별도로 켠 Bringup·SLAM·Nav2와 겹치지 않게 정리한 뒤, 로봇에서
웹 패널을 한 번만 실행한다. 페이지 연결만으로는 다른 노드를 켜거나 움직이지 않는다.

```zsh
ros2 run malbut_bringup robot_web_panel
```

Mac에서 `http://<로봇-IP>:8766` 접속 후 터미널의 `Access token`을 입력한다.

1. **지도 만들기 모드 켜기** → AutoSLAM 서버 준비를 확인한다.
2. 새 지도 이름 입력 → **자동 지도 만들기 시작**. 이 요청부터 탐색할 수 있다.
3. 완료 결과와 저장 지도를 확인 → **Bringup 종료** → `꺼짐` 상태를 확인한다.
4. 저장 지도 목록에서 지도를 선택 → **선택한 지도로 주행 모드 켜기**.
5. 초기 위치와 준비 상태를 확인한 뒤 사람 추적·순찰을 별도로 요청한다.

자동 지도 만들기가 종료되면 목록을 갱신하며, **지도 목록 새로고침**으로도
다시 읽을 수 있다. 기본 저장·조회 폴더는 `~/.ros/malbut/maps`다. 다른 폴더는
웹 패널의 ROS parameter로 지정한다:

```zsh
ros2 run malbut_bringup robot_web_panel --ros-args \
  -p map_directory:="$HOME/.ros/malbut/maps"
```

웹 패널을 다시 실행하거나 모드마다 추가 실행할 필요는 없다. 웹의 Bringup
종료는 미션 취소·종료 확인 후 자기가 켠 프로세스를 정리한다. 정지를 확인하지
못하면 오류를 표시하며, 사용자는 실제 로봇 상태를 확인해야 한다.

터미널로 직접 구성하려면 웹 방식 대신 아래처럼 실행할 수 있다:

```zsh
ros2 launch malbut_bringup robot.launch.py mode:=mapping
```

이 launch를 유지하고 다른 터미널에서 Goal을 보낸다:

```zsh
ros2 action send_goal /autoslam malbut_interfaces/action/AutoSlam \
  '{map_name: home2}' --feedback
```

AutoSLAM은 센서/오도메트리, SLAM, Nav2의 현재 소유자를 확인하고 없는 구성만
`mapping_backend.launch.py`로 켠다. 준비를 확인한 뒤 탐색하고, 완료·취소 시
자기가 켠 구성만 종료한다. 외부 구성은 유지한다. 부분 실행/중복 실행/AMCL
충돌은 임의로 종료하지 않고 사유를 반환한다. 기존 스택을 직접 관리하려면
`ros2 launch malbut_autoslam autoslam.launch.py auto_start:=false`를 사용한다.

제조사 SLAM 설정은 `slam/config/slam.yaml`을 소스/설치 경로에서 읽는다.
없다면 자동 기동 로그가 누락 경로를 알려주며 임의의 시뮬레이션 설정으로 대체하지 않는다.

저장 결과는 `~/.ros/malbut/maps/home2.yaml`과 이미지다. 같은 이름은 덮어쓰지 않는다.
터미널 방식이라면 매핑 모드를 종료한 뒤 저장 지도로 실행한다:

```bash
ros2 launch malbut_bringup robot.launch.py \
  mode:=navigation map:="$HOME/.ros/malbut/maps/home2.yaml" \
  publish_debug_image:=true
```

`map`은 지도 이름이 아닌 **실제 YAML 파일 경로**다. 연결된 이미지 파일도
존재해야 한다. 다른 검토된 설정을 사용하려면
`nav2_params_file:=/실제/경로/nav2_params.yaml`을 함께 지정한다.
Nav2 설정과 추적 서버의 planner/controller/goal-checker ID, 속도 제한 topic,
정적 padding 값을 대조한다. 필요하면 `following_config`와 `lidar_config`로
기존 응용 설정을 지정한다. 시뮬레이션 튜닝을 제조사 Nav2에 덮어쓰지 않는다.

Nav2가 이미 별도로 실행 중이면 지도/드라이버를 다시 실행하지 않는다.

```bash
ros2 launch malbut_bringup robot.launch.py \
  mode:=navigation start_hardware:=false start_navigation:=false
```

AutoSLAM이 저장한 `<지도이름>.pose.yaml`이 있으면 저장 지도 주행에서 초기 위치로
복원한다. 수정 이전에 만든 지도처럼 위치 기록이 없거나 로봇을 옮겼다면 공식
RViz의 **2D Pose Estimate**로 실제 초기 위치를 지정한다.
같은 지도로 다시 실행하면
아래 위치 기억 기능이 마지막 AMCL 위치를 초기 추정치로 한 번 전달한다.
전원이 꺼진 동안 로봇을 옮겼다면 반드시 수동으로 초기 위치를 바로잡는다.

```bash
ros2 launch navigation rviz_navigation.launch.py
```

## 위치 기억·주행 설정·장애물 입력

- `pose_memory`는 AMCL이 활성일 때 최신 `/amcl_pose`를 **5초마다** 저장한다.
  경로는 `~/.ros/malbut/localization/last_pose.yaml`이며 Git 밖의 로봇 실행 데이터다.
  지도 YAML+이미지 해시, 위치·방향·공분산·저장 시각을 기록하고 원자적으로 교체한다.
  동일 지도에서 AMCL 준비 후 `/initialpose`로 한 번 복원한다. 일치하는 AMCL 기록이
  없으면 AutoSLAM의 지도별 `.pose.yaml`을 사용한다. 두 기록 모두 지도 내용이 같아야
  하며 초기 추정치일 뿐이다. 수동 초기화가 우선이다.
  `restore_pose:=false`면 복원 없이 저장만, `pose_memory:=false`면 노드를 켜지 않는다.
  복원은 위치 확인의 대체가 아니며, covariance가 유한하다는 것이 정확도를 보증하지 않는다.
- Local/Global 차체 반경 **0.18m**, Local/Global inflation **0.20m**.
  사용자 실기기 확인값을 적용했다. 추가 장착물은 실제 외곽선을 다시 측정해야 한다.
- Velocity smoother **전후 ±0.4m/s, 회전 ±1.0rad/s**, 가속도는 제공된 제조사 DWB의
  `2.5m/s²`, `3.2rad/s²`와 일치한다. 제조사 DWB 파일·최대속도는 수정하지 않는다.
  의도된 횡이동 0·AMCL Differential 모델·추적 거리별 속도 정책은 유지한다.
- LiDAR는 Local/Global 모두 표준 2D `ObstacleLayer`를 사용한다.
  단일 평면 스캔에 불필요한 Voxel 저장·발행은 하지 않는다. 스캔 토픽과
  관측 거리·높이 필터는 유지하며 Depth 레이어의 장애물을 직접 지우지 않는다.
  기존 Local Voxel의 암묵적 48cm 저장 한계는 없어지므로 기울어진 스캔에서
  완전히 동일한 동작을 보장하는 변경은 아니다.
- 작은 장애물 폭/최소 클러스터 크기 필터는 추가하지 않는다. Depth 점군
  `/depth_cam/depth0/points`도 양쪽 costmap의 별도 표준 VoxelLayer에 반영한다.
  유효 obstacle 높이 **5~20cm**는 costmap 좌표계 기준이다. 바닥은 표시하지 않되
  floor clearing 관측을 별도로 사용해 사라진 낮은 장애물이 남는 것을 줄인다.
  5cm 미만은 배제되며, 카메라 시야 밖/최소거리 안의 장애물을 보장하지 않는다.
  20cm 상한은 사용자 확인 기본 구성과
  [제조사 표준형 높이 16.6cm](https://www.hiwonder.com/products/rosorin)
  에 약 3.4cm 여유를 둔 초기값이다. 실측·TF·바닥 노이즈 검증은 아직 필요하며,
  추가 장착물/다른 기종에는 그대로 적용하지 않는다.
  Depth Voxel 저장 공간은 3cm × 16층(0.48m)이며 제거용 관측은 -5~48cm다.
  같은 점군에 표시용·제거용 높이 필터를 다르게 적용하는 Nav2 설정이다.
  카메라 노드나 추론을 두 번 실행하는 구성이 아니지만, 점군 관측 처리는 각각 수행한다.
  제거용 관측은 장애물을 등록하지 않으므로 20cm 위 선반을 통행 금지로 만들지 않는다.
  장애물 표시 상한과 voxel 저장 높이가 반드시 같아야 하는 것은 아니다.
- `scan_normalizer`는 원본 각도 정보로 고정 각도 격자에 재배치한다. 가변 점 개수를
  단순 절단하지 않으며 미관측 방향은 NaN, 같은 칸의 장애물은 가까운 값을 유지한다.
  타임스탬프는 원본이고 `time_increment=0`이다(운동 보정/deskew 기능 아님).
  원본 각도 metadata 자체가 잘못됐거나 odometry가 밀리는 문제는 고치지 못한다.
  외부 SLAM을 재사용하면 그 노드의 scan 입력도 운영자가 따로 확인해야 한다.

### 기존 구현 검토

- LiDAR `ObstacleLayer` + Depth `VoxelLayer` 조합은
  [Linorobot2 Humble 설정](https://github.com/linorobot/linorobot2/blob/humble/linorobot2_navigation/config/navigation.yaml)을 참고한다.
- 같은 Depth 토픽의 marking/clearing 분리는
  [UBR-1 실제 적용 사례](https://www.robotandchisel.com/2020/09/01/navigation2/#tilting-head-node)가 있다.
  이 구성을 유지하되 위 높이값을 현장 검증값으로 오해하지 않는다.
- 스캔 정규화는 당장 교체 가능한 Humble 필터가 확인되지 않아 기존 adapter를 유지한다.
  [laser_filters 2.0.9](https://github.com/ros-perception/laser_filters/blob/2.0.9/laser_filters_plugins.xml)에는
  `LaserScanBinningFilter`가 없으며,
  [검토한 후속 구현](https://github.com/ros-perception/laser_filters/blob/rolling/include/laser_filters/binning_filter.h)은
  입력에 따라 시작 각도를 바꾸고, 겹치는 각도에서 최근접 대신 마지막 측정을 남기며,
  빈 intensities 배열을 검사하지 않고 접근한다. 현재 adapter를 그대로 대체하지 않는다.
  제조사 드라이버가 안정된 각도 격자를 직접 제공하는 것이 확인되면 navigation/sensors 모드에서
  `start_scan_adapter:=false scan_topic:=/scan_raw`로 생략할 수 있다.

## Mac에서 웹으로 확인

단독 `ros2 run malbut_bringup robot_web_panel` 실행을 기본으로 사용한다. Mac에서
`http://<로봇-IP>:8766` 접속 후 로봇 터미널의 접근 토큰을 입력한다.
저장 지도 선택·Bringup 시작/종료, 원본/인식 영상, 추적 상태,
AutoSLAM·사람추적·순찰 실행·취소를 제공한다. 실시간 2D 지도는 `/map`,
로봇 위치·방향은 TF에서 가져오며 지도를 클릭해 이동하거나 초기화하지 않는다.

기존 Bringup에 `web_panel:=true`로 포함한 패널은 영상·지도·미션 요청만 제공하고
Bringup 시작/종료는 비활성화한다. 단독 패널과 같은 포트로 중복 실행하지 않는다.
브라우저 종료/연결 끊김은 정지가 아니며, 미션 취소 버튼은 이 서버가 보낸 Goal만 취소한다.
자세한 실행·안전 범위는 [웹 테스트 안내](README_WEB.md)를 참고한다.

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
실로봇 검증이 아니다. 실제 로봇의 드라이버 경로, TF/토픽, RGB-D 정렬,
Jetson GPU 동작, 실지도 초기 위치, Nav2 설정 호환성, 이동·정지는 실기기에서
확인해야 한다. 로컬에서 확인하지 못한 실기기 성공을 주장하지 않는다.
