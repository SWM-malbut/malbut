# 실기기와 AWS 서비스 웹 연결

이 폴더는 배포 적용본이다. `homecam_web/`을 AWS에 배포하고 나머지 로봇 코드는
기존처럼 실제 로봇의 `ros2_ws/src/malbut`에 복사한다. AWS에서 ROS나 YOLO를 실행하지 않는다.

## 연결 경계

브라우저 → AWS HTTPS 서비스 → 인증된 장치 명령 큐 → 로봇 `robot_cloud_sync` → 기존 매니저/Action.
카메라·음성은 로봇 `homecam_media_agent` → AWS KVS → 브라우저 경로다.
두 프로세스는 같은 장치에 발급한 제한된 장치 토큰 파일을 사용한다. AWS 장기 액세스 키를 로봇에 저장하지 않는다.

원격 연결을 시작해도 Bringup이나 주행은 자동 시작하지 않는다. 웹에서 모드와 지도를
선택하고, 준비 완료 후 기능을 요청한다. 같은 로봇에서 기존 LAN 테스트 패널과 cloud bridge를
동시에 Bringup 관리자 역할로 실행하지 않는다.

## 1. AWS 웹

`homecam_web/infra/cdk/README.md`의 CloudFront 기본 HTTPS 주소 배포 절차를 사용한다.
도메인 구매나 IP 게이트웨이 EC2는 필요 없다. 배포 입력은 이 적용본의 `homecam_web/`이어야 한다.
소유자 계정과 장치를 등록하고 장치 토큰을 발급한 뒤 로봇으로 안전하게 전달한다.
실제 AWS 인증·배포·장치 등록이 끝나기 전에는 예시 주소로 연결되지 않는다.

## 2. 최초 준비와 통합 빌드

제조사 ROS 환경을 source한 뒤, 최초 설치나 의존성 변경 때 `setup.sh`를 실행한다.
OS 개발 패키지·홈캠 의존성·로봇 ROS 의존성을 설치하고, 고정 whisper.cpp 소스와
검증된 STT 모델을 준비한다. ROS 의존성 탐색에는 로컬 `homecam_detector`도 포함된다.
정상 음성 소스·모델은 재사용하며, 기존 수정 파일은 덮어쓰지 않는다.
현재 JetPack과 맞는 `nvcc`가 필요하며 JetPack·CUDA·PyTorch를 재설치하지 않는다.

```zsh
source /opt/ros/humble/setup.zsh
source ~/ros2_ws/install/setup.zsh
bash ~/ros2_ws/src/malbut/setup.sh
```

준비가 끝나면 `build.sh`로 음성 가상환경·STT CUDA 라이브러리·로봇 패키지·홈캠 영상
노드를 같은 배포 구성에 빌드한다. 코드만 갱신한 경우에는 이 단계부터 실행한다.

```zsh
bash ~/ros2_ws/src/malbut/build.sh --cmake-args -DBUILD_TESTING=OFF
source ~/ros2_ws/install/malbut_test/local_setup.zsh
```

`build.sh`는 긴 빌드 전에 음성 소스·모델·도구를 확인하며, 외부 음성 소스나 모델을
다운로드하지 않는다. KVS SDK는 홈캠 빌드에서 최초 다운로드하고 이후 재사용한다.
SDK·미디어 빌드 스크립트를 따로 호출할 필요 없다. AWS 웹 자체는 AWS에서 별도 배포한다.
GStreamer 개발 패키지 등의 apt 설치가 실패했다면 먼저 해결해야 한다.
누락된 의존성이 있으면 중단하며 영상 없는 health-only 빌드로 성공 처리하지 않는다.
Jetson의 `nvvidconv`, `nvv4l2h264enc`는 기존 JetPack 환경을 사용한다.
음성 경로와 장치 설정은 [README_SPEECH.md](malbut_bringup/README_SPEECH.md)를 따른다.

## 3. 웹 연결은 별도, 카메라는 Bringup에 포함

위 환경을 source한 터미널에서 Agent·TTS의 `OPENAI_API_KEY`와 장치 설정을 export한다.
`cloud.launch.py`와 웹에서 시작한 Bringup은 이 환경을 상속하며 `.env`를 자동으로 읽지 않는다:

```zsh
export HOMECAM_BACKEND_URL='https://실제배포주소.cloudfront.net'
export HOMECAM_DEVICE_TOKEN_FILE="$HOME/.config/malbut/device-token"
export HOMECAM_DEVICE_ID='jetson-homecam'
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  read -rs 'OPENAI_API_KEY?OpenAI API key: '
  print
fi
export OPENAI_API_KEY
test -s "$HOMECAM_DEVICE_TOKEN_FILE" &&
chmod 600 "$HOMECAM_DEVICE_TOKEN_FILE" &&
ros2 launch malbut_bringup cloud.launch.py
```

토큰 파일은 서버가 발급한 `hc1.…` 값만 담은 기존 파일이어야 한다. 채팅·Git·명령행 인자로 토큰을 전달하지 않는다.
`HOMECAM_DEVICE_ID`는 서버에 등록한 장치 ID와 같아야 한다.
웹에서 로봇 상태와 저장 지도 목록이 갱신되는지 먼저 확인한다.

- 웹의 **Bringup 준비**가 하드웨어·카메라·`homecam_media_agent`와 STT·Agent·TTS를 함께 켠다.
  기존 드라이버가 준비되어 있다면 중복 기동 없이 재사용한다.
- 시작은 센서·주행 준비 → Manager 또는 AutoSLAM 서버 준비 → 음성 점검 →
  Agent·TTS 준비 → STT 마이크 시작 순서다. 웹은 음성 준비까지 끝나야 준비 완료로
  표시하며, 그전에는 `음성 모델·마이크 준비 대기`를 표시한다.
- 지도 작성 모드도 카메라를 먼저 켜고 센서 준비 후 AutoSLAM 요청을 받는다.
  SLAM·Nav2 탐색은 AutoSLAM Goal 이후 시작한다. 저장 지도 주행 모드와 합치지 않는다.
- **Bringup 종료**는 해당 Bringup이 켠 영상·음성 노드도 종료한다. 웹 연결은 남아 다시 준비할 수 있다.
- 영상 노드나 `malbut-homecam.service`를 별도로 함께 켜지 않는다.
- 클라우드 주소가 없는 기존 오프라인/LAN Bringup에는 AWS 영상 노드를 추가하지 않는다.

기본 영상 입력은 `/depth_cam/rgb0/image_raw`, CameraInfo는 `/depth_cam/rgb0/camera_info`다.
실제 송출은 기존 웹의 카메라 ON/OFF 설정을 따른다. 미디어 launch는 별도 검출기를
켜지 않으며, 사람 추적은 기존 공유 YOLO를 사용한다.

## 4. 최소 실물 확인 순서

1. 로그인 → 왼쪽 이벤트 아래 **로봇 기능** → 등록된 로봇 상태 갱신 확인(연결만으로 주행하지 않음).
2. 지도 작성 모드 → 준비 완료 → 새 이름으로 AutoSLAM → 결과와 지도 저장 확인.
3. Bringup 종료 → 저장 지도 선택 → 주행 모드 준비 → 실제 위치 일치 확인.
4. 사람 추적·순찰·목적지 이동 요청 → 실행 상태 → 취소와 실제 정지 확인.
5. 카메라 라이브 영상 확인. 연결 끊김이나 브라우저 종료를 정지 수단으로 사용하지 않는다.
6. `speech_preflight_passed` → `speech_peers_ready` 확인 후 마이크에 말하고 스피커 응답을 확인한다.
   준비 로그만으로 실제 대화·CUDA 추론이 검증된 것은 아니다. 음성 기본 proposal 모드의
   대화와 이동 명령 실행은 별개다.

이 절차는 로봇에서 확인할 항목이며, 준비 스크립트·빌드 성공은 실물 검증 결과를 대신하지 않는다.

명령 `accepted/queued`는 미션 성공이 아니다. 웹의 실행 상태·결과에서
`SUCCEEDED/CANCELED/ABORTED/REJECTED/ERROR`를 확인한다.
새 요청의 자원 충돌·우선순위·선점은 기존 시스템 관리자 규칙 그대로다.
`웹에서 요청한 작업 취소`는 이 cloud bridge 소유 Goal만 취소한다.
`Bringup 종료`는 이 bridge가 켠 전체 스택을 종료하므로 다른 클라이언트의 미션도 중지될 수 있다.
