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

## 2. 로봇 명령·상태 연결

기존 `build.sh`로 로봇 패키지를 빌드한다. Zsh 터미널에서는:

```zsh
source /opt/ros/humble/setup.zsh
source ~/ros2_ws/install/setup.zsh
source ~/ros2_ws/install/malbut_test/local_setup.zsh
export HOMECAM_BACKEND_URL='https://실제배포주소.cloudfront.net'
export HOMECAM_DEVICE_TOKEN_FILE="$HOME/.config/malbut/device-token"
chmod 600 "$HOMECAM_DEVICE_TOKEN_FILE"
ros2 launch malbut_bringup cloud.launch.py
```

토큰 파일은 서버가 발급한 `hc1.…` 값만 담은 기존 파일이어야 한다. 채팅·Git·명령행 인자로 토큰을 전달하지 않는다.
웹에서 로봇 상태와 저장 지도 목록이 갱신되는지 먼저 확인한다.

## 3. 영상 전송 빌드 — 최초 또는 SDK 변경 시

로봇 기본 빌드에 무거운 AWS SDK 빌드를 끼워 넣지 않는다. 실제 영상을 사용할 때만:

```zsh
cd ~/ros2_ws
bash src/malbut/homecam_agent/scripts/install_dependencies.sh
bash src/malbut/homecam_agent/scripts/build_kvs_webrtc_sdk.sh \
  "$PWD/.deps/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1"
bash src/malbut/homecam_agent/scripts/build_robot_cloud.sh
source ~/ros2_ws/install/malbut_test/local_setup.zsh
```

실제 Jetson의 `nvvidconv`, `nvv4l2h264enc` 플러그인은 JetPack 환경을 사용한다.
기본 `HOMECAM_ENABLE_KVS=OFF` 빌드로는 실제 AWS 영상이 나오지 않는다.

별도 터미널에서 같은 ROS 환경과 위 두 환경변수를 설정하고:

```zsh
ros2 launch homecam_media_agent homecam_robot.launch.py \
  backend_url:="$HOMECAM_BACKEND_URL" device_id:='서버에등록한장치ID'
```

기본 카메라 입력은 `/depth_cam/rgb0/image_raw`, CameraInfo는 `/depth_cam/rgb0/camera_info`다.
센서가 아직 꺼져 있다면 웹에서 Bringup을 준비한 뒤 영상을 확인한다.
미디어 launch는 별도 검출기를 켜지 않으며, 사람 추적은 기존 공유 YOLO를 사용한다.

## 4. 최소 실물 확인 순서

1. 로그인 → 등록된 로봇 상태 갱신 확인(연결만으로 주행하지 않음).
2. 지도 작성 모드 → 준비 완료 → 새 이름으로 AutoSLAM → 결과와 지도 저장 확인.
3. Bringup 종료 → 저장 지도 선택 → 주행 모드 준비 → 실제 위치 일치 확인.
4. 사람 추적·순찰·목적지 이동 요청 → 실행 상태 → 취소와 실제 정지 확인.
5. 카메라 라이브 영상 확인. 연결 끊김이나 브라우저 종료를 정지 수단으로 사용하지 않는다.

명령 `accepted/queued`는 미션 성공이 아니다. 웹의 실행 상태·결과에서
`SUCCEEDED/CANCELED/ABORTED/REJECTED/ERROR`를 확인한다.
새 요청의 자원 충돌·우선순위·선점은 기존 시스템 관리자 규칙 그대로다.
`웹에서 요청한 작업 취소`는 이 cloud bridge 소유 Goal만 취소한다.
`Bringup 종료`는 이 bridge가 켠 전체 스택을 종료하므로 다른 클라이언트의 미션도 중지될 수 있다.
