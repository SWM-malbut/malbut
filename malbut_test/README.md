# 실로봇 적용본 — malbut_test

기존 기능을 ROSOrin / Jetson Orin NX / ROS 2 Humble에 연결하는 복사본이다.
**아래 명령은 이 폴더의 내용을 로봇의 `~/ros2_ws/src/malbut`으로 복사한 경우다.**
개발 수정은 저장소 루트의 원본 패키지에서 하고 이 적용본에도 반영한다.
제조사 원본은 수정하지 않으며 시뮬레이션 지도를 실기기에 대신 넣지 않는다.
추적·순찰·관리자의 알고리즘과 공개 인터페이스는 유지한다.

단위 테스트는 저장소의 원본 패키지 `test/`에서만 관리한다. 이 적용본에 같은
pytest 파일을 복제하지 않는다. 복사본의 빌드 경계는 원본 Bringup의
`test/test_deployment.py`에서 확인하며, 실기기 웹 패널과 수동 GPU 검사는 유지한다.

### 낙상 감지 포팅 시 추가 준비

낙상 기능을 설정한 경우 Bringup은 VLM과 `malbut_fall_pose`를 각각 한 번 시작한다.
`homecam_detector`도 빌드 목록에 포함한다. Pose는 영상 저장 ON/OFF가 아니라
낙상 감지 설정·카메라 허용·VLM 실행 상태를 보고 동작한다.

VLM 설정·Cloud 키 외에 YOLO26s pose ONNX 모델과 실행 환경을 별도로 준비해야 한다.
`bash homecam_agent/scripts/prepare_fall_pose_runtime.sh`로 Pose 전용 Python 환경을
만들 수 있다. 이 명령은 모델을 내려받거나 카메라·Cloud를 실행하지 않는다.
기본 모델 경로, 실행 인자와 테스트 순서는
[낙상 감지 로봇 실행 준비](malbut_agent_server/docs/fall_robot_preparation.md)를 따른다.
PC에서 연결 테스트를 통과해도 Jetson 성능과 카메라 수신이 검증된 것은 아니다.

## 구조와 빌드 경계

```text
~/ros2_ws/
├── src/
│   ├── slam/, navigation/, peripherals/, ...   # 기존 제조사 코드
│   └── malbut/                                # malbut_test 내용의 복사 위치
│       ├── malbut_bringup/
│       ├── malbut_interfaces/
│       ├── malbut_agent_server/
│       ├── malbut_stt/
│       ├── malbut_tts/
│       ├── malbut_system_manager/
│       ├── malbut_yolo/
│       │   └── vendor/yolo_ros/                # 함께 포함된 upstream 소스
│       ├── malbut_reid/
│       ├── malbut_tracking/
│       ├── malbut_patrol/
│       ├── malbut_autoslam/
│       ├── homecam_agent/                     # 실제 카메라 → AWS KVS
│       ├── malbut_web/                        # AWS에 배포하는 서비스 웹
│       ├── setup.sh                          # 최초 의존성·음성 소스·모델 준비
│       ├── build.sh
│       └── COLCON_IGNORE
├── build/malbut_test/                          # 이 복사본의 빌드 결과
├── install/malbut_test/                        # 이 복사본의 설치 결과
└── log/malbut_test/
```

`COLCON_IGNORE`는 **삭제하지 않는다.** 기본 colcon 탐색에서 원본과 복사본의
패키지 이름이 겹치지 않게 한다. `build.sh`는 홈캠 영상 노드와 필요한 KVS SDK,
11개 로봇·음성 패키지와 포함된 `yolo_ros`, `yolo_msgs`를 한 번에 빌드한다.
경로를 직접 지정하므로 제조사 패키지를 재빌드하거나
제조사의 `install/setup.zsh`를 덮어쓰지 않는다. 패키지명은 그대로 유지한다.

Gazebo·actor·시나리오·벤치마크는 포함하지 않는다.
STT·Agent·TTS 음성 기능과 실기기용 간단한 웹 테스트 패널은 포함한다.
`build.sh` 하나가 음성 런타임·STT CUDA 라이브러리와 ROS 패키지를 빌드하고,
`robot.launch.py` 하나가 로봇 전체와 STT·Agent·TTS를 기본으로 함께 실행한다.
실행 모드는 없으며, 저장 지도 선택 여부만 실행 중에 바뀐다
([실행 구성](malbut_bringup/README.md#실행-구성)).
홈캠 영상 전송도 위 빌드에 포함한다. 서비스 웹 자체는 AWS에 별도 배포한다.
Cloud VLM도 설정 파일이 준비되면 센서 준비 후 함께 시작한다. 설정 경로와
감지·전송 전 대기 조건은 [Bringup 안내](malbut_bringup/README.md#cloud-vlm-자동-실행)를 따른다.
클라우드 연결과 Bringup을 통한 영상 실행은 [README_CLOUD.md](README_CLOUD.md)를 따른다.
제조사 하드웨어 launch가 차체·센서·로봇 description과 TF를 제공하므로 시뮬레이션용 description을
별도로 실행하지 않는다. 순찰은 기존 `malbut_autonomy/malbut_patrol`의 복사본이다.

## 1. 로봇 환경과 소스 준비

실기기에서 확인된 값: **Zsh, L4T R36.4.7, Python 3.10.12,
PyTorch 2.8.0, torchvision 0.23.0, CUDA available=True**.
`slam`, `navigation`의 설치 경로도 확인됐다. 기존 PyTorch와 ROS를 재설치하지 않는다.
이 기록과 현재 설정 기준으로 CUDA를 사용하는 구성이다. YOLO는 PyTorch CUDA,
STT는 whisper.cpp CUDA를 사용하며 Agent와 TTS는 기본 OpenAI API 방식이다.
이 기록은 이번 변경의 실기기 검증 결과가 아니며 실제 동시 GPU 추론은 별도 확인해야 한다.

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

최초 준비와 의존성 변경 때는 `setup.sh`를 실행한다. 필요한 OS 개발 패키지와
홈캠 의존성을 설치하고, 명시한 로봇 패키지 경로만 `rosdep`으로 준비한다.
`homecam_media_agent`와 로컬 `homecam_detector`도 함께 탐색한다.
음성용 고정 버전 whisper.cpp 소스와 SHA-256을 검증한 다국어 small 모델도 준비한다.
현재 JetPack과 맞는 CUDA toolkit 및 빌드 터미널의 `nvcc`는 미리 있어야 한다.
필요한 ROS 패키지 의존성은 설치하며, JetPack·CUDA·PyTorch를 재설치하지 않는다.
자세한 경로와 재실행 동작은 [음성 최초 준비](malbut_bringup/README_SPEECH.md#최초-준비)를 따른다.

```zsh
# 위 Humble·제조사 환경을 source한 같은 터미널에서 실행
bash ~/ros2_ws/src/malbut/setup.sh
```

준비가 성공하면 빌드한다. 이후 코드만 갱신한 경우에는 아래 빌드부터 실행한다.
`build.sh`는 긴 홈캠 빌드 전에 음성 소스·모델·CUDA 도구를 확인하고, 빠진 준비가 있으면
`setup.sh` 실행을 안내한다. 음성 가상환경·CUDA 라이브러리·ROS 빌드는 여기서 처리한다.

```zsh
cd ~/ros2_ws
bash src/malbut/build.sh --cmake-args -DBUILD_TESTING=OFF
source ~/ros2_ws/install/malbut_test/local_setup.zsh
```

위 경로 밖의 원본·제조사·시뮬레이션 패키지는 빌드 대상으로 잡지 않는다.
YOLO 소스도 적용본 안에 있으므로 별도로 다운로드하지 않는다.
메모리 부족 시 빌드 명령 뒤에 `--parallel-workers 1`을 붙여 colcon 동시 빌드를 줄일 수 있다.
STT 네이티브 빌드는 `nproc`으로 현재 프로세스에서 사용 가능한 CPU 수를 확인해 자동으로 병렬 빌드한다.
CUDA 없는 CI나 센서 전용 빌드는 `MALBUT_BUILD_SPEECH=0 bash src/malbut/build.sh`로
음성 환경·네이티브 빌드를 생략할 수 있으며, 그 결과로 실행할 때는 `speech:=false`를 지정한다.
`0`은 캐시된 whisper 브리지(`~/.cache/malbut_speech/whisper-cpp-build`)를 그대로 쓴다.
`malbut_stt`가 요구하는 브리지 ABI가 바뀌면(예: 2→3) `build.sh`가 이 불일치를 찾아
실패하므로, 그때는 `MALBUT_BUILD_SPEECH=1`로 한 번 빌드한다(`cmake`·`nvcc` 필요, 없으면
`setup.sh`). 그렇지 않으면 음성 사전 점검이 `whisper.cpp requires rebuilding the
packaged ABI 3 bridge`로 실패해 Bringup이 종료된다.

실기기 비교에서 대용량 Depth 점군 수신과 TF 지연이 연결되어, costmap은 LiDAR만
사용한다([Depth costmap을 쓰지 않는 이유](malbut_bringup/README.md#depth-costmap을-쓰지-않는-이유)).
Depth costmap 플러그인은 소스만 `malbut_bringup/depth_costmap`에 남기고 기본 빌드에서
뺐으므로 `ros-humble-depth-image-proc`은 필요 없다. RGB·Depth 영상과 사람 추적은 그대로다.
Nav2 서버·Collision Monitor는 `navigation2`, 컴포넌트 컨테이너는 `rclcpp_components`에
있으며 위 `rosdep install`로 준비된다. 시스템 패키지 전체 업그레이드는 하지 않는다.

이전 버전(분리된 Depth 패키지, 또는 Depth 플러그인을 함께 빌드하던 Bringup)에서
갱신할 때는 Bringup을 종료하고, 배포본을 완전히 교체한 뒤 아래 캐시만 한 번 지우고
위 `setup.sh`(새 패키지 `malbut_relocalization`의 의존성)와 `build.sh`를 실행한다.
지도·모델·토큰은 지우지 않는다.

```zsh
rm -rf /home/ubuntu/ros2_ws/build/malbut_test/malbut_bringup \
  /home/ubuntu/ros2_ws/install/malbut_test/malbut_bringup \
  /home/ubuntu/ros2_ws/build/malbut_test/malbut_depth_costmap \
  /home/ubuntu/ros2_ws/install/malbut_test/malbut_depth_costmap
```

새 터미널마다 위 제조사 환경을 설정한 다음
`source ~/ros2_ws/install/malbut_test/local_setup.zsh`를 실행한다.
`ros2 pkg prefix malbut_bringup` 결과가 **`install/malbut_test/malbut_bringup`**
아래인지 확인한다. 원본 패키지 경로라면 잘못된 overlay를 사용 중이다.

## 2. 센서 연결과 실제 지도

사용자가 보낸 실기기 topic 목록과 기본값은 일치한다:
`/scan_raw`, `/odom`, `/depth_cam/rgb0/image_raw`,
`/depth_cam/depth0/image_raw`, `/depth_cam/depth0/camera_info`,
`/depth_cam/rgb0/camera_info`.
Header의 frame·시각과 RGB-D 정렬은 추가 실기기 확인 대상이다.
제조사 `robot.launch.py`의 루트 네임스페이스 인자는 빈 문자열이 아닌 `/`다.
Bringup도 `robot_name=/`, `master_name=/`를 전달한다.

공식 앱이 센서·차체를 이미 실행 중이면 중복해서 켜지 않는다. 기존 드라이버를
재사용할 때는 `start_hardware:=false`, 직접 Bringup이 켜도록 할 때는 기존
공식 실행을 운영자가 종료한 후 아래 기본 실행을 사용한다. 동시에 실행하지 않는다.

GPU·음성 준비 전에는 인식과 음성을 끄고 센서·Nav2·관리자만 확인할 수 있다.

```zsh
ros2 launch malbut_bringup robot.launch.py perception:=false speech:=false
```

확인이 끝나면 이 실행을 종료하고 웹 패널을 한 번 실행한다.
이 명령과 페이지 접속만으로는 Bringup을 켜거나 움직이지 않는다.

```zsh
ros2 run malbut_bringup robot_web_panel
```

로봇의 `hostname -I`로 같은 Wi-Fi IP를 확인한 뒤 Mac에서
`http://<로봇-IP>:8766` 접속 → 터미널의 `Access token` 입력 → **연결**.

1. **지도 만들기 모드**를 누르고 Bringup 준비 완료를 확인한다. 지도 없이 켜지므로
   관리자가 slam_toolbox로 지도를 작성한다.
2. 새 지도 이름(예: `home2`)을 넣고 **자동 지도 만들기 시작**을 누른다.
3. 결과와 저장 완료를 확인한다. Bringup은 끄지 않는다.
4. 같은 페이지에서 저장 지도를 선택하고 **선택한 지도로 주행**을 누른다(아래 4절).

화면 지도는 `/global_costmap/costmap`(장애물·팽창 비용 포함), 로봇 위치·방향은
TF를 이용해 표시한다. 새 이미지와 좌표 정보가 준비되면 함께 교체하며,
수신 중에는 기존 지도를 유지한다. 저장 지도 원본은 기존 `/map`이다. 지도 클릭으로
이동 명령이나 초기 위치를 보내지는 않는다.

Bringup 안의 AutoSLAM은 Bringup이 켠 SLAM·Nav2를 그대로 쓰며 새 구성을 켜지 않는다.
관리자는 지도 작성 중에만 자동 지도 만들기를, 저장 지도 선택 후에만 추적·순찰·목적지
이동을 받는다.

기본 저장·목록 조회 폴더는 Git 밖의 `~/.ros/malbut/maps`다. 다른 폴더를 쓰려면
단독 패널 실행에 `--ros-args -p map_directory:=/원하는/지도폴더`를 추가한다.
이미 있는 이름은 거부하므로 새 이름으로 요청한다. 자동 지도 만들기가 종료되면
저장 목록을 갱신하며 **지도 목록 새로고침**으로도 다시 읽는다.
성공 결과의 `map_yaml`을 지도 선택이나 Bringup의 `map` 인자로 사용한다. YAML과
이미지가 모두 필요하다. 저장 지도(AMCL)와 실시간 SLAM은 관리자가 동시에 켜지 않는다.

## 3. 공유 인식 준비

YOLO·ReID GPU 런타임과 모델은 기존 도구로 최초 준비한다. 음성 환경은 위
`build.sh`가 별도 경로에 준비하며, Bringup은 설치 도구를 자동 실행하지 않는다.
이미 준비된 인식 모델은 그대로 재사용할 수 있다.

```zsh
bash "$(ros2 pkg prefix malbut_yolo)/share/malbut_yolo/scripts/prepare_runtime.sh"
bash "$(ros2 pkg prefix malbut_reid)/share/malbut_reid/scripts/prepare_inference_runtime.sh"
bash "$(ros2 pkg prefix malbut_reid)/share/malbut_reid/scripts/prepare_osnet_model.sh"
```

빌드·source 후 실행하는 설치된 도구 경로이므로 소스 복사 위치와 무관하다.
Bringup은 인식용 Python 실행 파일·모델 누락을 미리 검사하고 준비 명령과 함께
오류로 알린다. 웹에서 시작했다면 Bringup 상태에 표시되는 로봇 로그 경로를
확인한다. 자동으로 설치하지 않으며, 파일 존재 확인은 실제 GPU 추론 검증이 아니다.

- YOLO: 기존 Jetson PyTorch/torchvision을 유지하는 `~/.cache/malbut_yolo/runtime`.
- ReID: `~/.cache/malbut_reid/runtime`. 로봇의 공용 NumPy·ONNX Runtime을 제거하거나 변경하지 않는다.
- 음성: `~/.cache/malbut_speech/runtime`. YOLO의 NumPy 1.26.4, ReID의 NumPy 1.23.5와
  분리된 환경에 음성 의존성의 NumPy `>=1.26,<2`를 설치한다.
- OSNet 모델 변환: 별도 CPU export 환경. 로봇의 GPU Torch와 별개다.
- 모델: `~/.cache/malbut_perception/yolo26n.pt`, `osnet_ain_x1_0_msmt17.onnx`.
- `XDG_CACHE_HOME`, `MALBUT_YOLO_RUNTIME`, `MALBUT_REID_RUNTIME`을 바꾸면
  설치와 실행 양쪽에서 동일하게 설정한다. 기본값을 쓰면 별도 설정은 필요 없다.

OSNet 준비 중 GPU provider가 표시되더라도 실제 모델 추론까지 성공한 것은 아니다.
실제 실행 로그와 검출 결과를 확인한다. PC의 venv나 TensorRT 엔진은 로봇에 복사하지 않는다.
자세한 내용은 [YOLO](malbut_yolo/README.md), [ReID](malbut_reid/README.md)를 참고한다.

터미널에서 인식 영상과 함께 실행하려면, 웹에서 실행한 Bringup 등 기존 실행을
종료한 뒤 아래 명령을 사용한다. 웹으로 켠 Bringup도 인식 파이프라인을 켜므로
평소에는 이 명령을 따로 실행할 필요 없다.

```zsh
ros2 launch malbut_bringup robot.launch.py publish_debug_image:=true speech:=false
```

## 4. 저장 지도로 Bringup 및 기능 요청

**저장 지도**에서 `home2` 등을 선택 → **선택한 지도로 주행**.
Bringup이 켜져 있으면 재시작 없이 SLAM을 끄고 저장 지도·AMCL로 바꾸며, 꺼져 있으면
그 지도로 켠다. 이동 미션이 남아 있으면 거부되므로 먼저 취소한다. 지도를 선택한
것만으로는 움직이지 않으며, 준비 완료 후 사람 추적·순찰을 각각 시작한다.
미션을 그만두려면 취소하고 실제 정지를 확인한다.

웹 대신 터미널로 직접 실행하는 경우, 처음부터 저장 지도로 켜거나:

```zsh
ros2 launch malbut_bringup robot.launch.py \
  map:="$HOME/.ros/malbut/maps/home2.yaml" publish_debug_image:=true
```

실행 중에 지도를 바꾼다:

```zsh
ros2 service call /malbut/localization/load_map nav2_msgs/srv/LoadMap \
  "{map_url: $HOME/.ros/malbut/maps/home2.yaml}"
ros2 service call /malbut/localization/start_mapping std_srvs/srv/Trigger
```

저장 지도로 바꿀 때마다 관리자가 위치 보정(`/relocalize`)을 요청한다. 같은 지도의
마지막 AMCL 위치(없으면 AutoSLAM이 지도와 함께 저장한 `<지도이름>.pose.yaml`)를 넣고
LiDAR 스캔이 지도와 맞는지 확인한다. 맞지 않거나 기록이 없으면(꺼 둔 동안 옮긴 경우)
**제자리에서 한 바퀴 돌며** AMCL 전역 탐색으로 위치를 찾으므로 주변을 비운다.
AMCL 위치는 5초 주기로 `~/.ros/malbut/localization/last_pose.yaml`에 저장한다.
찾지 못하거나 결과가 실제와 다르면 RViz의 **2D Pose Estimate**로 실제 위치를 지정한다.
지도 YAML·이미지가 바뀌면 이전 위치는 쓰지 않는다.
관리자는 처음부터 켜져 위치 추정을 관리하고, 센서·TF·Nav2와 응용 서버가 준비되면
미션을 받기 시작한다. 준비 검사기는 **부팅 확인용**이며 주행 안전감시기를 대체하지 않는다.
아래 요청 전에는 로봇이 자동으로 순찰/추적을 시작하지 않는다.
같은 웹 주소에서 지도·영상 확인·추적·순찰·이 패널의 요청 취소가 가능하다.
위치 보정이 실패하면 웹의 지도 표시만으로 초기화되지 않으므로
RViz를 별도로 열어 **2D Pose Estimate**를 사용한다.

```zsh
ros2 launch navigation rviz_navigation.launch.py
```

Bringup에 `web_panel:=true`로 포함한 패널은 영상·지도·미션 요청만 가능하고,
Bringup 시작/종료는 비활성화된다. 위 단독 패널과 같은 포트로 중복 실행하지 않는다.
웹 취소는 비상 정지가 아니며, 브라우저 닫기나 Wi-Fi 끊김으로 주행이 정지하지 않는다.
웹 **Bringup 종료**는 미션 취소가 확인된 뒤 자기가 켠 구성만 종료한다.
확인하지 못하면 사유를 표시하므로 실제 로봇과 로그를 확인한다.

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

수동 조작은 조이스틱이나 웹 방향 버튼을 조작하면 시작되어(Nav2 AssistedTeleop)
진행 중인 추적·순찰을 멈추고, 5초 동안 조작이 없으면 끝난다. 수동 조작 중에는
추적·순찰 요청이 거부된다. 자세한 동작은 [수동 조작](malbut_bringup/README.md#수동-조작)을 따른다.

관리자를 통해 실행한 모든 미션 취소:

```zsh
ros2 service call /malbut/mission/execute/_action/cancel_goal \
  action_msgs/srv/CancelGoal '{}'
```

취소 수락과 실제 정지 완료는 다르다. 정지를 확인한 후 Bringup을 종료한다.
최초 실기기 시험은 별도 정지 수단을 확보한 안전한 공간에서 한다.

## 5. STT·Agent·TTS 음성 대화

최초 준비와 위 `build.sh`를 마치고 실행 터미널에 `OPENAI_API_KEY`를 설정하면,
기본 Bringup 명령 하나로 센서·인식과 음성 대화를 시작한다. 이미 실행 중인 Bringup에
음성 launch를 중복 실행하지 않는다. 저장 지도로 켜려면 위 4절처럼 `map:=...`을 지정한다.

```zsh
ros2 launch malbut_bringup robot.launch.py
```

`speech:=true`가 기본값이다. 로봇 준비 확인 뒤 음성 점검을 시작하며,
점검 → Agent·TTS → 관리자 준비 확인 → STT 순서이고, 실패 시 Bringup 전체가 종료된다.
STT는 로컬 whisper.cpp CUDA, Agent와 TTS는 기본 OpenAI API를 사용한다.
말로 추적·순찰을 실행하는 연결은 별도 범위다.

기본 모델 경로는 `~/.cache/malbut_speech/models/ggml-small.bin`, CUDA 라이브러리는
`~/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so`다.
장치 선택은 `speech_input_device`, `speech_output_device`를 사용한다.
입력 기본값은 `speech_input_device:=0`이며 현재 로봇의
`XFM-DP-V0.0.18: USB Audio` (sounddevice index `0`, ALSA `hw:0,0`)를 선택한다.
제조사 `xf_mic_asr_offline/voice_control`이 XFM을 선점하면 시작할 수 없으므로,
먼저 음성 실행 안내의 선점 해제·`startup_check` 자동실행 해제 절차를 따른다.
모델·경로 변경과 음성만 점검하는 하위 launch 사용법은
[음성 실행 안내](malbut_bringup/README_SPEECH.md)에 정리했다.
음성을 끄고 로봇 구성만 진단할 때는 `speech:=false`를 지정한다.

Preflight는 유료 API 요청을 보내지 않으며, 통과만으로 실제 CUDA 추론·API 응답·
마이크에서 스피커까지의 대화나 YOLO 동시 실행이 검증되지는 않는다.
해당 시험 결과를 로봇에서 남겨야 한다.

## 변경할 설정과 남은 확인

센서·제조사 launch 연결은 `malbut_bringup/launch/robot.launch.py` 인자로 지정한다.
추적의 Nav2 planner/controller/goal-checker ID와 여유값은
`malbut_tracking/config/person_following.yaml`을 실제 Nav2와 대조한다.
Nav2 공통 설정은 로봇에서 받은 파일을 `malbut_bringup/config/nav2_params.yaml`로
복사했다. 기본 BT의 Spin/Wait/BackUp, 숫자 표기와 임의의 원점 초기화를 정리했고,
이번 실물 설정은 다음과 같다:

- Local·Global footprint는 공식 치수 `0.277m × 0.212m` 사각형, 팽창 반경 `0.30m`.
  모서리 바깥은 벽 근처 주행을 덜 선호하게 하는 비용 구간이며 전부 통행 금지는 아니다.
- 제조사 드라이버가 `/cmd_vel`을 전후·횡 `0.2m/s`, 회전 `0.5rad/s`로 자르므로
  DWB(제조사 값 0.4m/s, 1.0rad/s)·smoother·behavior 회전 상한을 이 값에 맞춤.
  가감속과 나머지 DWB 값, 횡이동 비활성 설정은 제조사 설정과 같다.
- AMCL은 메카넘에 맞는 `OmniMotionModel`. 조이스틱·수동 조작은 횡이동을 쓴다.
- SLAM·AMCL·Nav2·사람 추적은 드라이버의 `/scan_raw`를 직접 사용.
  고정 각도 격자는 로봇 드라이버의 `bins` 설정으로 제공하며 별도 정규화 노드는 없다.
- LiDAR는 Local/Global 모두 표준 2D ObstacleLayer 사용.
  스캔 토픽·관측 범위는 유지하며 LiDAR 전용 Voxel 저장·발행은 제거.
- Depth costmap 플러그인은 빌드하지 않고, 원본 `/depth_cam/depth0/points` 구독도 없다.
  RGB·Depth 영상과 사람 추적은 유지한다.
- 자율 주행은 DWB의 footprint 검사로 장애물을 피하고, 수동 조작만 Collision Monitor
  (`FootprintApproach`, LiDAR)를 거쳐 `/cmd_vel`로 간다.
- 저장 지도마다 진입 금지·우회 권장 구역(Zone)을 웹에서 그려 costmap에 적용한다.

따라서 LiDAR 평면보다 낮거나 높은 물체는 costmap·Collision Monitor 모두에 반영되지 않는다.
원본 점군 발행 OFF 인자는 우리가 시작하는 하드웨어에만 전달하며, 외부에서
이미 실행 중인 제조사 카메라의 설정은 변경하지 않는다.
드라이버의 `bins` 설정·수정은 로봇에서 별도로 적용한다. Malbut은 스캔을 재가공하지 않는다.
점군 통신 제거의 실측 근거와 적용 확인은 [Bringup 설명](malbut_bringup/README.md#depth-점군-수신과-tf-지연)에 정리했다.
다른 검토한 복사본은 `nav2_params_file`, `slam_params_file`로 지정할 수 있다.
Nav2는 제조사 navigation launch 대신 공식 Nav2 서버를 제조사와 같은 컴포넌트
컨테이너 구성으로 Bringup이 직접 실행하므로(`malbut_bringup/nav2_stack.py`), 제조사
소스 경로(`~/ros2_ws/src/navigation`)에 의존하지 않는다. 제조사 원본은 수정하지 않는다.
알고리즘을 바꾸거나 임의의 TF/지도/초기 위치를 만들어 맞추지 않는다.

제공받은 하드웨어·navigation launch, 공통 YAML, DWB YAML을 대조해 Nav2 값을
`malbut_bringup/config/nav2_params.yaml`로 옮겼다. Nav2 출력은
`cmd_vel_nav → velocity_smoother → /cmd_vel`이며, Spin/BackUp은 behavior 서버가 `/cmd_vel`을
직접 낸다. 제조사 조이스틱은 `/cmd_vel_teleop`로 연결되어 수동 조작 중에만
`teleop_behavior_server`의 AssistedTeleop → `cmd_vel_pre_collision` → `collision_monitor`를
거쳐 움직인다. `/cmd_vel`의 발행자는 `velocity_smoother`, `behavior_server`,
`collision_monitor`뿐이어야 한다.

실제 이동 전에는 `/cmd_vel`의 차체 측 구독자가 있는지 확인한다. Topic 이름이
보이는 것만으로 모터까지 연결되었다고 판단하지 않는다.

```zsh
ros2 topic info /cmd_vel --verbose
ros2 topic info /cmd_vel_pre_collision --verbose
ros2 topic info /controller/cmd_vel --verbose
```

RGB-D 정렬·TF, 실제 GPU 추론, 저장 지도·초기 위치, 명령 수신·이동·정지는
아직 실기기 검증이 필요하다.
로컬 빌드/모의 검사가 통과했다고 이 항목까지 검증된 것은 아니다.
