# SWM25-137 오래된 상태·E-stop·지도 변경에서 Nav2 goal 0개를 확인

## 1. 결론

SWM25-137은 새로운 Safety 정책이나 별도 E2E runner를 만드는 작업이 아니다. 정상
요청과 confirmation 승인을 거쳐 durable RobotAction이 만들어진 뒤에도, 실제
dispatch 직전 상태가 바뀌면 기존 production `ApprovedActionWorker`가 실행을
차단하는지 검증한다.

```text
정상 텍스트 요청
  -> ActionProposal
  -> 정상 RobotState와 target으로 confirmation 생성
  -> "네" 승인
  -> RobotAction 1개 생성
  -> production worker claim
  -> read-only Robot Web preview
  -> 승인 후 Safety 조건 1회 재현
  -> production fresh preflight
  -> RobotAction BLOCKED
  -> dispatch intent 0
  -> Robot Web start 0
  -> actual Nav2 goal 0
```

제품 결과 `BLOCKED`는 이번 시험의 기대 결과이므로 campaign 판정은 `PASSED`다.
아무 일도 일어나지 않았다는 사실만으로 합격하지 않는다. 승인과 RobotAction이
실제로 존재하고, fault가 claim 뒤 정확히 한 번 관찰됐으며, 정확한 block code와
zero-effect count가 함께 증명돼야 한다.

## 2. 목표와 이유

confirmation은 사용자가 행동 제안에 동의했다는 기록이지 로봇 실행 권한 자체가
아니다. 사용자가 대답하는 동안 RobotState가 오래되거나 E-stop이 켜질 수 있고,
승인했던 semantic target의 map revision이 바뀔 수도 있다. 따라서 실행 권한은
승인 뒤 새 상태, 현재 Safety policy와 현재 target binding을 다시 검사한 다음에만
durable dispatch intent로 만들어야 한다.

이번 작업은 production Worker, SafetyPolicy, SQLiteActionRepository의 상태 전이와
검사 순서를 수정하지 않는다. scenario 전용 adapter는 실제 입력을 결정적으로
변화시키고 content-free 관측을 발행할 뿐이며, 최종 `BLOCKED` 판정은 기존 production
코드가 수행한다.

## 3. 달성 조건 5개

### 3.1 기존 runner와 campaign 재사용

- [x] SWM25-133 installed actual Gazebo runner를 그대로 사용한다.
- [x] SWM25-134 campaign에 `stale_state`, `emergency_stop`,
  `map_revision_changed` allowlisted case를 추가한다.
- [x] 기존 concurrency `fault_profile`과 새 dispatch `safety_profile`을 분리하고 두
  profile을 한 case에서 결합하지 않는다.

### 3.2 승인 후 dispatch 경계에서만 조건 재현

- [x] proposal 시점에는 실제 Robot Web의 정상 readiness를 사용한다.
- [x] production repository의 실제 `claim_next()`가 Action을 반환한 뒤에만 fault를
  arm한다.
- [x] worker는 read-only preview를 마친 뒤 실제 RobotState source를 먼저 읽고,
  allowlisted 변환 또는 map switch를 정확히 한 번 수행한다.
- [x] claim 전 적용, 누락, 중복 적용, observation 발행 실패와 map switch 실패는
  fail-closed한다.

### 3.3 exact BLOCKED와 zero-effect 검증

- [x] stale case는 `BLOCKED/robot_state_stale`만 허용한다.
- [x] E-stop case는 `BLOCKED/safety_emergency_stop`만 허용한다.
- [x] map case는 `BLOCKED/target_binding_changed`만 허용한다.
- [x] 각 case의 Agent proposal, confirmation, approved confirmation과 RobotAction은
  각각 1개다.
- [x] dispatch intent, Robot Web start/cancel, actual Nav2 goal/terminal과 replay 추가
  효과는 모두 0개다. read-only verified preview 1회만 허용한다.

### 3.4 typed content-free evidence

- [x] child evidence v5가 `test_status=passed`와
  `product_outcome=blocked`를 별도 필드로 기록한다.
- [x] safety profile, exact block result code와 bounded fault observation을 결속한다.
- [x] campaign evidence v4가 child 결과, expected outcome/code, zero-effect count,
  source/install provenance와 cleanup을 교차 검증한다.
- [x] private observation은 owner-owned regular file, mode `0600`, canonical JSON,
  exact key와 no-overwrite/no-symlink 규칙을 통과해야 한다.
- [x] 원문, 좌표, request/action/worker ID, evidence ID, map revision, token과 host/DB
  path를 public evidence에 기록하지 않는다.

### 3.5 installed actual Gazebo 3/3와 zero residue

- [x] 변경된 계층의 unit/contract/integration test와 기존 success/concurrency 회귀를
  통과한다.
- [x] 같은 clean commit의 isolated installed overlay build와 non-actuating check를
  통과한다.
- [x] actual headless Gazebo campaign이 세 case를 순서대로 3/3 통과한다.
- [x] child와 campaign 종료 후 owned process, ROS node, socket, worker thread와 forced
  termination이 모두 0임을 확인한다.

## 4. 계층별 책임

```text
Campaign CLI/core
  -> Safety case allowlist, 순서, expected BLOCKED와 전체 verdict
Installed SWM25-133 runner
  -> case별 fresh runtime·SQLite·ROS domain·Gazebo 실행
Acceptance supervisor
  -> 승인, ledger/Robot Web/Nav2 관찰, replay와 cleanup 검증
DispatchSafetyFaultCoordinator
  -> real claim 뒤 fault arm, real post-claim read 뒤 1회 적용
Private Small House fixture
  -> 같은 map_id의 유효한 alternate map revision 제공
Production ApprovedActionWorker
  -> freshness, target binding, deterministic Safety 재검사
Production SQLiteActionRepository
  -> CLAIMED -> BLOCKED CAS와 exact result code 영속화
Child v5 / Campaign v4 evidence
  -> BLOCKED 제품 결과, PASS 시험 판정, zero effect와 provenance 결속
```

## 5. case별 정확한 의미

### stale_state

timestamp를 임의로 승인 이전으로 돌리지 않는다. 그러면 Worker가 먼저
`robot_state_predates_approval`로 차단하여 intended boundary를 시험하지 못한다.
claim 뒤 실제 Robot Web fresh sample을 얻고, 같은 sample이 production max age 2초와
작은 bounded margin을 넘을 때까지 기다린 뒤 반환한다. 따라서 sample은 Action 생성
이후에 관찰됐지만 dispatch 시점에는 실제로 오래된 상태가 된다.

### emergency_stop

실제 Robot Web readiness와 runtime binding을 먼저 확인한다. 그 후 dispatch용
`RobotStateEvidence`를 실제 `RobotState` 타입으로 다시 만들면서
`emergency_stop=True`만 적용한다. 기존 `SafetyPolicy.evaluate_confirmed_action()`이
이를 거절하고 Worker가 `safety_emergency_stop`으로 영속화해야 한다.

이것은 hardware E-stop sensor integration 증거가 아니라 Gazebo simulation에서
production Safety 분기를 시험하는 trusted state fault다.

### map_revision_changed

Small House private fixture는 같은 occupancy image, resolution, origin과 stable map_id를
유지하면서 occupancy threshold가 다른 유효한 두 번째 map revision을 미리 만든다.
old revision으로 Robot Web verified preview를 완료하고 실제 post-claim readiness를
읽은 다음, `active.json`을 alternate manifest로 원자 전환한다. 이후 production target
resolver가 current binding을 다시 읽어 승인 당시 binding과 다름을 발견해야 한다.

변경을 preview 전에 적용하면 `navigation_prepare_failed`, dispatch intent 뒤 적용하면
start 단계 실패가 될 수 있으므로 이 순서를 고정한다.

## 6. 기대 수량과 판정

```text
Agent proposal                 1
confirmation                   1
approved confirmation          1
RobotAction                    1 (BLOCKED)
Robot Web preview              1
Robot Web verified preview     1
dispatch intent                0
Robot Web start                0
Robot Web cancel               0
actual Nav2 distinct goal      0
actual Nav2 terminal           0
replay additional effect       0
```

세 blocked case의 goal-set은 모두 빈 배열이므로 같은 canonical empty goal-set digest를
가지는 것이 정상이다. campaign은 blocked case에서만 이 반복을 허용한다. 기존 성공
case는 non-empty goal-set digest와 semantic target별 고유성을 계속 요구한다.

## 7. 최종 검증 결과

2026-08-30 같은 clean source/install tree에서 다음 검증을 완료했다.

- 변경된 11개 계층의 focused unit/contract/integration test 429개 통과
- 독립 리뷰의 stale 귀속·Path 검증·campaign core 호환성 보완 뒤 focused review test
  313개 통과
- isolated overlay에서 Malbut ROS package 11개 build 통과
- 최종 installed `malbut_scenarios` package test 613개 통과
- 변경 Python 파일 22개의 flake8와 PEP257 통과, `git diff --check` 통과
- non-actuating campaign check에서 Safety profile 3개를 검증하고 Nav2 start 0개 확인
- actual headless Gazebo campaign 3/3 통과, `stopped_early=false`

실제 campaign의 제품 결과는 다음과 같다.

| 순서 | Safety profile | RobotAction 결과 | exact code | Nav2 goal |
|---:|---|---|---|---:|
| 1 | `stale_state` | `BLOCKED` | `robot_state_stale` | 0 |
| 2 | `emergency_stop` | `BLOCKED` | `safety_emergency_stop` | 0 |
| 3 | `map_revision_changed` | `BLOCKED` | `target_binding_changed` | 0 |

각 case는 proposal·confirmation·approved confirmation·RobotAction과 verified preview를
각각 1개 만들었다. dispatch intent, Robot Web start/cancel, actual Nav2 goal/terminal과
replay 추가 효과는 모두 0개였다. 전체 종료 뒤 owned process, ROS node, socket과 forced
termination도 모두 0개였으며 `simulation=true`, `physical_authorized=false`를
유지했다.

첫 독립 리뷰에서 이미 stale인 sample을 시험 fault가 만든 결과로 오인할 수 있는
경계와 `Path(':memory:')` durable DB 검증 우회를 발견했다. 최종 구현은 원본 sample이
fresh·nonfuture인지 먼저 확인하고, 잘못된 sample에서는 sleep과 observation 발행을
모두 0회로 만든다. campaign pure core의 기존 generic `BLOCKED` 사용법은 유지하되,
SWM25-137 CLI와 evidence 경계에서는 세 exact code를 계속 강제한다.

repository-wide PEP257에는 이번 diff 밖의 기존 4건이 남아 있으며 이번 Story에서
수정하지 않았다.

## 8. 명시적 제외

- 실제 hardware E-stop 버튼과 sensor provenance
- 실제 로봇, physical authority와 physical Nav2
- 배터리·forbidden zone·localization 전체 fault matrix
- 여러 Safety fault의 동시 결합
- Nav2 장애, 응답 유실, crash와 UNKNOWN reconciliation(SWM25-138)
- fake Nav2를 최종 인수 증거로 사용
- production SafetyPolicy/Worker/Repository의 test hook 또는 DB 직접 변경
- STT/TTS, Homecam/AWS

## 9. Jira 결론

> SWM25-133 runner와 SWM25-134 campaign을 재사용해 승인 후 dispatch 직전의 오래된
> 상태, simulation E-stop, map revision 변경을 각각 한 번씩 재현했다. 세 case 모두
> production Worker가 기대한 exact code로 RobotAction을 `BLOCKED` 처리했고, dispatch
> intent·Robot Web start·actual Nav2 goal은 0개였다. clean installed package test
> 613개와 actual headless Gazebo 3/3를 통과했으며 종료 잔류 resource와 physical
> authority는 0개다. Nav2 장애·응답 유실의 `UNKNOWN` reconcile은 SWM25-138로
> 인계한다.
