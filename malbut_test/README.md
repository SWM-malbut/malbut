# 실로봇 적용본 — malbut_test

기존 기능을 ROSOrin / Jetson Orin NX / ROS 2 Humble에 연결하는 복사본이다.
**아래 명령은 이 폴더의 내용을 로봇의 `~/ros2_ws/src/malbut`으로 복사한 경우다.**
개발 수정은 저장소 루트의 원본 패키지에서 하고 이 적용본에도 반영한다.
제조사 원본은 수정하지 않으며 시뮬레이션 지도를 실기기에 대신 넣지 않는다.
추적·순찰·관리자의 알고리즘과 공개 인터페이스는 유지한다.

단위 테스트는 저장소의 원본 패키지 `test/`에서만 관리한다. 이 적용본에 같은
pytest 파일을 복제하지 않는다. 복사본의 빌드 경계는 원본 Bringup의
`test/test_deployment.py`에서 확인하며, 실기기 웹 패널과 수동 GPU 검사는 유지한다.

## 구조와 빌드 경계

```text
~/ros2_ws/
├── src/
│   ├── slam/, navigation/, peripherals/, ...   # 기존 제조사 코드
│   └── malbut/                                # malbut_test 내용의 복사 위치
│       ├── malbut_bringup/
│       ├── malbut_interfaces/
│       ├── malbut_system_manager/
│       ├── malbut_yolo/
│       │   └── vendor/yolo_ros/                # 함께 포함된 upstream 소스
│       ├── malbut_reid/
│       ├── malbut_tracking/
│       ├── malbut_patrol/
│       ├── malbut_autoslam/
│       ├── build.sh
│       └── COLCON_IGNORE
├── build/malbut_test/                          # 이 복사본의 빌드 결과
├── install/malbut_test/                        # 이 복사본의 설치 결과
└── log/malbut_test/
```

`COLCON_IGNORE`는 **삭제하지 않는다.** 기본 colcon 탐색에서 원본과 복사본의
패키지 이름이 겹치지 않게 한다. `build.sh`는 이 안의 8개 패키지와 포함된
`yolo_ros`, `yolo_msgs` 경로를 직접 지정한다. 제조사 패키지를 재빌드하거나
제조사의 `install/setup.zsh`를 덮어쓰지 않는다. 패키지명은 그대로 유지한다.

Gazebo·actor·시나리오·벤치마크·기존 홈카메라 웹·음성 기능은 포함하지 않는다.
실기기용 간단한 웹 테스트 패널은 Bringup에 포함한다. 제조사 하드웨어
launch가 차체·센서·로봇 description과 TF를 제공하므로 시뮬레이션용 description을
별도로 실행하지 않는다. 순찰은 기존 `malbut_autonomy/malbut_patrol`의 복사본이다.

## 1. 로봇 환경과 소스 준비

실기기에서 확인된 값: **Zsh, L4T R36.4.7, Python 3.10.12,
PyTorch 2.8.0, torchvision 0.23.0, CUDA available=True**.
`slam`, `navigation`의 설치 경로도 확인됐다. 기존 PyTorch와 ROS를 재설치하지 않는다.
CUDA 인식 확인은 아직 실제 YOLO/OSNet 추론 검증을 의미하지 않는다.

로봇의 Zsh 터미널에서:

```zsh
source /opt/ros/humble/setup.zsh
source ~/ros2_ws/install/setup.zsh
ros2 pkg prefix slam
ros2 pkg prefix navigation
```

Git 저장소는 별도 위치(예: `~/malbut`)에서 받고 그 안의 `malbut_test` 내용을
위 위치에 복사한다. 전체 저장소를 `src/malbut`에 두는 방식도 지원하지만, 그때는
아래 **소스 명령 경로에만** `/malbut_test`를 추가한다. 설치 경로는 동일하다.
Bringup이 제조사 실행에 필요한 `need_compile=False`를 자체 설정한다.

ROS 의존성을 준비한다. 없는 도구는 `python3-rosdep`,
`python3-colcon-common-extensions`, `python3-venv`,
`python3-pip` 패키지로 준비한다. ROS/Gazebo 설치기를 다시 실행하지 않는다.

```zsh
cd ~/ros2_ws
rosdep update
rosdep install --from-paths src/malbut/malbut_* \
  src/malbut/malbut_yolo/vendor/yolo_ros/{yolo_ros,yolo_msgs} \
  --ignore-src -r -y --rosdistro humble \
  --skip-keys 'ament_python python3-torchvision-pip python3-ultralytics-pip'
bash src/malbut/build.sh
source ~/ros2_ws/install/malbut_test/local_setup.zsh
```

위 경로 밖의 원본·제조사·시뮬레이션 패키지는 빌드 대상으로 잡지 않는다.
YOLO 소스도 적용본 안에 있으므로 별도로 다운로드하지 않는다.
메모리 부족 시 빌드 명령 뒤에 `--parallel-workers 1`을 붙일 수 있다.

새 터미널마다 위 제조사 환경을 설정한 다음
`source ~/ros2_ws/install/malbut_test/local_setup.zsh`를 실행한다.
`ros2 pkg prefix malbut_bringup` 결과가 **`install/malbut_test/malbut_bringup`**
아래인지 확인한다. 원본 패키지 경로라면 잘못된 overlay를 사용 중이다.

## 2. 센서 연결과 실제 지도

사용자가 보낸 실기기 topic 목록과 기본값은 일치한다:
`/scan_raw`, `/odom`, `/depth_cam/rgb0/image_raw`,
`/depth_cam/depth0/image_raw`, `/depth_cam/rgb0/camera_info`.
Header의 frame·시각과 RGB-D 정렬은 추가 실기기 확인 대상이다.
제조사 `robot.launch.py`의 루트 네임스페이스 인자는 빈 문자열이 아닌 `/`다.
Bringup도 `robot_name=/`, `master_name=/`를 전달한다.

공식 앱이 센서·차체를 이미 실행 중이면 중복해서 켜지 않는다. 기존 드라이버를
재사용할 때는 `start_hardware:=false`, 직접 Bringup이 켜도록 할 때는 기존
공식 실행을 운영자가 종료한 후 아래 기본 실행을 사용한다. 동시에 실행하지 않는다.

```zsh
ros2 launch malbut_bringup robot.launch.py perception:=false
```

실제 지도가 없다면 센서-only 실행을 종료하고 다음 지도 작성 모드를 사용한다.
아직 이동하지 않으며 [AutoSLAM](malbut_autoslam/README.md) 서버와 웹 패널만 켠다.

```zsh
ros2 launch malbut_bringup robot.launch.py mode:=mapping web_panel:=true
```

Mac에서 `http://<로봇-IP>:8766` 접속 → 터미널의 Access token 입력 →
**자동 지도 만들기 시작**. 또는 다른 터미널에서:

```zsh
ros2 action send_goal /autoslam malbut_interfaces/action/AutoSlam \
  "{map_name: home2}" --feedback
```

Goal을 받으면 누락된 차체·센서·SLAM·Nav2·스캔 정규화기를 기동하고 준비된 뒤
탐색한다. 이미 온전히 실행 중인 구성은 재사용한다. AMCL/저장 지도 모드와
동시 실행하거나 중복·불완전한 드라이버 구성은 거부한다. 종료 시 자기가 켠
구성만 정리한다. 외부 실행을 임의로 죽이지 않는다.

기본 저장 폴더는 Git 밖의 `~/.ros/malbut/maps`이며 서버의 `map_directory`로
변경한다. 이미 있는 이름은 거부하므로 새 이름으로 요청한다.
성공 결과의 `map_yaml`을 이후 Bringup의 `map` 인자로 사용한다. YAML과
이미지가 모두 필요하다. 관리자를 통해 요청하려면 관리자와 `/autoslam` 서버를
함께 실행한다. 저장 지도 Navigation 모드와 실시간 SLAM은 동시에 사용하지 않는다.
준비된 외부 SLAM을 그대로 쓸 때는 기존 scan 설정도 유지되므로, 스캔 정규화
수정까지 적용하려면 기존 SLAM을 종료하고 위 Mapping 모드로 새로 시작한다.

## 3. 공유 인식 준비

ROS 빌드와 GPU 패키지 설치는 별개다. 다음은 로봇에서 필요한 최초 준비이며
Bringup이 자동 실행하지 않는다. 이미 준비된 모델은 그대로 재사용할 수 있다.

```zsh
cd ~/ros2_ws/src/malbut
bash malbut_yolo/scripts/prepare_runtime.sh
bash malbut_reid/scripts/prepare_osnet_model.sh
bash malbut_reid/scripts/prepare_inference_runtime.sh
```

- YOLO: 기존 Jetson PyTorch/torchvision을 유지하는 `~/.cache/malbut_yolo/runtime`.
- ReID: `~/.cache/malbut_reid/runtime`. 로봇의 공용 NumPy·ONNX Runtime을 제거하거나 변경하지 않는다.
- OSNet 모델 변환: 별도 CPU export 환경. 로봇의 GPU Torch와 별개다.
- 모델: `~/.cache/malbut_perception/yolo26n.pt`, `osnet_ain_x1_0_msmt17.onnx`.
- `XDG_CACHE_HOME`, `MALBUT_YOLO_RUNTIME`, `MALBUT_REID_RUNTIME`을 바꾸면
  설치와 실행 양쪽에서 동일하게 설정한다. 기본값을 쓰면 별도 설정은 필요 없다.

OSNet 준비 중 GPU provider가 표시되더라도 실제 모델 추론까지 성공한 것은 아니다.
실제 실행 로그와 검출 결과를 확인한다. PC의 venv나 TensorRT 엔진은 로봇에 복사하지 않는다.
자세한 내용은 [YOLO](malbut_yolo/README.md), [ReID](malbut_reid/README.md)를 참고한다.

기존 센서-only 실행을 종료한 뒤:

```zsh
ros2 launch malbut_bringup robot.launch.py publish_debug_image:=true
```

## 4. 저장 지도로 Bringup 및 기능 요청

이전 실행을 종료하고:

```zsh
ros2 launch malbut_bringup robot.launch.py \
  mode:=navigation map:="$HOME/.ros/malbut/maps/home2.yaml" \
  publish_debug_image:=true web_panel:=true
```

같은 지도의 마지막 AMCL 위치가 있으면 초기 추정치로 한 번 복원한다. 위치는
5초 주기로 `~/.ros/malbut/localization/last_pose.yaml`에 저장한다. 최초 실행이거나
로봇을 꺼 둔 동안 옮겼다면 RViz의 **2D Pose Estimate**로 실제 위치를 지정한다.
지도 YAML·이미지가 바뀌면 이전 위치는 복원하지 않는다.
센서·TF·Nav2와 응용 서버가 준비되면
관리자가 시작된다. 준비 검사기는 **부팅 확인용**이며 주행 안전감시기를 대체하지 않는다.
아래 요청 전에는 로봇이 자동으로 순찰/추적을 시작하지 않는다.
같은 웹 주소에서 영상 확인·추적·순찰·이 패널의 요청 취소가 가능하다.
웹 취소는 비상 정지가 아니며, 브라우저 닫기나 Wi-Fi 끊김으로 주행이 정지하지 않는다.

```zsh
ros2 action send_goal /malbut/mission/execute \
  malbut_interfaces/action/ExecuteMission \
  "{capability_id: follow_person, arguments_yaml: '{target_mode: 0, desired_distance_m: 1.0}'}" \
  --feedback
```

```zsh
ros2 action send_goal /malbut/mission/execute \
  malbut_interfaces/action/ExecuteMission \
  "{capability_id: patrol, arguments_yaml: '{thoroughness: 0}'}" --feedback
```

관리자를 통해 실행한 모든 미션 취소:

```zsh
ros2 service call /malbut/mission/execute/_action/cancel_goal \
  action_msgs/srv/CancelGoal '{}'
```

취소 수락과 실제 정지 완료는 다르다. 정지를 확인한 후 Bringup을 종료한다.
최초 실기기 시험은 별도 정지 수단을 확보한 안전한 공간에서 한다.

## 변경할 설정과 남은 확인

센서·제조사 launch 연결은 `malbut_bringup/launch/robot.launch.py` 인자로 지정한다.
추적의 Nav2 planner/controller/goal-checker ID와 여유값은
`malbut_tracking/config/person_following.yaml`을 실제 Nav2와 대조한다.
Nav2 공통 설정은 로봇에서 받은 파일을 `malbut_bringup/config/nav2_params.yaml`로
복사했다. 기본 BT의 Spin/Wait/BackUp, 숫자 표기와 임의의 원점 초기화를 정리했고,
이번 실물 설정은 다음과 같다:

- Local·Global 차체 반경 `0.18m`, 팽창 반경 `0.20m`.
- 속도 smoother를 제조사 DWB와 동일한 전후 `0.4m/s`, 회전 `1.0rad/s` 및
  가감속 제한으로 일치. 제조사 DWB의 횡이동 비활성 설정은 유지.
- `/scan_raw`를 실제 각도 기준으로 `/scan_normalized`의 일정한 격자로 변환.
  SLAM·AMCL·Nav2·사람 추적에서 사용. 빈 방향을 자유 공간으로 만들지 않는다.
- LiDAR는 Local/Global 모두 표준 2D ObstacleLayer 사용.
  스캔 토픽·관측 범위는 유지하며 LiDAR 전용 Voxel 저장·발행은 제거.
- 두 costmap에 `/depth_cam/depth0/points` 기반 별도 VoxelLayer 연결.
  바닥 위 `0.05~0.20m` 점을 장애물로 표시하며 바닥 관측은 지우기에만 사용.
  사용자 확인 기본 구성과 제조사 높이 `0.166m`에 약 `0.034m` 여유를 둔
  초기 상한이다. 실측·TF 확인이 끝난 값은 아니며 추가 장착 시 재검토한다.
  Depth 저장 공간은 `0.03m × 16층 = 0.48m`다. Depth의
  `-0.05~0.48m` 제거용 관측은 높은 선반을 장애물로 표시하지 않는다.

5cm 바닥 기준은 실제 카메라 TF·바닥 높이로 검증할 필요가 있다. 5cm 미만,
카메라 사각·최소 측정 거리·유리/반사체까지 검출된다는 의미는 아니다.
스캔 정규화도 잘못된 드라이버 각도·TF·오도메트리까지 교정하지는 않는다.
검토한 외부 binning 필터는 Humble 배포 여부와 동작 차이 때문에 그대로 교체하지 않았다.
기존 구현 비교·Depth 표시/제거 설정의 근거는
[Bringup 설명](malbut_bringup/README.md#기존-구현-검토)에 정리했다.
다른 검토한 복사본은 `nav2_params_file`로 지정할 수 있다.
현재 제조사 설치 폴더에는 navigation 하위 launch가 누락되어 있으므로
`navigation_launch_file` 기본값은 기존
`~/ros2_ws/src/navigation/launch/include/bringup.launch.py`다. 제조사 원본은 수정하지 않는다.
알고리즘을 바꾸거나 임의의 TF/지도/초기 위치를 만들어 맞추지 않는다.

제공받은 하드웨어·상위/하위 navigation launch, 공통 YAML, DWB/TEB YAML을
대조했다. DWB 선택 및 공통 설정 전달을 유지하고 Nav2 출력도 제조사 그대로
`/cmd_vel_nav → velocity_smoother → /cmd_vel`을 사용한다.
Spin/BackUp은 제조사 구성대로 `/cmd_vel`에 직접 출력한다.
원본의 controller 전용 YAML만 읽는 구조도 변경하지 않는다. 내부 costmap은
컨테이너에 전달된 공통 YAML을 사용한다.

실제 이동 전에는 `/cmd_vel`의 차체 측 구독자가 있는지 확인한다. Topic 이름이
보이는 것만으로 모터까지 연결되었다고 판단하지 않는다.

```zsh
ros2 topic info /cmd_vel --verbose
ros2 topic info /controller/cmd_vel --verbose
```

RGB-D 정렬·TF, 실제 GPU 추론, 저장 지도·초기 위치, 명령 수신·이동·정지는
아직 실기기 검증이 필요하다.
로컬 빌드/모의 검사가 통과했다고 이 항목까지 검증된 것은 아니다.
