# MALBUT Homecam Web

모바일 PWA, 홈캠 장치 API, AWS Kinesis Video Streams broker, 최근 7일
이벤트·녹화 재생과 로봇 지도·목적지 주행 기능을 제공하는 독립 Node.js 서비스입니다. ROS 패키지가
아니며 `COLCON_IGNORE`로 로봇 워크스페이스 빌드와 분리됩니다.

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

`npm test`는 Next.js production build, 인증·스트리밍 계약 테스트와 PGlite를
사용한 PostgreSQL 스키마·이벤트 outbox 테스트를 실행합니다. 실제 RDS 통합은
배포된 개발 스택에서 별도로 smoke test합니다.

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
- 저장 SLAM 지도와 현재 위치는 장치 bearer API로 RDS에 동기화
- 가족은 지도를 조회할 수 있고 지도 생성·목적지 주행 명령은 소유자만 등록
- 목적지 좌표는 AWS에서 직접 `/cmd_vel`로 변환하지 않고, 장치의 Nav2
  preview/start API와 최신 costmap·Zone 검사를 반드시 통과

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

## Agent semantic 내부 경계

`POST /api/internal/agent/semantic`은 브라우저 cookie를 Agent에 전달하지 않고,
독립 service bearer를 먼저 검사한 뒤 고정된 Agent user·Cognito subject·owner
email·device를 서버 설정에서 선택합니다. 하나의 DB snapshot에서 다음 조건을
모두 만족할 때만 finalized semantic map을 반환합니다.

- 설정된 subject와 email이 정확히 일치하는 Web session이 활성·미폐기 상태
- 같은 email이 해당 device의 `owner` membership을 보유
- 해당 device에 finalized `robot_maps` snapshot이 존재

응답은 5초 TTL, server-owned membership/map generation, content digest와 HMAC
서명을 포함합니다. raw subject, email, session token과 secret은 반환하지
않습니다. 동일 finalized PUT 재시도는 generation을 유지하지만 material
변경과 A→B→A 복원은 매번 새 generation을 발급합니다. 이 응답은 방을 어떤
snapshot으로 해석했는지 증명할 뿐, Nav2·camera·KVS 실행 capability가 아닙니다.
owner가 로그아웃해 모든 Web session이 폐기·만료되면 endpoint도 fail-closed합니다.

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
