# SWM25-133 실제 Gazebo에서 전체 흐름을 한 번 완료

## 1. 결론

SWM25-133은 SWM25-131의 텍스트 요청·승인과 SWM25-132의 durable 실행 경로를
새로 구현하는 작업이 아니다. 두 경로를 **clean installed overlay에서 한 명령으로
시작·검증·종료하는 인수 시험**으로 묶은 작업이다.

```text
clean Git HEAD와 source <-> installed byte attestation
  -> source_tree_digest 생성
인증된 텍스트 요청: "거실로 가줘"
  -> mock Provider가 navigate({"location":"거실"}) 1건 제안
  -> Safety·active map target 검사
  -> durable confirmation 1건 생성
  -> 승인 전 RobotAction/dispatch/Nav2 goal 0건 확인
  -> 텍스트 승인: "네"
  -> approved confirmation + RobotAction 1건
  -> 승인 후 fresh RobotState·Safety·target 재검사
  -> durable dispatch intent 1건
  -> Robot Web preview target 검증 1회
  -> Robot Web start 1회
  -> actual NavigateToPose goal 1개
  -> Nav2 SUCCEEDED
  -> RobotAction·outbox known terminal
  -> 같은 승인 replay와 늦은 새 "네"의 추가 효과 0건
  -> 소유 process·ROS node·socket 0건으로 종료
```

2026-08-29 provenance 도입 전 headless Small House rehearsal(run-2)에서 제품
흐름을 1/1 완료했다. 승인 전에
Nav2 goal은 0개였고, 승인 후 proposal·confirmation·RobotAction·dispatch intent·
Robot Web start·distinct Nav2 goal·terminal result가 각각 정확히 1개였다.
`simulation=true`, `physical_authorized=false`를 유지했으며 replay 추가 효과와
종료 후 잔류 자원은 모두 0개였다.

이 rehearsal은 strict source provenance 계약을 추가하기 전에 생성되었으므로
최종 SWM25-133 인수 evidence로 사용하지 않는다. 최종 provenance-sealed run은
현재 변경을 Git에 commit하고, 그 commit으로 clean overlay를 다시 build한 뒤
수행해야 한다. 따라서 이 문서는 아직 최종 clean run 완료를 주장하지 않는다.

## 2. 목표와 달성 조건

### 목표

개발자가 여러 terminal을 수동으로 조합하지 않아도 설치된 Malbut 산출물로
텍스트 요청부터 실제 Gazebo Nav2 terminal까지의 연결 상태를 재현 가능하게
판정한다. “성공한 것처럼 보였다”가 아니라 서로 독립된 Agent 원장, Robot Web
호출과 Nav2 status 증거가 모두 일치할 때만 성공 evidence를 발행한다.

### 달성 조건

- [x] `--check`가 설치 산출물을 검사하되 process, HTTP server와 Nav2 goal을
  만들지 않는다.
- [x] 명시적인 `--run --execute-approved-simulation`에서만 simulation 실행을
  허용한다.
- [x] 승인 전 Nav2 goal 0개, 승인 후 proposal·confirmation·RobotAction·dispatch
  intent·Robot Web verified preview target·start·distinct Nav2 goal·known
  terminal을 각각 정확히 1개로 검증한다.
- [x] 승인 replay와 pending confirmation이 없는 늦은 `네`가 추가 실행을 만들지
  않는지 확인한다.
- [x] content-free evidence를 owner-only 파일로 원자적으로 발행하고, 종료 뒤
  소유 process·ROS node·socket과 강제 종료가 모두 0개인지 확인한다.
- [x] exact clean Git HEAD 및 tracked source와 installed 파일의 byte equality를
  확인하고 evidence v2를 `source_tree_digest`에 결속한다.
- [ ] 변경 commit과 동일한 clean installed overlay에서 최종
  provenance-sealed run을 수행하고 Jira 실행 기록에 결과를 첨부한다.

## 3. SWM25-132와 무엇이 다른가

SWM25-132는 다음 제품 경로를 만들었다.

```text
approved confirmation
  -> durable RobotAction
  -> fresh preflight
  -> durable dispatch intent
  -> SWM25-130 named-navigation façade
  -> Robot Web
  -> Nav2
```

SWM25-133은 이 경로 위에 새로운 실행 권한이나 우회 경로를 추가하지 않는다.
인수 runner가 public Agent HTTP API를 실제 client처럼 사용하고, 다음 세 관측을
교차 검증한다.

1. SQLite read-only observer: confirmation, RobotAction과 outbox 상태
2. loopback counting proxy: Robot Web preview/start/cancel 호출 횟수와 preview
   request가 현재 fixture의 expected target과 일치했다는 digest 검증
3. read-only ROS observer: `/navigate_to_pose/_action/status`의 distinct goal과
   terminal status

따라서 Agent 원장만 `SUCCEEDED`이거나, HTTP start만 202를 반환하거나, Gazebo
화면에서 로봇이 움직이는 것 중 하나만으로는 합격하지 않는다. 세 증거가 exact
once로 일치하고 cleanup까지 완료되어야 evidence 타입을 만들 수 있다.

## 4. 실행 순서

runner가 한 process 안에서 다음 자원을 소유하고 수명 순서를 관리한다.

```text
source tree exact toplevel·HEAD·clean 상태 검사
  -> tracked source <-> installed artifact byte equality 검사
  -> commit + Git tree 기반 source_tree_digest 생성
  -> 비어 있는 격리 ROS_DOMAIN_ID 확인
  -> private 임시 runtime과 Small House map fixture 생성
  -> Nav2 status 관측 window 시작
  -> Small House/Nav2/Robot Web launch
  -> 연속 2회의 exact readiness 확인
  -> Robot Web counting proxy 시작
  -> mock Provider 기반 installed Agent server 시작
  -> 인증된 conversation과 텍스트 요청 생성
  -> 승인 전 side effect 0건 확인
  -> "네" 승인
  -> known terminal과 actual Nav2 SUCCEEDED 대기
  -> 동일 승인 replay + 늦은 새 승인 입력
  -> 0.25초 간격 8 samples, 총 2초간 모든 effect count 불변 확인
  -> Agent -> proxy/observer -> Gazebo 순서로 종료
  -> SQLite quick_check와 process/node/socket 잔류 검사
  -> 성공 evidence 신규 파일 발행
```

목적지 fixture, 인증 token, user/session/confirmation/action ID와 포트는 runner가
실행마다 private하게 생성한다. Agent 요청은 현재 필수 MVP 범위인 단일
`navigate(location="거실")`이며 Provider는 결정적인 `mock`을 사용한다. 실제
OpenAI/RAI proposal smoke는 SWM25-131에서 별도로 검증했으므로, 이번 인수 시험이
네트워크나 LLM 응답 변동 때문에 흔들리지 않게 하기 위해서다.

## 5. 실행 방법

먼저 검증할 source와 격리 install overlay를 동일한 revision으로 build한다.
아래 placeholder는 각 개발 환경의 실제 경로로 바꾼다.

```bash
cd <source-worktree>
source_tree="$(pwd -P)"
source_commit="$(git rev-parse HEAD)"

source /opt/ros/humble/setup.bash
source <isolated-overlay>/install/setup.bash
```

### 5.1 non-actuating 설치 검사

```bash
ros2 run malbut_scenarios run_text_gazebo_acceptance -- \
  --check \
  --source-commit "$source_commit" \
  --source-tree "$source_tree"
```

정상 출력은 public-safe installed digest와 다음 경계만 포함한다.

```json
{
  "installed_digest": "<sha256>",
  "mode": "check",
  "nav2_start_count": 0,
  "physical_authorized": false,
  "simulation": true,
  "source_tree_digest": "<sha256>",
  "status": "ok"
}
```

`--check`에는 execution flag, evidence 경로, GUI 또는 ROS domain을 함께 줄 수
없다. check mode는 Gazebo, Agent server, HTTP listener, SQLite runtime이나 ROS
goal을 시작하지 않는다.

### 5.2 headless 인수 실행

evidence parent는 미리 owner-only `0700`으로 준비하고, 매 실행마다 존재하지
않는 새 절대 파일명을 사용한다.

```bash
evidence_root=<absolute-private-evidence-directory>
ros_domain_id=85  # 현재 사용하지 않는 1~100 범위 값으로 변경
install -d -m 0700 "$evidence_root"

ros2 run malbut_scenarios run_text_gazebo_acceptance -- \
  --run \
  --execute-approved-simulation \
  --source-commit "$source_commit" \
  --source-tree "$source_tree" \
  --ros-domain-id "$ros_domain_id" \
  --evidence "$evidence_root/run-1.json"
```

화면으로 확인하려면 같은 명령에 `--gui`를 추가한다. run mode에서는 사용하지
않는 1~100 범위의 `--ros-domain-id`가 필수다. runner가 domain을 암묵적으로
선택하지 않으며, 선택한 domain에 기존 ROS node가 있으면 실행 전에
fail-closed한다. check mode에는 `--ros-domain-id`를 줄 수 없다.

정상 run 출력은 `status=succeeded`, manifest digest, simulation/physical 경계만
포함한다. 비정상 run은 private exception 내용을 출력하지 않고 bounded
`error_code`로 실패한다. 실패한 실행은 성공 evidence를 만들지 않는다.

## 6. default-OFF와 안전 경계

- `--run`만으로는 실행할 수 없다. 별도
  `--execute-approved-simulation`이 없으면 설치 탐색 전에 거절한다.
- 이 runner는 `simulation=true`, `physical_authorized=false`만 만든다. 실로봇
  authority나 실제 hardware E-stop source를 구성하지 않는다.
- public Agent HTTP API로 요청·승인한다. SQLite에는 SELECT-only observer로
  접근하며 원장 값을 수정하지 않는다.
- Nav2 observer는 status topic만 구독한다. goal publish, action start와 cancel
  client를 갖지 않는다.
- 실제 start 권한은 SWM25-132 worker에만 있다. runner와 counting proxy는
  Safety, confirmation, durable intent 또는 named-navigation façade를 우회하지
  않는다.
- 승인 전 goal과 start가 하나라도 관측되면 즉시 실패한다.
- known terminal과 actual Nav2 `SUCCEEDED`가 모두 있어야 성공한다. timeout이나
  결과 불명은 성공으로 보정하거나 자동 재전송하지 않는다.
- replay 뒤 confirmation/action/outbox/Robot Web/Nav2 count가 하나라도 늘면
  실패한다. 단일 순간 비교가 아니라 0.25초 간격 8 samples, 총 2초의 stability
  window 전체에서 불변이어야 한다.
- child process는 shell 없이 exact argv와 새 process session으로 시작한다.
  종료도 소유 session에만 `SIGINT -> SIGTERM -> SIGKILL` 순서로 제한한다.
- primary 실행이 실패해도 각 owner cleanup을 독립적인 best-effort로 계속한다.
  counting proxy는 active upstream과 downstream connection을 모두 닫고 owner
  thread/handler를 bounded deadline 안에서 join한다.
- 성공 evidence는 cleanup이 완전하고 강제 종료가 0회일 때만 생성된다.

## 7. content-free evidence

evidence 형식은 `malbut.text-gazebo-e2e-evidence.v2`이다. 성공 receipt에는 다음
정보만 들어간다.

- exact clean HEAD와 Git tree를 SHA-256으로 결속한 `source_tree_digest`
- build/source binding을 위한 commit과 installed artifact digest
- private goal ID와 runtime binding 원문 대신 SHA-256 digest
- readiness, confirmation, RobotAction, dispatch와 navigation의 제한된 상태
- proposal, confirmation, RobotAction, intent, verified preview target, start,
  goal, terminal과 replay의 집계 횟수
- monotonic duration
- cleanup 완료 여부와 잔류 process/node/socket 및 강제 종료 횟수
- `simulation=true`, `physical_authorized=false`

다음 내용은 evidence, stdout/stderr 또는 일반 로그에 넣지 않는다.

- 요청·승인 원문과 Provider payload
- 인증 token, Cookie와 CSRF 값
- user/session/conversation/confirmation/action/operation/goal 원본 ID
- device/map ID와 revision 원문
- pose, 좌표, private fixture·DB·환경변수·host 경로

evidence parent는 owner `0700`, 파일은 `0600`이어야 한다. symlink component와
기존 파일 overwrite를 거절하며 temporary file, fsync와 hard-link publish로
원자적으로 신규 파일만 만든다.

### 7.1 source/install provenance가 의미하는 것

`--source-tree`는 단순 참고 경로가 아니다. runner는 다음 조건을 모두
fail-closed로 확인한다.

1. 경로가 canonical absolute Git toplevel이다.
2. HEAD가 `--source-commit`의 full lowercase object ID와 정확히 같다.
3. tracked 변경과 untracked 파일을 포함한 porcelain 상태가 비어 있다.
4. runner가 선택한 모든 source binding이 Git tracked regular file이며 source
   tree 밖으로 나가지 않고 symlink를 사용하지 않는다.
5. 대응 installed artifact도 symlink가 아닌 regular file이고 source와
   byte-for-byte 같다.
6. 비교 후 HEAD·Git tree·clean 상태가 그대로인지 다시 확인한다.

반환되는 `source_tree_digest`는 commit과 Git tree를 domain-separated SHA-256으로
결속한다. installed artifact digest와 함께 기록되므로 “어떤 clean source를 어떤
설치본으로 실행했는가”를 content-free하게 확인할 수 있다.

## 8. 2026-08-29 pre-provenance rehearsal 결과

isolated installed overlay에서 수행한 기존 run-2는 strict source/install
attestation을 도입하기 전의 rehearsal이다. 제품 폐루프와 cleanup을 검증한
참고 결과는 다음과 같다.

```text
states:
  readiness=ready
  confirmation=approved
  robot_action=succeeded
  dispatch=terminal
  navigation=succeeded

counts:
  agent proposal=1
  confirmation=1
  approved confirmation=1
  RobotAction=1
  dispatch intent=1
  Robot Web verified preview target=not measured (v2에서 신규 추가)
  Robot Web start=1
  pre-approval Nav2 goal=0
  distinct Nav2 goal=1
  terminal result=1
  replay additional effect=0

cleanup:
  completed=true
  owned process remaining=0
  ROS node remaining=0
  owned socket remaining=0
  forced termination=0

authority:
  simulation=true
  physical_authorized=false
```

관측 시간은 readiness 약 13.87초, 승인부터 terminal·replay 검사까지 약
38.27초, cleanup 약 2.74초, 전체 약 58.21초였다. 이 실행에서 생성된 이전
installed/manifest digest는 source provenance가 봉인되지 않은 v1 rehearsal
값이므로 이 문서에서 제거했으며 최종 인수 증거로 인용하지 않는다.

첫 실제 시도에서는 이전에 중단된 incremental build가 남긴 0-byte ROS interface
object 때문에 Robot Web process에서 type-support symbol을 불러오지 못했다.
runner는 이를 readiness timeout으로 실패 처리했고, 그 시도에서도 소유 process,
ROS node와 socket을 0개로 정리했다. 해당 interface package의 build target을
clean rebuild하고 Python type-support import를 확인한 뒤 두 번째 실행이 위
결과로 성공했다. 이 사례는 source test만이 아니라 실제 install artifact를
검사하고, readiness가 증명되지 않으면 요청과 goal을 시작하지 않아야 하는
이유를 보여준다.

최종 provenance-sealed run의 commit, `source_tree_digest`, installed digest와
manifest digest는 변경을 commit하고 같은 commit에서 clean rebuild·run한 뒤
Jira 실행 기록과 owner-private evidence에 보관한다. 최종 digest를 이 tracked
문서에 다시 적으면 문서 변경으로 Git tree와 commit이 바뀌고 이전 attestation이
즉시 무효가 되는 self-reference가 생긴다. 따라서 source 문서는 절차와 판정
기준만 유지하고, 실행별 최종 digest는 immutable Jira 기록/private evidence에
둔다.

## 9. 검증 범위와 후속 작업

unit/contract test는 다음을 독립적으로 검증한다.

- installed layout·clean source attestation과 default-OFF argument gate
- status-only Nav2 관측과 distinct terminal goal 집계
- Robot Web proxy의 exact method/path 제한과 bounded shutdown
- SQLite read-only snapshot, preapproval와 known-success 판정
- exact process ownership, bounded output와 cleanup
- evidence의 exact-success invariant, redaction, mode와 atomic no-overwrite
- supervisor의 preapproval/replay/terminal/cleanup 실패 처리
- clean Git HEAD, tracked source와 installed byte equality, timeout/output bound,
  symlink·out-of-root·dirty/untracked 차단

현재 `malbut_scenarios/test` 전체 결과는 **298 passed**다. 이는 runner와 위
계약의 source test 결과이며, 아직 post-commit final provenance-sealed Gazebo
run이 완료됐다는 뜻은 아니다.

SWM25-133에는 STT, 실제 TTS, Homecam/AWS, 자율 로밍, 다중 waypoint coverage,
실로봇 authority와 실제 sensor evidence가 포함되지 않는다. 반복 3회와 핵심 장애
campaign은 SWM25-122에서 이어간다.

## 10. Jira 결론

`run_text_gazebo_acceptance`에 clean Git HEAD와 tracked source↔installed
byte-exact attestation, evidence v2의 `source_tree_digest`, Robot Web verified
preview target exact-once, replay 후 8 samples/2초 stability window와 실패 시에도
이어지는 bounded best-effort cleanup을 추가했다. `malbut_scenarios/test`는
298개가 통과했다. provenance 도입 전 run-2 rehearsal에서는
`거실로 가줘 -> navigate(거실) -> 네 -> RobotAction -> Robot Web -> actual Nav2
SUCCEEDED` 제품 흐름과 zero-residue cleanup을 1/1 확인했지만 최종 evidence로
간주하지 않는다. 최종 완료 판정은 이 변경을 Git에 commit하고 동일 commit으로
overlay를 clean rebuild한 뒤, 명시적인 `--source-tree`와 `--ros-domain-id`로
provenance-sealed run을 성공했을 때 내린다. 실행 digest는 self-referential 문서
commit을 피하기 위해 Jira 실행 기록과 owner-private evidence에 보관한다.
