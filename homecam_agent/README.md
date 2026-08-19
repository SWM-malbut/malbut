# homecam_agent

ROS 2 Humble 기반 이동형 홈캠 PoC 에이전트다. Gazebo 또는 Aurora RGB
이미지를 H.264/Opus로 변환해 AWS KVS WebRTC로 전송하고, 사람·개·고양이
및 일반 움직임 이벤트를 온디바이스에서 판정한다.

이 저장소는 자율주행 소유권을 침범하지 않는다. `/odom`은 일반 움직임
오탐을 억제하기 위해 읽기만 하며 `/cmd_vel` publisher를 만들지 않는다.

## 현재 구현 상태

| 기능 | 상태 |
| --- | --- |
| ROS Image/CameraInfo/Odometry 구독과 카메라 health | 구현 |
| x264 및 Jetson `nvv4l2h264enc` 파이프라인 생성 | 구현 |
| GStreamer appsrc 프레임 입력 | GStreamer 개발 패키지가 있을 때 구현 |
| ALSA→Opus 캡처, 마이크 OFF 시 무음 Opus | GStreamer 개발 패키지가 있을 때 구현 |
| 수신 Opus→ALSA PTT 재생 경로 | transport callback까지 연결 |
| 백엔드 heartbeat와 desired state 적용 | 구현 |
| `POST`/`DELETE /api/device/v1/session`, 단기 AWS credential 갱신 | 구현 |
| YOLO ONNX 사람·개·고양이 감지 | 모델이 있을 때 구현 |
| `/odom` 정지 확인, frame confirmation, cooldown | 구현 |
| idempotent HTTPS 이벤트 전송 | 구현 |
| AWS KVS P2P/Storage signaling, H.264/Opus `writeFrame` | SDK 활성 빌드에서 구현 |
| KVS 수신 Opus→단일 PTT 재생 | 구현, 점유 권한은 백엔드·클라이언트가 중재 |

`HOMECAM_ENABLE_KVS`의 기본값은 `OFF`다. OFF 빌드는 원격 전송을
fail-closed로 막고 로컬 파이프라인·감지 개발만 허용한다. 실제 전송은 아래의
고정 버전 SDK를 빌드한 뒤 옵션을 ON으로 명시해야 한다. SDK ON 경로는
컴파일·링크·단위 테스트를 통과했지만, 실제 AWS 계정과 외부 모바일 네트워크를
사용한 종단 간 성능 검증 및 Jetson/Aurora 2시간 검증은 아직 남아 있다.

## 패키지

- `homecam_media_agent`: C++17, ROS 구독, GStreamer 파이프라인, heartbeat,
  단기 KVS session lease/refresh 경계
- `homecam_detector`: Python, OpenCV DNN YOLO, frame motion, odometry gate,
  이벤트 중복 제거 및 HTTPS 전달

YOLO 모델 파일은 저장소에 포함하지 않는다. 현재 시뮬레이션 기본 모델은
Ultralytics YOLO26n COCO end-to-end ONNX이며, 기존 YOLOv5/v8 호환 ONNX도
`model_path`로 교체할 수 있다. 사람 자세는 일반 모델이 사람을 감지한 경우에만
YOLO26n-pose를 최대 5 FPS로 실행하는 2단계 구조다. 모델 준비 스크립트를 한 번
실행하면 `~/.cache/malbut_perception/yolo26n.onnx`와
`~/.cache/malbut_perception/yolo26n-pose.onnx`를 실행 스크립트가 자동으로 찾는다.

```bash
./malbut_autonomy/malbut_perception/scripts/prepare_yolo26_model.sh
```

YOLO26의 end-to-end `TopK` 연산은 Ubuntu 22.04 기본 OpenCV 4.5.4 DNN에서
로드되지 않으므로 detector는 준비 스크립트가 설치한 ONNX Runtime을 사용한다.
Gazebo 실행 스크립트와 실제품 systemd 서비스는 해당 사용자 캐시 환경을 자동
연결하며, 별도 위치를 쓰면 `HOMECAM_ONNX_RUNTIME_SITE_PACKAGES`로 지정한다.

Ultralytics 모델·코드의 상용 이용 조건은 배포 전에 별도로 검토해야 한다.
모니터링 중 `detectorHealthy`는 단순 모델 로드 여부가 아니라 최근 10초 내
성공한 inference가 있는지를 나타낸다. 연속 세 번 inference가 실패하면 즉시
false가 되고, 다음 성공 시 복구된다.
pose 모델은 일반 사람·반려동물 이벤트와 독립적으로 동작한다.
`/homecam/pose_healthy`는 pose 모델 준비와 연속 실패 상태를,
`/homecam/person_pose`는 정규화된 COCO 17개 사람 관절 관측 JSON을 제공한다.
이 값은 자세 특징이지 낙상 판정이 아니다. 낙상 이벤트를 만들려면 시간 축
분류기와 실제 카메라 데이터셋 기반 임계값 검증이 별도로 필요하다.
카메라 또는 모니터링을 끄면 detector의 유효 monitoring 상태도 즉시 false가
되며 대기 중 이벤트와 재시도를 폐기한다. 카메라 privacy 상태에서는 신규
추론·이벤트 POST가 수행되지 않는다.
desired state heartbeat 기본 주기는 1초이며 응답 완료 후 250ms 주기로
수집한다. 따라서 정상 네트워크에서 OFF 반영은 최악 약 1.25초이고, 여기에
진행 중인 backend 요청 시간이 더해질 수 있다. 반영 즉시 세대가 바뀌어
기존 세션의 송수신과 PTT는 fail-closed된다.
backend URL을 설정한 장치는 재부팅 후 첫 desired state가 확인될 때까지
effective monitoring을 false로 유지한다. detector 역시 media agent의
transient-local privacy 상태를 처음 수신하기 전에는 이미지를 처리하지 않는다.

## malbut 저장소에서 바로 사용

팀원은 이미
[SWM-malbut/malbut](https://github.com/SWM-malbut/malbut)와 Ubuntu 22.04,
ROS 2 Humble, Gazebo Fortress 기반 환경을 설치한 상태라고 가정한다.
`Foundation`은 다시 설치하지 않는다. 기존 `malbut` 소스와 작업 중인 파일도
checkout하거나 수정하지 않는다.

이 디렉터리가 `malbut/homecam_agent`로 병합된 뒤에는 기존 저장소를 pull하고
다음 명령만 실행한다. workspace 경로는 자동으로 찾는다.

```bash
git pull
./homecam_agent/scripts/setup_portable_sim.sh
./homecam_agent/scripts/run_gazebo_homecam.sh --check-only
```

`--check-only`는 device id나 token 없이 `small_house` Gazebo를 실행하고,
RGB 및 CameraInfo 토픽과 실제 RGB 프레임 수신까지 검증한 뒤 종료한다.
이 점검 모드는 저장 지도를 생성하거나 교체하지 않는다.

원격 스트리밍에는 PC별로 발급된 장치 정보가 한 번 필요하다.

```bash
./homecam_agent/scripts/configure_sim_device.sh \
  --device-id REGISTERED_DEVICE_ID \
  --backend-url https://YOUR_BACKEND
./homecam_agent/scripts/run_gazebo_homecam.sh
```

이미 팀원이 Gazebo를 실행하고 있다면 기존 시뮬레이션을 끄지 않고 연결한다.

```bash
./homecam_agent/scripts/run_gazebo_homecam.sh --reuse-gazebo
```

setup은 다음을 한 번에 수행한다.

- 기존 `src/malbut`의 `malbut_gazebo`, `small_house` 에셋 확인
- 기존 `malbut` 파일을 변경하지 않고 현재 시뮬레이션 패키지를 다시 빌드
- 홈캠에 필요한 ROS/GStreamer 패키지 설치 및 `rosdep` 확인
- AWS KVS WebRTC C SDK `v1.19.1` 고정 빌드
- KVS·GStreamer·CURL이 모두 활성화된 홈캠 패키지 빌드
- 패키지 테스트와 공용 토픽 탐색 로직 테스트

독립된 별도 저장소나 archive로 배포할 때는 `homecam_agent`를
`<workspace>/src/homecam_agent`에 둘 수도 있다. 완전히 새로운 독립 workspace에서
`malbut`까지 받아야 하는 경우에만 `--bootstrap-malbut`을 사용한다. 둘 다 팀원
PR 배포의 기본 절차는 아니다.

설정 스크립트는 device token을 화면에 보이지 않는 prompt에서 입력받아
`${XDG_CONFIG_HOME:-$HOME/.config}/homecam/device.token`에 권한 `600`으로
보관한다. 설정과 token은 Git에 커밋하지 않는다.

일반 실행은 먼저 기기별 영속 저장소의 활성 지도를 확인한다. 지도가
없을 때만 SLAM과 `우리 집 지도 만들기` 화면을 시작하고, 저장 지도가
있으면 정적 지도와 AMCL을 사용한다. 기본 저장 경로는
`${XDG_DATA_HOME:-$HOME/.local/share}/malbut/devices/<device-id>/maps`이다.
AWS KVS 세션 시작·종료와 카메라·모니터링 설정은 이 지도를 삭제하거나
교체하지 않는다.

기본값인 `HOMECAM_CLOUD_MAP_ENABLED=true`에서는 같은 bearer token으로
저장 지도와 현재 위치를 AWS `homecam_web`에 동기화한다. AWS 웹의 지도
생성·저장 명령은 로컬 onboarding API로, 목적지 미리보기·이동·취소 명령은
로컬 Nav2 웹 API로 전달된다. 목적지 좌표를 받은 장치는 반드시 로컬의 최신
costmap·Zone 안전 검사를 통과한 preview token으로만 주행을 시작한다.
저장 지도에서 `지도 다시 만들기`를 요청하면 장치 supervisor는 Gazebo와
카메라 스트림을 유지한 채 Nav2/AMCL 스택만 SLAM 탐색 스택으로 교체한다.
저장 또는 취소가 끝나면 같은 방식으로 저장 지도 주행에 복귀한다. 작성 중인
지도는 cloud draft로만 보이고, 취소 시 기존 사용자 지도를 다시 표시한다.
완료된 웹 미리보기는 raw costmap이 아니라 저장 과정에서 정리한
`preview.png`를 사용하므로 inflation 그림자를 사용자 지도에 노출하지 않는다.

이후 `small_house`와 로봇을 headless 모드로 시작하고
`sensor_msgs/msg/Image` 및 `CameraInfo` 토픽을 자동 탐색한다. RGB 프레임
수신, 필수 GStreamer plugin, KVS 활성 빌드, SDK CA 파일을 확인한 뒤에만
원격 세션을 시작한다. Ctrl+C를 누르면 이 스크립트가 시작한 Gazebo와 homecam
프로세스만 종료한다.

로컬 Gazebo 화면도 함께 확인해야 할 때만 보호된 `sim.env`에서
`HOMECAM_GAZEBO_GUI=true`, `HOMECAM_GAZEBO_HEADLESS=false`로 바꾼다.
브라우저 스트리밍 시연은 Qt/QML GUI 종료가 전체 시뮬레이션을 중단하지 않도록
headless 설정을 유지하는 것을 권장한다.

시뮬레이션을 다시 실행해도 같은 `device-id`는 같은 지도를 재사용한다.
다른 테스트 지도가 필요하면 보호된 `sim.env`에 별도의 절대 경로로
`HOMECAM_MAP_STORE`를 지정한다. 이 값은 AWS 자격 증명과 무관하다.

재기동 시 supervisor는 활성 지도에 저장된 마지막 위치를 Gazebo spawn과
AMCL 초기 위치에 동시에 전달한다. 둘 중 한쪽에만 적용하지 않으므로 물리
시뮬레이션 위치와 지도 마커가 갈라지지 않는다. 이 보장은
`run_gazebo_homecam.sh`가 가진 단일 실행 lock과 독립 프로세스 그룹 수명주기를
통해 제공되므로 제품 시뮬레이션은 `managed_home.launch.py`를 별도 터미널에서
중복 실행하지 않는다.

실제 로봇은 이 시뮬레이션 복원 정책을 사용하지 않는다. 전원이 꺼진 동안
기기가 옮겨졌을 수 있으므로 기본값은 저장된 마지막 위치를 신뢰하지 않으며,
동일 odometry 세션 또는 도킹 스테이션·fiducial 같은 검증된 위치 기준이 없으면
`위치 확인 필요` 상태로 주행을 차단한다. 외부 supervisor가 물리 위치 기준도
함께 소유할 때만 `trusted_initial_pose:=true`를 사용할 수 있다.
`trusted_localization_handoff:=true`도 Gazebo/바퀴 odometry가 중단되지 않은
SLAM→Navigation 전환에서만 사용하며 제품 부팅 시에는 항상 false로 시작한다.

마이크 없는 PC에서는 기본적으로 무음 Opus track을 사용한다. 실제 마이크를
사용할 때는 보호된 `sim.env`의 `HOMECAM_MICROPHONE_ENABLED=true`와
`HOMECAM_AUDIO_SOURCE`를 설정하고, backend의 microphone 설정도 켠다.

### PC별 장치 격리

여러 PC가 같은 token과 KVS channel을 공유하면 master session이 충돌하고,
한 PC의 폐기·설정 변경이 다른 PC에도 적용될 수 있다. 따라서 배포 단위는
반드시 다음처럼 구성한다.

```text
PC 1대
  = 고유 device_id 1개
  = 고유 device token 1개
  = 고유 P2P channel + Storage channel + stream
```

현재 backend는 장치와 AWS 리소스를 자동 생성하지 않는다. 그래서 팀 관리자가
각 테스터 PC를 먼저 등록하고 `device_id`와 1회 표시되는 token을 안전하게
전달해야 한다. setup/run 스크립트나 Git 저장소에 공용 운영 token을 넣지 않는다.

### 수동 빌드

원격 전송은 upstream SDK `v1.19.1`
(`d7322f63af3c600ee7031b28436e3f8a12664272`)에 고정한다. SDK 경로는 바이너리
RUNPATH에 기록되므로 checkout을 옮긴 경우 해당 PC에서 다시 빌드한다.

```bash
HOMECAM_WS=/absolute/path/to/homecam_ws
cd "$HOMECAM_WS/src/malbut"
./homecam_agent/scripts/install_dependencies.sh
./homecam_agent/scripts/build_kvs_webrtc_sdk.sh \
  "$HOMECAM_WS/.deps/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1"

cd "$HOMECAM_WS"
source /opt/ros/humble/setup.bash
colcon build \
  --packages-up-to homecam_detector homecam_media_agent \
  --cmake-force-configure \
  --cmake-args \
    -DHOMECAM_ENABLE_KVS=ON \
    "-DKVS_WEBRTC_SDK_ROOT=$HOMECAM_WS/.deps/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1" \
    "-DHOMECAM_KVS_CA_CERT_PATH=$HOMECAM_WS/.deps/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1/certs/cert.pem"
```

SDK 저작권·라이선스 고지는 `THIRD_PARTY_NOTICES.md`에 기록되어 있다.

`model_path`가 비어 있거나 파일을 읽지 못하면 노드는 종료하지 않고
motion-only 모드로 내려간다. 이벤트 탭의 분류 의미는 다음과 같다.

- `사람`: YOLO가 COCO person 클래스를 인식
- `반려동물`: YOLO가 COCO dog 또는 cat 클래스를 인식
- `움직임`: 객체 종류를 특정하지 못했지만 프레임 차이가 확인됨

한 구간에서 사람과 일반 움직임이 함께 확인되면 클립은 중복 생성하지 않고
`사람`을 대표 유형으로 표시하며 두 라벨은 모두 보존한다. generic motion은
다음 조건을 모두 만족할 때만 이벤트가 된다.

- 최근 2초 이내 `/odom`을 받음
- 선속도와 각속도가 threshold 이하
- 활성 Nav2 목적지 주행이 없음
- 2초 이상 정지
- 여러 연속 프레임에서 움직임 확인

`/odom`이 없거나 오래되거나 로봇이 이동 중이거나 Nav2 goal이 실행 중이면
generic motion은 억제된다. 주행 종료 후 2초 동안 배경을 다시 잡으므로 카메라
시점 변화 자체가 `움직임` 클립이 되지 않는다. 사람·개·고양이 YOLO 감지는
로봇 주행 중에도 계속 수행된다.

Gazebo 이벤트 검증에서는 아래 고정 스크립트를 사용한다. 이 스크립트는
`small_house`에서 로봇 카메라 앞의 열린 바닥만 왕복하며, 사람의 팔 너비까지
포함한 0.8m 여유를 실제 벽·가구 형상에 대해 검사한다. 집 전체를 순회하는
perception demo actor는 이벤트 검증에 사용하지 않는다. Gazebo actor는 물리
충돌체가 아닌 kinematic 대상이므로 별도 `set_pose` 반복 명령으로 이동시키면
가구를 통과할 수 있으며 검증으로 인정하지 않는다.

```bash
./homecam_agent/scripts/spawn_event_test_person.sh
```

## Aurora / Jetson 실행

Deptrum Aurora 드라이버를 먼저 실행한 뒤 실제 RGB topic을 찾는다. 토픽명은
드라이버 버전에 따라 달라질 수 있으므로 저장소에서 하드코딩하지 않는다.

```bash
ros2 topic list -t | grep 'sensor_msgs/msg/Image'
ros2 topic echo --once /DISCOVERED_RGB_TOPIC
```

그 다음 발견한 토픽을 필수 launch 인자로 넘긴다.

```bash
read -rsp 'Homecam device token: ' HOMECAM_DEVICE_TOKEN
echo
export HOMECAM_DEVICE_TOKEN
ros2 launch homecam_media_agent homecam_aurora.launch.py \
  image_topic:='/DISCOVERED_RGB_TOPIC' \
  camera_info_topic:='/DISCOVERED_CAMERA_INFO_TOPIC' \
  backend_url:='https://YOUR_BACKEND' \
  device_id:='REGISTERED_DEVICE_ID' \
  model_path:='/opt/homecam/models/yolo26n.onnx' \
  pose_model_path:='/opt/homecam/models/yolo26n-pose.onnx'
unset HOMECAM_DEVICE_TOKEN
```

Aurora profile은 `nvvidconv`로 프레임을 NVMM 메모리로 옮긴 뒤
`nvv4l2h264enc`를 선택한다. 두 Jetson GStreamer plugin 중 하나라도 없으면
파이프라인 시작이 실패하고 로그에 원인이 표시된다. 이 경로는 문자열 단위
테스트를 통과했지만 현재 개발 호스트에는 Jetson/Aurora가 없어 실물
plugin·카메라 조합 검증은 아직 수행하지 않았다.

## 백엔드 계약

모든 장치 요청은 다음 헤더를 사용한다.

```text
Authorization: Bearer hc1.<credential-uuid-v4>.<64-hex-secret>
Content-Type: application/json
```

`device_id`에는 백엔드 장치 등록 응답의 실제 장치 ID를 넣는다. bearer
token 안의 credential UUID는 장치 ID가 아니며 서로 바꿔 쓰면 session 응답
검증 단계에서 거부된다.

에이전트는 `POST /api/device/v1/heartbeat`에 `sourceProfile`,
`imageTopic`, `streamMode`, `mediaHealthy`, `p2pHealthy`,
`storageHealthy`, `detectorHealthy`를 보낸다.
응답의 `desiredState.monitoringEnabled`, `cameraEnabled`,
`microphoneEnabled`를 적용한다.

heartbeat body는 백엔드가 허용하는 필드만 전송한다. 실제 peer/storage
연결과 최근 `writeFrame` 성공이 없으면 카메라 토픽이 정상이어도
`mediaHealthy=false`, `streamMode=idle`을 유지한다.

- 카메라 OFF: 영상 encoder 중지
- 마이크 OFF: KVS 호환용 silent Opus source로 재시작
- 모니터링/카메라 변경: 유효 상태(`monitoringEnabled && cameraEnabled`)를
  transient-local `/homecam/monitoring_enabled`에 전달

감지 노드는 기본적으로 5프레임 중 3프레임 확인 후 이벤트를 열고,
마지막 감지 10초 뒤 닫는다. 120초를 넘는 이벤트는 같은 `eventGroupId`의
다음 `segmentIndex`로 분할한다. 시작과 종료는 각각
`POST /api/device/v1/event-clips/started`,
`POST /api/device/v1/event-clips/ended`에 전송한다.

```json
{
  "eventGroupId": "0198a0e8-5800-7000-8000-000000000001",
  "segmentIndex": 0,
  "primaryType": "person",
  "labels": ["person", "motion"],
  "confidence": 0.91,
  "detectedAt": "2026-07-26T12:34:56.789Z",
  "startAt": "2026-07-26T12:34:51.789Z",
  "sessionIds": ["22222222-2222-4222-8222-222222222222"],
  "bootId": "11111111-1111-4111-8111-111111111111",
  "clockSource": "wall",
  "clockSteppedDuringEvent": false,
  "notificationEligible": true,
  "idempotencyKey": "64-character-sha256"
}
```

종료 payload에는 `endAt`과 steady clock으로 계산한
`monotonicDurationMs`가 추가된다. 전송은 camera callback과 분리된 bounded
worker에서 시작 2회, 종료 3회까지 재시도한다.
같은 idempotency key를 사용하므로 백엔드는 재시도를 중복 생성하지 않아야 한다.
장치 token이 들어 있는 요청은 HTTP redirect를 절대 따라가지 않으며 3xx를
전송 실패로 처리한다.

운영 backend URL은 HTTPS만 허용한다. 개발용 plaintext HTTP는 hostname이
정확히 `localhost`, `127.0.0.1`, `[::1]`인 경우에만 허용한다.
`localhost.evil`, userinfo가 포함된 URL 및 LAN IP plaintext는 시작 단계에서
거부된다.

## KVS 세션 동작

카메라의 최근 프레임과 H.264/Opus 파이프라인이 모두 준비된 뒤에만 장치
token으로 `POST /api/device/v1/session`을 모드별로 호출한다. 요청은
`{"mode":"p2p"}` 또는 `{"mode":"storage"}`를 명시하며, 응답의 장치 ID, mode,
region, channel ARN, session/credential 만료 시각을 엄격히 검증하고
백엔드 session과 STS credential 중 더 이른 만료 시각을 사용한다.

- 카메라 ON: 저장 설정과 무관하게 P2P master session을 준비
- 카메라 ON + 모니터링 ON: P2P와 별도로 `useMediaStorage=true`인
  Storage Session을 동시에 실행
- P2P와 Storage는 세션 ID, 상태, 재시도, bounded sender queue와 transport
  lock을 독립 관리하며 H.264/Opus encoder 출력만 공유
- AWS sample configuration은 두 개지만 process-wide WebRTC runtime은 참조
  계수로 공유해 한쪽 재연결이 다른 쪽 runtime을 deinit하지 않음
- 각 lease 만료 5분 전: 해당 모드의 단기 session credential만 교체
  - Storage Session은 AWS 1시간 상한과 backend 만료 시각이 어긋나도
    50분에 선제 갱신하고 55분에는 fail-closed로 교체한다.
  - P2P viewer가 연결되어 있으면 routine 교체를 유예하고, peer 종료 직후
    갱신한다.
  - viewer가 계속 연결된 경우에도 현재 lease 만료 60초 전에는 fail-closed
    안전 경계로 교체를 강제한다. mode·privacy 변경과 종료는 유예하지 않는다.
- 카메라 OFF, 토픽/pipeline 중단, 종료: transport 정지 후
  `DELETE /api/device/v1/session`에
  `{"sessionId":"<POST 응답 UUIDv4>"}` 전송
- 삭제 `200 {"ended":true|false}`는 terminal success로 처리한다. network/5xx는
  동일 session ID만 1~30초 지수 backoff로 재시도하고, 400/401과 계약 위반은
  fail-closed 상태에서 재시도하지 않는다.
- 생성·삭제 상태가 불확실하면 새 session 생성보다 해당 ID 삭제를 우선한다.

H.264와 Opus는 서로 독립된 GStreamer pipeline에서 생성되므로 각 pipeline의
PTS를 직접 신뢰하지 않고 공통 steady-clock timeline으로 다시 stamp한다.
pipeline 재시작 후에도 두 track의 시간축과 단조 증가가 유지된다. 장치에는
장기 AWS key를 저장하지 않으며 SDK가 요구하는 provider에는 백엔드가 발급한
단기 credential만 넣는다. upstream 기본 signaling file cache도 비활성화해
runtime working directory에 비밀·상태 파일을 만들지 않는다.

수신 Opus는 로봇 스피커로 재생된다. 동시에 한 명만 PTT를 점유하도록 하는
권한은 백엔드 lease와 PWA가 중재한다. 현재 C SDK media callback 자체는
임의의 peer가 보낸 오디오를 별도 서명으로 검증하지 않으므로, 비협조적인
클라이언트까지 장치 내부에서 차단하는 cryptographic peer authorization은
PoC 이후 보강 항목이다.

실제 token, AWS credential, 모델 파일은 Git에 커밋하지 않는다. Jetson
실물 장비는 빌드 후 다음처럼 systemd 서비스를 설치합니다. Gazebo 프로필에는
적용하지 않습니다.

```bash
sudo ./homecam_agent/scripts/install_homecam_systemd.sh \
  --workspace /opt/malbut \
  --environment-file /etc/malbut-homecam-device.env \
  --user malbut
sudo systemctl start malbut-homecam.service
```

환경 파일에는 다음 필수값을 넣고, 토큰 파일은 서비스 사용자만 읽을 수 있게
`0600`으로 둡니다. 전체 예시는 `config/device.env.example`에 있습니다.

```text
HOMECAM_DEVICE_TOKEN_FILE=/etc/malbut-homecam.token
HOMECAM_BACKEND_URL=https://malbut.example.com
HOMECAM_DEVICE_ID=registered-device-id
HOMECAM_IMAGE_TOPIC=/discovered/rgb/image_raw
HOMECAM_CAMERA_INFO_TOPIC=/discovered/rgb/camera_info
HOMECAM_MODEL_PATH=/opt/homecam/models/yolo26n.onnx
HOMECAM_POSE_MODEL_PATH=/opt/homecam/models/yolo26n-pose.onnx
```

서비스는 부팅 시
자동 시작하고 장애 종료 시 재시작합니다. 첫 heartbeat의 desired state를 받기
전에는 기존 fail-closed 계약에 따라 이벤트 감지와 Storage 업로드를 시작하지
않습니다.
