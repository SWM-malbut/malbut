# Malbut Agent Server

`malbut_agent_server`는 LLM과 로봇 실행 계층 사이의 안전 계약, 사용자별
멀티턴 세션, 제한된 대화·기억 컨텍스트, LLM provider 연결과
서버 소유 Tool capability 경계를
제공하는 ROS 2 Python 패키지다.

SWM25-72에서 오프라인 `mock`과 OpenAI Responses API를 같은
요청·응답 규격으로 연결했다. 다음 기능을 검증할 수 있다.

- `(user_id, conversation_id)` 단위 SQLite 세션 격리
- `request_id`, `turn_id` 기반 내구성 있는 중복 요청 방지
- 사용자·로봇 발화의 순서 저장과 최근 10턴 전달
- 세션 생성·조회·초기화·종료·삭제
- 유휴 만료와 reset·delete 중 늦게 도착한 응답 차단
- 같은 대화의 동시 요청 직렬화와 서로 다른 대화의 provider 병렬 처리
- `아까 말한 것`, `그 사람`, `그거`의 Mock 기반 후속 표현 회귀
- 최근 N턴 원문과 그 이전 대화의 결정론적 rolling summary 분리
- 사용자별 장기 기억의 별도 검색과 만료 항목 제외
- 전체 모델 입력 문자 제한, overflow fallback과 내용 없는 크기 메트릭
- 과거 대화·요약·기억을 `_untrusted` JSON 데이터로 직렬화
- OpenAI 구조화 응답·엄격한 Tool schema·사용량 메타데이터 정규화
- 유한 retry, backoff, circuit breaker와 옵션 모델 fallback
- API 오류 시 로봇 행동이 아닌 안전 응답으로 fail-closed
- 30개 한국어 고정 테스트셋·반복 실행·비용 추정 평가 CLI
- 서버 소유 capability registry와 요청 Tool 부분집합 계산
- 읽기 전용·명시적 시뮬레이션·제안 전용 Tool 모드 분리
- 인증된 capability 조회와 비부작용 Tool query API
- Tool 입력 schema, timeout, 결과 크기·상태 freshness 검증
- 프로세스 내 Tool query 중복 억제와 오류 원문 비공개
- 확인 근거·CAS·영속 멱등성·내용 없는 audit를 갖춘 장기 기억 core
- final transcript만 받는 비실행 음성 대화 경계와 TTS 취소 계약
- 고수준 `monitor_room` 제안, 비실행 확인 요청과 로컬
  approve/deny/cancel/expire 상태 전이
- strict semantic room snapshot 검증과 immutable device/map/room/effects
  binding
- request-scoped trusted RobotState UDS client, peer/device/boot/sequence/TTL
  검증과 `monitor_room` 전용 Safety wiring
- immutable tri-state snapshot, material-change sequence, receipt/map fencing,
  bounded UDS lifecycle을 갖춘 ROS 독립 RobotState collector core
- Nav2 lifecycle·endpoint readiness와 fresh `map→base_footprint` TF만 읽어
  collector에 원자적으로 넣는 별도 비명령 ROS observation node
- exact device heartbeat·적용 generation·엄격한 frame/GStreamer 수락을 묶어
  camera와 software privacy gate를 원자 반영하는 Homecam media evidence 경계
- immutable `TargetBinding`에서 500 mm global map lattice의 strict-interior
  semantic sample plan을 결정적으로 만드는 bounded pure-Python coverage core
- 승인과 current binding을 같은 SQLite transaction에서 다시 검사하는
  terminal-only·simulation-only v4 원장과 coordinate-free plan/result/receipt digest
- fresh v4 simulation receipt에서 직접 파생한 비실행 trusted result를
  같은 transaction에 저장하고, 다음 Provider turn의 별도 server-trusted
  context로 전달하는 내구성 경계
- trusted result와 같은 transaction에서 fixed-template 알림 event를 만드는
  non-authorizing TTS outbox와 leased claim·terminal ACK 경계
- 그 outbox를 실제 speaker가 아닌 인증된 scripted text pull에만 연결하는
  generation-fenced claim·cancel·terminal bridge
- 최종 안전 응답을 제한된 visual cue로 바꾸는 비실행 감정 표현 정책

장기 기억 변경 core는 구현했지만 신뢰된 person identity와 확인 token이 필요한
공개 HTTP/ROS CRUD adapter는 열지 않았다. 실제 ROS 부작용 Tool 실행기도 후속
스토리에서 연결한다. 모델이 추론한 내용을 자동 저장하는 경로는 없다. 현재 서버는
request-scoped RobotState source가 없으면 모든 행동 제안을 fail-closed하며,
`MALBUT_AGENT_TOOL_MODE=proposal`이 기본이라 OpenAI 또는 Mock이 반환한 Tool
제안을 물리 실행하지 않는다. UDS client·runtime wiring·ROS 독립 collector core와
read-only Nav2/localization 및 Homecam camera/privacy observation은 구현됐지만,
authoritative battery/e-stop/zone source는 아직 없다. media evidence의 physical
GStreamer/Aurora 환경과 ROS publisher trust도 별도 운영 검증이 필요하다.

## 테스트

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server
PYTHONPATH=. python3 -m pytest -q test
```

2026-08-16 최신 Agent 회귀 결과는 `1073 passed`다. Homecam media producer C++
회귀는 55개, trusted RobotState observer focused 회귀는 49개가 통과했다.
별도 패키지인 `malbut_voice` M0는 명시적 one-shot microphone capture용
source core만 추가됐으며 현재 package 회귀는 85 passed, hardware smoke 1개와
ament lint 2개 skipped다. Gazebo monitor-room durable state core + injected
Nav2 adapter focused 회귀는 86 passed다. `malbut_voice`는 임시 install space에서
`ros2 run ... --help`까지 확인했으며, 실제 microphone capture smoke는
실행하지 않았다.

전체 계약은
[`docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md`](docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md)에
정리되어 있다. 여섯 연관 스토리의 책임 경계는 관리자 승인을 받았지만,
SWM25-73~77의 오프라인 계약을 구현·검증했더라도, 실제 ROS·장치 adapter와
통합 안전 시험을 별도로 완료하고 승인하기 전에는 실행 가능한 물리 기능으로
취급하지 않는다.

승인 증거와 후속 구현 전 확인할 항목은
[`SWM25-69 인터페이스 승인 가이드`](docs/jira/SWM25-69_INTERFACE_APPROVAL_GUIDE.md)에
정리되어 있다. CI 통과나 PR 병합은 구현 근거이며 책임 경계 승인만으로
후속 물리 기능 구현이 완료되지는 않는다.

## Mock 서버 실행

먼저 설정과 DB 초기화를 검사한다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --provider mock \
  --database /tmp/malbut-agent-demo.sqlite3 \
  --check
```

서버를 실행한다. 기본 주소는 `http://127.0.0.1:8765`다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --provider mock \
  --database /tmp/malbut-agent-demo.sqlite3
```

세션을 만든다.

```bash
curl -X POST http://127.0.0.1:8765/v1/conversations \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "local-user",
    "conversation_id": "demo-conversation"
  }'
```

첫 번째 발화를 보낸다.

```bash
curl -X POST http://127.0.0.1:8765/v1/agent/respond \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "request-001",
    "user_id": "local-user",
    "conversation_id": "demo-conversation",
    "turn_id": "turn-001",
    "utterance": "내 이름은 사용자A야",
    "robot_state": null,
    "available_tools": []
  }'
```

같은 `request_id`와 동일한 입력을 재전송하면 저장된 응답을 반환하며 Mock을
다시 호출하지 않는다. 같은 ID로 다른 입력을 보내면 `409`로 거절한다.

## SWM25-73 Tool Gateway

`available_tools`는 클라이언트가 capability를 선언하는 필드가 아니다. 서버
registry가 허용한 목록을 이번 요청에서 더 좁히는 selector다. 모델과 safety
policy에는 다음 교집합만 전달된다.

```text
정적 Tool schema ∩ 서버 capability registry ∩ 요청 available_tools
```

현재 capability와 실행 가능 여부를 확인한다. 인증을 사용하는 서버라면 같은
Bearer 헤더를 추가해야 한다.

```bash
curl http://127.0.0.1:8765/v1/tools/capabilities
```

기본 `proposal` 모드에서 이동을 query해도 실제 Nav2 goal은 발행되지 않고
`confirmation_required`로 차단된다.

```bash
curl -X POST http://127.0.0.1:8765/v1/tools/query \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "tool-query-001",
    "user_id": "local-user",
    "tool_name": "navigate",
    "arguments": {"location": "거실"}
  }'
```

로컬 연결 시험에서만 시뮬레이션을 명시적으로 켤 수 있다. 이 모드는 LLM
provider 선택과 독립적이며 Mock provider를 선택했다고 자동으로 켜지지 않는다.

```bash
MALBUT_AGENT_TOOL_MODE=simulation \
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --provider mock \
  --database /tmp/malbut-agent-simulation.sqlite3
```

시뮬레이션 adapter는 결과에 `simulated=true`를 남기며 Nav2 goal, 사진 파일,
외부 알림을 만들지 않는다. `/v1/tools/query`는 읽기 전용 또는 이 Mock
시뮬레이션만 처리한다. confirmation, 실제 행동, `tool_call_id`, 재시작 뒤에도
유지되는 1회 소비와 취소·feedback은 SWM25-74 범위다.

현재 query cache는 프로세스 내 최대 256건으로 제한된다. adapter 응답
deadline이 지나도 이미 시작된 Python thread를 강제로 중단하지 못하므로,
73에서는 자체 I/O timeout이 있고 부작용이 없는 adapter만 연결한다.

`/v1/agent/respond`의 `execution.proposal_authorized`는 로컬 정책을 통과한
제안이라는 뜻일 뿐이다. `execution.authorized`와 `consume_once`는
SWM25-74 전까지 항상 `false`이고 `tool_call_id`는 `null`이다.

## 방 모니터링 확인 수직 단면

현재 `monitor_room(location)`은 production과 simulation 모두 영구
`proposal_only`다. Provider 추론 뒤 `monitor_room` 제안에 대해서만 고정 UDS에서
request-scoped RobotState evidence를 새로 읽고, peer UID·device·host boot·
collector instance/sequence·field receipt·TTL을 검증한다. complete하고 current한
evidence만 Safety용 `RobotState`로 바뀐다. 그 다음 signed Homecam resolver가
device/map/semantic revision/room과 이동·영상·녹화·음성 효과를 immutable
`TargetBinding`으로 묶으며, RobotState evidence의 device/map/map revision과
exact-match해야 `ToolConfirmationRequest`를 만들고 `awaiting_confirmation`에서
멈춘다.

UDS client와 factory wiring, ROS와 분리된 collector store/server core, 그리고
별도 `malbut_gazebo/trusted_robot_state_observer`가 구현되어 있다. observer는
Nav2 lifecycle·action/service readiness와 fresh `map→base_footprint` TF만 읽고,
goal·costmap command·velocity를 호출하거나 semantic zone을 추정하지 않는다.
Homecam media agent는 device-bound heartbeat, 적용 generation, 엄격한 image shape와
GStreamer frame 수락을 `HomecamMediaEvidence`로 발행하고, observer는 software
media gate 의미의 camera/privacy를 동일 receipt·TTL로 원자 반영한다. 동일
sequence의 body는 고정되고 material observation만 sequence를 전진시키며, source
restart·map 변경·오래된 receipt를 fence한다. 그러나 authoritative battery/e-stop/
zone source가 없고 observer도 자동 실행되지 않는다. 관련 환경 변수와 room
allowlist도 기본적으로 비어 있으므로 production 기본 경로는
`untrusted_robot_state`로 fail-closed한다. 현재 빌드 머신에는 GStreamer 개발
패키지와 실제 Aurora/backend가 없어 physical frame branch는 장치 환경에서 별도
검증해야 한다. HTTP body의 `robot_state`는 Provider나
Safety의 권한 근거가 아니며, 모델에는 all-false snapshot 대신
`robot_state_untrusted={"availability":"unknown"}`이 전달된다. Homecam adapter는
고정된 server-owned 사용자·장치에 대해 HTTPS bearer와 HMAC 서명 envelope를
검증하지만 실제 요청자의 인증 principal이나 물리 실행 권한을 대신하지 않는다.
Homecam endpoint는 설정된 Cognito subject·email과 일치하는 활성 Web session,
owner membership, finalized map을 한 DB snapshot에서 확인하며 Agent 쪽 user는
아직 고정된 단일 사용자 MVP다.

RobotState와 방 allowlist는 다음처럼 서버가 함께 고정한다.

```bash
MALBUT_HOMECAM_ORIGIN=https://homecam.example.com
MALBUT_HOMECAM_AGENT_TOKEN='<server bearer>'
MALBUT_HOMECAM_SIGNING_SECRET='<independent envelope secret>'
MALBUT_HOMECAM_PRINCIPAL_SUBJECT_DIGEST='<lowercase sha256>'
MALBUT_HOMECAM_DEVICE_ID=malbut-robot-01
MALBUT_ROBOT_STATE_SOCKET_PATH=/run/malbut/robot-state.sock
MALBUT_ROBOT_STATE_EXPECTED_UID=1000
MALBUT_ROBOT_STATE_DEVICE_ID=malbut-robot-01
MALBUT_ROBOT_STATE_TIMEOUT_SECONDS=2
MALBUT_AGENT_MONITORABLE_ROOMS='거실,주방'
```

`SOCKET_PATH`, `EXPECTED_UID`, `DEVICE_ID`는 all-or-nothing이다. non-empty
`MALBUT_AGENT_MONITORABLE_ROOMS`는 complete Homecam binding과 RobotState binding을
모두 요구하며 두 `DEVICE_ID`가 정확히 같아야 한다. timeout 허용 범위는 1~5초,
room allowlist의 안전한 기본값은 빈 목록이다. collector가 required field를
`null`로 보내거나 응답이 누락·timeout·stale이면 confirmation 이전에 차단한다.
collector core는 기존 socket path를 자동 삭제하지 않는다. 비정상 종료 뒤 남은
UDS는 supervisor가 owner/type을 확인해 정리해야 하며, 정상 종료는 자신이 만든
inode만 제거한다.

관측 node의 수동 실행과 정확한 읽기 범위는
[`malbut_gazebo/README.md`](../malbut_gazebo/README.md#trusted-robotstate-observation-boundary)에
정리되어 있다. `physical_authority` 기본값은 `false`이며, 이를 켠 상태에서는
startup과 runtime 모두 `use_sim_time=true`를 거절한다. 이 node 하나만으로는
required battery/e-stop/zone field가 완성되지 않으므로 `monitor_room`
confirmation까지 갈 수 없다. Homecam의 `privacy_mode`는 물리 셔터 증거가 아니라
backend-bound software media gate의 적용 상태라는 좁은 의미다.

확인 질문 TTS가 끝나거나 barge-in으로 취소되어도 pending 요청은 유지된다.
pending 상태의 `네`, `아니요`, `취소`는 LLM으로 보내지 않고 서버의 좁은 로컬
문법으로만 분류한다. 확인 문장은 모델 문구가 아니라 서버가 target/effects에서
결정적으로 생성한다. 음성과 UI 확인 입력은 서로 다른 DTO를 사용하며 UI는 오디오
epoch나 client-supplied user/Tool 실행 필드를 받지 않는다. 요청은 conversation의
private session instance, generation, revision, ordinal과 비공개 target/effects
snapshot을 함께 fingerprint에 묶는다. 삭제 후 같은 conversation ID를 다시
만들어도 오래된 음성 세션은 Provider 호출 전에 차단된다.

동일 response ID의 첫 payload는 pending 동안 eviction하지 않으며, 서버의
deadline sweep은 사용자 response claim과 독립적으로 confirmation을 만료한다.
결과는 사용자 의사 기록이며 다음 값을 고정한다.

```text
execution_authorized=false
consume_once=false
tool_call_id=null
mission_id=null
```

따라서 확인은 계속 non-authorizing이며, 이 단계에는 실제 Nav2, 카메라, KVS
또는 Tool Gateway 실행 호출이 없다. 별도 `malbut_voice` M0의 one-shot
microphone/STT source core는 있지만 Agent runtime sink에 연결하지 않았고,
실제 speaker TTS 장치도 없다. scripted 경로는 인증된 text-only
simulator다. 전체 범위와 다음
durable gate는
[SWM25-78 방 모니터링 재구축 기준](docs/jira/SWM25-78_ROOM_MONITORING_REBUILD.md)에
정리되어 있다.

confirmation 요청은 Agent turn 완료와 같은 SQLite transaction에서
`confirmation_intents`에 저장된다. 승인·거절·취소·서버 만료도 현재
session instance/generation/revision과 deadline을 다시 검사한 뒤 한 terminal
winner로 영속화한다. reset, close, session expiry, 다음 turn은 기존 pending을
무효화하고 delete는 관련 기록을 제거한다. 프로세스 메모리는 음성/UI UX를 위한
mirror일 뿐이며, 재시작 후 deadline sweep은 SQLite 기록을 기준으로 수행한다.

다만 이 durable row도 여전히 사용자 의사 증거일 뿐 실행 권한이 아니다.
device/map/room/effects binding은 request schema v3에 저장되지만, 실제 실행
전에는 인증된 principal과 현재 semantic snapshot을 다시 검증하고 confirmation
소비와 `tool_call_id` 1회 발급을 별도의 실행 원장 transaction에서 처리해야
한다. storage schema v1의 request v2 terminal row는 감사용으로 보존하고,
기존 pending은 migration 때 영구 무효화한다.

그 다음 작은 단면으로 server-internal terminal simulation ledger를 구현했다.
테스트가 만든 HMAC-bound approval과 fresh `TargetBinding`을 받아 같은 SQLite
`BEGIN IMMEDIATE` 안에서 confirmation/session/deadline/target/effects를 다시
검사한다. 통과하면 500 mm global `map` lattice에서 Polygon/MultiPolygon의 모든
component에 대해 경계와 hole을 제외한 strict-interior semantic sample plan을
결정적으로 만든다. 이 계산은 candidate/sample/geometry-test 상한을 먼저 확인하며
clock, random, 파일, network, ROS, Nav2 또는 카메라를 호출하지 않는다. 좌표는
private plan 안에만 있고 SQLite에는 planner/profile/plan/result digest와
sample/component 수, terminal `receipt_digest`만 저장한다. 재시작 exact replay는
같은 ID와 digest를 반환하고 planner를 다시 호출하지 않는다. reset/close/delete,
deadline 또는 관측된 target/effects 변경은 영구 tombstone이 된다. 결과는
항상 다음을 명시한다.

```text
authority.kind=simulation_only 또는 none
simulation=true
physical_authorized=false
physical_effects=false
viewer_live=false
nav2_validated=false
camera_coverage_validated=false
coverage_achieved=false
```

따라서 이 성공은 “semantic sample plan을 만들었다”는 뜻일 뿐, 경로의 도달 가능성,
충돌·금지구역, 카메라 FOV·가림, 실제 frame 전송이나 방 전체 촬영 완료를 증명하지
않는다. storage v3 terminal은 v4 migration에서 `legacy_unplanned` 감사행으로만
보존되고 coverage 필드는 모두 `null`이며 새 계약으로 replay/승격되지 않는다.
모든 v4 terminal field는 content-free SHA-256 `receipt_digest`로 함께 묶지만,
이는 keyed MAC이 아니므로 hostile privileged DB writer에 대한 암호학적 서명은 아니다.
`completed_at`은 planner wall-clock 종료 시각이 아니라 동일 transaction에서 소비를
확정하기 직전에 샘플한 terminal-decision 시각이다.

별도 Gazebo package에는 이 결과와 아직 연결되지 않은 durable operation store와
pure injected Nav2 controller가 있다. 이 controller는 store의 private 좌표와
map/semantic/zone/plan binding만 읽고 exact preflight/goal report, stable goal
UUID, lease/fence/deadline을 검사한 뒤 `ensure_started` 포트 계약을 호출한다.
현재 포트는 테스트 주입물뿐이며 ROS/Nav2 import나 action 전송은 없다. 실제
포트는 side-effect 직전 현재 clock·fence를 원자적으로 확인하고 cross-process
start/cancel을 멱등 처리해야 하므로, 이 단면의 navigation progress는 물리 실행
또는 room coverage 증거가 아니다.

이제 fresh `planned`/`planning_failed` v4 receipt는 같은 `BEGIN IMMEDIATE`
transaction에서 `conversation_trusted_tool_results` v1 행으로 직접
파생된다. 외부 DTO를 신뢰 입력으로 받지 않고 terminal receipt와
confirmation·turn·session을 다시 검증한다. 행 삽입과 session revision
증가는 원장 terminalization과 원자적이며 TTL은 늘리지 않는다.
이 revision fence 때문에 이전 context로 실행 중인 Provider 응답은 CAS에서
지고 새 context로 재시도해야 한다.

다음 uncached turn은 식별자·digest·좌표·device/map 정보가 제거된
`trusted_server_tool_results` 섹션으로 최신 10건만 Provider에
전달한다. 이 섹션은 시뮬레이션 과거 사실이지 명령이나 실행
권한이 아니며, `physical_effects`, `viewer_live`, `nav2_validated`,
`camera_coverage_validated`, `coverage_achieved`, `execution_authorized`는
모두 false로 고정된다. invalidated/legacy receipt와 activation 이전
receipt는 승격·backfill하지 않고, conversation delete는 대화 result를
제거하면서 재실행 방지용 content-minimized terminal receipt는 남긴다.
활성화 표식은 실행 원장의 immutable preactivation 영역에 별도로
남겨 result 스키마 전체 삭제를 fresh activation으로 오인하지 않는다.

fresh trusted result와 같은 transaction에서 non-authorizing
`trusted_result_tts_outbox` v1 event도 파생한다. 원문 TTS message나 prompt를
저장하지 않고 source binding, 고정 template identity·digest, delivery state와
lease credential을 저장한다. activation 이전 result는 `preactivation`
cancelled event로 남겨 새 알림으로 backfill하지 않는다. Store 내부 API는 stable
TTS event ID, 1~300초 lease, 최대 5개 fence와 동일 claim/terminal-ACK 요청의
exact replay를 제공한다. reset·close·expiry는 pending/claimed event를 cancel하고,
delete는 conversation-private trusted result와 outbox를 함께 제거한다.
claim token은 commit-before-response exact replay를 위해 durable credential로
저장되므로 운영 DB 접근 통제·암호화·보존 정책의 대상이다.

outbox claim/ACK는 opt-in authenticated scripted speech coordinator와 HTTP의
명시적 pull/terminal 경계까지 연결됐다. 이 bridge는 고정된 speech session과
conversation instance/generation을 다시 검사하고, 일반 TTS·confirmation·inference와
단일 process-local playback slot을 공유한다. claim token은 서버 내부에만 두며
generic TTS terminal은 이 outbox를 ACK할 수 없다. barge-in과 lease 만료는 먼저
결정론적 cancel을 반환하고, 그 cancel의 terminal 신호가 오기 전에는 다음 claim이나
transcript를 받지 않는다. 미완료 claim은 같은 speech session과 claim request로
재시작 후 복구할 수 있고, 이미 ACK된 event는 다시 전달하지 않는다.

하지만 background drain, ROS dispatcher와 실제 speaker adapter는 여전히 없고
production 인증 verifier도 없다. 현재 HTTP 경계는 `scripted_text_only`이며,
`acknowledged`는 trusted downstream adapter가 terminal request를 확인했다는 뜻일
뿐 오디오가 재생됐거나 사람이 들었다는 증거가 아니다
(`physical_audio_verified=false`). 재생 후 ACK 전 crash에서는 재전달될 수 있으므로
audio exactly-once도 보장하지 않는다. 실제 연결에는 stable event ID를 사용하는
idempotent·observe 가능한 adapter, multi-process durable playback arbitration과
barge-in/reset/close cancel·reconcile가 필요하다.

운영 기본 Store는 verifier가 없어 fail-closed한다. Python 객체를 client가 직접
만드는 것은 인증이 아니며, 생성되는 `tool_call_id`·`mission_id`·`operation_id`는
simulation 추적 ID일 뿐 ROS capability가 아니다. 물리 Nav2/Homecam 실행에는 이
TTS outbox와 별개의 durable execution outbox, lease, fence, idempotent adapter와
observe/reconcile가 필요하다.

`consume_request_id`는 confirmation/approval 범위의 멱등성 키다. terminal row가
생긴 뒤의 exact replay는 동일한 서명된 approval/request를 요구하지만 target freshness를
갱신하거나 simulator를 다시 실행하지 않는다. 신규 소비만 현재 target evidence를
검사한다. activation 전 proposal은 immutable proposal-fingerprint denylist로 승격을
차단한다. conversation 삭제 뒤에도 terminal receipt의 opaque ID·digest·timestamp와
denylist는 재실행 방지 감사 기록으로 남으므로, production 배포 전 보존 기간·삭제·
가명화·키 폐기 정책을 정해야 한다.

## SWM25-75~77 오프라인 계약

세 후속 스토리는 외부 장치나 유료 API를 호출하지 않는 범위에서 구현했다.

- SWM25-75: 확인된 장기 기억 create/update/delete, record CAS, 사용자별 영속
  revision, 재시작 후 멱등 replay와 내용 없는 audit
- SWM25-76: 원시 오디오를 받지 않는 final transcript 계약, 신뢰된
  사용자·세션 binding, self-echo 차단과 TTS barge-in/cancel 상태기계
- SWM25-77: 최종 Safety 응답의 결정적 visual cue, 긴급·privacy 우선 억제,
  TTL·빈도 제한·bounded process-local idempotency와 neutral fallback

각 기능의 대표 검사를 300회씩 반복하는 명령은 다음과 같다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
python3 scripts/run_swm25_75_77_stress.py \
  --iterations 300 \
  --output \
  docs/evaluations/artifacts/SWM25-75_77_300X_OFFLINE_2026-08-13.json
```

이 검증은 실제 사람 인식, STT/TTS, ROS, frontend renderer 또는 운영 성능
시험을 대신하지 않는다. 현재 완료 범위와 blocker는 각 스토리 문서와
300회 반복 보고서에 분리해 두었다.

## OpenAI 서버 실행

`.env.example`을 Git에서 제외되는 로컬 파일로 복사한 뒤 권한을 제한한다.

```bash
cp .env.example .env.local
chmod 600 .env.local
```

`.env.local`에서 `MALBUT_AGENT_PROVIDER=openai`, `OPENAI_API_KEY`,
`MALBUT_AGENT_AUTH_TOKEN`을 설정한다. API key는 코드·Git·명령행 인자에
넣지 않는다. 실측 기준 운영 후보는 `gpt-5.6-terra`, 저비용
fallback 후보는 `gpt-5.6-luna`다.

먼저 유료 API 호출 없이 설정을 검사한다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --env-file .env.local \
  --check
```

검사가 통과하면 서버를 실행한다. OpenAI 모드는 loopback bind와 HTTP
Bearer 인증을 모두 강제한다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --env-file .env.local
```

`/healthz`를 제외한 요청에는
`Authorization: Bearer <MALBUT_AGENT_AUTH_TOKEN>` 헤더가 필요하다.

### 인증된 text-only speech 시험

Agent runtime에 verified microphone/STT sink를 붙이기 전에는 명시적인
시험 모드만 열 수 있다.

```bash
MALBUT_AGENT_ENABLE_SCRIPTED_SPEECH=true
MALBUT_AGENT_AUTH_TOKEN='<32자 이상의 local bearer token>'
```

또는 CLI에 `--enable-scripted-speech`를 전달한다. 이 모드는 다음 여섯 경로만
추가한다.

```text
POST /v1/speech/scripted/sessions/open
POST /v1/speech/scripted/transcripts
POST /v1/speech/scripted/tts/terminal
POST /v1/speech/scripted/trusted-result-tts/claim
POST /v1/speech/scripted/trusted-result-tts/terminal
POST /v1/speech/scripted/sessions/close
```

서버가 user, speaker, source와 Tool registry를 고정한다. 요청 body의
`user_id`, `robot_state`, `available_tools`는 거절한다. 모든 응답에는
`runtime=scripted_text_only`, `physical_authority=false`,
`physical_audio_verified=false`가 붙는다. trusted-result claim은
`speech_session_id`, client-generated idempotency용 `claim_request_id`와 선택적
1~300초 lease만 받고, terminal은 공개 `tts_request_id`와
`terminal_request_id`만 받는다. user/conversation/result/text/claim token은 body로
받지 않으며, 일반 `/tts/terminal`은 durable result notification을 끝낼 수 없다.
따라서 이
경로는 멀티턴·TTS lifecycle·confirmation UX를 관찰하는 통합 시험일 뿐,
마이크 출처나 로봇 상태를 증명하거나 Nav2·카메라 실행을 허가하지 않는다.
잘못된 bearer는 기본 30회/분의 독립된 제한을 사용하고, 정상 요청의 rate-limit
bucket과 분리된다. 종료 시에는 진행 중인 handler가 끝난 뒤 SQLite와 Gateway를
닫아 응답 도중 저장소가 사라지지 않게 한다.

## Provider 평가

오프라인 Mock 계약을 먼저 확인한다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.eval_runner \
  --provider mock \
  --repetitions 3 \
  --output /tmp/malbut-agent-mock-eval.json
```

실제 비교는 동일한 30개 테스트를 모델별 최소 3회 반복한다. 원문
발화·응답·API key는 보고서에 저장하지 않으며, 출력 JSON은
`0600` 권한으로 저장된다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.eval_runner \
  --provider openai \
  --model gpt-5.6-luna \
  --model gpt-5.6-terra \
  --repetitions 3 \
  --timeout-seconds 5 \
  --request-delay-seconds 0.1 \
  --env-file .env.local \
  --output /tmp/malbut-agent-openai-eval.json \
  --progress
```

## 합성 대화 흐름 기록

일반 평가 JSON은 개인정보 보호를 위해 발화·응답·원문 prompt를 저장하지
않는다. 요청부터 컨텍스트 선택, Mock 원결정, SafetyPolicy, 최종 응답과
DB 저장까지 사람이 읽어야 할 때는 합성 데이터 전용 trace를 실행한다.

```bash
PYTHONPATH=. python3 scripts/run_synthetic_conversation_trace.py
```

이 명령은 인메모리 SQLite와 결정론적 MockProvider만 사용하며 OpenAI,
ROS, 카메라, 파일 생성 Tool, 알림 전송을 호출하지 않는다. 전체 JSON은
`0600`으로, 사람이 읽기 쉬운 Markdown은 `0644`로 기록한다. 실제 사용자
대화나 운영 자격 증명을 이 trace에 넣으면 안 된다.

## 사용자 컨텍스트

모델 입력은 다음 영역을 서로 다른 데이터로 구성한다.

- `conversation_history_untrusted`: 현재 세션의 최근 완료 N턴 원문
- `conversation_summary_untrusted`: 최근 N턴 이전 구간의 rolling summary
- `memory_context_untrusted`: 현재 사용자에게 속한 활성 장기 기억
- `robot_state_untrusted`: Provider 요청에서는
  `{"availability":"unknown"}`; client 상태를 known-safe 값으로 정규화하지 않음
- `current_user_utterance`: 현재 요청의 사용자 발화

과거 세 영역 안의 `SYSTEM`, `developer`, Tool 호출 문장은 현재 명령으로
승격하지 않는다. 전체 입력은 기본 20,000자로 제한하며, 초과하면 선택
문맥을 줄인 뒤 현재 발화의 가능한 prefix를 보존한다. 응답의
`provider.context`에는 원문 대신 각 영역의 원본·포함 개수와 문자 수,
잘린 영역과 overflow 여부만 들어간다.

주요 설정은 다음과 같다.

| 환경 변수 | 기본값 | 허용 범위 |
| --- | ---: | ---: |
| `MALBUT_AGENT_MEMORY_LIMIT` | 5 | 1~10 |
| `MALBUT_AGENT_CONVERSATION_HISTORY_LIMIT` | 10 | 10~50 |
| `MALBUT_AGENT_CONVERSATION_SUMMARY_MAX_CHARS` | 2,000 | 256~8,000 |
| `MALBUT_AGENT_MAX_MODEL_INPUT_CHARS` | 20,000 | 4,096~1,000,000 |
| `MALBUT_AGENT_TIMEOUT_SECONDS` | 5 | 1~120 |
| `MALBUT_AGENT_PROVIDER_TOTAL_TIMEOUT_SECONDS` | 11 | 1~300 |
| `MALBUT_AGENT_PROVIDER_MAX_RETRIES` | 0 | 0~3 |
| `MALBUT_AGENT_TOOL_MODE` | `proposal` | `proposal`, `simulation` |
| `MALBUT_ROBOT_STATE_SOCKET_PATH` | 빈 값 | absolute UDS path; UID/device와 함께 설정 |
| `MALBUT_ROBOT_STATE_EXPECTED_UID` | 빈 값 | 0~2,147,483,647; path/device와 함께 설정 |
| `MALBUT_ROBOT_STATE_DEVICE_ID` | 빈 값 | Homecam device와 exact-match |
| `MALBUT_ROBOT_STATE_TIMEOUT_SECONDS` | 2 | 1~5 |
| `MALBUT_AGENT_MONITORABLE_ROOMS` | 빈 값 | comma-separated exact allowlist, 최대 32개 |
| `OPENAI_MODEL` | `gpt-5.6-terra` | 출력 가능한 공식 model ID |
| `OPENAI_FALLBACK_MODEL` | 빈 값 | 선택, 주력과 다른 model ID |
| `OPENAI_REASONING_EFFORT` | `none` | 지원 effort 값 |
| `OPENAI_MAX_OUTPUT_TOKENS` | 500 | 64~4,096 |

## 문서

- [SWM25-69 대화·에이전트 계약](docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md)
- [SWM25-69 인터페이스 승인 가이드](docs/jira/SWM25-69_INTERFACE_APPROVAL_GUIDE.md)
- [SWM25-70 멀티턴 대화 세션](docs/jira/SWM25-70_MULTITURN_CONVERSATION_SESSION.md)
- [SWM25-71 사용자 컨텍스트 통합](docs/jira/SWM25-71_USER_CONTEXT_INTEGRATION.md)
- [SWM25-72 LLM provider 연결](docs/jira/SWM25-72_LLM_PROVIDER_INTEGRATION.md)
- [SWM25-73 Agent Tool Gateway](docs/jira/SWM25-73_AGENT_TOOL_GATEWAY.md)
- [SWM25-75 장기 기억 오프라인 core](docs/jira/SWM25-75_LONG_TERM_MEMORY_INTEGRATION.md)
- [SWM25-76 음성 대화 오프라인 계약](docs/jira/SWM25-76_VOICE_CONVERSATION_PIPELINE.md)
- [SWM25-77 감정 표현 오프라인 계약](docs/jira/SWM25-77_EMOTION_EXPRESSION_INTEGRATION.md)
- [SWM25-78 방 모니터링 재구축 기준](docs/jira/SWM25-78_ROOM_MONITORING_REBUILD.md)
- [SWM25-72 OpenAI baseline 평가](docs/evaluations/SWM25-72_OPENAI_EVALUATION_2026-08-05.md)
- [SWM25-72 OpenAI post-fix parity 평가](docs/evaluations/SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md)
- [SWM25-69~74 구현 재검증·300회 반복 보고서](docs/evaluations/SWM25-69_74_REVALIDATION_2026-08-12.html)
- [SWM25-75~77 기능별 300회 반복 보고서](docs/evaluations/SWM25-75_77_300X_OFFLINE_2026-08-13.md)
- [SWM25-75~77 완성도 강화 보고서](docs/evaluations/SWM25-75_77_HARDENING_2026-08-13.md)
- [합성 대화·컨텍스트 전체 흐름 기록](docs/evaluations/SYNTHETIC_CONVERSATION_TRACE_2026-08-13.md)
- [Malbut LLM Agent 구현·출시 승인 기준](docs/LLM_AGENT_IMPLEMENTATION_ACCEPTANCE_CRITERIA.md)

다중 프로세스 분산 잠금, Tool query cache의 재시작 후 보존, 주기적 만료
sweeper, 독립 provider 장애 fallback과 ROS 2 대화 bridge는 이 MVP의 운영
완료 범위가 아니다.
