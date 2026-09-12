# 실로봇 적용본 — malbut_test

기존 기능을 ROSOrin / Jetson Orin NX / ROS 2 Humble에 연결하는 복사본이다.
**Git 저장소 전체를 `~/ros2_ws/src/malbut`에 clone한 뒤 이 폴더를 선택해 빌드한다.**
제조사 패키지와 원본 Malbut 패키지는 수정하지 않는다. 새 드라이버·응용 기능을
구현하거나 시뮬레이션 지도를 실기기에 대신 넣는 구성은 아니다.

복사 기준은 `cc5208a` + SWM25-169 Bringup이다. 추적·순찰·관리자의 알고리즘과
공개 인터페이스는 유지한다. 이후 실기기 연결 수정은 이 폴더 안에서만 한다.
원본 변경이 자동 동기화되지는 않는다. 실기기 수정분 위에 통째로 덮어쓰지 않는다.

## 구조와 빌드 경계

```text
~/ros2_ws/
├── src/
│   ├── slam/, navigation/, peripherals/, ...   # 기존 제조사 코드
│   └── malbut/                                # Git clone 위치
│       ├── 기존 패키지들/
│       └── malbut_test/
│           ├── malbut_bringup/
│           ├── malbut_interfaces/
│           ├── malbut_system_manager/
│           ├── malbut_yolo/
│           │   └── vendor/yolo_ros/            # 함께 포함된 upstream 소스
│           ├── malbut_reid/
│           ├── malbut_tracking/
│           ├── malbut_patrol/
│           ├── malbut_autoslam/
│           ├── build.sh
│           └── COLCON_IGNORE
├── build/malbut_test/                          # 이 복사본의 빌드 결과
├── install/malbut_test/                        # 이 복사본의 설치 결과
└── log/malbut_test/
```

`COLCON_IGNORE`는 **삭제하지 않는다.** 기본 colcon 탐색에서 원본과 복사본의
패키지 이름이 겹치지 않게 한다. `build.sh`는 이 안의 8개 패키지와 포함된
`yolo_ros`, `yolo_msgs` 경로를 직접 지정한다. 제조사 패키지를 재빌드하거나
제조사의 `install/setup.zsh`를 덮어쓰지 않는다. 패키지명은 그대로 유지한다.

Gazebo·actor·시나리오·벤치마크·웹·음성 기능은 포함하지 않는다. 제조사 하드웨어
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
# 현재 제조사 이미지의 navigation 하위 launch는 소스 폴더에만 있다.
export need_compile=False
ros2 pkg prefix slam
ros2 pkg prefix navigation
```

이 작업 브랜치가 **원격에 반영된 뒤**, 아직 clone하지 않은 경우:

```zsh
cd ~/ros2_ws/src
git clone --branch feat/SWM25-169-robot-bringup \
  https://github.com/SWM-malbut/malbut.git malbut
```

PR이 main에 병합된 뒤에는 `--branch ...`를 생략한다. 기존 clone이 있다면
그 저장소의 변경 여부부터 확인하고 해당 브랜치를 받는다. 새 clone으로 덮어쓰지 않는다.

ROS 의존성을 준비한다. 없는 도구는 `python3-rosdep`,
`python3-colcon-common-extensions`, `python3-venv`,
`python3-pip` 패키지로 준비한다. ROS/Gazebo 설치기를 다시 실행하지 않는다.

```zsh
cd ~/ros2_ws
rosdep update
rosdep install --from-paths src/malbut/malbut_test/malbut_* \
  src/malbut/malbut_test/malbut_yolo/vendor/yolo_ros/{yolo_ros,yolo_msgs} \
  --ignore-src -r -y \
  --skip-keys 'python3-torchvision-pip python3-ultralytics-pip'
bash src/malbut/malbut_test/build.sh
source ~/ros2_ws/install/malbut_test/local_setup.zsh
```

위 경로 밖의 원본·제조사·시뮬레이션 패키지는 빌드 대상으로 잡지 않는다.
YOLO 소스도 적용본 안에 있으므로 별도로 다운로드하지 않는다.
메모리 부족 시 빌드 명령 뒤에 `--parallel-workers 1`을 붙일 수 있다.

새 터미널마다 위 제조사 환경과 `need_compile=False`를 설정한 다음
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

현재 실제 지도가 없으므로 센서 모드가 기본이다. 지도 작성 단계에서
실행 환경이 **실시간 SLAM 지도·TF·Nav2를 제공한 뒤** 자동 탐색 서버를 켠다.
이 서버는 [malbut_autoslam](malbut_autoslam/README.md)이며 SLAM·Nav2 자체를
기동하지 않는다. 저장 지도를 요구하는 `mode:=navigation`은 지도 작성 후 사용한다.

```zsh
ros2 launch malbut_autoslam autoslam.launch.py use_sim_time:=false
```

다른 터미널에서 요청하면 실제 로봇이 탐색을 시작한다:

```zsh
ros2 action send_goal /autoslam malbut_interfaces/action/AutoSlam \
  "{map_name: home}" --feedback
```

기본 저장 폴더는 Git 밖의 `~/.ros/malbut/maps`이며 서버의 `map_directory`로
변경한다. 이미 있는 이름은 거부하므로 새 이름으로 요청한다.
성공 결과의 `map_yaml`을 이후 Bringup의 `map` 인자로 사용한다. YAML과
이미지가 모두 필요하다. SLAM과 Bringup이 같은 드라이버를 중복 실행하지 않게
종료/재사용한다. 관리자를 통해 요청하려면 관리자뿐 아니라 `/autoslam` 서버도
먼저 실행되어 있어야 한다. 센서/Navigation Bringup에서 자동으로 켜지는 서버는 아니다.

## 3. 공유 인식 준비

ROS 빌드와 GPU 패키지 설치는 별개다. 다음은 로봇에서 필요한 최초 준비이며
Bringup이 자동 실행하지 않는다. 이미 준비된 모델은 그대로 재사용할 수 있다.

```zsh
cd ~/ros2_ws/src/malbut/malbut_test
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
  mode:=navigation map:="$HOME/.ros/malbut/maps/home.yaml"
```

공식 RViz에서 실제 초기 위치를 설정한다. 센서·TF·Nav2와 응용 서버가 준비되면
관리자가 시작된다. 준비 검사기는 **부팅 확인용**이며 주행 안전감시기를 대체하지 않는다.
아래 요청 전에는 로봇이 자동으로 순찰/추적을 시작하지 않는다.

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
복사했다. 변경은 기본 BT에 필요한 Spin/Wait/BackUp 활성화, 잘못된 숫자 표기
수정, 임의의 초기 위치(0, 0, 0) 자동 설정 해제뿐이다. 실제 초기 위치는 RViz에서
지정한다. 제조사 속도·가속도·costmap 설정은 유지한다.
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
