# MALBUT Web

모바일 PWA, 홈캠 장치 API, AWS Kinesis Video Streams broker, 최근 7일
이벤트·녹화 재생과 로봇 지도·목적지 주행 기능을 제공하는 독립 Node.js 서비스입니다. ROS 패키지가
아니며 `COLCON_IGNORE`로 로봇 워크스페이스 빌드와 분리됩니다.

## 현재 실기기 연결

배포 소스는 `malbut_test/malbut_web`입니다. 개발은 원본에서 하고 배포 복사본에도 반영합니다.
실기기는 `malbut_bringup cloud.launch.py`가 장치 인증으로 클라우드에 먼저 연결합니다.
인바운드 ROS 포트나 실험용 웹 패널을 인터넷에 공개하지 않습니다.

- 웹 → 기존 장치 명령 큐 → 실기기 cloud bridge → `/malbut/mission/execute`.
- 사람 추적·순찰·목적지 이동의 실행·선점·취소는 현재 시스템 관리자가 담당합니다.
- AutoSLAM은 관리자 실행 시 관리자를 거치며, 지도 작성 준비 단계에는 기존 `/autoslam` 직접 요청을 사용합니다.
- 기존 홈·홈캠·지도·이벤트 화면은 유지하며, 이벤트 아래 **로봇 기능** 메뉴에서
  Bringup 준비·종료, AutoSLAM, 사람 추적, 순찰과 목적지 이동을 요청합니다.
  이 메뉴는 `malbut_manager_v1` 연결에서만 제어를 활성화하고 기존 방 편집과 분리합니다.
- 같은 메뉴에서 실로봇 기능을 추가로 사용합니다. 배치는 이후 다시 정리합니다.
  - 모드 전환: Bringup 실행 중에는 **지도 만들기로 전환**, **선택한 지도로 전환**이
    재시작 없이 위치 추정만 바꿉니다. 이동 중인 작업이 있으면 로봇이 거부합니다.
  - 지도 삭제: 선택한 저장 지도와 그 지도의 구역·저장 위치 파일을 로봇에서 지웁니다.
    사용 중인 지도는 지우지 않습니다.
  - 위치 보정: **위치 다시 찾기**(저장 위치 먼저), **지도 전체에서 찾기**, 그리고 지도의
    **현재 위치 지정** 탭에서 누르고 끌어 정한 위치·방향으로 **이 위치로 설정**.
    모두 시스템 관리자의 `relocalize` 기능입니다.
  - 수동 조작: 조이스틱 패드를 누른 채 끌면 끈 만큼의 속도(최대 0.15 m/s, 0.5 rad/s)를,
    패드에 포커스를 두고 방향키·W·A·S·D(회전 A·D), Q·E(옆이동)를 누르면 고정 속도를
    0.2초마다 반복해 보냅니다. 손을 떼면 정지 명령을 보내고, 로봇은 1초 넘게 새 명령이
    없으면 스스로 멈춥니다. 큐에 남은 이전 속도는 새 속도가 대체하고, 2초 안에 로봇이
    가져가지 못한 속도는 버립니다. 로봇은 수동 입력이 있는 동안 명령을 0.2초마다 가져갑니다.
  - 구역: **구역 편집** 탭에서 꼭짓점을 찍어 진입 금지·우회 권장 구역을 그리고
    **구역 저장·주행에 반영**을 누릅니다. 로봇이 지도별 구역 파일에 저장하고, 지도
    업로드에 구역을 함께 실어 이 화면에 다시 표시합니다.
  - 디버깅(소유자): 왕복 시간 측정, 로봇 진단(노드·토픽 발행자·Action·위치 추정·구역·
    수동 조작 상태와 등록 기능 목록), 등록된 기능을 JSON 인자로 직접 실행, 최근 명령
    기록과 상태 원본(JSON) 보기. 직접 실행도 시스템 관리자가 Manifest로 검사합니다.
- 명령 큐의 `completed`는 로봇 측 **접수 처리 완료**입니다. 미션 성공·실패·취소는
  로봇이 올리는 `target.requests`의 최종 상태·결과를 봅니다. 지도 삭제·구역 저장·
  왕복 시간·진단은 로봇이 바로 답하므로 명령 결과가 곧 실행 결과입니다.
- 지도는 `/map`, 로봇 위치는 TF에서 받습니다. 목적지 선택은 경로 미리보기나 주행 가능 판정이 아닙니다.
- 로봇 기능 지도는 원본 셀 해상도를 유지한 무손실 PNG를 선명하게 확대하고,
  위치·방향을 SVG로 겹쳐 표시합니다. 새 이미지와 좌표 정보가 준비되면 함께 교체합니다.
  화면 확대가 센서/SLAM 자체의 해상도를 높이거나 새 방 경계를 만드는 것은 아닙니다.
- 영상·음성은 기존 `homecam_media_agent`의 KVS 전송을 사용합니다. 실기기 launch는
  별도 YOLO/OSNet을 실행하지 않습니다. 홈캠 전용 이벤트 검출 연결은 이 범위에 포함하지 않습니다.
- 로봇 `build.sh`가 영상 패키지와 SDK까지 빌드합니다. 웹 연결은 별도로 켜 두고,
  웹에서 Bringup을 준비·종료하면 카메라와 영상 전송 노드도 함께 시작·종료합니다.

자세한 실기기 빌드·접속 절차는 적용본 `README_CLOUD.md`를 따릅니다.

## 로컬 검증

Node.js 22.13 이상과 Docker Compose가 필요합니다.

```bash
cp .env.example .env.local
npm ci
npm run db:up
npm run db:migrate
npm run dev
```

로컬 인증은 `AUTH_MODE=dev_header`일 때만 사용할 수 있습니다. 요청에는
`.env.local`의 `AUTH_DEV_USER_EMAIL`과 동일한 `x-malbut-dev-user-email` 헤더가
필요하며, loopback 요청과 비운영 환경에서만 허용됩니다. 운영 모드에서는
이 방식이 강제로 차단됩니다.

검증 명령:

```bash
npm run lint
npm test
npm audit --audit-level=high
```

`npm test`는 빌드 없이 인증·스트리밍 계약 테스트와 PGlite를 사용한 PostgreSQL
스키마·이벤트 outbox 테스트를 실행합니다. `npm run test:full`은 Next.js production
build까지 함께 검증합니다. 실제 RDS 통합은 배포된 개발 스택에서 별도로 smoke
test합니다.

## AWS 런타임

운영 구성은 다음 경계를 사용합니다.

- ALB는 HTTPS 요청을 ECS의 Next.js 서비스로 전달하고 인증은 앱이 수행
- `/auth/login`의 MALBUT 화면이 서버 전용 Cognito API를 호출하며, 최초
  비밀번호 변경과 TOTP MFA도 같은 화면에서 처리
- 브라우저에는 Cognito token 대신 `HttpOnly`·`Secure`·`SameSite=Lax`인
  불투명 세션 쿠키만 발급하고, PostgreSQL에는 원문이 아닌 HMAC digest 저장
- Cognito challenge session은 5분 동안만 AES-256-GCM으로 암호화해 저장
- RDS PostgreSQL에는 ECS Task Role과 Secrets Manager로만 접속 정보 주입
- 서울 리전 RDS 공개 루트 CA 번들은 검증된 이미지 자산으로 고정하고
  `verify-full`로 서버 인증서와 호스트 이름 검증
- 장치는 장기 AWS 키 없이 backend가 발급한 제한된 STS 자격 증명 사용
- P2P와 Storage KVS 채널은 장치마다 분리하고 archive stream은 168시간 보존
- 이벤트 클립은 감지 전 5초부터 마지막 감지 10초 후까지의 메타데이터만
  PostgreSQL에 저장하고, KVS fragment 경계에 맞춘 HLS URL을 요청 시 발급
- 이벤트의 `목록에서 삭제`는 메타데이터를 즉시 숨기지만 KVS 원본은 개별
  fragment 삭제가 불가능하므로 7일 retention 만료 시 자동 삭제
- 저장 SLAM 지도와 현재 위치는 장치 bearer API로 RDS에 동기화
- 가족은 지도를 조회할 수 있고 지도 생성·목적지 주행 명령은 소유자만 등록
- 목적지 좌표는 AWS에서 직접 `/cmd_vel`로 변환하지 않습니다. 실기기는
  시스템 관리자의 `navigate_to_pose` → Nav2 경로를 사용합니다.
  Gazebo의 기존 preview/start 경로는 시뮬레이션 프로필에서만 사용합니다.

필수 운영 설정:

```text
DATABASE_URL
DATABASE_SSL_MODE=verify-full
DATABASE_SSL_CA_FILE=/app/certs/ap-northeast-2-bundle.pem
AUTH_MODE=cognito_session
AUTH_AWS_REGION
AUTH_SESSION_SECRET
AUTH_PUBLIC_ORIGIN
COGNITO_USER_POOL_ID
COGNITO_USER_POOL_CLIENT_ID
KVS_DEVICE_CHANNELS_JSON
KVS_BROKER_URL
KVS_BROKER_SECRET
PUSH_BROKER_URL
PUSH_BROKER_SECRET
PUSH_VAPID_PUBLIC_KEY
PETCAM_SHARE_SECRET
MAINTENANCE_SECRET
```

장치 최초 등록을 허용하는 짧은 시간 동안에는
`DEVICE_PROVISIONING_SECRET`, `DEVICE_PROVISIONING_MANIFEST_SHA256`,
`DEVICE_PROVISIONING_EXPIRES_AT`도 필요합니다.

실제 비밀 값은 Git, 이미지, Jetson 설정 파일에 넣지 않습니다. AWS Secrets
Manager 또는 ECS secret injection을 사용합니다. 공개 신뢰 앵커인 RDS CA
번들은 예외로 `certs/`에 출처와 SHA-256을 기록해 이미지에 고정합니다.
`.env.example`의 ARN과 주소는 동작하지 않는 예시입니다.

## 배포

`infra/cdk`는 팀 AWS의 개발 스택을 정의합니다. 먼저 SSO 로그인 후 synth로
변경될 리소스를 확인합니다.

```bash
aws sso login --profile malbut-team
cd infra/cdk
npm ci
npm test
npm run synth -- --profile malbut-team
```

`cdk deploy`는 VPC, ALB, ECS Fargate, RDS, KVS와 Lambda 등 과금
자원을 생성합니다. 비용·도메인·장치 ID를 팀에서 승인하기 전에는 실행하지
않습니다.

## 데이터 마이그레이션

`scripts/migrate.mjs`는 `db/migrations`를 이름순으로 한 트랜잭션에서 적용하며
PostgreSQL advisory lock으로 동시 실행을 직렬화합니다. 컨테이너는 시작 전에
이 스크립트를 실행합니다. 기존 개인 D1의 환경 전용 seed와 장치 credential은
이관하지 않고 새 장치를 provisioning해야 합니다.

`0003_robot_map`은 장치별 최신 지도 1개, 실시간 위치 상태 1개와 직렬화된
지도·주행 명령 큐를 추가합니다. 지도 PNG는 revision 기반으로만 교체되고,
장치 상태는 15초 이상 갱신되지 않으면 웹에서 오프라인으로 표시됩니다.

## 낙상 감지 설정

낙상 감지·Cloud VLM 동의 설정은 영상 녹화 설정과 분리되어 있습니다.
`0011_fall_settings` 적용 후 홈캠 설정 화면에서 소유자가 변경할 수 있고,
로봇의 기존 heartbeat로 설정을 전달하고 적용 회신 이력을 받습니다.
현재 실행 상태의 웹 연결은 아직 없으므로 회신 이력을 ‘감지 실행 중’으로 표시하지 않습니다.
[설정 API·DB 변경·남은 검증](docs/fall_settings.md)을 참고하세요.

## 장치 최초 등록

기존 개인 환경의 bearer token을 복사하지 않습니다. 배포 전에 다음 명령으로
새 토큰과 1회용 등록 manifest를 만듭니다.

```bash
npm run provisioning:bundle -- \
  --device-id malbut-sim-01 \
  --display-name "MALBUT simulator" \
  --owner-email owner@example.com \
  --source-profile sim
```

결과는 기본적으로 `.local/provisioning/malbut-sim-01/`에 생성되며 Git과 Docker
build context에서 제외됩니다. `device-token`은 터미널에 출력되지 않고 권한
`600`으로 저장됩니다. `runtime-values.json`의 manifest SHA-256과 만료 시각을
CDK deployment parameter로 전달한 뒤, 배포된 provisioning secret을 환경 변수로
주입해 manifest를 한 번 등록합니다.

```bash
read -rsp 'Provisioning secret: ' DEVICE_PROVISIONING_SECRET && echo
export DEVICE_PROVISIONING_SECRET
npm run provisioning:apply -- \
  https://homecam.example.com \
  .local/provisioning/malbut-sim-01/manifest.json
unset DEVICE_PROVISIONING_SECRET
```

등록 후 `device-token`을 Jetson의 systemd credential 파일에만 설치하고 원본
bundle은 안전한 비밀 저장소로 옮기거나 폐기합니다. provisioning endpoint는
설정된 만료 시각 이후 자동으로 `404`를 반환합니다.
