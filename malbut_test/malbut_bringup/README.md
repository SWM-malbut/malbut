# Malbut 실로봇 Bringup (SWM25-169)

ROSOrin / Jetson Orin NX / ROS 2 Humble용 최상위 실행 패키지다.
제조사 드라이버·TF를 재사용하고, 공식 Nav2와 Malbut 응용 서버를 연결한다.
기존 `build.sh` 하나로 음성 런타임·STT CUDA 라이브러리와 ROS 패키지를 빌드하고,
`bringup.launch.py` 하나로 로봇 전체와 STT·Agent·TTS를 함께 실행한다.
Gazebo와 시나리오는 이 실행에 포함하지 않는다.
클라우드 연결이 설정되어 있으면 기존 홈캠 KVS 영상·음성 전송 노드를 함께 실행한다.
추적·순찰 알고리즘과 시스템 관리자의 정책은 변경하지 않는다.

음성은 기본 `speech:=true`이며 [음성 준비·점검 안내](README_SPEECH.md)를 따른다.
`speech.launch.py`는 이 최상위 launch가 포함하는 하위 구성으로, 음성만 점검할 때도 사용한다.

## 실행 구성

공통 기능과 응용 기능을 독립 launch로 실행한다. 각 모듈은 다른 모듈의 Topic,
TF, Action이 없어도 먼저 켜진다. 외부 의존성은 실제 작업을 요청하거나 입력을
처리할 때 확인하며, 이전 단계 준비 성공을 다음 모듈의 실행 조건으로 사용하지 않는다.

| Launch | 소유하는 기능 |
| --- | --- |
| `robot.launch.py` | 제조사 차체·센서·TF·오도메트리, Nav2·Collision Monitor·Zone, 지도·위치 추정·시스템 관리자 |
| `tracking.launch.py` | 기존 사람 인식(YOLO·ReID·RGB-D 위치 추정)과 사람 추적 launch |
| `patrol.launch.py` | 순찰 서버 |
| `autoslam.launch.py` | 탐색 서버·지도 저장 서버·내부 lifecycle 관리자 |
| `manual.launch.py` | 수동 입력 adapter와 조이스틱 |
| `relocalization.launch.py` | 위치 보정 Action 서버 |
| `speech.launch.py` | STT·Agent·TTS |
| `fall.launch.py` | 낙상 Pose·VLM 모니터·낙상 코디네이터 |
| `homecam.launch.py` | 기존 홈캠 미디어 launch, 공통 카메라 Topic 재사용 |
| `bringup.launch.py` | 선택한 위 모듈을 함께 실행하는 통합 진입점 |

기존 전체 실행 명령의 `robot.launch.py`를 `bringup.launch.py`로 바꾼다.
공통 기반만 필요하면 `ros2 launch malbut_bringup robot.launch.py`를 사용한다.
별도 터미널에서 필요한 기능만 켤 수도 있다. 이미 통합 실행한 기능은 중복 실행하지 않는다.

```bash
ros2 launch malbut_bringup robot.launch.py
# 다른 터미널: 위 명령보다 먼저 실행해도 노드가 떠 있어야 한다.
ros2 launch malbut_bringup tracking.launch.py
ros2 launch malbut_bringup patrol.launch.py
```

통합 실행에서 `perception`, `patrol`, `autoslam`, `manual`, `relocalization`,
`speech`는 기본 `true`다. `homecam:=auto`는 백엔드 환경이 있을 때,
`fall_monitor:=auto`는 설정 파일이 있을 때 포함한다.
모델·실행 환경·설정 파일 오류는 해당 모듈의 시작 실패로 로그에 남기며,
다른 모듈의 실행을 막거나 정상 노드를 종료하지 않는다. 단독 launch의 잘못된
설정은 오류로 종료한다. 프로세스 실행은 기능 준비 완료/안전한 주행을 뜻하지 않는다.

모듈 분리는 프로세스 분리가 아니다. Nav2는 기존 컨테이너를 유지하되,
기본 주행과 지도 기반 경로 계획의 lifecycle 관리를 분리한다.
이번 변경에서는 복구를 개편하지 않는다. 새 통합 launch의 기존 단계형 복구 요청은
거절하며, 모듈별 복구 연결은 후속 작업이다. 빈 복구 작업을 성공으로 보고하지 않는다.
`malbut_test`의 자원 기록기는 통합 진입점에서 모듈들보다 먼저 실행되며,
기록기 실패가 기능 실행을 막지 않는 기존 동작을 유지한다.

단독 실행도 모델·Python 환경·장치 접근 같은 자기 설정은 필요하다. 외부 ROS
상대가 없는 것과 로컬 설치/설정이 잘못된 것은 구분한다. 홈캠과 낙상을 각각
실행하면서 연동하려면 `fall_manager_runtime_id`, `fall_bridge_runtime_id`,
`fall_vlm_runtime_id` 세 값을 동일하게 지정한다. 통합 실행은 매 실행마다 같은
ID 묶음을 자동 전달한다. STT와 홈캠의 XFM 동시 입력도 통합 실행에서 기존
PulseAudio 공유 설정을 적용하며, 단독 실행에서는 같은 입력 공유 환경을 사용한다.

| 저장 지도 | 위치 추정 | 가능한 이동 미션 |
| --- | --- | --- |
| 선택 안 됨 (기본, `map` 인자 없음) | odom + 센서 장애물, SLAM·AMCL 미실행 | 사람 추적, 자동 지도 만들기, 수동 조작 |
| 선택됨 (`map:=...` 또는 실행 중 선택) | 저장 지도 + AMCL, 선택할 때 위치 보정 | 사람 추적, 순찰, 목적지 이동, 위치 보정, 수동 조작 |

지도 없이는 `nav2_mapless.yaml`을 추가 적용해 global costmap을 odom rolling window로
사용한다. LiDAR·inflation·차체 footprint는 유지하고 static map·금지 구역 입력만 뺀다.
저장 지도를 선택하면 원래 map 기반 설정을 복원한다. 사람 추적은 유휴 시 costmap
좌표계에 맞추며 실행 중에는 좌표계를 바꾸지 않는다.

자동 지도 만들기 Goal을 받으면 기존 관리자 서비스로 SLAM을 시작한다. 완료·실패·취소 시
하위 Nav2 종료를 확인한 뒤 SLAM을 종료하고 지도 없는 상태로 돌아온다. 저장 지도를
사용하려면 새로 저장한 지도를 선택한다. 관리자는 전체 READY를 기다리지 않고 기능별
Action·지도·안전 조건만 확인한다.

통합 launch의 `bringup_connections`는 노드 응답·Nav2 lifecycle·Action·센서 발행자와
기존 음성 상태를 조회해 `/malbut/bringup/status`와 `/malbut/bringup/progress`로 알린다.
웹 완료 표시에만 쓰며, 준비가 늦어도 다른 모듈을 막거나 종료·복구하지 않는다.

STT·Agent·TTS는 기본 포함이며 Nav2·관리자 준비를 기다리지 않고 시작한다.
STT의 기존 CUDA 초기화 재시도와 제한시간은 유지하되, 음성 실패가 전체 Bringup을
종료하지 않는다. 관리자가 없으면 Agent의 실제 로봇 명령만 실행할 수 없다.
시뮬레이션 지도를 실로봇에 대신 넣지 않는다.

실기기 적용본은 `build.sh` 하나로 홈캠 미디어까지 빌드한다.
`cloud.launch.py`는 웹 명령·상태 연결만 유지하고, 웹이 시작하는 `bringup.launch.py`가
카메라와 영상 노드를 함께 관리한다. `HOMECAM_BACKEND_URL`이 설정되어 있으면
기존 `homecam_robot.launch.py`를 한 번 포함하며 토큰 파일 환경을 그대로 전달한다.
별도 미디어 launch나 systemd 서비스를 중복 실행하지 않는다.
전체 절차는 [실기기 클라우드 연결](../malbut_test/README_CLOUD.md)을 따른다.

### Cloud VLM 자동 실행

`bringup.launch.py`는 독립 `fall.launch.py`를 통해 Cloud VLM 실행기
`malbut-fall-monitor`를 함께 시작한다. Manager는 위치 추정을 위해 먼저 시작하며,
설정이 준비되어 있으면 VLM·Pose·`malbut_fall_coordinator`는 다른 모듈 준비를 기다리지 않고 한 번만 실행한다. 카메라는 추가로 띄우지 않고 Bringup의
`rgb_topic`을 사용한다. 음성을 꺼도 VLM 노드는 별도로 시작할 수 있다.

최신 Bringup은 `mode` 인자를 없앴다. `mode:=navigation`을 넘길 필요가 없다.
별도 `mapping_backend.launch.py`는 VLM을 시작하지 않는다.

| 설정 | 동작 |
| --- | --- |
| `fall_monitor:=auto` (기본) | 설정 파일이 있으면 시작. 없으면 이유를 출력하고 건너뜀 |
| `fall_monitor:=true` | 설정 파일 필수. 없거나 잘못되면 낙상 모듈 시작 실패 |
| `fall_monitor:=false` | VLM 노드를 띄우지 않음 |
| `fall_config:=/절대경로/fall_runtime.json` | 사용할 설정 파일 지정 |

설정 파일 기본 위치는 `/etc/malbut/fall_runtime.json`이다.
`MALBUT_FALL_CONFIG` 환경변수로 기본 위치를 바꿀 수 있다.
어느 경로든 파일이 있는데 값이 잘못됐으면 낙상 모듈의 오류로 보고한다.
설정 양식은 Agent 패키지의 `config/fall_runtime.example.json`이며,
수치는 로봇 테스트용 시작값이다. 등록된 로봇 ID와 키·저장 경로를 준비하고 실물에서 확인해야 한다.

```bash
ros2 launch malbut_bringup bringup.launch.py map:=/실제/지도.yaml fall_monitor:=true fall_config:=/etc/malbut/fall_runtime.json
```

낙상 Pose의 기본값은 `fall_pose_execution_provider:=auto`, ORT 스레드 `2`,
spinning `false`, OpenCV 스레드 `1`이다. 추가 인자 없이 스레드 제한이 적용된다.
실행 Python은 낙상 전용 `~/.cache/malbut_fall_pose/runtime/bin/python`이다
(`XDG_CACHE_HOME` 지원). 실기기 `build.sh`가 이 환경에 GPU용 ONNX Runtime을 준비한다.
OSNet/ReID 가상환경을 재사용하거나 `runtime-cuda`를 자동 선택하지 않는다. 환경변수
`MALBUT_FALL_POSE_PYTHON` 또는 `fall_pose_python_executable` 인자를 명시하면 그 값이 우선한다.
이미 설치된 CUDA·cuDNN·Torch는 변경하지 않는다. Bringup 실행 중에는 설치하지 않는다.
`auto`는 **해당 노드의 실행 Python**에 CUDA EP가
있으면 CUDA를 선택하고, 없으면 CPU를 선택하면서 GPU 미사용 경고를 남긴다.
CUDA가 설치돼 있지만 초기화에 실패하면 CPU로 조용히 바꾸지 않고 시작에 실패한다.
GPU 실행을 필수로 하려면 `fall_pose_execution_provider:=cuda`를 사용한다.
시작 로그의 `provider=cuda (requested=auto)` / `provider=cpu (requested=auto)`로
실제 선택을 확인한다. `ros2 param get`의 `auto`는 GPU 활성화 증거가 아니다.
`fall_pose_intra_op_num_threads`, `fall_pose_allow_spinning`, `fall_pose_opencv_num_threads`로
변경할 수 있으며 재빌드 후 노드 재시작이 필요하다. 이전 실행 명령/YAML의 명시적인
`cpu`, `0`, `true`, `0` 값은 새 기본값보다 우선하므로 함께 확인한다.
GPU 의존성 설치와 사전 점검은
[로봇 준비 문서](../malbut_agent_server/docs/fall/fall_robot_preparation.md#1-1-낙상용-yolo-pose-준비)를 따른다.
소스만 갱신하고 로봇 빌드를 생략하면 기존 Python 환경은 바뀌지 않는다.
`MALBUT_BUILD_FALL_POSE=0`으로 환경 준비를 생략할 수 있으며 실행 시 `auto` 정책은 그대로다.
Jetson에서의 실제 처리 fps·CPU/GPU 부하는 별도 확인 대상이다.
[PC 비교 결과와 실행 예시](../homecam_agent/docs/FALL_POSE_PERFORMANCE_20260929.md)를 참고한다.

이 명령은 **VLM 노드 시작**이지 전송 동의가 아니다. 실제 영상 수집에는
시작 시 지정한 낙상 코디네이터 실행 ID, 현재 VLM에 적용한 감지·카메라 허용 설정,
코디네이터의 새 연결 확인 메시지가 필요하다. VLM은 KVS 저장 허용 Bool을 더 이상 받지 않는다.
Cloud 전송에는 `cloud_consent`와 15초 안의 서버 설정 확인도 필요하다.
Bringup은 홈캠·낙상 코디네이터·VLM에 같은 실행 ID 묶음을 전달하고,
홈캠이 서버 설정을 받으면 코디네이터가 VLM에 적용한다. 준비 완료나 노드 시작만으로
감지·Cloud 전송을 켜지는 않는다. 확인 대화는 코디네이터가 관리자에
`fall_confirmation` 미션(`URGENT`, `BASE·SPEAKER`)을 요청해 기존 Agent Action으로 연결한다.
관리자는 낙상 판단이나 설정 전달을 하지 않는다. 기존 wire 필드의 `manager_runtime_id`는
호환성을 위해 이름만 유지하며 코디네이터 ID를 담는다.

API 키는 런타임 설정의 `cloud_key_file`에서 읽으며 launch 인자로 전달하지 않는다.
설정 검사는 키를 읽거나 DB를 만들지 않는다. 실제 노드가 시작될 때 키와 의존성을
확인하며, 실패하거나 실행 중 노드가 종료되면 해당 오류를 로그로 보고한다.
다른 정상 모듈은 계속 실행한다.
Bringup과 별도의 `malbut-fall-monitor --execute`를 동시에 실행하지 않는다.
실물 카메라·Cloud 인증·Manager 연결을 합친 동작 검증은 별도로 필요하다.

제조사 실행은 하드웨어 `slam/launch/include/robot.launch.py` 하나만 include한다.
Nav2는 제조사 navigation launch 대신, 공식 `nav2_bringup`이 쓰는 것과 같은 Nav2
서버를 제조사처럼 하나의 컴포넌트 컨테이너(`nav2_container`)에 직접 구성한다
([`nav2_stack.py`](malbut_bringup/nav2_stack.py)). 공식 launch를 그대로 쓰지 않는 이유는
세 가지다. 수동 조작(AssistedTeleop)을 별도 behavior 서버에 두어 그 출력만 Collision
Monitor를 거치게 하고, Zone 필터 서버를 추가하고, 위치를 찾는 회전이 지도 위치 없이도
가능하도록 lifecycle 순서를 정한다. 이 순서에서는 behavior·smoother·Collision Monitor가
map TF를 기다리는 planner보다 먼저 켜진다.

Nav2 컨테이너에만 `RMW_IMPLEMENTATION=rmw_fastrtps_cpp`와
`FASTRTPS_DEFAULT_PROFILES_FILE=<패키지 share>/config/fastdds_nav2.xml`을 전달한다.
이 프로필은 `rtps/allocation/send_buffers/dynamic=true`만 설정한다. 동시에 발행하는
스레드가 송신 버퍼를 모두 점유하면, 반환을 기다리는 대신 추가 버퍼를 할당하고
반환된 버퍼를 재사용한다. 동시 송신량에 따라 버퍼 메모리가 늘어날 수 있다.
카메라 SHM 크기·토픽 큐·QoS·발행 모드는 바꾸지 않는다. 수동 복구도 기존 실행 환경을
재사용하므로 재시작된 Nav2에 같은 설정이 적용된다. 설정은 다음 Nav2 시작부터 적용된다.
근거: [Fast DDS 2.6.12 송신 버퍼 설정](https://github.com/eProsima/Fast-DDS/blob/v2.6.12/include/fastdds/rtps/attributes/RTPSParticipantAllocationAttributes.hpp).

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
ros2 launch malbut_bringup bringup.launch.py --show-args
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

### 카메라 Fast DDS 공유 메모리

Bringup이 제조사 하드웨어(카메라 포함)를 시작할 때 해당 launch 그룹에만
`RMW_IMPLEMENTATION=rmw_fastrtps_cpp`와 `FASTRTPS_DEFAULT_PROFILES_FILE`을 전달한다.
설정 파일은 설치된 `malbut_bringup/config/fastdds_camera.xml`이며 SHM 세그먼트는
**4 MiB (4,194,304 bytes)**, 원격 통신용 UDPv4는 유지한다. Nav2·인식·음성 등 다른
그룹의 환경변수는 변경하지 않는다. 이 그룹의 기존 사용자 DDS XML은 대체된다.
`RMW_FASTRTPS_USE_QOS_FROM_XML=1`은 추가하지 않는다. 토픽 QoS·발행 주기도 바꾸지 않는다.

설정 적용은 프로세스 시작 시점이다. 업데이트 후 Bringup을 종료하고 다시 시작한다.
`start_hardware=false`로 기존 드라이버를 재사용하면 적용되지 않는다. 별도 서비스로
카메라를 켜는 경우에는 그 서비스 시작 환경에도 같은 XML 경로를 설정해야 한다.
드라이버·CUDA 재컴파일은 필요 없으며, 저장소 적용 시에는 평소 빌드로 새 XML을 설치한다.
SHM 공간은 DDS participant마다 할당되므로 이 그룹에 속한 다른 하드웨어 프로세스도
영향을 받는다. 실제 지연 개선 여부는 로봇에서 별도로 확인해야 한다.

공식 근거: [Fast DDS SHM 설정](https://fast-dds.docs.eprosima.com/en/v2.6.11/fastdds/transport/shared_memory/shared_memory.html),
[Humble RMW XML 설정](https://github.com/ros2/rmw_fastrtps/tree/humble#full-qos-configuration).

## 1. 처음 실행

GPU·음성 준비 전에는 인식과 음성을 끄고 센서·Nav2·관리자만 확인할 수 있다.
이 경우 사람 추적 서버도 켜지지 않는다.

```bash
ros2 launch malbut_bringup bringup.launch.py perception:=false speech:=false
```

인식·음성 환경과 API 키가 준비되면 전체 실행:

```bash
ros2 launch malbut_bringup bringup.launch.py
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
ros2 launch malbut_bringup bringup.launch.py scan_topic:=/scan
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
ros2 launch malbut_bringup bringup.launch.py
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
ros2 launch malbut_bringup bringup.launch.py \
  map:="$HOME/.ros/malbut/maps/home2.yaml" publish_debug_image:=true
```

추적 서버의 planner/controller/goal-checker ID(`GridBased`, `FollowPath`,
`general_goal_checker`)는 `nav2_params.yaml`과 일치한다. 추적기는 사람 위치 자체를 목표로
경로를 요청하고, 사람 몸이 차지한 칸은 planner의 `GridBased.tolerance`(0.5 m)가 가장 가까운
갈 수 있는 칸으로 옮긴다. 사람이 너무 가까우면 후진 경로 대신 behavior 서버의
`BackUp`으로 곧게 물러난다. 필요하면 `following_config`와 `lidar_config`로 기존 응용
설정을 지정한다.
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
| `/malbut/localization/stop_mapping` | `std_srvs/srv/Trigger` | SLAM·AMCL 정리 → odom 센서 기반 주행 복원 |
| `/malbut/localization/state` | `std_msgs/String`(JSON) | `mode`(`NONE`·`SWITCHING`·`MAPPING`·`LOCALIZATION`·`ERROR`), `map`, `message` |

- `BASE`를 쓰는 다른 미션이 실행·대기 중이면 전환을 거부한다. 먼저 취소한다.
  AutoSLAM 자체가 요청하는 지도 작성 시작·종료는 허용한다.
- 전환 중(`SWITCHING`, 위치 보정 포함)에는 `BASE`를 쓰는 미션을 모두 거부한다.
  위치 보정이 로봇을 회전시킬 수 있기 때문이다. 수동 조작도 이때만 거부되며
  전환이 끝나면 다시 요청된다. 오류 상태에서는 지도가 필요한 미션을 거부한다.
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
- `FollowPath`의 `PreferForward` critic(Nav2 기본 제공, 원래 설정에는 꺼져 있었음)은 후진
  궤적에만 벌점(scale 40 × penalty 1.0)을 더한다. `theta_scale`·`strafe_x`를 0으로 두어
  회전·전진은 벌점이 없다. 40은 공개 DWB 설정들이 PathDist 32·GoalDist 24 옆에 두는
  범위(1~40)의 위쪽으로, 전진을 약하게 선호하는 값이다. 후진이 경로 거리를 크게 줄이거나
  전진 궤적이 모두 장애물에 걸리면 여전히 후진한다. 사람 추적의 후퇴는 DWB 경로가 아니라
  `BackUp` behavior라서 이 벌점과 무관하다.
- 도착 판정 `xy_goal_tolerance`는 0.12 m다(제조사 0.25 m는 차체 길이만큼 앞에서 멈추고,
  사람 추적의 0.90~1.10 m 거리 띠 밖에서 멈췄다). 방향은 보지 않는다(`yaw_goal_tolerance`
  6.28 rad). 도착 반경 안에서 DWB는 제자리 회전만 하고, footprint critic은 직사각형 차체가
  벽을 쓸고 지나가는 회전을 거부하므로, 벽 옆 목표에서 방향을 맞추려다 BT의 BackUp 복구
  (약 80초)까지 멈춰 있었다. 사람을 바라보는 것은 추적기의 Spin이, 순찰 지점의 둘러보기는
  순찰의 한 바퀴 회전이 맡는다. `GridBased.tolerance` 0.5 m는 목표 칸이
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
- 우선순위 `HIGH`, 자원 `BASE`. 실행 중인 추적·목적지 이동·위치 보정(`NORMAL`)과
  순찰(`LOW`)을 취소한 뒤 시작하고, 수동 조작 중에는 `NORMAL`·`LOW` 이동 요청을 거부한다. 이 동안
  `/malbut/state`의 `control_mode`는 `MANUAL`이다. 지도 선택 여부와 무관하지만, 위치
  추정 전환 중에는 받지 않으며 조작을 계속하면 전환이 끝난 뒤 시작된다.
- 입력: Bringup이 하드웨어를 직접 켜면 제조사 조이스틱을 `use_joy:=false`로 끄고,
  같은 제조사 노드(0.15m/s, 0.45rad/s)를 `/cmd_vel_teleop`로 연결해 다시 실행한다.
  그래서 조이스틱 명령은 Nav2 명령과 섞이지 않는다. 외부 하드웨어를 재사용하는
  `start_hardware:=false`에서는 제조사 조이스틱이 기존처럼 드라이버를 직접 움직인다.
- 서비스 웹의 조작 패드(누른 채 끌기 또는 방향키)도 같은 입력을 사용한다. 페이지가
  0.2초마다 속도를 반복해 보내고 로봇 브리지가 그대로 `/cmd_vel_teleop`에 낸다. 명령
  큐는 로봇이 가져가지 않은 이전 속도를 새 속도로 바꾸고, 브리지는 수동 입력이 있는 동안
  큐를 0.2초마다 확인한다. 손을 떼면 0을 보내고, 1초 넘게 새 명령이 없으면 브리지가
  스스로 0을 보낸다(LAN 패널은 0.5초).
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
- 관리자는 함께 시작해 위치 추정을 켜고, 기능별 요청 시 실제 서버·지도 조건을 확인한다.
  선택 기능의 부재를 전체 미션 차단 조건으로 사용하지 않는다.
- `wait_for_robot`의 센서·TF·Nav2 진단 코드는 남아 있지만 모듈 시작 조건으로 실행하지 않는다.
- 웹의 `ready`는 실행 중인 관리자에게 요청을 전달할 수 있다는 의미다.
  음성 모델 로딩이 다른 기능 버튼을 막지 않으며, 개별 기능의 실제 준비를 보장하지 않는다.
  음성 준비는 `/malbut/speech/status`, 지도 상태는 `/malbut/localization/state`,
  실제 Action·Topic은 웹의 진단 화면에서 따로 확인한다.
- 한 응용 노드 종료로 전체 launch를 종료하지 않는다. Ctrl+C 또는 웹 종료는 소유한
  프로세스들에 전달하며, 외부에서 실행한 노드는 종료하지 않는다.
- 이것만으로 모터 정지를 보장하지는 않는다. 제조사 드라이버는 `/cmd_vel` 수신이
  끊겨도 마지막 속도를 유지하는 코드이므로 실제 정지 동작은 하드웨어 검증 대상이다.
  Collision Monitor도 입력이 끊기면 아무것도 보내지 않으므로 이를 대신하지 않는다.

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
