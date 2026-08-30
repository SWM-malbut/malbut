# SWM25-138 Nav2 불명 결과를 UNKNOWN으로 봉인하고 재전송하지 않는다

## 1. 결론

SWM25-138은 `UNKNOWN`을 성공이나 실패로 추정하는 기능이 아니다. 승인된
`RobotAction`이 durable dispatch intent를 만든 뒤 Nav2 start 또는 terminal status의
결과를 신뢰할 수 없게 되면, 기존 production 원장이 Action과 outbox를 같은 exact
code의 `UNKNOWN`으로 봉인하고 같은 작업을 자동 재전송하지 않는지 actual Gazebo에서
검증한다.

```text
정상 텍스트 요청과 승인
  -> RobotAction 1개
  -> read-only preview와 fresh preflight
  -> durable dispatch intent 1개
  -> Robot Web start attempt 1개
  -> start/status 결과 불명
  -> RobotAction UNKNOWN
  -> execution outbox UNKNOWN
  -> 승인 replay와 late approval
  -> 추가 start 0개, 추가 Nav2 goal 0개
```

제품 결과 `UNKNOWN`은 이번 장애 case의 기대 결과이므로, exact result code, 제품 원장,
독립 ROS 관측, no-resend와 cleanup이 모두 일치하면 시험 판정은 `PASSED`다. 반대로
actual Nav2 goal이 성공했다는 사실만으로 제품 원장의 `UNKNOWN`을 `SUCCEEDED`로
바꾸지 않는다.

최종 campaign은 장애 case 세 개만 실행하지 않는다. 장애 주입 전후에 정상
`happy_living_room`을 양의 대조군으로 배치해 총 5개를 다음 순서로 실행한다.

```text
1. happy_living_room                (front positive control)
2. nav2_unavailable                 (UNKNOWN)
3. start_response_lost              (UNKNOWN)
4. terminal_status_response_lost    (UNKNOWN)
5. happy_living_room                (back positive control)
```

앞 대조군은 fault campaign을 해석하기 전에 Agent, Robot Web과 실제 Nav2 성공 경로가
정상임을 보인다. 뒤 대조군은 장애 profile이 다음 fresh case에 남지 않았고 cleanup 뒤
정상 경로가 다시 성공함을 보인다. 둘 중 하나라도 실패하면 3개 `UNKNOWN` 결과만으로
Story를 완료하지 않는다.

## 2. 목표와 안전 근거

외부 command의 요청과 응답은 하나의 원자 transaction이 아니다. 다음 순서가 가능하다.

```text
Agent가 dispatch intent를 commit
  -> Robot Web로 start 전송
  -> Robot Web 또는 Nav2가 요청을 처리
  -> HTTP 응답이나 이후 status 응답만 유실
```

이때 Agent가 같은 start를 다시 보내면 첫 요청이 이미 반영된 경우 goal을 두 개 만들
수 있다. `FAILED`로 기록하면 실제 로봇이 움직였을 수 있다는 사실을 숨기고,
`SUCCEEDED`로 기록하면 terminal 결과를 관측하지 않은 거짓 성공이 된다. 따라서 현재
안전한 제품 판정은 다음과 같다.

- durable intent 이후 효과 여부를 증명하지 못하면 `UNKNOWN`으로 기록한다.
- `RobotAction`과 `execution_outbox`를 같은 result code로 함께 봉인한다.
- terminal인 `UNKNOWN`을 worker가 다시 claim하거나 start를 재전송하지 못하게 한다.
- 같은 승인 replay와 새 late approval 뒤에도 effect count가 증가하지 않아야 한다.
- 실제 Nav2 효과는 subscription-only ROS observer로 별도 관측하되 제품 원장을
  보정하는 authority로 사용하지 않는다.

즉 SWM25-138의 reconciliation은 현재 정보로 확정할 수 없는 작업을
`UNKNOWN/no-resend`로 안전하게 닫는 보수적 reconciliation이다. 외부 terminal을 찾아
제품 원장을 사후 확정하는 end-to-end reconciliation은 이번 범위가 아니다.

## 3. 달성 조건 5개

아래 checkbox는 검증 결과를 기록하는 자리다. 명령을 실제 실행하고 증거를 확인하기
전에는 완료로 바꾸지 않는다.

### 3.1 기존 runner와 campaign에 5-case 경계를 추가

- [x] SWM25-133 installed actual Gazebo runner와 SWM25-134 ordered campaign을
  재사용하고 별도 E2E runner를 만들지 않는다.
- [x] `TextGazeboExecutionProfile`은 `none`, `nav2_unavailable`,
  `start_response_lost`, `terminal_status_response_lost`만 허용하며 임의 fault 입력을
  받지 않는다.
- [x] execution profile은 기존 concurrency `fault_profile` 및 dispatch
  `safety_profile`과 분리하고 한 case에 여러 fault 계열을 결합하지 않는다.
- [x] 앞/뒤 `happy_living_room` 대조군과 세 execution case를 위의 exact order로 실행하며,
  각 case는 fresh private runtime, SQLite와 owned Gazebo/Agent process를 사용한다.

### 3.2 세 실행 장애를 정확한 effect boundary에서 재현

- [x] `nav2_unavailable`은 lifecycle/localization과 preview를 정상으로 유지한 채
  default-off 고정 action endpoint
  `/swm25_138_unavailable_navigate_to_pose`만 선택한다. arbitrary ROS action name을
  입력받지 않으며 start를 upstream에 정확히 한 번 전달한다. Robot Web는
  `send_goal_async` 직전에 readiness를 확인해 정확한 HTTP 503
  `NAV2_ACTION_UNAVAILABLE`을 반환하고 goal은 생성하지 않는다. 독립 proxy는 이
  응답을 content-free counter로 한 번 관찰한 뒤 Agent 방향 응답만 끊어 제품 결과를
  `UNKNOWN`으로 보존한다.
- [x] `start_response_lost`는 Robot Web가 실제 start를 HTTP 202로 수락한 뒤 proxy가
  그 응답 하나만 끊는다. 요청 전달 전 차단이나 synthetic success를 허용하지 않는다.
- [x] `terminal_status_response_lost`는 start HTTP 202를 Agent까지 전달하고 실제
  Nav2 goal이 terminal이 된 status 응답 하나만 끊는다. nonterminal status나 start
  응답을 대신 끊으면 실패한다.
- [x] fault 누락, 두 번 적용, 예상하지 않은 drop, proxy 관측 불일치와 arbitrary
  endpoint 주입은 모두 fail-closed한다.

### 3.3 exact UNKNOWN 원장과 no-resend를 증명

- [x] 세 장애 모두 proposal, confirmation, approved confirmation, RobotAction,
  dispatch intent, verified preview와 Robot Web start attempt가 각각 1개다.
- [x] `nav2_unavailable`과 `start_response_lost`는 RobotAction/outbox를
  `UNKNOWN/navigation_start_outcome_unknown`으로 함께 기록한다.
- [x] `terminal_status_response_lost`는 RobotAction/outbox를
  `UNKNOWN/navigation_status_outcome_unknown`으로 함께 기록한다.
- [x] 승인 replay와 late approval 후 안정화 sample에서 Action/outbox snapshot과 모든
  effect count가 그대로이며 `replay_additional_effect_count=0`이다.
- [x] Robot Web cancel은 0개이고 start attempt는 총 1개다. no-resend는 “start를 한
  번도 시도하지 않음”이 아니라 “첫 시도 뒤 추가 start가 0개임”을 뜻한다.

### 3.4 제품 원장과 독립 ROS 증거를 함께, 그러나 분리해 검증

- [x] SELECT-only `SQLiteAcceptanceObserver`가 confirmation, RobotAction과
  `execution_outbox`의 exact state/code/count를 직접 읽는다.
- [x] Counting Robot Web proxy가 preview, verified preview, start attempt/forward,
  response drop과 cancel count를 payload 없이 집계한다.
- [x] subscription-only `Nav2GoalStatusObserver`가 실제 action status topic에서 distinct
  goal과 terminal status를 관측하며 goal 생성·취소 authority를 갖지 않는다.
- [x] `nav2_unavailable`은 제품 `UNKNOWN`과 실제 goal 0개를, 두 response-loss case는
  제품 `UNKNOWN`과 실제 succeeded goal 1개를 동시에 증명한다.
- [x] 독립 ROS 성공은 product reconciliation이나 ledger write-back으로 해석하지
  않는다. 두 증거의 불일치는 숨기지 않고 각각의 typed state로 보존한다.

### 3.5 typed evidence, 5/5와 zero residue를 하나의 판정으로 결속

- [x] child evidence format은
  `malbut.text-gazebo-e2e-evidence.v6`이고 `execution_profile`,
  `unknown_result_code`, `execution_fault_observation`, 제품 결과와 시험 상태를
  결속한다.
- [x] campaign evidence format은
  `malbut.text-gazebo-campaign-evidence.v5`이고 expected/observed UNKNOWN code,
  child provenance, exact order와 cleanup을 교차 검증한다.
- [x] 같은 clean commit의 isolated installed overlay, non-actuating `--check`와 focused
  regression test를 통과한다.
- [x] actual headless Gazebo campaign이 5/5, `stopped_early=false`로 완료된다.
- [x] 각 child와 aggregate 종료 뒤 owned process, ROS node, socket과 forced
  termination이 모두 0이고 proxy worker thread의 `close/join`이 성공했으며
  `simulation=true`,
  `physical_authorized=false`다.

한 case라도 실패하거나 cleanup이 불완전하면 campaign은 즉시 중단한다. 실패한 실행의
일부 child를 새 실행의 성공 child와 합쳐 5/5로 만들지 않고, fresh evidence 경로에서
전체 순서를 다시 실행한다.

## 4. case별 exact 계약

세 execution case 공통 기대 수량은 다음과 같다.

```text
Agent proposal                    1
confirmation                      1
approved confirmation             1
RobotAction                       1 (UNKNOWN)
Robot Web preview                 1
Robot Web verified preview        1
dispatch intent                   1 (UNKNOWN)
Robot Web start attempt           1
Robot Web cancel                  0
preapproval Nav2 goal             0
replay additional effect          0
```

profile별 차이는 다음 표의 모든 열이 정확히 일치해야 한다.

`execution_fault_observation` 안의 counter는 **fault profile이 정확히
적용됐는지를 보이는 관측치**이며, 해당 run의 전체 effect 수량이
아니다. 따라서 `execution_profile=none`인 정상 대조군은
`execution_fault_observation.start_forward_count=0`이지만, 실제 제품 start는
`EvidenceCounts.robot_web_start_count=1`로 별도 기록된다. 이 두
counter를 같은 의미로 비교하지 않는다.

| execution profile | 제품 result code | fault proxy start forward | start response drop | terminal status response drop | unavailable endpoint | actual Nav2 goal | actual terminal |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `nav2_unavailable` | `navigation_start_outcome_unknown` | 1 | 0 | 0 | 1 | 0 | 0 |
| `start_response_lost` | `navigation_start_outcome_unknown` | 1 | 1 | 0 | 0 | 1 | 1 |
| `terminal_status_response_lost` | `navigation_status_outcome_unknown` | 1 | 0 | 1 | 0 | 1 | 1 |

`nav2_unavailable`의 downstream disconnect는 일반적인 accepted-start 응답 유실과
구분한다. 따라서 `start_response_drop_count=0`이고
`unavailable_endpoint_count=1`이어야 한다. proxy의 이 동작은 한 번만 적용되며,
재시도가 발생하면 start count가 2가 되어 no-resend 계약에서 실패한다.

두 response-loss case의 독립 ROS terminal은 `succeeded`여야 한다. 이는 장애 주입이
실제 effect 뒤 응답 경계를 끊었다는 양의 증거지만, 제품 결과는 계속 `UNKNOWN`이다.

앞/뒤 `happy_living_room` 양의 대조군은 다음 exact success 계약을 각각 만족해야 한다.

```text
product_outcome                   succeeded
RobotAction                       SUCCEEDED
execution outbox                  TERMINAL
Robot Web start                   1
actual Nav2 distinct goal         1
actual Nav2 terminal              1 (succeeded)
replay additional effect          0
```

따라서 5-case 전체에서 제품 성공은 2개, 기대된 제품 `UNKNOWN`은 3개다. 시험 verdict는
각 case가 자신의 제품 계약을 만족할 때 5개 모두 `PASSED`다. 제품 결과와 시험 verdict를
같은 필드로 합치지 않는다.

## 5. 계층별 책임과 금지 경계

```text
Campaign CLI/core
  -> 5-case allowlist와 order, expected product outcome/code, stop-on-failure

Installed SWM25-133 adapter
  -> scenario/fault/safety/execution profile을 child에 전달
  -> source/install provenance와 fresh owned runtime 구성

Acceptance supervisor
  -> 정상 요청·승인, replay/late approval, ledger/proxy/ROS 비교, cleanup

CountingRobotWebProxy
  -> fixed local HTTP surface 전달과 exact response-loss fault 1회
  -> payload, cookie, token 또는 private ID를 public evidence에 기록하지 않음

Small House launch fixture
  -> nav2_unavailable case에서만 fixed unavailable action endpoint 선택
  -> lifecycle/localization/preview success path는 유지

Production ApprovedActionWorker
  -> durable intent 뒤 start 최대 1회, ambiguous start/status를 exact UNKNOWN으로 종료

Production SQLiteActionRepository
  -> RobotAction과 outbox의 UNKNOWN/code를 한 transaction으로 일치시킴
  -> UNKNOWN을 다시 claim 가능한 상태로 되돌리지 않음

Robot Web -> actual Gazebo Nav2
  -> 실제 external effect와 terminal 생성

Nav2GoalStatusObserver
  -> ROS status subscription만 소유하는 독립 read-only effect observer

Child v6 / Campaign v5 evidence
  -> 제품 원장, 독립 effect, fault counters, provenance와 cleanup 결속
```

scenario/proxy 계층은 production DB를 직접 수정하거나 worker의 terminal 판정을
대신하지 않는다. ROS observer도 Action이나 outbox를 수정하지 않는다. 그래야 시험
통과가 test-only 성공 주입이 아니라 production `UNKNOWN/no-resend` 결정의 결과임을
보장할 수 있다.

## 6. 제품 원장과 독립 ROS 증거가 다른 이유

`start_response_lost`를 예로 들면 두 관측자는 서로 다른 사실을 말한다.

```text
제품 원장
  start 응답을 받지 못함
  -> goal 수락 여부를 신뢰할 수 없음
  -> Action UNKNOWN + outbox UNKNOWN

독립 ROS observer
  실제 NavigateToPose status topic에서 goal 1개와 succeeded terminal 관측
  -> 이 fresh campaign runtime에는 외부 effect가 실제 존재했음
```

두 결과는 모순이 아니다. 제품은 start 요청과 특정 Nav2 goal을 재시작 뒤에도 연결할
수 있는 end-to-end identifier를 갖고 있지 않으므로, acceptance observer가 우연히 본
goal을 해당 Action의 authoritative terminal로 귀속할 수 없다. fresh isolated case에서
goal count를 검증하는 것은 fault가 올바른 경계에서 일어났음을 증명할 뿐이다.

따라서 evidence는 다음을 분리한다.

- `states.robot_action`과 `states.dispatch`: durable 제품 원장의 지식
- `states.navigation`, `nav2_goal_count`, `terminal_result_count`,
  `goal_set_digest`: 독립 ROS effect 관측
- `product_outcome`: 제품이 안전하게 공개할 수 있는 결과
- `test_status`: 그 제품 결과와 독립 effect가 case 계약을 만족했는지 여부

`UNKNOWN + navigation succeeded`는 response-loss case의 의도된 합격 shape다.
`UNKNOWN`을 success로 덮거나 ROS success를 무시해 goal 0으로 기록하면 모두 실패다.

## 7. stable operation ID gap과 SWM25-124 인계

현재 Agent SQLite에는 server-generated `operation_id`가 `robot_actions`와
`execution_outbox`에 durable하게 존재한다. 그러나 이 ID는 Robot Web start request나
Nav2 goal까지 전달되지 않는다.

현재 경계는 다음처럼 끊겨 있다.

```text
Agent operation_id (durable)
  -X-> Robot Web start request

Robot Web session_id
  <- start 202 response를 받아야 Agent가 알 수 있는 opaque process-local handle

Nav2 goal UUID
  <- ROS가 생성하며 Agent operation_id와 durable binding이 없음
```

따라서 start 202가 유실되면 Agent는 Robot Web `session_id`를 얻지 못한다. process가
재시작되면 process-local prepared/session handle도 복원할 수 없다. ROS observer가 goal
UUID를 보더라도 그것을 Agent `operation_id`에 안전하게 귀속할 durable mapping이 없다.

이번 Story는 이 gap을 숨기지 않고 `UNKNOWN/no-resend`를 유지한다. 다음 기능은
SWM25-124로 명시적으로 연기한다.

- Agent에서 Robot Web과 Nav2까지 이어지는 stable operation ID 전달
- 같은 operation ID에 대한 idempotent start와 authoritative lookup
- restart 뒤 기존 operation의 exact status 조회
- 검증된 binding으로 `UNKNOWN`을 known terminal로 승격하는 write-back 규칙
- retention, 충돌, 위조 방지와 권한 모델

SWM25-124가 완료되기 전에는 독립 ROS 관측을 제품 reconciliation으로 사용하거나,
`UNKNOWN` 작업을 새 start로 복구하지 않는다.

## 8. 검증 명령과 결과 기록

아래는 2026-08-31 KST에 최종 보강 코드로 다시 실행한 명령과 결과다.

### 8.1 focused source regression

```bash
PYTHONPATH=malbut_agent_server:malbut_gazebo:malbut_scenarios \
python3 -m pytest -q \
  malbut_agent_server/test/test_approved_action_worker.py \
  malbut_agent_server/test/test_sqlite_action_repository.py \
  malbut_gazebo/test/test_robot_web_server.py \
  malbut_gazebo/test/test_small_house_nav2_testbed_launch.py \
  malbut_scenarios/test/test_counting_robot_web_proxy.py \
  malbut_scenarios/test/test_text_gazebo_scenario.py \
  malbut_scenarios/test/test_text_gazebo_runtime.py \
  malbut_scenarios/test/test_text_gazebo_acceptance.py \
  malbut_scenarios/test/test_text_gazebo_evidence.py \
  malbut_scenarios/test/test_text_gazebo_campaign_core.py \
  malbut_scenarios/test/test_text_gazebo_campaign_runtime.py \
  malbut_scenarios/test/test_text_gazebo_campaign_evidence.py
```

결과: source와 isolated installed overlay의 집중 회귀가 각각 `558 passed`다.
여기에는 Robot Web의 final action readiness, exact unavailable 응답 관측,
campaign `UNKNOWN` code 양방향 invariant 회귀가 포함된다.

### 8.2 isolated build와 installed package test

같은 clean commit에서 isolated overlay를 만든 뒤 다음 범위를 실행한다. 실제 build와
test 명령은 사용한 overlay 절차를 Jira 실행 기록에 그대로 남긴다.

```text
build 범위: colcon --packages-up-to malbut_scenarios, non-symlink isolated overlay
build 결과: 11 packages succeeded
test 범위:  malbut_agent_server, malbut_gazebo, malbut_scenarios
test 결과:  306 + 367 + 679 = 1,352 passed, failed 0, skipped 0
```

검증은 원 feature branch를 commit하지 않기 위해 현재 변경 byte로 만든
detached ephemeral clean commit에서 수행했다. 이 SHA는 pre-commit 구현
검증용이며 최종 PR commit provenance로 주장하지 않는다. 최종 Git commit과
동일한 provenance가 필요한 경우 commit 후 동일 campaign을 재실행한다.
위 결과 수치와 digest를 적은 이 Jira 문서는 실행 뒤 갱신했으며, production Python과
launch byte는 검증 snapshot과 동일하다. 따라서 문서 갱신을 최종 PR commit 자체의
실행 provenance로 확대 해석하지 않는다.

### 8.3 non-actuating campaign check

`<canonical-clean-source-tree>`와 `<full-lowercase-commit>`은 같은 source/install
provenance를 가리켜야 한다.

```bash
ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --check \
  --case-profile happy_living_room \
  --case-profile nav2_unavailable \
  --case-profile start_response_lost \
  --case-profile terminal_status_response_lost \
  --case-profile happy_living_room \
  --source-commit "<full-lowercase-commit>" \
  --source-tree "<canonical-clean-source-tree>"
```

결과: `status=ok`, `case_count=5`, `nav2_start_count=0`,
`simulation=true`, `physical_authorized=false`.

### 8.4 actual headless Gazebo 5-case campaign

`<new-private-evidence-path>`는 존재하지 않는 신규 file이어야 하며 owner-private parent
아래에 둔다. raw path는 public Jira 결론에 복사하지 않는다.

```bash
ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --run \
  --execute-approved-simulation \
  --case-profile happy_living_room \
  --case-profile nav2_unavailable \
  --case-profile start_response_lost \
  --case-profile terminal_status_response_lost \
  --case-profile happy_living_room \
  --source-commit "<full-lowercase-commit>" \
  --source-tree "<canonical-clean-source-tree>" \
  --ros-domain-id "<isolated-domain-id>" \
  --evidence "<new-private-evidence-path>"
```

결과: `5/5 passed`, `stopped_early=false`, child v6 5개, campaign v5,
`simulation=true`, `physical_authorized=false`. Typed dataclass로 aggregate를 재구성해
canonical byte와 digest를 다시 검증했다. public manifest digest는
`2fed687aa844e6fca8a06050c8709b543ec92012d8b50abf12792264967d4ab4`이다.

### 8.5 결과 표

| 검증 | 상태 | 기록할 값 |
| --- | --- | --- |
| focused source regression | 통과 | source 558, installed 558 |
| isolated build | 통과 | 11 packages |
| installed package test | 통과 | 1,352 passed, failed/error/skipped 0 |
| non-actuating check | 통과 | Nav2 start 0, profile 5개 |
| actual Gazebo campaign | 통과 | 5/5, `stopped_early=false` |
| child evidence | 통과 | v6 child 5개, typed parse 완료 |
| campaign evidence | 통과 | v5 `passed`, public digest 재검증 |
| cleanup | 통과 | process/node/socket/forced termination 0, thread close/join 성공 |

## 9. 명시적 제외

- `UNKNOWN`을 성공 또는 실패로 임의 변환
- response-loss 뒤 자동 start resend
- 독립 ROS observer의 제품 DB write-back
- stable operation ID와 restart-safe external lookup(SWM25-124)
- 여러 execution/Safety/concurrency fault를 한 case에 결합
- arbitrary ROS action endpoint 또는 caller-defined fault
- fake Nav2를 최종 actual acceptance evidence로 사용
- 실제 로봇, physical authority, hardware E-stop, STT/TTS, Homecam/AWS

## 10. Jira 결론

실제 검증 결과를 다음과 같이 정리한다.

> SWM25-133 actual Gazebo runner와 SWM25-134 campaign을 재사용해 앞/뒤
> `happy_living_room` 양의 대조군 사이에서 `nav2_unavailable`, `start_response_lost`,
> `terminal_status_response_lost` 세 execution profile을 실행했다. 세 장애 case의
> production RobotAction과 execution outbox는 각각 기대한
> `navigation_start_outcome_unknown` 또는 `navigation_status_outcome_unknown`으로
> `UNKNOWN`이었고, 승인 replay와 late approval의 추가 start/goal은 모두 0개였다.
> 독립 ROS 증거는 unavailable case goal 0개와 두 response-loss case의 succeeded goal
> 각 1개를 제품 원장과 분리해 보존했다. child v6 5개, campaign v5
> public digest 재검증, source/installed focused test 각 558개, colcon package test
> 1,352개와
> actual headless Gazebo 5/5 `passed`를 확인했으며 `stopped_early=false`, 종료
> 잔류 process/node/socket과 forced termination은 모두 0이고 proxy thread의
> close/join도 성공했다. 전 과정은
> `simulation=true`, `physical_authorized=false`다. stable operation ID 기반 terminal
> 승격은 SWM25-124로 인계하며, 그 전까지 `UNKNOWN/no-resend`를 유지한다.
