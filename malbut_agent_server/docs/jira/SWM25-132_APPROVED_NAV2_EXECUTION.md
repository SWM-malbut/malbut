# SWM25-132 승인된 작업을 실제 Gazebo Nav2 이동과 연결

## 1. 결론

SWM25-132는 SWM25-131의 승인 기록과 SWM25-130의 장소명 이동 façade 사이를
다음과 같이 연결한다.

```text
"거실로 가줘"
  -> LLM/RAI가 navigate({"location":"거실"}) 제안
  -> Tool·인자·post-LLM RobotState·Safety·active target 검사
  -> "거실로 이동할까요?"
  -> "네"
  -> confirmation 승인 CAS + RobotAction 생성(동일 SQLite transaction)
  -> read-only Robot Web preview
  -> 승인 후 fresh RobotState 재수집
  -> 현재 Safety policy·target binding 재검사
  -> fresh evidence가 포함된 dispatch intent 영구 기록
  -> SWM25-130 façade start 1회
  -> Robot Web가 Nav2 goal 최대 1개 전달
  -> exact terminal result 또는 UNKNOWN
```

중요한 경계는 `approved confirmation` 자체가 실행 권한으로 바뀌지 않는다는
점이다. 기존 confirmation 응답은 계속 다음 값을 유지한다.

```text
execution_authorized=false
consume_once=false
physical_authorized=false
```

별도의 server-owned `RobotAction`만 실행 후보가 되며, 이 후보도 승인 뒤 새로
수집한 상태와 현재 정책을 통과한 `DispatchAuthorization`이 원장에 저장되기
전에는 외부 start를 호출할 수 없다.

## 2. 목표와 달성 조건

### 목표

인증된 사용자의 승인 1건을 Small House의 장소명 기반 Nav2 goal 최대 1개와
결속한다. 중복 요청, process restart, worker 경쟁 또는 Robot Web 응답 유실이
있어도 같은 물리 효과를 자동으로 다시 만들지 않는다.

### 달성 조건

- [x] 신규 approve CAS와 RobotAction 생성은 한 SQLite transaction에서
  commit 또는 rollback된다.
- [x] deny·cancel·expire·invalidated confirmation은 RobotAction을 만들지 않는다.
- [x] 실행 전 fresh RobotState, 현재 Safety policy와 exact target binding을
  다시 검사하고 실패하면 `BLOCKED`로 종료한다.
- [x] 외부 start 전에 durable dispatch intent를 기록하고, 중복 response·restart·
  두 worker가 Nav2 start를 두 번 호출하지 못한다.
- [x] start 또는 status 결과가 불명확하면 `UNKNOWN`으로 봉인하고 자동
  재전송하지 않는다.
- [x] 기본 server와 `--check`는 계속 Nav2 0회이며, 명시적인
  `--execute-approved-simulation`에서만 simulation authority를 구성한다.
- [x] installed build에서 실제 Small House goal 1개를 전송하고 terminal 결과를
  확인했다. 2026-08-29 로컬 Gazebo 증거는 13절에 기록했다.

## 3. 왜 confirmation과 RobotAction을 분리하는가

사용자의 “네”는 “방금 표시된 제안에 동의한다”는 뜻이다. 다음 내용까지
증명하지는 않는다.

- 지금도 E-stop이 해제되어 있는가
- localization과 Nav2 lifecycle이 현재도 정상인가
- 지도가 바뀌지 않았는가
- `거실`이 여전히 같은 semantic target인가
- Safety policy revision이 바뀌지 않았는가
- 외부 start가 이미 전송되었는가

따라서 confirmation은 동의 원장으로 보존하고, 실제 실행 수명은 별도
RobotAction으로 관리한다. 이 구조는 LLM과 connector를 분리하는 RAI 원칙,
의미적 적합성과 실제 수행 가능성을 나누는 SayCan 원칙, typed proposal 뒤에
결정론적 검증을 두는 Joint Verification 원칙을 현재 Malbut에 맞게 적용한
형태다.

## 4. 두 번의 상태 검사

### 첫 번째: 제안·확인 질문 생성 전

Provider가 행동을 제안한 뒤 Agent가 server-owned RobotState를 읽는다. 이
검사를 통과해야 사용자에게 확인 질문을 보여준다. 해당 evidence ID,
observed time, Safety policy revision은 confirmation proposal fingerprint에
결속된다.

### 두 번째: 승인 뒤 dispatch 직전

worker는 먼저 side-effect가 없는 Robot Web preview를 만든다. preview가 최대
수십 초 걸릴 수 있으므로 이전 상태를 재사용하지 않고 그 뒤에 RobotState를
다시 읽는다. 다음을 모두 확인한다.

```text
evidence.trusted == true
0 <= now - observed_at <= 2초
observed_at >= RobotAction 생성 시각
confirmation policy revision == current policy revision
current target binding == approved target binding
Tool arguments가 현재 strict schema와 일치
E-stop == false
battery가 최소값 이상
Nav2 navigation_available == true
localization_ok == true
목적지가 forbidden zone이 아님
```

통과한 표본은 `DispatchAuthorization`으로 만들어 dispatch intent와 함께
commit한다. 이 durable evidence가 없으면 `DISPATCH_INTENT` 이후 상태를 만들 수
없다.

Robot Web도 경로 재계산이 끝난 뒤 실제 NavigateToPose 전송 직전에 lifecycle,
localization, autonomous mode와 기존 navigation 상태를 다시 읽는다. 계산 중
Collision Monitor가 inactive가 되거나 기존 goal이 canceling으로 바뀌면 새 goal은
0회다.

## 5. Robot Web readiness의 의미

Gazebo 실행 모드의 `RobotWebSimulationStateSource`는 Robot Web의 editor config와
status를 한 번씩 새로 읽어 다음을 확인한다.

- exact simulation device ID
- exact map ID와 map revision
- `simulation=true`
- navigation enabled
- AMCL, planner, controller, BT navigator, global costmap,
  Collision Monitor lifecycle가 모두 active
- localization state가 ok이고 현재 pose가 존재
- TF age와 pose age가 모두 존재하고 각각 2초 이내

좌표, pose, device/map ID, CSRF token과 raw response는 public dict나 repr에 넣지
않는다. HTTP 호출 전후 시각과 Robot Web이 보고한 TF/pose age를 함께 사용해
보수적인 실제 관측 시각을 만든다. 예를 들어 upstream age 1.9초와 local 왕복
0.2초를 0초짜리 새 표본으로 바꾸지 않고 2.1초 stale 표본으로 차단한다.
readiness content digest와 이 관측 시각으로 매 표본마다 다른 evidence ID를 만든다.
ROS time보다 2초 넘게 미래인 TF도 age 0으로 보정하지 않고 localization lost로
차단한다.

현재 battery 100%와 E-stop clear는 실제 센서 측정이 아닌 명시적인 Gazebo
가정이다. 따라서 이 state source는 `simulation=true`만 허용하며 실로봇에는
사용할 수 없다. 실제 battery와 hardware E-stop source는 SWM25-123 범위다.

## 6. Action 상태와 전이

```text
PENDING_PREFLIGHT
  -> CLAIMED
  -> DISPATCH_INTENT
  -> STARTED
  -> SUCCEEDED | FAILED | CANCELED

CLAIMED 전/후 검증 실패
  -> BLOCKED

외부 결과 불명, crash window, 관측 deadline 초과
  -> UNKNOWN
```

`BLOCKED`, `UNKNOWN`과 known terminal은 모두 terminal이다. `UNKNOWN`은 실패로
추정한 상태가 아니라 “효과가 있었는지 증명할 수 없으므로 다시 보내지 않는
상태”다.

승인으로 생성된 Action에는 별도의 30초 `dispatch_expires_at`이 있다. 확인
질문에 제때 답했더라도 worker가 장시간 중단된 뒤 오래된 Action을 갑자기
실행하지 못하게 하기 위한 새 deadline이다. 만료된 pending/claimed Action은
`BLOCKED/action_expired`로 닫힌다.

## 7. SQLite 원장과 중복 방어

Action row에는 다음을 복사해 conversation 수명과 독립적으로 보존한다.

- confirmation ID와 proposal fingerprint
- user, conversation, session instance, generation, revision
- Tool name과 canonical arguments/digest
- target room/category와 private binding digest
- confirmation 당시 state evidence와 Safety policy revision
- server-generated action ID와 operation ID
- dispatch expiry, revision, state와 terminal result code

`confirmation_request_id`는 unique다. action/operation/intent ID는 서로 다른
server-generated ID이며 LLM이나 HTTP caller가 제공할 수 없다. raw claim token은
DB에 저장하지 않고 digest만 저장한다.

worker claim은 lease, monotonically increasing fence와 action revision CAS로
보호된다. 두 worker가 같은 row를 보더라도 한 worker만 유효한 claim을 얻고,
오래된 claim token/fence는 dispatch intent를 기록할 수 없다.

실행 lease는 240초다. 이는 30초 dispatch window와 별개이며, 현재 adapter의
preview·readiness·start·마지막 status I/O 최악 예산 약 60초, 120초 status 관측과
최종 SQLite 처리·scheduling 여유를 포함한다. worker 구성도 lease가 status
deadline보다 최소 75초 길지 않으면 시작 전에 거절한다.
dispatch window는 “새 start intent를 언제까지 만들 수 있는가”만 제한하고,
제시간에 시작한 Nav2 작업의 관측 lease를 30초로 줄이지 않는다.

RobotAction은 volatile conversation table에 cascade foreign key를 두지 않는다.
대화 reset/delete가 이미 시도한 실행 증거를 지우면 안 되기 때문이다.

## 8. crash window 처리

| crash 위치 | 재시작 처리 | start 재호출 |
| --- | --- | ---: |
| Action 생성 전 | confirmation/action transaction 전체 rollback | 0 |
| CLAIMED, intent 전 | lease 만료 후 새 worker가 fresh preflight | 가능 |
| DISPATCH_INTENT, start 전 | 효과 여부를 증명할 수 없어 `UNKNOWN` | 0 |
| start 요청 뒤 응답 유실 | `UNKNOWN` | 0 |
| start 성공 뒤 STARTED commit 실패 | durable intent를 `UNKNOWN`으로 reconcile | 0 |
| STARTED 중 process 종료 | expired lease 뒤 `UNKNOWN` | 0 |

SWM25-130 executor도 한 action을 처리하는 동안 같은 intent의 중복 호출은 cached
handle을 반환하고, unknown intent를 다시 호출하면 같은 unknown을 반환한다.
action이 terminal/BLOCKED/UNKNOWN이 되면 worker가 process-local preview와 handle을
폐기한다. 폐기된 opaque reference는 다시 사용할 수 없으므로 start 재호출 없이
cache가 회수된다. process가 재시작되면 opaque Robot Web session을 복원할 수
없으므로 SQLite 원장이 더 보수적인 `UNKNOWN/no resend` 결정을 내린다.

## 9. 실행 방법

먼저 private map fixture를 준비한다.

```bash
run_root="$(mktemp -d /tmp/malbut-swm25-132.XXXXXX)"
ros2 run malbut_scenarios prepare_named_navigation_fixture -- \
  --destination "$run_root/map-store"
```

Small House testbed를 별도 terminal에서 실행한다.

```bash
ros2 launch malbut_gazebo small_house_nav2_testbed.launch.py \
  enable_named_navigation:=true \
  named_navigation_user_map:=<fixture-output-user_map_path> \
  named_navigation_map_store:="$run_root/map-store" \
  named_navigation_port:=8765 \
  gui:=true headless:=false rviz:=true
```

실제 active fixture 경로는 fixture 명령 출력과 `active.json`을 source of truth로
사용한다. 경로를 추측해서 입력하지 않는다.

Agent와 Robot Web은 같은 포트를 사용할 수 없으므로 private `.env.local`에서
Agent 포트를 분리한다.

```text
MALBUT_AGENT_PROVIDER=mock
MALBUT_AGENT_TOOL_MODE=proposal
MALBUT_AGENT_AUTH_TOKEN=<local-random-token>
MALBUT_AGENT_USER_ID=local-user
MALBUT_AGENT_HOST=127.0.0.1
MALBUT_AGENT_PORT=8877
MALBUT_AGENT_DB=<private-path>/swm25-132.sqlite3
MALBUT_NAMED_NAVIGATION_MAP_STORE=<run_root>/map-store
MALBUT_ROBOT_DEVICE_ID=malbut-sim-01
MALBUT_ROBOT_WEB_URL=http://127.0.0.1:8765
```

구성 확인은 non-actuating이다.

```bash
ros2 run malbut_scenarios malbut_text_agent_server -- \
  --env-file <private-path>/.env.local \
  --execute-approved-simulation --check
```

명시적으로 실행 모드를 켠다.

```bash
ros2 run malbut_scenarios malbut_text_agent_server -- \
  --env-file <private-path>/.env.local \
  --execute-approved-simulation
```

그 뒤 `http://127.0.0.1:8877`의 conversation/text-turn endpoint에 SWM25-131과
같이 요청과 `네`를 각각 보낸다. `아니요`, `취소`, 모호한 응답은 Action을
만들지 않는다.

## 10. default-OFF와 종료

- 실행 flag가 없으면 SWM25-131과 동일하게 static simulation state와
  non-authorizing confirmation만 사용한다.
- `--check`는 execution flag가 함께 있으면 action schema·repository·worker·
  executor 전체 구성을 생성·검증한 뒤 즉시 닫되, Robot Web HTTP와 Nav2는
  호출하지 않는다.
- Agent HTTP port와 Robot Web port가 같으면 bind 전에 실패한다.
- 실행 모드 HTTP handler는 non-daemon으로 두어 SQLite close 전에 drain한다.
- 종료 순서는 worker close, HTTP listener/handler drain, worker join,
  ActionRepository close, ConversationStore close, MemoryStore close다.

active navigation은 자동 cancel하지 않는다. 종료 중 terminal을 기다릴 수 없거나
status를 증명하지 못하면 원장은 `UNKNOWN`을 유지하며 start를 재전송하지 않는다.
startup recovery가 실패하면 HTTP serve 전에 시작 자체가 실패한다. 실행 중
recovery가 일시 실패하면 dispatcher는 살아 있지만 unhealthy 상태에서 claim을
중단하고 recovery만 bounded retry한다. recovery가 성공한 뒤에만 새 claim을
재개한다.

## 11. 포함하지 않는 기능

- STT, wake word, VAD/AEC와 TTS
- Homecam, KVS와 AWS staging
- 자율 로밍 또는 다중 waypoint coverage
- 실제 로봇 권한과 hardware E-stop source
- active voice/UI cancel
- trusted result를 다음 대화와 TTS로 전달하는 폐루프

SWM25-132는 승인과 단일 Gazebo 이동 사이의 durable 실행 경계만 완성한다.
자연어 요청부터 실제 Gazebo terminal까지 반복 시연하고 증거를 묶는 작업은
SWM25-133에서 이어간다.

승인 응답 문구는 실행 mode나 replay 시점에 따라 바뀌지 않는다. 응답은 승인
기록 자체가 이동 권한이 아니며 별도 안전 재검사가 이동 여부를 결정한다고만
말한다. 따라서 이미 실행됐을 수 있는 replay에서 “아직 시작하지 않았다”고
잘못 주장하거나, 원장이 없는 기본 mode에서 “실행 대기 중”이라고 주장하지
않는다.

이 중립 문구는 SWM25-131 기본 mode의 기존 한국어 `message` literal을 의도적으로
바꾼 호환성 예외다. JSON wire shape, `status`, `result_code`와 execution 값은
유지한다. client는 번역 가능한 `message` 문자열이 아니라 구조화된 필드로
분기해야 한다.

## 12. 검증 명령

```bash
PYTHONPATH=malbut_agent_server:malbut_gazebo:malbut_scenarios \
python3 -m pytest -q \
  malbut_agent_server/test/test_agent_contract.py \
  malbut_agent_server/test/test_text_confirmation_store.py \
  malbut_agent_server/test/test_text_turn.py \
  malbut_agent_server/test/test_approved_action_worker.py \
  malbut_agent_server/test/test_sqlite_action_repository.py \
  malbut_gazebo/test/test_robot_web_navigation_client.py \
  malbut_gazebo/test/test_named_navigation_facade.py \
  malbut_scenarios/test/test_approved_named_navigation_executor.py \
  malbut_scenarios/test/test_text_agent_server.py
```

최종 완료 보고에는 focused test, package 전체 test, colcon build/test, installed
import/CLI smoke와 실제 Gazebo action/Robot Web/Nav2 증거를 각각 구분해 기록한다.

## 13. 2026-08-29 완료 증거

최신 source와 isolated install overlay에서 다음을 확인했다.

- `colcon build`: `malbut_agent_server`, `malbut_gazebo`,
  `malbut_scenarios` 3개 package 성공
- `colcon test`: Agent Server 306, Gazebo 365, Scenarios 50, 합계 721개 통과
- installed domain·repository·worker·Robot Web client·executor import 성공
- installed CLI help와 설치된 SWM25-132 문서 확인
- execution flag를 포함한 `--check`: full action composition을 만들고
  `nav2=off`, Robot Web/ROS/Nav2 호출 0회로 종료

실제 headless Small House 시험은 다음 결과로 끝났다.

```text
Robot Web readiness:
  localization=ok
  AMCL/planner/controller/BT/global costmap/Collision Monitor=active

text request="거실로 가줘"
confirmation response="네"
Robot Web POST /api/navigation/start 202 = 1회
Nav2: (-3.67, -0.49) -> 거실 (5.35, -1.80)
terminal: Goal succeeded
RobotAction: SUCCEEDED / NAVIGATION_SUCCEEDED = 1건
execution outbox: TERMINAL / NAVIGATION_SUCCEEDED = 1건
simulation=true / physical_authorized=false
```

동일 승인 request replay는 `cached=true`, 늦은 새 `네`는
`confirmation_not_pending`이었다. 이후 Action과 outbox 수는 각각 1건으로
유지되어 두 번째 goal이 생성되지 않았다. Agent를 먼저 닫고 Gazebo launch를
종료한 뒤 소유 process와 8765/8877 listener 잔류도 0건이었다. private fixture,
DB, token과 raw 실행 증거는 Git에 포함하지 않는다.
