# MALBUT Homecam AWS CDK

홈캠 웹 백엔드의 `dev` AWS 환경을 만드는 독립 CDK 패키지다. 기본 리전은
서울(`ap-northeast-2`)이며 이 디렉터리의 명령은 CloudFormation 템플릿을
검증·합성할 뿐 자동으로 배포하지 않는다.

## 구성

```text
Internet
  │ HTTPS
  ▼
Route 53 + ACM ── ALB ── ECS Fargate (public subnet, public IP)
                              │
                              ├── RDS PostgreSQL (isolated subnet)
                              ├── Cognito User Pool (server-side auth API)
                              ├── KVS broker Lambda Function URL
                              └── Push broker Lambda Function URL

Jetson / Gazebo
  └── device bearer token ── ECS API ── 15분 STS ── KVS

장치별 KVS
  ├── {device}-p2p       : Media Storage DISABLED
  ├── {device}-storage   : Media Storage ENABLED
  └── {device}-archive   : H.264/AAC, 168시간 보존
```

주요 리소스:

- 2개 AZ의 public/database subnet. PoC 비용을 줄이기 위해 NAT Gateway는 사용하지 않음
- HTTPS ALB, public subnet Fargate 서비스, ARM64 task, 1~4개 자동 확장
- 암호화된 PostgreSQL 16 RDS와 Secrets Manager 관리 자격 증명
- 기존 사용자를 유지하는 Cognito User Pool과 서버 전용 admin password auth client
- 장치마다 분리된 P2P·Storage signaling channel과 archive stream
- Storage channel과 archive stream을 연결하는 custom resource. 신규 P2P channel은
  AWS 기본값인 Media Storage `DISABLED`를 유지
- 장치 한정 15분 STS 자격 정보를 발급하는 KVS broker Lambda
- VAPID Web Push broker Lambda
- 5분 주기 retention/push-outbox maintenance Lambda와 DLQ
- ALB·ECS·RDS·Lambda 로그와 CloudWatch 경보, SNS topic
- 애플리케이션 ECR 저장소와 30일 ALB access log 버킷
- 정확한 Git commit을 ARM64로 빌드해 ECR에 push하는 온디맨드 CodeBuild

## 보안 경계

- 장치, 브라우저, DB에는 AWS 장기 access key를 저장하지 않는다.
- KVS broker만 device-session 역할을 Assume할 수 있다.
- device-session 권한은 생성된 channel/stream ARN에만 적용되며 broker가
  발급하는 inline policy가 요청 장치 한 개로 다시 제한한다.
- Lambda Function URL은 서버 간 호출용 `AuthType=NONE`이지만 애플리케이션
  요청은 기존 broker 코드의 30초 HMAC으로 인증한다. Function URL을
  브라우저에 직접 연결하면 안 된다.
- 기존 KVS·Push broker handler가 환경 변수 계약을 사용하므로 PoC에서는
  HMAC·VAPID secret의 CloudFormation dynamic reference가 암호화된 Lambda
  환경 변수로 해석된다. 운영 전에는 Lambda가 Secrets Manager에서 값을
  런타임 조회·캐시하고 rotation을 따르도록 전환한다.
- RDS는 isolated subnet에 있고 Fargate security group에서만 5432 연결을
  허용한다.
- PostgreSQL TLS는 앱 이미지에 고정된 AWS 서울 리전 공개 루트 CA 번들을
  `DATABASE_SSL_CA_FILE`로 읽고 `verify-full` 검증을 수행한다. CA는 비밀이
  아니므로 CloudFormation parameter나 Secrets Manager에 복제하지 않는다.
- ECS task는 아웃바운드 통신을 위해 public subnet과 public IP를 사용하지만,
  security group은 ALB만 container port 3000에 접근하도록 한다. 인터넷에서
  task로의 직접 inbound 접근은 허용하지 않는다.
- 녹화 HLS는 같은 origin의 애플리케이션 proxy를 통해 제공해야 한다.
- ALB는 인증 상태를 판단하거나 Cognito로 redirect하지 않고 모든 HTTPS
  요청을 ECS target으로 전달한다. 로그인 화면과 사용자 API의 인증 경계는
  같은 origin의 애플리케이션이다. 장치·내부 API는 기존 bearer/HMAC 검증을
  계속 사용한다.
- Cognito Hosted UI와 User Pool domain은 생성하지 않는다. 서버만
  `ADMIN_USER_PASSWORD_AUTH`를 시작하고 임시 비밀번호·TOTP challenge에
  응답할 수 있다. 브라우저는 Cognito token이나 AWS 자격 증명을 받지 않는다.
- ECS task role의 Cognito 권한은 이 stack의 User Pool ARN에 대한
  `AdminInitiateAuth`, `AdminRespondToAuthChallenge`, `AdminGetUser`로 제한한다.
  app client는 secret과 OAuth flow를 사용하지 않는다.
- 브라우저 세션은 Secrets Manager가 만든 32-byte base64url
  `AUTH_SESSION_SECRET`으로 서버에서 보호하며, ECS에는 secret value로만
  주입한다. 저장소·CloudFormation output·브라우저 번들에 값을 넣지 않는다.
- ECS는 `AUTH_MODE=cognito_session`, Cognito pool/client ID, AWS region과
  public origin을 주입받는다. 앱이 미인증 페이지에는 같은 사이트의 로그인
  화면을 렌더링하고, 보호된 API는 자체 세션을 검증해 401을 반환한다.
- WebRTC 송신 계약은 H.264 영상과 Opus 오디오다. KVS Storage Session이
  Opus를 받아 저장 스트림에는 AAC로 변환하므로 archive stream의 정확한
  media type은 `video/h264,audio/aac`다.

## AWS 관리자가 준비할 값

CloudFormation 배포 시 다음 parameter가 반드시 필요하다. 값은 저장소에
커밋하지 않는다.

| Parameter | 내용 |
|---|---|
| `HomecamHostedZoneId` | 기존 public child Hosted Zone의 ID. `hyenje29.click` parent zone ID가 아니라 `malbut.hyenje29.click` zone ID |
| `HomecamHostedZoneName` | 홈캠 주소로 사용할 child zone apex. 현재 값은 `malbut.hyenje29.click` |
| `InitialOwnerEmail` | 최초 owner/broadcaster 이메일 |
| `DeviceProvisioningManifestSha256` | helper가 생성한 one-time provisioning manifest의 소문자 SHA-256 |
| `DeviceProvisioningExpiresAt` | provisioning 만료 UTC ISO 시각. 예: `2026-08-13T00:00:00.000Z` |
| `VapidSubject` | `mailto:` 또는 HTTPS 연락처 |
| `VapidPublicKey` | Web Push VAPID public key |
| `VapidPrivateKey` | Web Push VAPID private key |
| `ServiceDesiredCount` | 최초 배포 `0`, 이미지 push 후 업데이트 배포 `1` |

추가 선행 조건:

- `malbut.hyenje29.click` public Hosted Zone을 만들고 그 NS record를 parent
  `hyenje29.click` Hosted Zone에 위임한 상태
- `ap-northeast-2`에 CDK bootstrap 완료
- 최초 stack은 `ServiceDesiredCount=0`으로 생성해 ECR부터 준비
- `InitialOwnerEmail`로 Cognito 임시 사용자 초대 이메일을 받을 수 있어야 함
- 생성된 PostgreSQL에 AWS용 migration 적용
- 애플리케이션이 Cognito admin auth·서버 세션과 PostgreSQL 환경 변수를
  사용하도록 포팅 완료
- 관리자의 SNS alarm 구독 추가
- 조직 SCP가 CloudFormation, IAM, VPC, ECS, RDS, KVS, Lambda, Route 53,
  ACM, Secrets Manager 리소스 생성을 허용

현재 `malbut-team` 계정의 서울 리전에는 재사용 가능한 애플리케이션 VPC,
KVS, ECS, RDS, Lambda, Route 53, ACM, Cognito, Secret이 확인되지 않았다.
따라서 이 stack은 기존 리소스를 import하지 않고 dev 환경을 새로 만든다.
조직 baseline StackSet은 별도이며 이 stack에서 변경하지 않는다.

## 로컬 검증

Node.js 22에서 실행한다.

```bash
cd homecam_web/infra/cdk
npm ci
npm --prefix ../aws/kvs-broker ci
npm --prefix ../aws/push-broker ci
npm run build
npm test
npm run synth -- --no-lookups --quiet
```

장치 목록이나 이미지 tag를 바꾸려면 context를 사용한다.

```bash
npm run synth -- \
  -c stage=dev \
  -c region=ap-northeast-2 \
  -c 'deviceIds=["gazebo-homecam","jetson-homecam-01"]' \
  -c containerImageTag=git-commit-sha \
  --no-lookups --quiet
```

`cdk synth` 결과의 다음 내용을 배포 리뷰에서 확인한다.

- 각 장치의 channel 두 개와 168시간 stream 하나
- P2P channel에 Media Storage 연결이 없고 Storage channel만 `ENABLED`인지
- IAM policy에 wildcard KVS resource가 없는지
- RDS `PubliclyAccessible=false`
- Fargate `AssignPublicIp=ENABLED`와 ALB security group에서만 3000 ingress
- NAT Gateway가 없고 RDS는 isolated subnet을 사용하는지
- HTTPS listener와 ACM DNS validation
- HTTPS listener default action이 단일 target group forward이며
  `authenticate-cognito` listener rule이 없는지
- `malbut.hyenje29.click` child Hosted Zone apex의 ALB alias A record
- VAPID parameter의 `NoEcho=true`
- Cognito client에 `ALLOW_ADMIN_USER_PASSWORD_AUTH`와 token refresh만
  활성화되고 OAuth, client secret, Hosted UI domain이 없는지
- ECS task role의 Cognito admin auth 권한이 User Pool ARN 하나로 제한되는지

## 배포 인계

이 구현에서는 `cdk deploy`를 실행하지 않았다. 실제 배포 전 AWS 관리자가
`cdk diff`와 예상 비용을 검토해야 한다. 특히 ALB, RDS 및 KVS
녹화는 실행 시간과 트래픽에 따라 지속 비용이 발생한다.

배포 순서는 다음과 같다.

1. 도메인·VAPID·owner 정보와 조직 배포 역할을 확정한다.
2. `ServiceDesiredCount=0`으로 stack change set을 생성하고 IAM·삭제 정책·비용을 검토한다.
3. 최초 stack 생성 후 출력된 ECR에 AWS 포팅된 앱 이미지를 올린다.
4. 같은 stack을 `ServiceDesiredCount=1`로 업데이트하고 DB migration을 수행한다.
5. 앱의 provisioning helper로 정확한 manifest hash와 가까운 미래의 만료
   시각을 만들고, Secrets Manager의 generated bearer를 노출하지 않는
   관리 세션에서 `/api/internal/device-provisioning`에 한 번만 전송한다.
   이 단계에서 최초 owner membership과 device credential이 생성된다.
6. provisioning 만료 후 외부 모바일 네트워크에서 P2P, Storage, HLS, PTT,
   Push를 smoke test한다.

dev stack은 반복 실험을 위해 RDS·KVS·Secrets 등에 `DESTROY` 정책을 쓴다.
운영 stack을 만들 때는 RDS Multi-AZ, deletion protection, backup 보존,
Secrets·KVS·S3 `RETAIN`, private application subnet과 NAT/VPC endpoint,
아웃바운드 제어, WAF를 별도 적용해야 한다.

ECR은 tag immutability를 사용한다. 이미 사용한 tag를 덮어쓰지 말고 배포마다
Git commit SHA처럼 고유한 `containerImageTag`를 지정한다.

## ARM64 이미지 빌드

로컬 Docker나 AWS 장기 키를 필요로 하지 않는다. 최초 stack을
`ServiceDesiredCount=0`으로 만든 뒤 output의 CodeBuild project를 한 번
실행한다. project에 설정된 `GIT_SHA`는 CDK의
`containerImageTag`과 같은 40자 Git commit SHA이며, 빌드는 해당 commit을
재검증하고 `linux/arm64` 아키텍처만 push한다.

```bash
project_name=$(aws cloudformation describe-stacks \
  --profile malbut-team --region ap-northeast-2 \
  --stack-name MalbutHomecam-dev \
  --query 'Stacks[0].Outputs[?OutputKey==`ImageBuilderProjectName`].OutputValue' \
  --output text)
build_id=$(aws codebuild start-build \
  --profile malbut-team --region ap-northeast-2 \
  --project-name "$project_name" --query 'build.id' --output text)
aws codebuild batch-get-builds \
  --profile malbut-team --region ap-northeast-2 \
  --ids "$build_id" --query 'builds[0].buildStatus' --output text
```

CodeBuild service role은 이 stack의 ECR repository pull/push만 허용하며,
소스 URL과 repository URI는 build 시작 요청으로 바꾸지 않는다.
