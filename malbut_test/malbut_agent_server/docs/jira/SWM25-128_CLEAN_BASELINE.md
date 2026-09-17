# SWM25-128 혼재된 기존 변경을 제외하고 새로운 baseline 제작

## 문서 정보

| 항목 | 값 |
| --- | --- |
| Jira 하위 작업 | SWM25-128 혼재된 기존 변경을 제외하고 새로운 baseline 제작 |
| 기준일 | 2026-08-27 |
| 기준 저장소 | `SWM-malbut/malbut` |
| Clean baseline | `origin/main@af015c4f2d33bda0a182394d4feed6cd97c62b07` |
| 작업 branch | `chore/SWM25-128-clean-baseline` |
| 범위 | WIP 보존·분류, clean baseline 검증, RAI 적용 경계 확정 |
| 상태 | 로컬 완료 후보 — 사용자 검토·commit·PR CI 대기 |

## 1. 목표

현재 컴퓨터에서 진행한 Agent 개발과 Git `main` 사이의 차이가 커진 상태를
해소한다. 혼재된 변경을 한꺼번에 `main`에 합치지 않고 먼저 손실 없이
보존·분류한다. 이후 최신 `origin/main`에서 검증 가능한 clean baseline을
만들고, SWM25-129부터 필요한 변경만 독립적으로 테스트하고 선별 이식할 수
있게 한다.

또한 RAI를 단순히 모방하는 데 그치지 않고 실제 `rai-core`를 선택 가능한
Agent runtime으로 적용할 목표 구조를 확정한다. RAI는 대화와 Tool 선택을
담당하되, Malbut의 Capability·Safety·승인·실행 원장을 우회할 권한은 갖지
않는다.

이 하위 작업은 기존 WIP 통합이나 RAI 제품 코드 구현이 아니다. 안전하게
통합을 시작할 기준점과 책임 경계를 만드는 작업이다.

## 2. 범위

### 포함

- 원본 dirty worktree의 상태와 파일 지문 기록
- tracked·untracked WIP의 로컬 recovery pack 생성과 복원 검증
- 모든 변경 경로를 Story·책임 영역별로 분류
- 최신 `origin/main` 기반 독립 branch와 worktree 생성
- clean baseline의 build·test·installed import 검증
- 실제 RAI sidecar와 `MalbutRaiTool`의 권한 경계 확정
- 다음 하위 작업에서 이식할 항목과 제외할 항목 기록

### 제외

- WIP 전체를 하나의 commit으로 만들거나 `main`에 통째로 병합
- 기존 Draft PR #36의 merge·rebase·수정
- RAI sidecar, IPC 또는 Provider 제품 코드 구현
- CapabilityRegistry·Safety·confirmation·ledger 코드 이식
- Gazebo·Nav2 goal 전송과 물리 효과 검증
- Homecam·AWS·STT·TTS 연결

## 3. 원본 WIP 기준선

원본 작업 경로는 `<workspace>/src/malbut`이며 다음 상태에서
보존을 시작한다.

| 항목 | 기준값 |
| --- | --- |
| branch | `feat/SWM25-93-monitor-room-safe-rebuild` |
| HEAD/upstream | `ce538e61022150e5a4bb84c9381f6e9a0bc207f4` |
| staged | 0 |
| tracked modified | 89 |
| nonignored untracked | 450 |
| 전체 분류 대상 | 539 |

원본 worktree에서는 `reset`, `clean`, `stash -u/-a`, branch 전환,
rebase, merge, commit과 `git add -A`를 수행하지 않는다. 기존 worktree도
삭제하거나 재사용하지 않는다.

### 3.1 복구 보존 규칙

Git 저장소 밖의 다음 보호 경로에 recovery pack을 둔다.

```text
<private-recovery-root>/SWM25-128/<UTC>/
```

recovery pack은 다음만 포함한다.

- tracked binary patch
- Git이 인식하는 nonignored untracked 파일 archive
- source branch·HEAD·status와 canonical fingerprint
- 539개 경로의 분류 및 SHA-256 manifest
- archive와 manifest checksum
- 독립적인 복원 절차

ignored `.env`, database, credential, cache, build/install/log와 raw media는
읽거나 포함하지 않는다. 디렉터리는 `0700`, 파일은 `0600` 권한으로
제한한다. 임시 clone에서 patch와 archive를 적용한 뒤 원본과 동일한 상태와
파일 hash를 재현해야 recovery가 검증된 것으로 판정한다.

검증 완료된 recovery pack은 다음 위치에 있다.

```text
<private-recovery-root>/SWM25-128/20260827T112926Z
```

| 검증 항목 | 결과 |
| --- | --- |
| directory/file 권한 | `0700` / `0600` |
| tracked/untracked/staged 복원 | `89 / 450 / 0`, exact match |
| 539개 mode·size·SHA-256 | PASS |
| archive member | 450개, PASS |
| canonical fingerprint 4종 | 전부 exact match |
| checksum 검증 | PASS |
| `SHA256SUMS` 파일 SHA-256 | `4c4f8aaa2d0c52c6645ba52976cea248bda123d027ff363ca4e50b07e33cc4e1` |
| 임시 clone 정리 | PASS |

Git patch만으로는 tracked 파일의 group-write mode를 완전히 복원하지 못하는
사례가 확인됐다. Private manifest의 검증된 mode만 적용하는
`RESTORE_MODES.py`를 복구팩에 포함해 두 번째 전체 복원 검사를 통과했다.

고신뢰 secret 형식 검사에서 탐지된 테스트 파일 1건은 코드 흐름을 값 없이
검토했다. 테스트 함수 내부의 synthetic secret canary이며 외부 provider,
환경변수 또는 network에서 사용되지 않는다. 테스트 목적의 정확한 복원을
위해 보호된 archive에는 포함하지만 값은 공개 문서·Jira·로그에 기록하지
않는다. 실제 credential 탐지 건수는 0건이어야 한다.

### 3.2 분류 계약

모든 경로에는 하나의 primary owner를 부여하고 필요한 secondary tag를
추가한다.

| Primary owner | 의미 |
| --- | --- |
| `SWM25_121` | 텍스트 Agent→승인→Gazebo Nav2 수직 단면 |
| `SWM25_120_126_VOICE` | STT·마이크·실로봇 음성 연동 |
| `HOMECAM_AWS` | Homecam Web·KVS·Cognito·AWS staging |
| `AGENT_SHARED_STAGE_A_B` | 공통 Agent domain/application/ports와 안전 기반 |
| `PHYSICAL_EXECUTION` | 실제 로봇 권한·상태·실행기·취소 |
| `STAGE_C_D_RESEARCH` | Observation·dataset·planner·VLA·HELIX |
| `GENERATED_RUNTIME_OR_SECRET` | 생성물·runtime·ignored 민감 자산 |

분류 manifest에는 경로, Git 상태, owner, dependency 역할, disposition,
SHA-256, 크기, 분류 이유와 review 상태를 기록한다. 완료 시 539/539 경로가
분류되고 `manual_review`가 0이어야 한다.

최종 분류 결과는 다음과 같다.

| Disposition | 파일 수 |
| --- | ---: |
| Backlog | 103 |
| 후속 Story 보존 | 224 |
| 공유 선행조건 | 149 |
| SWM25-129 선택 이식 | 6 |
| SWM25-130 선택 이식 | 5 |
| SWM25-131 선택 이식 | 10 |
| SWM25-132 선택 이식 | 34 |
| SWM25-133 선택 이식 | 8 |

합계는 539개이며 `manual_review=0`, synthetic secret canary 1건,
actual credential 0건이다.

## 4. Clean baseline

clean baseline은 로컬의 오래된 `main`이 아니라 2026-08-27 실행 시점에
원격에서 다시 확인한 다음 commit으로 고정한다. 최초 계획 후 `main`이
fast-forward됐으므로 이전에 조사한 `806f57c`가 아닌 최신 원격 HEAD를
사용한다.

```text
origin/main@af015c4f2d33bda0a182394d4feed6cd97c62b07
```

독립 작업 위치는 다음과 같다.

```text
branch:   chore/SWM25-128-clean-baseline
worktree: <development-root>/malbut-swm25-121-ws
```

이 baseline에는 최신 제품·지도·Autonomy 변경이 포함되지만, 현재 dirty
SWM25-93 WIP에서 개발한 Stage A/B Agent 구조, durable confirmation,
execution ledger/outbox, trusted result와 monitor-room 실행 기능이 전부
포함돼 있다고 주장하지 않는다. 해당 기능은 분류 manifest를 근거로
SWM25-129 이후에 필요한 dependency closure만 선별 이식한다.

## 5. RAI 적용 목표 구조

실제 제품 흐름은 다음 경계로 고정한다.

```text
사용자 텍스트
  → Malbut session/auth router
      ├─ pending confirmation의 네/아니요
      │    → deterministic confirmation handler
      └─ 일반 요청
           → sanitized request + server-owned identity
           → RAI Agent sidecar의 bounded single-proposal turn
           → TextReply 또는 untrusted ActionProposalDTO 반환
           → Malbut process의 MalbutRaiTool adapter
           → CapabilityRegistry + strict arguments
           → post-LLM fresh RobotState + deterministic Safety
           → 사용자 승인
           → durable action/outbox + dispatch preflight
           → 기존 Nav2/Homecam/제한된 ROS 2 adapter
           → known·redacted trusted terminal result
           → 다음 RAI turn의 sanitized context
```

### 5.1 RAI가 소유하는 책임

- 자연어 대화와 고수준 의도 해석
- CapabilityRegistry에서 투영된 schema 중 고수준 Tool 선택
- Tool 결과를 다음 대화 context에 연결
- 명시적으로 주입된 LLM provider를 통한 응답 생성

### 5.2 RAI가 소유하지 않는 책임

- 사용자·session·robot identity 발급
- action 승인과 confirmation ticket 판정
- robot/map/state/policy revision 생성
- capability allowlist와 argument 안전 판정
- Nav2 goal ID, ROS 요청, physical authority 생성
- durable state 전이, 중복 실행 방지와 crash reconciliation
- E-stop·Collision Monitor·forbidden zone 판단

`MalbutRaiTool`은 실행기가 아니라 Malbut process가 소유하는 proposal
adapter다. 모델이 생성한 `skill`, `arguments`와 설명용 metadata만 Malbut
application 경계에 전달한다. Tool schema는 수동으로 복제하지 않고
CapabilityRegistry의 비민감 projection을 source of truth로 생성한다.
`approved`, `robot_id`, `map_revision`, `state_version`, `action_id`,
`goal_id`, `policy_version`처럼 authoritative한 필드는 모델의 출력에서
신뢰하지 않는다.

RAI의 범용 ROS topic/service/action, shell Tool과 `rai.tools.ros2`는
등록하거나 제품 코드에서 import하지 않는다. Nav2와 Homecam은 기존의
제한된 Malbut adapter를 통해서만 접근한다.

### 5.3 배포와 dependency 결정

초기 적용 기준은 `rai-core==2.12.1`, Python 3.10 전용 sidecar process다.
검증된 wheel SHA-256은 다음과 같다.

```text
e38b90691710d1d2ddb00feabef8d5366d54c2de85cff32429fe73aecd8a05ab
```

호환성 감사에서는 24개 직접 dependency, 128개 설치 artifact와 약 917MB의
임시 venv가 확인됐다. 모든 artifact에 SHA-256이 있었으며 canonical
dependency manifest digest는 다음과 같다.

```text
9847407a83a51a891e8630b344b91bd9ee90329a26e18a5ca8384b3ab1b36922
```

core Agent import와 synthetic no-I/O LangChain structured Tool 1회 호출은
통과했다. 이는 실제 `MalbutRaiTool` 또는 제품 통합 검증이 아니다. ROS
Humble을 source한 환경에서 `rai.tools.ros2`는 `tf_transformations` 부재로
import에 실패했고 `rai_interfaces` 부재 경고도 발생했다. 해당 Tool은
미검증·제품 사용 금지로 판정한다.

RAI 환경은 ROS 제공 버전을 NumPy `1.21.5→1.26.4`, OpenCV
`4.5.4→4.11`, PyYAML `5.4.1→6.0.3`, requests `2.25.1→2.34.2`로
shadow한다. GUI/headless OpenCV wheel도 92개 RECORD 경로에서 겹친다.

따라서 `rai-core`를 `malbut_agent_server/setup.py`, ROS overlay 또는 system
Python에 추가하지 않는다. RAI sidecar에서는 ROS setup을 source하지 않는다.
별도 venv와 격리된 working directory에서 lazy import하고 기본값은 OFF로
둔다. RAI import가 root coloredlogs handler를 추가하고 pydub/ffmpeg 경고를
낼 수 있으며 `rai-config-init`은 현재 디렉터리에 `config.toml`을 작성하므로,
제품 CWD에서 config 초기화를 실행하지 않는다.

LLM은 명시적으로 주입하고 자동 vendor 초기화를 금지한다. 격리된 명시
config에서 Langfuse·LangSmith tracing을 OFF로 두고 tracing credential
환경변수를 제거한다. RAI Agent와 ToolRunner의 기본 INFO 로그가 사용자
message, Tool argument와 결과 일부를 남길 수 있으므로 logger 차단·redaction
및 canary 불포함 검증 전에는 실제 사용자 텍스트를 연결하지 않는다.

### 5.4 한 turn당 하나의 proposal 계약

stock RAI ReAct graph는 Tool 실행 뒤 다시 LLM으로 돌아갈 수 있다. Malbut
MVP는 이 loop를 그대로 실행하지 않고 다음 상한을 적용한다.

- Malbut→RAI sidecar의 단일 request/response IPC만 허용한다.
- RAI sidecar가 Malbut, ROS, Nav2 또는 Homecam으로 callback하지 않는다.
- 한 turn은 `TextReply` 또는 `ActionProposalDTO` 최대 하나만 반환한다.
- 첫 action proposal이 만들어지면 RAI graph를 halt/park하고 재추론하지 않는다.
- session·turn·request identity는 Malbut가 envelope에 넣으며 모델이 만들지 않는다.
- request identity와 proposal fingerprint가 같으면 같은 결과만 replay한다.
- partial RAI 처리 이후 기존 Provider로 자동 failover하지 않는다.
- Agent backend는 turn 시작 전에 명시적으로 하나만 선택한다.
- confirmation ticket이 발급된 뒤의 응답은 RAI가 아닌 confirmation handler가
  처리한다.
- 다음 context에는 known·redacted typed result만 허용한다. confirmation token,
  action/goal ID, 좌표, raw evidence, private device ID와 credential은 제외한다.

RAI sidecar와 IPC 구현은 텍스트 요청·승인을 소유하는 SWM25-131에서 별도
Plan 승인을 받아 진행한다. SWM25-129에는 RAI를 추가하지 않는다.
승인된 action의 실제 Nav2 연결은 SWM25-132가 소유한다.

## 6. 검증 매트릭스

| 검증 | 명령·대상 | 완료 기준 | 결과 |
| --- | --- | --- | --- |
| CI selector | `.github/scripts/test-detect-ci-modules.sh` | selector contract 통과, ROS job 선택 | PASS — `ros=true`, 나머지 `false` |
| Clean build | GitHub CI와 동일한 10개 ROS package | build 대상 전체 성공 | PASS — 10 packages, 33.21s |
| ROS package test | Patrol 및 CI 대상 7개 package | fail 0 | PASS — CI 545 tests, Patrol 35 tests |
| Focused pytest | Agent·Gazebo·Roaming을 각각 수집·실행 | fail 0, skipped 0 | PASS — 153 / 194 / 59 |
| Contract smoke | Xacro 생성과 navigation `--show-args` | 오류 0, 실제 launch 0 | PASS |
| Installed smoke | install space에서 package·CLI import | import error 0 | PASS — 10 prefixes·7 imports·CLI 2개 |
| Recovery replay | 임시 clone에 patch·archive 복원 | 539개 상태·hash 일치 | PASS |
| Original preservation | 작업 전후 canonical fingerprint | 동일 | PASS |
| Secret gate | nonignored WIP 및 공개 산출물 | 실제 credential 0 | PASS |
| GitHub PR CI | 원격 PR의 CI·PR Guard | 전체 성공 | 사용자 승인 후 실행 |

package별 pytest는 별도 working directory에서 실행한다. 같은 이름의 ROS
lint test module을 한 번에 수집할 때 발생할 수 있는 `ImportMismatchError`를
제품 결함으로 오인하지 않는다. build/install/log는 clean source 밖의 임시
경로를 사용한다.

세부 결과는 Agent 153 passed, Gazebo 194 passed·3 warnings, Roaming 59
passed·2 warnings다. GitHub CI와 같은 7-package test는 545 tests, 실패·오류·
skip 0이다. Patrol은 35 tests 중 34 passed와 copyright check 1 skipped이며
실패·오류는 0이다. 경고는 xacro의 Python 3.11 예정 변경과 ament flake8의
`SelectableGroups` deprecation이다. 실제 node를 시작하지 않은
`navigation --show-args`와 16,211-byte URDF 생성도 통과했다. 종료 후 소유한
pytest·colcon·ROS 2·Gazebo process는 0개였다.

이 검증에서는 실제 Gazebo process, Nav2 goal, ROS physical command, LLM/API
호출을 수행하지 않는다. 따라서 물리 실행이나 RAI 제품 통합 성공의 증거로
사용하지 않는다.

## 7. 완료 조건

- [x] 현재 WIP가 Git 저장소 밖의 보호된 recovery pack으로 보존됨
- [x] 임시 복원 검증에서 원본 상태와 파일 hash가 일치함
- [x] 작업 전후 원본 dirty worktree fingerprint가 동일함
- [x] 539개 변경 경로가 누락 없이 책임 영역별로 분류됨
- [x] 미분류·수동 검토 대기 항목이 0개임
- [x] `origin/main@af015c4` 기반 독립 branch와 worktree가 생성됨
- [x] clean baseline build·test·installed import가 통과함
- [x] RAI sidecar·MalbutRaiTool·기존 안전 계층의 책임 경계가 확정됨
- [x] RAI generic ROS/shell Tool 금지와 optional/default-OFF 원칙이 확정됨
- [x] 제품 코드·API·DB schema·ROS runtime 변경이 0개임
- [x] 사용자 검토 전 commit·push·PR·merge가 수행되지 않음

## 8. SWM25-129 인계 조건

SWM25-129 이후 작업은 이 문서와 private 분류 manifest를 기준으로 시작한다.

- SWM25-129는 roaming 없는 Small House·static Nav2 환경만 구성하며 RAI나
  Agent request 처리를 추가하지 않는다.
- SWM25-130은 고정 target을 device·map·revision과 결속한다.
- SWM25-131은 authenticated text request·text confirmation과 함께 RAI
  sidecar를 optional/default-OFF adapter로 추가한다.
- `MalbutRaiTool`의 unknown·malformed proposal은 side effect 0회여야 한다.
- 기존 Provider 경로는 default/reference backend로 유지한다. 한 turn의 RAI
  partial 처리 뒤 자동 fallback에는 사용하지 않는다.
- 필요한 Stage A/B 코드는 파일 단위가 아니라 dependency closure 단위로
  선별 이식한다.
- 실제 Nav2 goal은 SWM25-130의 단일 목적지 계약과 SWM25-131의 승인 흐름이
  준비되기 전까지 전송하지 않는다.

## 9. Git·Jira 완료 절차

로컬 구현과 검증이 완료되면 먼저 변경 파일, recovery 결과, test 결과와
남은 위험을 사용자에게 보고한다. 이 시점에는 commit·push·PR을 만들지
않는다.

사용자가 결과를 승인하고 Git 진행을 요청한 뒤에만 다음 이름을 사용한다.

```text
commit: SWM25-128 혼재된 기존 변경을 제외하고 새로운 baseline 제작
PR:     [SWM25-128] 혼재된 기존 변경을 제외하고 새로운 baseline 제작
target: main
```

CI와 사용자 검토 후에만 merge한다. Jira에 baseline SHA, test 결과,
commit과 PR을 기록하고 사용자가 `다했다`고 확인하기 전에는 SWM25-129를
시작하지 않는다.

현재 저장소에는 PR 검증용 GitHub CI와 PR Guard가 있으며, SWM25-128에서
배포를 수행하는 CD job은 없다. 따라서 Jira의 검증 조건은 `CI/CD 통과`가
아니라 `로컬 CI-equivalent 검증 및 GitHub PR CI 통과`로 기록한다. PR
Guard가 허용하는 branch prefix를 사용하기 위해 `task/` 대신 `chore/`를
선택했다.
