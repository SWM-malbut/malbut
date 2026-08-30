# SWM25-136 중복 요청·동시 승인·worker 경쟁에도 goal을 추가 생성하지 않는다

## 1. 결론

SWM25-136은 새로운 이동 기능을 만드는 작업이 아니다. SWM25-131의 durable
confirmation, SWM25-132의 RobotAction 실행 원장, SWM25-133의 실제 Gazebo E2E
runner와 SWM25-134의 campaign을 그대로 사용하면서, 다음 세 경쟁 조건에서도
하나의 승인된 요청이 **정확히 하나의 실제 Nav2 goal**만 만드는지 검증한다.

```text
case-001 duplicate_request
  -> 동일 request envelope을 승인 전에 2회 전송
case-002 concurrent_approval
  -> 같은 pending confirmation에 승인 2개를 동시에 전송
case-003 competing_workers
  -> 독립 SQLite connection을 가진 worker 2개가 같은 Action을 claim

각 case 공통 결과
  -> durable Agent turn 1
  -> confirmation 1
  -> RobotAction 1
  -> dispatch intent 1
  -> Robot Web start 1
  -> distinct actual Nav2 goal 1
  -> known terminal result 1
  -> replay 추가 효과 0
```

세 case는 모두 server-owned `happy_living_room` target을 사용하지만, 서로 다른 fresh
runtime·SQLite·Gazebo에서 실행된다. 따라서 SWM25-135처럼 목적지 다양성을 시험하는
것이 아니라, 동일한 제품 경계에 서로 다른 중복·동시성 압력을 가하는 시험이다.

## 2. 목표

네트워크 재시도, 사용자의 빠른 연속 승인, worker 경쟁은 실제 운영에서 자연스럽게
발생한다. 이때 HTTP 요청이나 thread가 두 번 실행되더라도 durable 원장과 외부 Nav2
효과는 하나만 남아야 한다.

이번 작업은 production confirmation CAS, SQLite Action claim, lease/fence 또는
Safety 알고리즘을 테스트용으로 수정하지 않는다. scenario 계층의 bounded barrier는
두 contender가 실제 경쟁 지점에 도착하도록 재현성을 높일 뿐이며, 최종 승자는 기존
production transaction과 CAS가 결정한다.

## 3. 달성 조건 5개

### 3.1 기존 runner와 campaign 재사용

- [x] 새 E2E runner를 만들지 않고 installed SWM25-133 runner를 사용한다.
- [x] SWM25-134 campaign에 `duplicate_request`, `concurrent_approval`,
  `competing_workers` case token을 추가한다.
- [x] 세 token은 임의 입력이 아닌 allowlist이며 모두 living-room semantic target과
  각자의 fault profile에 고정된다.

### 3.2 세 경쟁 조건을 결정론적으로 재현

- [x] duplicate case는 user, conversation, request ID, turn ID와 본문이 모두 같은
  HTTP envelope을 정확히 재전송한다.
- [x] concurrent approval case는 client 시작 barrier와 server target-resolution
  barrier를 모두 사용한다. 서버 증거가 contender 2, release 2를 증명하고 결과가
  fresh approval 1개와 HTTP 409 terminal conflict 1개일 때만 통과한다.
- [x] worker case는 같은 DB에 대한 독립 SQLite connection과 서로 다른 worker 2개를
  사용한다. 두 실제 `claim_next()` 결과가 winner 1, non-winner 1로 확정되기 전에는
  winner가 외부 start로 진행하지 못한다.

고정 sleep만으로 동시성을 주장하지 않는다. barrier timeout, 한 contender 미도착,
관측 파일 발행 실패 또는 승자 수 불일치는 모두 fail-closed한다.

### 3.3 제품 효과 exact-once 검증

- [x] read-only SQLite observer가 전체 `conversation_turns`, confirmation,
  RobotAction과 outbox count를 직접 확인한다.
- [x] Counting Robot Web proxy가 preview, verified preview, start와 cancel을 집계한다.
- [x] Nav2 status observer가 실제 action status topic의 distinct goal을 집계한다.
- [x] replay와 late approval 뒤 안정화 sample 동안 모든 효과 count가 그대로인지
  확인한다.
- [x] actual Gazebo 세 case에서 각 제품 효과가 정확히 1개이고 추가 효과가 0개다.

같은 문장이라도 새로운 request ID를 가진 새 요청은 별도 사용자 요청이다. 의미
유사도 기반 문장 dedupe는 이번 Story의 정확성 경계가 아니다.

### 3.4 content-free evidence를 강하게 결속

- [x] child evidence를 v4로 올리고 `fault_profile`과 exact pressure counter를
  추가한다.
- [x] campaign evidence를 v3로 올리고 case token, semantic profile, fault profile,
  pressure와 child provenance를 교차 검증한다.
- [x] 승인·worker barrier observation은 fresh private runtime에 mode `0600`으로
  원자 발행한다.
- [x] observation reader는 symlink를 따르지 않고 owner, regular file, mode, size,
  canonical JSON, duplicate key와 exact field set을 검사한다.
- [x] 원문, 좌표, request/turn/action/worker ID, claim token, DB·host path는 public
  evidence에 기록하지 않는다.

기대 pressure counter를 상수로 쓰는 것만으로는 합격하지 않는다. duplicate HTTP
응답, concurrent server observation, worker observation과 durable/external effect
count가 실제로 관찰된 경우에만 해당 profile의 typed pressure evidence를 만든다.

### 3.5 3/3와 zero residue를 하나의 판정으로 증명

- [x] source unit/contract test가 각 분기, strict evidence mismatch, timeout, stale
  file, duplicate key와 replay 불변성을 검증한다.
- [x] 동일 clean commit의 isolated installed overlay build와 non-actuating check를
  통과한다.
- [x] actual headless Gazebo campaign이 세 case를 순서대로 3/3 통과한다.
- [x] 각 child와 campaign 종료 후 owned process, ROS node, socket, worker thread가
  모두 0이고 forced termination이 0이다.

한 case라도 실패하거나 cleanup이 불완전하면 campaign은 즉시 중단한다. 실패 뒤
남은 case만 이어서 성공으로 합치지 않고 fresh evidence 경로에서 처음부터 다시
실행한다.

## 4. 계층별 책임

```text
Campaign CLI/core
  -> 허용된 fault case와 순서, 전체 verdict
Installed SWM25-133 adapter
  -> semantic profile + fault profile을 child에 전달
Acceptance supervisor
  -> HTTP pressure, 원장/Robot Web/Nav2 관찰, replay 안정성
Scenario-only barriers
  -> 실제 경쟁 지점 동시 도착과 content-free observation
Production confirmation store
  -> 한 승인만 terminal CAS winner로 결정
Production SQLiteActionRepository
  -> 한 worker만 lease/fence claim winner로 결정
Production ApprovedActionWorker
  -> fresh state/Safety/target 재검증과 durable dispatch intent
SWM25-130 façade -> Robot Web -> actual Gazebo Nav2
  -> 외부 goal 1개와 known terminal result
Child v4 / Campaign v3 evidence
  -> effect count + pressure + provenance + cleanup 결속
```

barrier가 승자를 선택하거나 DB 값을 직접 바꾸지는 않는다. 이를 지키는 이유는
테스트가 통과했다는 사실이 테스트용 우회 로직이 아니라 실제 production CAS의
효과임을 보장하기 위해서다.

## 5. case별 정확한 의미

### duplicate_request

첫 요청과 byte-equivalent한 envelope을 다시 보낸다. 기존 request claim이 같은
confirmation binding을 반환해야 하며 durable Agent turn과 confirmation이 늘어나면
실패한다. 이후 승인과 실행은 정상 경로를 한 번만 탄다.

### concurrent_approval

서로 다른 승인 request/turn ID 두 개가 같은 pending confirmation을 읽은 뒤 함께
CAS로 진행한다. 하나는 fresh HTTP 200 approval이고 다른 하나는 HTTP 409
`confirmation_already_terminal`이어야 한다. 늦게 도착해 `no pending`을 받은 200
응답은 intended race를 증명하지 못하므로 실패한다. 승자의 동일 request replay만
cached approval로 허용한다.

### competing_workers

worker마다 별도 SQLite connection과 worker identity를 갖는다. 두 contender가 승인된
`PENDING_PREFLIGHT` Action을 본 뒤 production `claim_next()`를 호출한다. 정확히 한
claim만 반환되어야 하며, 두 결과가 확정되기 전에 winner가 prepare/start를 호출할
수 없다. 이후 loser가 반복 polling하더라도 새 Action이나 goal은 생기지 않는다.

## 6. 검증 상태

2026-08-30 현재 다음 source 검증을 완료했다.

- fault/scenario/pressure typed contract와 child v4/campaign v3 schema
- exact duplicate request replay와 strict concurrent HTTP 409 판정
- server-owned approval barrier observation
- independent SQLite two-worker claim coordination
- acceptance supervisor 세 fault branch와 winner replay
- strict observation file security 및 mismatch fail-closed
- focused integration test 143개 통과
- clean installed `malbut_scenarios` test 533개 통과
- non-actuating campaign check에서 case 3개와 Nav2 start 0개 확인
- actual headless Gazebo campaign 3/3 `PASSED`, stopped early false
- 각 child의 proposal·confirmation·RobotAction·outbox·Robot Web start·Nav2 goal·
  known terminal 각각 1개, preapproval/replay 추가 goal 0개 확인
- campaign cleanup에서 process·ROS node·socket·forced termination 모두 0 확인

전역 PEP257 test에는 이번 diff 밖에 기존부터 존재하던 docstring 4건이 남아 있다.
source tree 밖의 private evidence에는 child v4 세 개와 campaign v3 aggregate 한
개만 보존하며, 원문·private identity·host path는 public Jira 결론에 복사하지 않는다.

## 7. 명시적 제외

- semantic 문장 유사도 기반 중복 제거
- lease 만료 뒤 worker takeover와 crash/UNKNOWN reconciliation
- stale state, E-stop, map revision 변경과 Nav2 장애 주입
- 여러 fault를 한 case에 동시에 결합
- fake Nav2를 최종 인수 증거로 사용
- production Safety/CAS에 테스트 hook 추가
- 실제 로봇, STT/TTS, Homecam/AWS와 physical authority

lease/restart와 불명 결과 reconcile은 SWM25-138, Safety 차단은 SWM25-137에서 같은
campaign 경계를 확장해 검증한다.

## 8. Jira 결론

> 기존 SWM25-133 actual Gazebo runner와 SWM25-134 campaign을 재사용해 exact duplicate
> request, concurrent approval, competing workers의 세 bounded fault profile을
> 추가했다. duplicate는 같은 request binding을 재사용하고, concurrent approval은
> server-side 2-contender barrier 뒤 production confirmation CAS에서 1승 1패를,
> worker case는 독립 SQLite connection 두 개의 production claim에서 1승 1패를
> 증명한다. 세 case 모두 durable Agent turn·confirmation·RobotAction·outbox·Robot
> Web start·distinct Nav2 goal·known terminal을 각각 1개로 제한하며 replay 추가
> 효과는 0개다. child v4/campaign v3 content-free evidence가 pressure, source/install,
> semantic target과 cleanup을 결속한다. clean installed test 533개와 non-actuating
> check를 통과했고, actual headless Gazebo campaign은 세 case 3/3 PASSED 및 종료
> 자원 0개를 확인했다. 전 과정은 simulation 전용이며 physical authority는 OFF다.
