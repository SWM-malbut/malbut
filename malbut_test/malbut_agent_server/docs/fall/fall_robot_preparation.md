# 낙상 감지 로봇 실행 준비

2026-09-23. 로봇이 없는 상태에서 준비한 설정이다.
로컬 PC의 빌드·설정 검사는 Jetson 실행, Aurora 수신, 주행·음성과의 동시 성능 검증을 대신하지 않는다.

## 설정 파일

`config/fall_runtime.example.json`의 빈 수치를 아래 값으로 채웠다.
기존에 정한 최근 5초·최대 12장, Cloud 대기 20초, 추가 확인 2회는 바꾸지 않았다.
아래 수치는 **실물 테스트를 시작하기 위한 값**이다. 측정 후 조정해야 한다.

| 설정 | 시작값 | 의미와 확인할 점 |
| --- | --- | --- |
| `input_fps` | 5 | VLM 버퍼에 초당 최대 5장 입력. 카메라 발행 주기나 YOLO 실행 주기를 바꾸지 않음 |
| `retention_s` | 10 | 메모리에 최근 10초 보관. Cloud에는 이 중 최근 5초만 사용 |
| `buffer_frames` | 64 | 보관 장수 상한. 5 fps·10초의 약 51장을 담을 여유 |
| `buffer_bytes` | 16,777,216 (16 MiB) | JPEG 버퍼만의 용량 상한. 이미지 변환·Cloud 요청·다른 노드 메모리는 별도 |
| `max_source_age_s` | 1 | 촬영·관측 후 1초 넘게 지연된 입력은 받지 않음 |
| `policy.max_frame_age_s` | 2 | 후보 수신 시 관측 시각, 분석 요청 시 최신 RGB 시각이 2초 넘게 오래됐으면 사용하지 않음. 오래된 센서 요약은 제외 |
| `policy.max_person_observation_age_s` | 2 | 사람 관측·대상 연결에 사용하는 정보의 나이 상한 |
| `policy.retry_interval_s` | 3 | 앞선 분석 시도 후 재확인까지 최소 간격. 3초마다 무조건 호출하지 않음 |
| `policy.max_calls_per_minute` | 5 | 사건 확인과 주기적 확인을 합친 최근 60초 호출 상한 |
| `policy.max_incidents` | 10 | 실행 중 사건 관리 상한 |
| `image_topic` | `/depth_cam/rgb0/image_raw` | 기존 Aurora Bringup 기본값과 맞춤. 현재 실물 토픽 확인 결과는 아님 |

버퍼는 시간·장수·용량 중 하나라도 상한에 닿으면 오래된 영상부터 버린다.
장면에 따라 JPEG 크기가 커지거나 입력이 끊기면 5초 전체·12장을 항상 확보할 수 있는 것은 아니다.
호출·사건 상한에 걸려도 정상으로 판정하지 않는다. 확인이 지연되거나 처리하지 못한 이유를 기록한다.
여러 사람이 한꺼번에 감지될 때 상한이 확인을 지나치게 막는지 시험해야 한다.

이 파일은 설정 검사만 통과하는 예시다. 감지·카메라·전송 동의를 자동으로 켜지 않는다.
실제 실행 전 준비할 것은 다음과 같다.

- `device_id`: 서버에 등록된 실제 로봇 ID. 예시 ID로는 실행을 거부한다.
- `cloud_key_file`: 실행 계정만 읽을 수 있는 실제 Cloud 키 파일. 키를 Git에 넣지 않는다.
- `journal_path`: 실행 계정이 쓸 수 있는 보호된 저장 경로.
- Manager에서 받은 최신 설정과 연결 확인, 카메라 허용, Cloud 전송 동의.
- 계정의 사용 한도·요금 확인. 예시 설정은 무료 호출을 보장하지 않는다.

## 로봇 없이 확인

빌드된 패키지에서 다음 명령은 설정만 검사한다.

```bash
ros2 run malbut_agent_server malbut-fall-monitor --config "$(ros2 pkg prefix malbut_agent_server)/share/malbut_agent_server/config/fall_runtime.example.json"
```

`--execute`를 넣지 않으면 키를 읽거나 DB를 만들지 않고, 카메라 수신·Cloud 요청도 하지 않는다.
설정 검사 통과는 의존성·계정·실제 카메라까지 준비됐다는 의미가 아니다.

## 이번 로컬 확인 결과

2026-09-23, x86_64 PC·ROS Humble에서 로봇 배포용 소스(`malbut_test/`)로 확인했다.
아래 표는 main 병합 전 확인 기록이다. 현재 기본 Bringup은 depth costmap을 빌드하지
않으므로 당시의 `depth_image_proc` 누락을 현재 기본 빌드의 차단 사유로 보지 않는다.

| 확인 항목 | 결과 |
| --- | --- |
| 패키지 빌드 | `malbut_interfaces`, `malbut_agent_server`, `malbut_system_manager`, `homecam_detector`, `homecam_media_agent` 5개 성공 |
| Bringup 빌드 | PC에 `depth_image_proc`가 없어 CMake 설정 단계에서 중단. 나머지 Bringup 빌드 의존성은 그 이후 단계까지 확인하지 못함 |
| 홈캠 빌드 범위 | GStreamer·KVS·curl을 끈 빌드. 실제 로봇 Cloud 미디어 빌드 성공을 뜻하지 않음 |
| 실행 의존성 import | `rclpy`, `cv_bridge`, OpenCV 4.5.4, Pillow 9.0.1, aiohttp 3.14.3, 새 ROS 자료형 확인 |
| 설치된 실행기의 설정 검사 | 통과. ROS 시작·키 읽기·DB 생성·Cloud 호출 없음 |
| 예시 ID 실행 차단 | 종료 코드 2로 차단되는 것 확인 |
| 관련 자동 테스트 | 394개 통과. 낙상 설정·분석·Manager 연결·ROS 자료형·Bringup 설정/launch 검사 포함 |
| 버퍼 검사 | 임의 잡음으로 만든 640×400 JPEG를 5 fps로 입력해 최근 5초에서 12장 선택 확인 |
| 실물 확인 | Jetson·Aurora 토픽·카메라 실제 입력·동시 실행 부하는 미검증 |

빌드와 추가 Python 의존성은 임시 디렉터리에 두었다.
PC에 없던 `aiohttp`는 테스트 전용 경로에 설치했으며 시스템 Python·로봇 설정은 바꾸지 않았다.
실제 영상 전송이나 유료 모델 호출은 하지 않았다.

## 로봇을 받으면 할 순서

포팅용 Pose 연결을 추가한 뒤 2026-09-23에 다시 확인했다.
관련 테스트 787개가 통과했고, `malbut_test`의 `homecam_detector`와
`malbut_bringup` 두 패키지도 PC·ROS Humble에서 빌드됐다.
테스트에서는 영상 저장 OFF 시 Pose 실행, 설정 해제·상태 수신 중단 시 정지,
원본/포팅본 일치, Pose 후보에서 대상자 정보가 붙은 VLM 요청 생성까지 확인했다.
VLM 공급자는 테스트 대역을 썼으며, 실제 ONNX 가중치 추론·Cloud API 호출·Jetson
동시 실행 성능을 측정한 결과는 아니다. 모델과 키, 실행 환경은 로봇에서 따로 준비한다.

최신 main 병합 뒤에도 기능 검사를 다시 했다. 관련 Python·ROS·Manager·Bringup
검사 518개와 Manager 패키지의 린트·문서 검사 2개가 통과했다.
웹 테스트 109개, 린트·TypeScript·웹 빌드도 통과했다.
이는 로컬 결과이며 Jetson 실물 확인과 구분한다.

병합 과정에서 웹 경로를 `malbut_web`으로 맞추고,
낙상 설정 DB 변경 파일은 기존 main의 0010번과 겹치지 않게 `0011_fall_settings.sql`로 옮겼다.
운영 DB에는 적용하지 않았다.

### 1. 의존성과 패키지 빌드

제조사 ROS Humble 환경을 먼저 불러오고 배포용 `malbut_test/README.md`와
`malbut_test/build.sh`의 절차를 따른다. 이미 설치된 Jetson Torch·CUDA를 PC용으로 덮어쓰지 않는다.
로봇용 빌드는 Cloud 미디어 SDK·음성 CUDA 의존성도 확인하므로 PC의 제한된 빌드와 구분한다.

VLM 실행 Python에서 `rclpy`, `cv_bridge`, `cv2`, `PIL`, `aiohttp`와 새
`malbut_interfaces` 자료형을 읽을 수 있어야 한다. `colcon build` 성공만으로
Python 실행 의존성이 모두 설치되는 것은 아니다.

### 1-1. 낙상용 YOLO-Pose 준비

`malbut_test`에는 원본의 여러 사람 추적, 낙상 후보 생성, RGB/depth 처리 모듈도 함께 넣는다.
빌드 목록에 `homecam_detector`와 `malbut_fall_coordinator`를 포함하고, Bringup이 준비되면 VLM·
`malbut_fall_pose`·낙상 코디네이터를 각각 한 번 시작한다. 기존 홈캠 미디어의 `start_detector=false`는
유지한다. 영상 저장용 감지기를 별도로 켜서 두 번 실행하지 않는다.

Pose 실행 환경은 다음처럼 따로 준비한다. 명령은 의존성을 설치하지만 모델 다운로드,
카메라 사용, Cloud 호출은 하지 않는다. 시스템 Torch/CUDA는 변경하지 않는다.

```bash
bash ~/ros2_ws/src/malbut/homecam_agent/scripts/prepare_fall_pose_runtime.sh
```

전체 저장소를 옮긴 구조라면 경로의 `malbut/` 뒤에 `malbut_test/`를 붙인다.
검증한 YOLO26s pose ONNX 파일도 별도로 준비한다. 기본 경로는
`~/.cache/malbut_perception/yolo26s-pose.onnx`이며 가중치는 Git에 넣지 않는다.
현재 해석기는 입력 `[1, 3, 640, 640]`, 출력 행당 57개 값
(`xyxy`, 신뢰도, 클래스, 17개 관절의 `x/y/신뢰도`)인 end-to-end 모델을 사용한다.
다른 YOLO 출력 형식을 이름만 바꿔 넣지 않는다.

| Bringup 인자 | 기본값 / 의미 |
| --- | --- |
| `fall_pose_model_path` | 위 ONNX 경로. 환경변수 `MALBUT_FALL_POSE_MODEL`로 변경 가능 |
| `fall_pose_python_executable` | `~/.cache/malbut_fall_pose/runtime/bin/python`. `MALBUT_FALL_POSE_PYTHON`으로 변경 가능 |
| 입력 | VLM과 같은 `rgb_topic`. 640×400 영상을 비율 유지해 640×640으로 만들고 여백 추가 |
| 결과 좌표 | 여백을 제외하고 원본 RGB 기준으로 되돌림. 여백 안의 관절은 근거로 사용하지 않음 |
| 실행 빈도 | 최대 5 fps. 실제 Jetson 처리 속도는 미측정 |

모델 파일·Python 경로가 없으면 Bringup 시작 전에 실패한다.
ONNX Runtime이나 모델을 불러오지 못하면 Pose 노드 시작에 실패하고 Bringup도 종료한다.
현재 ONNX 실행은 CPU 방식이다. TensorRT/GPU 가속과 주행·음성 동시 부하는 별도 검증 대상이다.

Pose는 `/malbut/falls/status`에서 **이번 실행의 VLM ID**와 상태 번호를 확인한다.
설정 적용 완료·낙상 감지 ON·카메라 허용·영상 수신 가능이 모두 참일 때만 영상을 처리한다.
영상 저장이나 Cloud 동의가 OFF여도 이 조건을 만족하면 Pose는 실행한다.
허용이 해제되거나 VLM 상태가 5초간 오지 않으면 멈추고 추적 이력을 지운다.
Manager 연결 중단은 VLM의 기존 5초 검사로 먼저 감지하며, Pose에는 다음 상태 보고로 전달된다.
VLM 자체가 멈추는 경우에 대비한 별도의 5초 검사도 Pose에 둔다.

Pose가 보내는 `/homecam/person_poses`, `/homecam/fall_candidates`를 VLM이 받는다.
일반 YOLO의 사람 검출 여부나 `perception=false`는 이 경로를 막지 않는다.
시험 스크립트의 모든 실험 조건을 옮긴 것은 아니다. 기본 판단 코드는
`pose-temporal-candidates-v2`이며, 과거 실험의 감지율을 이 배포본의 성능으로 그대로 쓰지 않는다.
실험용 추가 조건을 적용하려면 동일 영상으로 다시 비교하고 별도로 반영한다.

### 2. Aurora 토픽 확인

이미 카메라 드라이버가 켜져 있는 로봇 터미널에서 확인한다.
아래 명령은 새 드라이버·주행·VLM을 시작하거나 Cloud로 영상을 보내지 않는다.

```bash
ros2 topic list -t
ros2 topic info /depth_cam/rgb0/image_raw --verbose
ros2 topic info /depth_cam/rgb0/camera_info --verbose
ros2 topic info /depth_cam/depth0/image_raw --verbose
timeout 10s ros2 topic hz /depth_cam/rgb0/image_raw
ros2 topic echo /depth_cam/rgb0/image_raw --once --field width --qos-reliability best_effort
ros2 topic echo /depth_cam/rgb0/image_raw --once --field height --qos-reliability best_effort
ros2 topic echo /depth_cam/rgb0/image_raw --once --field encoding --qos-reliability best_effort
```

`hz`는 10초 뒤 종료하므로 종료 코드 124가 나올 수 있다.
RGB가 640×400인지, 프레임이 꾸준히 들어오는지, Header 시각이 로봇 ROS 시각과 맞는지 확인한다.
토픽명이 다르면 실제 이름에 맞춘다. Bringup은 `rgb_topic`으로 VLM 입력을 remap한다.
depth 토픽이 있다는 것만으로 RGB 정렬·단위·CameraInfo 일치를 확인했다고 보지 않는다.

### 3. 수집과 설정 전달부터 확인

등록된 ID·경로·키를 준비하고 설정 검사를 통과시킨다.
실제 실행 시에는 기존 navigation Bringup에서 VLM을 한 번만 시작한다.
단독 VLM 실행기와 Bringup을 동시에 띄우지 않는다.
먼저 Cloud 동의를 끈 상태로 영상 수신, 1초 상태 보고, Manager 설정 적용과 연결 중단 처리를 확인한다.
이 단계에서 로봇을 움직이거나 시험용 Cloud 요청을 보낼 필요는 없다.

### 4. 동의 후 Cloud 연결과 부하 확인

별도로 전송 동의와 계정 사용 조건을 확인한 뒤 테스트 영상으로 직접 API 분석을 확인한다.
그다음 주행·음성과 함께 입력 속도, JPEG 버퍼 크기, CPU/GPU·메모리,
요청 지연·누락·호출 상한 도달을 측정한다. 이 결과로 시작값을 조정한다.
