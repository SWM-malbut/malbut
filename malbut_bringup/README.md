# Malbut 실로봇 Bringup (SWM25-169)

ROSOrin / Jetson Orin NX / ROS 2 Humble용 최상위 실행 패키지다.
제조사 드라이버·TF를 재사용하고, 공식 Nav2와 Malbut 응용 서버를 연결한다.
기존 `build.sh` 하나로 음성 런타임·STT CUDA 라이브러리와 ROS 패키지를 빌드하고,
`robot.launch.py` 하나로 로봇 전체와 STT·Agent·TTS를 함께 실행한다.
Gazebo와 시나리오는 이 실행에 포함하지 않는다.
클라우드 연결이 설정되어 있으면 기존 홈캠 KVS 영상·음성 전송 노드를 함께 실행한다.
추적·순찰 알고리즘과 시스템 관리자의 정책은 변경하지 않는다.

음성은 기본 `speech:=true`이며 [음성 준비·점검 안내](README_SPEECH.md)를 따른다.
`speech.launch.py`는 이 최상위 launch가 포함하는 하위 구성으로, 음성만 점검할 때도 사용한다.

## 실행 구성

실행 모드는 없다. `robot.launch.py`는 항상 같은 구성을 켜고, 저장 지도 선택 여부만
실행 중에 바뀐다.

| 항상 실행 | 내용 |
| --- | --- |
| 제조사 하드웨어 | 차체·센서·TF·오도메트리. 조이스틱은 수동 조작 입력으로 연결 |
| Nav2 | 공식 Nav2 서버를 Malbut가 직접 컴포넌트로 구성(`nav2_stack.py`), Malbut 소유 `nav2_params.yaml` |
| 안전·구역 | Collision Monitor(수동 조작의 마지막 검사), Zone keepout 필터(`zone_filter`) |
| 위치 보정 | `malbut_relocalization`의 `/relocalize` Action (저장 위치 확인, 필요하면 전역 탐색) |
| 인식 | YOLO·RGB-D 사람 위치 추정 (`perception:=false`로 끌 수 있음) |
| 응용 서버 | 사람 추적, 순찰, AutoSLAM(대기) |
| 시스템 관리자 | 미션 실행·선점, 위치 추정 전환, 수동 조작 해제(`manual_control`) |
| 음성 | STT·Agent·TTS (`speech:=false`로 끌 수 있음) |

| 저장 지도 | 위치 추정 | 가능한 이동 미션 |
| --- | --- | --- |
| 선택 안 됨 (기본, `map` 인자 없음) | slam_toolbox로 지도 작성 | 자동 지도 만들기, 수동 조작 |
| 선택됨 (`map:=...` 또는 실행 중 선택) | 저장 지도 + AMCL, 선택할 때 위치 보정 | 사람 추적, 순찰, 목적지 이동, 위치 보정, 수동 조작 |

관리자는 시작하자마자 위치 추정을 켠다. Nav2 global costmap은 `map` TF가 생겨야
활성화되기 때문이다. 미션은 준비 검사기가 READY를 보낸 뒤에 받는다([위치 추정 전환](#위치-추정-전환)).

STT·Agent·TTS는 기본 포함이며, 로봇 준비 확인 뒤 음성 점검 → Agent·TTS →
관리자 준비 확인 → STT 순서로 시작한다. 시작 중 CUDA 메모리 부족은 5초, 10초 대기 후
최대 3회 시도하며, 재시도 소진·제한시간 초과 또는 다른 음성 구성 실패 시 Bringup 전체를 종료한다.
시뮬레이션 지도를 실로봇에 대신 넣지 않는다.

실기기 적용본은 `build.sh` 하나로 홈캠 미디어까지 빌드한다.
`cloud.launch.py`는 웹 명령·상태 연결만 유지하고, 웹이 시작하는 `robot.launch.py`가
카메라와 영상 노드를 함께 관리한다. `HOMECAM_BACKEND_URL`이 설정되어 있으면
기존 `homecam_robot.launch.py`를 한 번 포함하며 토큰 파일 환경을 그대로 전달한다.
별도 미디어 launch나 systemd 서비스를 중복 실행하지 않는다.
전체 절차는 [실기기 클라우드 연결](../malbut_test/README_CLOUD.md)을 따른다.

제조사 실행은 하드웨어 `slam/launch/include/robot.launch.py` 하나만 include한다.
Nav2는 제조사 navigation launch 대신, 공식 `nav2_bringup`이 쓰는 것과 같은 Nav2
서버를 제조사처럼 하나의 컴포넌트 컨테이너(`nav2_container`)에 직접 구성한다
([`nav2_stack.py`](malbut_bringup/nav2_stack.py)). 공식 launch를 그대로 쓰지 않는 이유는
세 가지다. 수동 조작(AssistedTeleop)을 별도 behavior 서버에 두어 그 출력만 Collision
Monitor를 거치게 하고, Zone 필터 서버를 추가하고, 위치를 찾는 회전이 지도 위치 없이도
가능하도록 lifecycle 순서를 정한다. 이 순서에서는 behavior·smoother·Collision Monitor가
map TF를 기다리는 planner보다 먼저 켜진다.
컨트롤러(DWB)를 포함한 모든 Nav2 값은
`config/nav2_params.yaml`, SLAM 값은 `config/slam_toolbox.yaml`에 있으며 둘 다
로봇에서 제공된 제조사 설정(Hiwonder ROSOrin ROS2 `navigation`, `slam` 패키지)을
옮긴 뒤 아래 보정을 적용한 프로젝트 소유 파일이다. 다른 검토된 파일은
`nav2_params_file`, `slam_params_file`, `hardware_launch_file`로 지정한다.
제조사 원본은 수정하지 않는다. 하드웨어 launch가 description과 카메라를 포함하므로
별도 `malbut_description` 또는 카메라 드라이버를 중복 실행하지 않는다.

## 로봇에서 처음 준비

1. 실로봇에는 `malbut_test` 내용을 `~/ros2_ws/src/malbut`에 복사한다.
   제조사 ROS 환경 위에 `install/malbut_test`의 별도 빌드 결과를 overlay한다.
   제조사 패키지를 재빌드하거나 제조사 설치 결과를 덮어쓰지 않는다.
2. ROS 2 Humble과 JetPack은 그대로 유지한다. 저장소 루트의 데스크톱용
   ROS/Gazebo 설치기를 실로봇에서 실행하지 않는다.
3. [YOLO 준비](../malbut_yolo/README.md)와 [OSNet 준비](../malbut_reid/README.md)를
   따라 **현재 JetPack에 맞는** GPU 런타임·모델을 준비한다.
   Bringup은 패키지·모델을 다운로드하거나 드라이버를 설치하지 않는다.
4. 적용본 최상위 `README.md`의 ROS 의존성과 [음성 최초 준비](README_SPEECH.md#최초-준비)를
   마친 뒤 아래처럼 빌드한다. `build.sh`가 별도 음성 Python 환경을 준비하고
   STT CUDA 라이브러리·음성·인식·주행 ROS 패키지를 함께 빌드한다.
   `COLCON_IGNORE`는 유지한다. 모델·외부 whisper.cpp 소스는 최초 준비에 필요하며
   빌드 중 자동 다운로드하지 않는다.

```zsh
source /opt/ros/humble/setup.zsh
source ~/ros2_ws/install/setup.zsh
cd ~/ros2_ws
bash src/malbut/build.sh
source install/malbut_test/local_setup.zsh
```

전체 저장소를 `src/malbut`에 둔 경우 빌드 명령만
`bash src/malbut/malbut_test/build.sh`로 바꾼다. 설치 경로는 동일하다.
`ros2 pkg prefix malbut_bringup`이 `install/malbut_test/malbut_bringup` 아래인지 확인한다.
실제로 source할 제조사 setup 경로는 로봇 설치 상태를 따른다.
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

### Depth costmap을 쓰지 않는 이유

2026-09-16 전달받은 실기기 비교에서, 약 8.2MB Depth 점군을 처리 없이
수신하기만 해도 같은 프로세스의 TF 수신이 최대 2.5초 지연됐다.
Nav2의 Depth 구독을 제거한 비교에서는 약 4분간 TF 누락이 없었다.
그래서 **두 costmap은 LiDAR만 사용한다.** LiDAR보다 낮은 장애물은 costmap에 없으며,
Collision Monitor도 같은 LiDAR를 쓰므로 이를 막지 못한다.

Depth를 costmap에 다시 넣을 수 있도록 만든 Bringup 내부
[depth_costmap](depth_costmap/README.md) 플러그인(`DepthVoxelLayer`)은 소스만 남기고
기본 빌드에서 뺐다(`-DMALBUT_DEPTH_COSTMAP=ON`으로만 빌드). 그래서 로봇 빌드에
`ros-humble-depth-image-proc`이 필요 없다. 복원 절차와 설정값은 그 README에 있다.
**RGB·Depth 영상 자체는 계속 사용한다.** 사람 추적이 Depth로 사람 위치를 계산한다.

하드웨어를 직접 시작할 때는 공식 Aurora launch 인자 `point_cloud_enable=false`를
전달해 원본 점군 스트림을 끈다(제조사 파일 수정 없음). 제조사 중간 launch가 값을
덮는 버전이라면 그 설정이 우선하므로, 원본 점군 발행 중단은 실기기에서 확인한다.
`ros2 topic info /depth_cam/depth0/points --verbose`로 Nav2 구독자가 없는지 볼 수 있다.

## 1. 처음 실행

GPU·음성 준비 전에는 인식과 음성을 끄고 센서·Nav2·관리자만 확인할 수 있다.
이 경우 사람 추적 서버도 켜지지 않는다.

```bash
ros2 launch malbut_bringup robot.launch.py perception:=false speech:=false
```

인식·음성 환경과 API 키가 준비되면 전체 실행:

```bash
ros2 launch malbut_bringup robot.launch.py
```

기본 토픽 이름은 사용자가 제공한 ROSOrin/Aurora 실기기 목록과 일치한다.
센서 Header·TF·RGB-D 정렬은 별도 실측 대상이다.
SLAM·AMCL·Nav2·사람 추적은 드라이버의 `/scan_raw`를 직접 사용한다.
드라이버의 `bins` 설정으로 고정 각도 격자가 제공된다고 가정하며, 별도 정규화 노드는 없다.

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
현재 실기기 테스트에서는 OSNet 초기화와 외형 특징 계산을 코드에서 생략한다.
색상 비교도 사용하지 않으며 기존 검출 박스·이동 기반 추적은 유지한다.
웹에서도 **지금 보이는 사람**으로 테스트한다. 지정된 사람 모드의 인터페이스와
선택 로직은 유지하지만, 이 테스트 상태에서는 외형 기반 동일인 재식별을 하지 않는다.
`model_path`, `python_executable`, `device`, `reid_model_path`,
`inference_backend`, `dnn_target`은 기존 인식 launch에 전달한다.
디버그 영상은 기본 끔, 필요하면 `publish_debug_image:=true`를 지정한다.

## 2. 자동 지도 만들기와 저장 지도 주행

기존에 별도로 켠 Bringup·SLAM·Nav2와 겹치지 않게 정리한 뒤, 로봇에서
`cloud.launch.py`로 서비스 웹([malbut_web](../malbut_web/README.md))에 연결하고
웹의 **로봇 기능**에서 진행한다([실기기 클라우드 연결](../malbut_test/README_CLOUD.md)).
연결만으로는 다른 노드를 켜거나 움직이지 않는다. LAN 테스트 패널
(`robot_web_panel`)도 남아 있지만 현재 사용하지 않는다.

1. **지도 만들기 모드** → Bringup이 지도 없이 켜지고 준비 완료를 확인한다.
2. 새 지도 이름 입력 → **자동 지도 만들기**. 이 요청부터 탐색할 수 있다.
3. 완료 결과와 저장 지도를 확인한다. Bringup은 끄지 않는다.
4. 저장 지도 목록에서 지도를 선택 → **선택한 지도로 전환**. 재시작 없이 SLAM을
   끄고 저장 지도와 AMCL로 바꾼 뒤 [위치 보정](#위치-보정)으로 로봇 위치를 찾는다.
   위치를 찾지 못했을 때 제자리에서 한 바퀴 돌 수 있으므로 주변을 비운다.
5. 웹의 **위치 추정** 줄과 지도 위 로봇 위치를 확인한 뒤 사람 추적·순찰을 별도로 요청한다.
   위치가 틀리면 **위치 보정**에서 다시 찾거나 **현재 위치 지정**으로 직접 정한다.

Bringup이 꺼져 있을 때 4번을 누르면 선택한 지도로 바로 켠다. 다시 지도를 만들려면
**지도 만들기로 전환**을 누른다. 두 버튼 모두 이동 미션이 남아 있으면 거부되므로 먼저
취소한다. 자동 지도 만들기가 종료되면 목록이 갱신된다. 필요 없는 지도는
**선택한 지도 삭제**로 지운다(사용 중인 지도는 제외). 기본 저장·조회 폴더는
`~/.ros/malbut/maps`이며, 다른 폴더는 `cloud.launch.py`의 `map_directory`로 지정한다.

웹의 Bringup 종료는 미션 취소·종료 확인 후 자기가 켠 프로세스를 정리한다. 정지를
확인하지 못하면 오류를 표시하며, 사용자는 실제 로봇 상태를 확인해야 한다.
Bringup이 스스로 끝나면 `Bringup exited (코드): <처음 죽은 노드> exited with code N;
<그 노드의 마지막 오류 줄>; <launch 종료 사유>`처럼 원인을 표시하고, 죽은 관리자가
남긴 위치 추정·시스템 상태 표시는 지운다. 전체 로그는 로봇의
`~/.ros/malbut/web_runtime/<mode>-*.log`에 있다. **Bringup 종료**를 눌러 ERROR를
정리한 뒤 다시 시작한다.

터미널로 직접 구성하려면 지도 없이 실행하고:

```zsh
ros2 launch malbut_bringup robot.launch.py
```

준비 완료 후 다른 터미널에서 관리자를 통해 요청한다:

```zsh
ros2 action send_goal /malbut/mission/execute \
  malbut_interfaces/action/ExecuteMission \
  "{capability_id: autoslam, arguments_yaml: '{map_name: home2}'}" --feedback
```

Bringup 안의 AutoSLAM은 Bringup이 켠 SLAM·Nav2를 그대로 사용하고 새 구성을 켜지 않는다.

저장 결과는 `~/.ros/malbut/maps/home2.yaml`과 이미지다. 같은 이름은 덮어쓰지 않는다.
실행 중 저장 지도로 바꾸려면:

```bash
ros2 service call /malbut/localization/load_map nav2_msgs/srv/LoadMap \
  "{map_url: $HOME/.ros/malbut/maps/home2.yaml}"
```

처음부터 저장 지도로 켜려면 `map`에 **실제 YAML 파일 경로**를 지정한다.
연결된 이미지 파일도 존재해야 한다.

```bash
ros2 launch malbut_bringup robot.launch.py \
  map:="$HOME/.ros/malbut/maps/home2.yaml" publish_debug_image:=true
```

추적 서버의 planner/controller/goal-checker ID(`GridBased`, `FollowPath`,
`general_goal_checker`)는 `nav2_params.yaml`과 일치한다. 추적기는 사람 위치 자체를 목표로
경로를 요청하고, 사람 몸이 차지한 칸은 planner의 `GridBased.tolerance`(0.5 m)가 가장 가까운
갈 수 있는 칸으로 옮긴다. 후퇴 경로는 `retreat_controller_id`(=`FollowPathReverse`)로
보낸다. 필요하면 `following_config`와 `lidar_config`로 기존 응용 설정을 지정한다.
시뮬레이션 튜닝을 실기기 Nav2에 덮어쓰지 않는다.

저장 지도로 바꿀 때마다 관리자가 [위치 보정](#위치-보정)을 요청한다. 이 지도의 마지막
AMCL 위치나 AutoSLAM이 저장한 `<지도이름>.pose.yaml`을 먼저 확인하고, 맞지 않거나
기록이 없으면 지도 전체에서 찾는다. 그래도 찾지 못했거나 결과가 실제와 다르면 공식
RViz의 **2D Pose Estimate**로 실제 위치를 지정한다.

```bash
ros2 launch navigation rviz_navigation.launch.py
```

## 위치 추정 전환

시스템 관리자가 map→odom을 내는 위치 추정을 하나만 유지한다. 표준 타입을 사용한다.

| 이름 | 타입 | 동작 |
| --- | --- | --- |
| `/malbut/localization/load_map` | `nav2_msgs/srv/LoadMap` | slam_toolbox 종료 → map_server·AMCL 시작 → 지도 로드 → 위치 보정 |
| `/malbut/localization/start_mapping` | `std_srvs/srv/Trigger` | map_server·AMCL 정리(RESET) → slam_toolbox 시작 |
| `/malbut/localization/state` | `std_msgs/String`(JSON) | `mode`(`SWITCHING`·`MAPPING`·`LOCALIZATION`·`ERROR`), `map`, `message` |

- `BASE`를 쓰는 미션이 실행·대기 중이면 전환을 거부한다. 먼저 취소한다.
- 전환 중(`SWITCHING`, 위치 보정 포함)에는 `BASE`를 쓰는 미션을 모두 거부한다.
  위치 보정이 로봇을 회전시킬 수 있기 때문이다. 수동 조작도 이때만 거부되며
  전환이 끝나면 다시 요청된다. 오류 상태에서는 지도가 필요한 미션과 자동 지도
  만들기를 거부한다.
- 다른 저장 지도로 바꿀 때는 AMCL을 RESET한 뒤 다시 켠다. AMCL이 이전 지도의
  위치를 새 지도에 넘기지 않게 하기 위해서다.
- slam_toolbox는 관리자가 자식 프로세스로 실행하며, 관리자가 종료되거나 강제
  종료되어도 함께 종료된다. AMCL·map_server는 `lifecycle_manager_localization`
  (autostart 끔)을 통해 켜고 정리한다.

## 위치 보정

위치 보정은 별도 패키지 [malbut_relocalization](../malbut_relocalization/README.md)의
`/relocalize` Action(`malbut_interfaces/action/Relocalize`)이다. 관리자는 저장 지도를
불러올 때마다 `AUTO`로 요청하고, 끝날 때까지 `SWITCHING`을 유지한다. 결과는
`/malbut/localization/state`의 `message`와 웹의 **위치 추정** 줄에 나온다.

1. 이 지도의 저장 위치(마지막 AMCL 위치, 없으면 AutoSLAM의 `.pose.yaml`)를
   `/initialpose`로 넣는다.
2. 그 위치에서 LiDAR 스캔 끝점이 지도 벽에 맞는 비율을 계산한다. 50% 이상이면 확정한다.
3. 맞지 않거나(꺼진 동안 옮겨짐) 저장 위치가 없으면 AMCL 전역 위치 추정을 켜고
   Nav2 Spin으로 제자리 360° 회전한 뒤 같은 기준으로 다시 확인한다(최대 2회).
4. 그래도 맞지 않으면 실패로 알린다. 웹의 **현재 위치 지정 → 이 위치로 설정**(`GIVEN_POSE`)
   또는 RViz **2D Pose Estimate**로 지정한다.

다른 클라이언트는 `relocalize` 기능으로 요청한다(저장 지도 선택 후, `BASE` 사용).
`restore_pose:=false`면 관리자가 요청하지 않고, `relocalization:=false`면 노드를 켜지 않는다.
기초 구현이며, 비슷한 방이 여러 개인 집에서는 전역 탐색이 틀린 곳을 고를 수 있다.

## 주행 설정·장애물 입력

- Local/Global footprint는 공식 제품 치수인 **0.277m × 0.212m 사각형**이다.
  제조사 설정의 원형 반경 0.08m는 차체보다 작고, 이전의 원형 0.18m는 폭을 0.36m로
  취급해 좁은 통로를 막았다. Inflation **0.30m**은 모서리(0.174m) 바깥 완충 구간이며
  전부 통행 금지는 아니다. 추가 장착물은 실제 외곽선을 다시 측정해야 한다.
- 제조사 드라이버는 `/cmd_vel`을 **전후·횡 ±0.2m/s, 회전 ±0.5rad/s**로 자른다.
  DWB(제조사 값 0.4m/s, 1.0rad/s), velocity smoother, behavior 회전 상한을 모두 이
  값에 맞췄다. 가속도와 나머지 DWB 값은 제조사 설정과 같다.
- AMCL은 메카넘 차체에 맞는 `OmniMotionModel`을 사용한다. 자율 주행(DWB)은 횡이동을
  쓰지 않지만 조이스틱·수동 조작은 횡이동을 쓴다.
- LiDAR는 Local/Global 모두 표준 2D `ObstacleLayer`를 사용한다. 스캔 토픽과
  관측 거리·높이 필터는 유지한다. Depth 레이어는 위 지연 때문에 쓰지 않는다.
- LiDAR 입력은 드라이버가 발행한 LaserScan 그대로 사용한다. Malbut에서 점 개수,
  각도, 타임스탬프를 바꾸지 않는다. 드라이버의 `bins` 적용은 로봇에서 별도로 수행한다.

### 장애물 회피와 Collision Monitor

자율 주행은 공식 Nav2 launch와 같이 `/cmd_vel`을 직접 낸다. 수동 조작만 공식
`nav2_collision_monitor`를 거친다.

```text
controller(DWB) ─cmd_vel_nav→ velocity_smoother ─cmd_vel→ 드라이버
behavior_server(Spin·BackUp·Wait) ────────────────cmd_vel→ 드라이버
teleop_behavior_server(AssistedTeleop) ─cmd_vel_pre_collision→ collision_monitor ─cmd_vel→ 드라이버
```

- 자율 주행의 충돌 회피는 DWB가 한다. 차체는 costmap `footprint`(0.277×0.212 m 직사각형,
  `footprint_padding` 0.01 m)이다. `BaseObstacle` critic(제조사 설정)은 차체 중심 칸으로
  내접원(0.106 m)을 검사하고 벽에서 떨어지려는 점수를 주며, `ObstacleFootprint` critic은
  궤적 자세마다 회전한 외곽선이 장애물 칸에 닿으면 그 궤적을 버린다. 중심 칸만 보면
  앞뒤(0.139 m)와 모서리(0.174 m)가 검사되지 않기 때문이다. 외곽선 아래 팽창 비용은
  차체 여유 0.35 m 안에서 174~253으로 거의 같아 거리 점수로 쓸 수 없으므로, 이 critic의
  가중치는 거부 판정만 남도록 작게 둔다(0이면 critic을 건너뛴다).
- `FollowPath`의 `PreferForward` critic(Nav2 기본 제공)은 후진 궤적에만 벌점(1000)을 더한다.
  회전·전진은 벌점이 없고, 전진 궤적이 모두 장애물에 걸리면 여전히 후진한다. 1.7초 안에
  후진으로 얻는 거리 점수는 최대 약 1250이라 바로 뒤의 목표는 후진하고, 뒤쪽 사분면의
  목표는 돌아서 전진한다. 사람 추적의 후퇴는 이 벌점이 없는 복사본 `FollowPathReverse`로
  실행한다(사람을 보면서 곧게 물러나야 하므로).
- 도착 판정 `xy_goal_tolerance`는 0.12 m다(제조사 0.25 m는 차체 길이만큼 앞에서 멈추고,
  사람 추적의 0.90~1.10 m 거리 띠 밖에서 멈췄다). `GridBased.tolerance` 0.5 m는 목표 칸이
  벽·가구·사람 안이면 그 반경 안의 가장 가까운 갈 수 있는 칸으로 목표를 옮긴다.
- Spin·BackUp은 behavior 서버가 local costmap으로 앞을 검사한 뒤 움직인다.
- Collision Monitor 최소 구성: 다각형 하나(`FootprintApproach`, `approach`). 수동 조작 명령
  방향으로 차체 외곽(`/local_costmap/published_footprint`)을 1초 앞까지 옮겨 보고, LiDAR
  점과 닿기 전에 멈추도록 속도를 줄인다. 반대 방향으로 벗어나는 명령은 줄이지 않는다.
  외곽 안의 점 3개까지는 잡음으로 본다.
- 입력은 `/scan_raw`뿐이다. LiDAR보다 낮은 물체는 costmap·Collision Monitor 모두 못 본다.
- Humble 구현은 스캔이 `source_timeout`(1초)보다 오래되면 무시하고 명령을 통과시킨다.
  비상 정지가 아니다.
- AutoSLAM의 자체 정체 감지기는 없앴다. 막힌 목표는 Nav2 progress checker가 실패시키고
  AutoSLAM은 그 경계를 건너뛴다.

### Zone(진입 금지·우회 권장 구역)

저장 지도마다 `<지도이름>.zones.geojson`(형식 `malbut-semantic-zones-v1`)에 구역을
저장한다. `zone_filter` 노드가 선택된 지도의 구역을 Nav2 keepout 마스크로 바꿔
`zone_filter_mask_server`에 넣고, Local/Global costmap의 `KeepoutFilter`가 적용한다.

| 구역 | 마스크 값 | 주행 |
| --- | --- | --- |
| 진입 금지(`restricted`) | 100 + 0.2m 여유 | 통과 불가. 차체가 걸치지 않도록 여유를 더한다 |
| 우회 권장(`avoid`) | 70 | 통과 가능하지만 비용이 높아 가능하면 피한다 |
| 통행 허용(`allow`) | 0 | 비용 변화 없음(기존 형식 호환) |

- 구역은 지도 YAML·이미지 해시와 함께 저장된다. 같은 이름으로 새로 만든 지도에는
  적용하지 않고 오류로 알린다.
- 지도 작성 중이거나 전환 중에는 빈 마스크를 넣어 이전 지도의 구역이 남지 않게 한다.
- 파일이 바뀌면 1초 안에 다시 적용한다. 상태는 `/malbut/zones/state`(JSON)에 나온다.
- 서비스 웹 **로봇 기능 → 구역 편집**에서 꼭짓점을 찍어 그리고 **구역 저장·주행에 반영**을
  누른다. 로봇 브리지가 이 파일에 저장하고, 저장 지도 업로드에 구역을 함께 싣는다.
- 이전 Gazebo 구현(`malbut_gazebo/zone_filter_mask.py`)의 구역 형식과 비용을 옮겼다.
  벽 주변 비용은 로봇의 inflation이 맡으므로 넣지 않았다.

## 수동 조작

수동 조작은 `manual_drive` 기능이다. 관리자가 Nav2 `AssistedTeleop`(`/assisted_teleop`,
별도 `teleop_behavior_server`)을 실행하고, `/cmd_vel_teleop` 입력을 local costmap 기준으로
충돌 검사해 감속·정지한 뒤 Collision Monitor를 거쳐 `/cmd_vel`로 보낸다. 자율 주행 명령은
이 경로를 거치지 않는다. 새 ROS 인터페이스는 없다.

- **조작하면 시작하고, 5초간 조작이 없으면 끝난다.** `manual_control` 노드가
  `/cmd_vel_teleop`의 움직임 명령을 받으면 `manual_drive`를 요청하고, 조작이 멈추고
  5초가 지나면 `/preempt_teleop`으로 끝낸다. Nav2가 로봇을 정지한다.
  CLI·웹으로 직접 시작한 세션도 같은 규칙으로 끝난다.
- 제조사 조이스틱 노드는 스틱이 바뀔 때만 명령을 보내므로, 같은 방향으로 계속 잡고
  있는 동안은 보드의 `/ros_robot_controller/joy`에서 스틱 기울기를 확인해 조작 중으로
  본다. 보드가 스틱 값을 변화 시에만 보내는 펌웨어라면 5초 이상 같은 방향으로 잡고
  있을 때 멈출 수 있으며, 스틱을 다시 움직이면 다시 시작한다.
- 우선순위 `HIGH`, 자원 `BASE`. 실행 중인 추적·순찰·목적지 이동·위치 보정(NORMAL)을
  취소한 뒤 시작하고, 수동 조작 중에는 NORMAL 이동 요청을 거부한다. 이 동안
  `/malbut/state`의 `control_mode`는 `MANUAL`이다. 지도 선택 여부와 무관하지만, 위치
  추정 전환 중에는 받지 않으며 조작을 계속하면 전환이 끝난 뒤 시작된다.
- 입력: Bringup이 하드웨어를 직접 켜면 제조사 조이스틱을 `use_joy:=false`로 끄고,
  같은 제조사 노드(0.15m/s, 0.45rad/s)를 `/cmd_vel_teleop`로 연결해 다시 실행한다.
  그래서 조이스틱 명령은 Nav2 명령과 섞이지 않는다. 외부 하드웨어를 재사용하는
  `start_hardware:=false`에서는 제조사 조이스틱이 기존처럼 드라이버를 직접 움직인다.
- 서비스 웹의 수동 조작 버튼도 같은 입력을 사용한다. 버튼 한 번이 한 걸음(0.15m/s 또는
  0.5rad/s로 0.8초)이며, 로봇 브리지는 `control_mode`가 `MANUAL`이 된 뒤에 움직이고
  스스로 0을 보낸다. 3초 안에 수동 조작이 시작되지 않으면(예: 위치 추정 전환 중) 버린다.
- 조이스틱은 놓으면 0을 보낸다. 입력이 끊긴 채 남은 마지막 명령도 5초 뒤 수동 조작
  종료로 정지한다.

직접 시작하려면(5초 안에 조작하지 않으면 끝남):

```bash
ros2 action send_goal /malbut/mission/execute \
  malbut_interfaces/action/ExecuteMission \
  "{capability_id: manual_drive, arguments_yaml: '{}'}" --feedback
```

## Mac에서 웹으로 확인

현재는 서비스 웹(malbut_web)을 사용한다. 아래 LAN 테스트 패널은 남겨 두었지만 사용하지 않는다.

단독 `ros2 run malbut_bringup robot_web_panel` 실행 시 Mac에서
`http://<로봇-IP>:8766` 접속 후 로봇 터미널의 접근 토큰을 입력한다.
저장 지도 선택·Bringup 시작/종료, 원본/인식 영상, 추적 상태,
AutoSLAM·사람추적·순찰·수동 조작 실행·취소와 Zone 편집을 제공한다. 실시간 2D 지도는
`/global_costmap/costmap`(장애물·팽창·구역 비용 포함),
로봇 위치·방향은 TF에서 가져오며 지도를 클릭해 이동하거나 초기화하지 않는다.
새 이미지와 지도 좌표 정보가 함께 준비되면 화면을 교체한다. 갱신 지연·실패 시
기존 지도는 유지하고 수신 상태만 알린다. 저장하는 지도 원본은 기존 `/map`이다.

기존 Bringup에 `web_panel:=true`로 포함한 패널은 영상·지도·미션 요청만 제공하고
Bringup 시작/종료는 비활성화한다. 단독 패널과 같은 포트로 중복 실행하지 않는다.
브라우저 종료/연결 끊김은 정지가 아니며, 미션 취소 버튼은 이 서버가 보낸 Goal만 취소한다.
자세한 실행·안전 범위는 [웹 테스트 안내](README_WEB.md)를 참고한다.

## 준비 확인과 미션 요청

- 모든 Malbut 노드는 `use_sim_time=false`. 제조사 include에도 이를 전달한다.
- 관리자는 Bringup과 함께 바로 시작해 위치 추정을 켜지만, 준비 검사기가
  `/malbut/bringup/status`로 READY를 보내기 전에는 `BOOTING`으로 미션을 거부한다.
- 준비 검사기는 최근 Scan·Odometry·RGB·Depth·CameraInfo와 TF를 확인한다.
  Scan은 최신 TF의 존재만 보지 않고, 최근 서로 다른 두 스캔의 원본 시각에
  `LiDAR → odom`, `LiDAR → map` 변환이 가능한지 확인한다.
  최신 스캔보다 TF가 조금 늦게 도착해도 이전의 최근 헤더로 비동기 재확인한다.
  인식을 켰다면 3D 인식 결과 수신도 확인한다. 사람이 없는 빈 검출도 정상이다.
- 지도·costmap, `map ↔ base` TF, Nav2 lifecycle `ACTIVE`(controller·planner·behavior·
  teleop_behavior·bt_navigator·velocity_smoother·collision_monitor)와 Nav2·순찰·수동 조작·AutoSLAM·
  위치 보정(인식을 켰다면 추적) Action 서버를 확인한다. AMCL·map_server는 관리자가
  켜고 끄므로 lifecycle 대상이 아니다. 저장 지도로 시작했다면 위치 보정이 끝나야
  `map` TF가 생긴다.
- 빠진 항목을 로그로 표시하며, 정해진 몇 초가 지났다는 이유로 READY를 보내지 않는다.
  `sensor_timeout_s`(기본 3초)는 센서의 신선도 기준이지 부팅 제한시간이 아니다.
- 준비 검사는 부팅 시 한 번이다. 이후 지도 전환의 상태는 `/malbut/localization/state`로 본다.
- 검사기는 지속 안전감시·비상정지가 아니다.
- 소유한 Malbut 프로세스가 종료되거나 자식 프로세스가 오류 종료하면 launch를
  종료한다. Ctrl+C도 이 launch가 실행한 프로세스에만 전달한다.
  이것만으로 모터 정지를 보장하지는 않는다. 제조사 드라이버는 `/cmd_vel` 수신이
  끊겨도 마지막 속도를 유지하는 코드이므로 실제 정지 동작은 하드웨어 검증 대상이다.
  Collision Monitor도 입력이 끊기면 아무것도 보내지 않으므로 이를 대신하지 않는다.
  이미 외부에서 실행하던 드라이버는 종료하지 않는다.

준비 이후 요청하기 전에는 추적/순찰을 시작하지 않는다.

```bash
ros2 action send_goal /malbut/mission/execute \
  malbut_interfaces/action/ExecuteMission \
  "{capability_id: patrol, arguments_yaml: '{thoroughness: 0}'}" --feedback
```

이것은 실제 이동 요청이므로, 최초 실행은 안전한 공간에서 운영자가 정지 수단을
확보한 뒤 한다. 수동 조작은 위 `manual_drive`로 요청하며, 비상 정지 연동은 아직 없다.

## 검증 범위

로컬에서는 launch 조합·인자·패키징과 준비 조건을 검증한다. 모의 입력 검증은
실로봇 검증이 아니다. 실제 로봇의 드라이버 경로, TF/토픽, RGB-D 정렬,
Jetson GPU 동작, 실지도 초기 위치, Nav2 설정 호환성, 이동·정지는 실기기에서
확인해야 한다. 로컬에서 확인하지 못한 실기기 성공을 주장하지 않는다.
