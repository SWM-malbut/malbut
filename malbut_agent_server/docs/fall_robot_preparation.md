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

### 1. 의존성과 패키지 빌드

제조사 ROS Humble 환경을 먼저 불러오고 배포용 `malbut_test/README.md`와
`malbut_test/build.sh`의 절차를 따른다. 이미 설치된 Jetson Torch·CUDA를 PC용으로 덮어쓰지 않는다.
로봇용 빌드는 Cloud 미디어 SDK·음성 CUDA 의존성도 확인하므로 PC의 제한된 빌드와 구분한다.

VLM 실행 Python에서 `rclpy`, `cv_bridge`, `cv2`, `PIL`, `aiohttp`와 새
`malbut_interfaces` 자료형을 읽을 수 있어야 한다. `colcon build` 성공만으로
Python 실행 의존성이 모두 설치되는 것은 아니다.

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
