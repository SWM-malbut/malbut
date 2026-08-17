# SWM25-78 방 모니터링 재구축 기준

## 1. 문서 목적

이 문서는 2026-08-16 기준으로 방 모니터링 음성 시나리오를 안전한 작은
수직 단면부터 다시 구축하는 현재 계약을 기록한다. 과거 실험에서 만들었던
로컬 STT, 연속 음성, Room Mission, SQLite mission ledger 코드는 롤백됐으며,
이 문서는 그 코드를 완료 상태로 주장하지 않는다.

요구사항 원문은 다음 첨부 자료다.

```text
/home/shin/.codex/attachments/
78b0cc2d-8666-4c8d-8cec-b2846809e2f9/pasted-text-1.txt
```

첨부 자료의 `local_stt.py`, `continuous_voice.py`, `room_mission.py`,
`room_mission_ledger.py`, `durable_room_mission.py`는 이전 실험의 구조와 교훈을
설명한다. 해당 파일들은 롤백되어 현재 워크트리에 없으므로, 이 문서는 과거
테스트 수나 완료 주장을 현재 구현으로 합치지 않는다. 대신 유효한 설계 원칙만
현재 코드에 다시 적용한다.

최종 목표는 다음과 같다.

```text
Wake → microphone → STT → conversation/LLM proposal
 → Safety → user confirmation → one-time server authority
 → semantic room → Nav2/coverage → camera/KVS
 → browser viewer evidence → trusted feedback → TTS
```

핵심 불변식은 `LLM은 제안하고 서버만 실행을 허가한다`이다.

## 2. 현재 구현된 수직 단면

현재 구현은 실제 로봇을 움직이지 않는다. 음성 경로는 사용자 확인에서
멈추며, 그 뒤에는 별도의 server-internal test harness로만 terminal-only
순수 Python simulation을 한 번 소비할 수 있다.

```text
trusted scripted SpeechTranscriptEvent
 → SpeechConversationCoordinator
 → AgentOrchestrator
 → Mock/OpenAI-compatible Tool proposal
 → request-scoped trusted RobotState UDS read
 → SafetyPolicy
 → monitor_room(location="거실")
 → signed Homecam semantic resolver
 → RobotState/target device·map·revision exact-match
 → immutable device/map/room/effects TargetBinding
 → ToolConfirmationRequest
 → conversation turn + confirmation_intents 원자 저장
 → exact local approve/deny/cancel classification
 → SQLite current-session/deadline 재검사와 terminal CAS
 → ToolConfirmationResolution
 → stop (음성 경로의 기본 종료점)

별도 test-only simulation harness:
HMAC-bound approval + fresh TargetBinding
 → 같은 SQLite의 BEGIN IMMEDIATE
 → confirmation/session/deadline/target/effects 재검증
 → confirmation 1회 소비
 → bounded deterministic semantic coverage sample plan
 → coordinate-free plan/result/receipt digest
 → immutable v4 terminal receipt
 → same-transaction conversation trusted result + fixed-template TTS outbox event
 → 다음 uncached turn의 별도 trusted Provider context
   또는 authenticated scripted pull의 leased claim/cancel/terminal ACK
```

모든 확인 결과는 다음 값을 강제한다.

```text
execution_authorized=false
consume_once=false
tool_call_id=null
mission_id=null
```

따라서 현재의 `approve`는 사용자 승인 의사를 기록했다는 뜻일 뿐, 실행
승인이나 ROS 명령이 아니다. simulation harness의 ID도 `simulation_only`
추적 ID이며 물리 실행 capability가 아니다.

### 2.1 최종 시스템의 전체 흐름과 현재 위치

| 단계 | 입력 → 출력 | 책임 경계 | 현재 상태 |
|---|---|---|---|
| 1. Wake·오디오 | wake → bounded microphone audio | 장치 adapter, VAD/AEC, self-echo 방지 | wake/continuous/AEC는 미구현; 별도 `malbut_voice` M0가 명시적 one-shot protected ALSA capture core만 구현 |
| 2. STT | audio → final transcript + confidence/provenance | STT adapter가 실제 microphone 출처를 증명 | 별도 `malbut_voice` M0가 local faster-whisper source core와 private provenance wrapper를 구현; Agent sink·speaker identity 미연결 |
| 3. 음성 세션 | transcript → user/conversation-bound event | `SpeechConversationCoordinator` | opt-in authenticated text-only HTTP와 simulation-result TTS pull/terminal bridge 구현 |
| 4. 대화·LLM | current utterance + bounded context → 답변/Tool 제안 | `AgentOrchestrator`, Provider | 구현 |
| 5. Safety | Tool 제안 → request-scoped UDS evidence → 허용·거절 | peer/device/boot/sequence/TTL을 검증한 complete RobotState만 신뢰 | Agent client/core, read-only Nav2·localization과 Homecam camera/privacy evidence 구현; battery/e-stop/zone은 불완전 |
| 6. 사용자 확인 | target/effects-bound proposal → approve/deny/cancel/expire intent | SQLite `confirmation_intents`; 실행권 없음 | 구현 |
| 7. 실행 소비 | 확인 증거 + current target → semantic sample plan + terminal receipt | SQLite v4 simulation ledger; 물리 권한 없음 | test-only 단면 구현 |
| 8. 방 해석 | `거실` → device/map/semantic revision/room ID | active subject/email + owner + finalized map을 묶는 signed Homecam fetch MVP | 고정 user/device 단면 구현 |
| 9. 이동·촬영 | room plan → Nav2 goal/coverage/camera evidence | ROS adapter + idempotency/reconcile | Gazebo-only durable state core와 pure injected controller 구현; 실제 ROS/Nav2 port·camera·Agent wiring 미구현 |
| 10. 영상 연결 | camera → KVS session → browser frames | Homecam backend/agent/browser | 별도 자산은 구현, Agent adapter 미구현 |
| 11. 결과 대화 | trusted mission evidence → feedback/TTS → next wake | durable feedback/outbox | simulation receipt→durable trusted result→fixed-template TTS outbox→authenticated scripted claim/cancel/terminal까지 구현; 실제 dispatcher·speaker·audible evidence 미구현 |

현재 음성 경로에서 검증된 연속 구간은 **3→5→8→6**이다. request-scoped
RobotState UDS client와 factory wiring, ROS 독립 collector store/server core,
read-only Nav2/localization observer, Homecam applied camera/privacy evidence와
signed Homecam resolver는 구현되어 있다. 다만 authoritative battery/e-stop/zone
source가 없고 production 기본 room allowlist도 비어 있으므로 기본 요청은 5에서
fail-closed한다. camera/privacy는 software media gate 의미이며 실제 Aurora와
GStreamer 활성 환경 검증이 남아 있다. 테스트는
검증된 RobotState evidence와 semantic snapshot을 주입해 target의
device/map/revision exact-match까지 포함한 8→6을 검증한다. 1→2는 실제 오디오가
아니라 opt-in authenticated text-only event로 대체한다. 6 이후의 terminal-only
7은 Speech나 HTTP에서 자동 소비되지 않고 test-only HMAC verifier를 명시한
Store에서만 열린다. Homecam과 Nav2의 기존 자산이 있더라도 7→10을 잇는 trusted
mission adapter가 없으므로 “거실을 실제로 촬영해 웹에서 보여줬다”고 말할 수
없다. 테스트 harness가 fresh planned/planning_failed receipt를 만들면 같은
SQLite transaction이 content-minimized trusted result와 fixed-template TTS
outbox event를 저장하고 session revision을 증가시킨다. 다음 uncached Provider
turn은 result를 untrusted history와 분리된 `trusted_server_tool_results`로
받는다. Store claim/ACK는 opt-in authenticated scripted HTTP의 별도
trusted-result lane으로 pull할 수 있다. exact speech session·conversation
instance/generation을 재검증하고, 일반 TTS·confirmation·inference와 겹치지
않으며, barge-in·lease expiry는 cancel terminal 전까지 다음 claim을 막는다.
다만 background consume bridge와 실제 dispatcher/speaker는 없다. ACK도 audible
evidence가 아니므로 사용자에게 음성이 재생됐거나 들렸다고 보장하지 않는다.
claim token은 exact commit-response replay용 durable credential이므로 public
projection이나 식별자 chain에 노출하지 않고 운영 DB 보호·보존 정책에 포함한다.

별도 Gazebo package에는 `gazebo_monitor_room_store.py` state core가 추가됐다.
이는 operation/sample/lease/fence/cancel/unknown terminal 상태를 SQLite에
내구화하고, private millimetre 좌표는 adapter용 조회에만 남기며 public
observation에는 navigation progress와 명시적 non-claim만 노출한다. stable goal
UUID는 operation/sample에서 파생되고 fence takeover로 바뀌지 않는다. DB는 절대
경로, non-symlink directory chain, service-owned mode 0600 파일, exact schema와
row/event digest를 요구하며, 열린 store도 매 transaction 전·BEGIN 후·
COMMIT 전·COMMIT 후에 path/inode/owner/mode/link-count, SQLite main path와
안전 PRAGMA를 다시 확인한다. 불확실한 post-commit은 handle을 poison하고
성공을 반환하지 않는다. 다만 프로세스가 꺼진 동안 DB 전체 삭제·rollback을
검출할 외부 monotonic anchor는 아직 없다. 별도 pure controller는 store에서만
private 좌표와 operation/map/semantic/zone/plan binding을 읽고 exact preflight
report를 검증하며, send intent를 먼저 기록한 뒤 stable goal UUID와 현재
owner/fence/lease/deadline을 재확인하는 injected `ensure_started` 계약까지
구현한다. controller-instance reservation은 같은 인스턴스의 중복만 합친다.
실제 port는 side-effect 경계에서 현재 clock/fence를 원자 재검증하고 동일
operation/goal/cancel request를 cross-instance·cross-process에서도 멱등 처리해야
한다. 현재 구현은 Nav2 goal을 보내거나 ROS를 import하지 않고, Agent simulation
ledger 또는 semantic coverage plan의 신뢰 검증자와도 연결되지 않았다.

### 2.2 요청 하나를 끝까지 묶는 식별자

같은 이름의 방이나 같은 대화 ID만으로 요청을 식별하지 않는다. 단계가
진행될수록 다음 ID와 revision을 누적해 exact binding을 만든다.

```text
user_id
 + conversation_id / session_instance_id / generation / revision / ordinal
 + agent request_id / turn_id / decision_id
 + confirmation_request_id / proposal_fingerprint / response_id
 + device_id / map_id / map_revision / semantic_revision / room_id
 + coverage planner/profile/plan/result/receipt digest
 + simulation tool_call_id / mission_id / operation_id
 + conversation trusted_result_id / TTS event_id / claim_request_id / claim_fence
 + (향후 물리 실행) fence_epoch / adapter operation_id
 + (향후) media_session_id / viewer evidence ID / feedback ID
```

앞 단계의 ID를 뒤 단계가 새로 해석하지 않고, 서버가 저장한 immutable row와
digest로 조회해야 한다. client나 LLM이 device, map, room, 실행 ID를 다시
제출해 권한을 넓힐 수 없다.

### 2.3 상태기계별 역할

전체 시스템은 하나의 거대한 상태 문자열이 아니라 서로 다른 상태기계로
나눈다.

```text
대화: active → turn pending → turn completed → reset/closed/expired

확인: pending → resolved(approve|deny|cancel|expired)
             └→ invalidated(context changed/inactive/deleted)

순수 시뮬레이션 소비: approved → succeeded|failed|invalidated (terminal-only)

결과 TTS: pending → claimed(fence N) → acknowledged
          claimed(lease expired) → claimed(fence N+1)
          pending|claimed → cancelled(preactivation|reset|inactive|attempts exhausted)

향후 물리 임무: proposed → confirmed → preflight → navigating
              → coverage → media ready → terminal
```

`approve`는 확인 상태만 terminal로 만들며 물리 임무 상태를 `confirmed`로
바꾸지 않는다. 현재 simulation ledger는 current session, deadline,
target/effects를 한 transaction에서 다시 검증하고 순수 계산 receipt만 만든다.
결과 TTS의 `acknowledged`는 downstream adapter의 terminal ACK일 뿐 실제
재생이나 사용자 청취 증거가 아니다.
물리 임무는 별도의 outbox·lease·fence·observe/reconcile 원장이 추가되기
전에는 생성되지 않는다.

## 3. `monitor_room` 제안 계약

모델에 공개하는 인자는 하나뿐이다.

```json
{
  "location": "거실"
}
```

모델은 좌표, 경로, `/cmd_vel`, Nav2 goal, 카메라 설정 또는 KVS 세션을 만들 수
없다. `monitor_room`은 production과 simulation 모두 영구
`proposal_only`이며 adapter를 등록할 수 없다.

Safety는 다음을 모두 확인해야 제안을 사용자에게 보여준다.

- 현재 발화 전체가 하나의 좁은 모니터링 명령인가
- 방이 서버가 주입한 `monitorable_locations`에 있는가
- 금지 구역이 아닌가
- 신뢰된 RobotState인가
- emergency stop, Nav2, localization, battery가 안전한가
- camera가 사용 가능하고 privacy mode가 꺼져 있는가

`MALBUT_AGENT_MONITORABLE_ROOMS` 기본값은 빈 집합이다. non-empty allowlist는
complete Homecam binding과 `MALBUT_ROBOT_STATE_SOCKET_PATH`,
`MALBUT_ROBOT_STATE_EXPECTED_UID`, `MALBUT_ROBOT_STATE_DEVICE_ID`의 all-or-nothing
binding을 모두 요구하며 두 device ID가 exact-match해야 한다.
`MALBUT_ROBOT_STATE_TIMEOUT_SECONDS`는 1~5초다. 실제 collector가 없거나 required
field가 missing·timeout·stale이면 성공 경로가 열리지 않는다.

HTTP 요청의 `robot_state`는 Provider 전에 제거되고 모델에는 all-false 값 대신
`robot_state_untrusted={"availability":"unknown"}`이 전달된다. Provider가
`monitor_room`을 제안한 뒤에만 UDS에서 fresh evidence를 읽으며 Safety는 그
request-scoped evidence로 새로 만든 RobotState만 사용한다. evidence의
device/map/map revision과 Homecam `TargetBinding`이 다르면 durable confirmation과
TTS를 만들기 전에 차단한다.

## 4. 확인 요청과 결과

`ToolConfirmationRequest`는 다음 항목을 하나의 immutable snapshot과
`proposal_fingerprint`로 묶는다.

- 사용자, 음성 세션, 원본 utterance
- conversation ID, private session instance, generation, revision, ordinal
- Agent request, turn, decision
- Tool 이름과 canonical arguments
- 발급·만료 시각과 L3 위험 등급
- 인증된 resolver가 만든 device/map/semantic/room/geometry binding
- 이동·카메라·외부 영상·녹화·음성·talkback·최대 시간 effects

request schema v3의 확인 문장은 모델 문구가 아니라 서버가 target/effects에서
결정적으로 생성한다. public 응답에는 방 이름·category와 명시적 effects 및
opaque digest만 노출하고, device/map ID·geometry·대표점은 private durable
evidence로만 저장한다. storage schema v1의 기존 request v2 terminal row는
감사용으로 보존하고, unresolved v2 pending은 migration 시
`confirmation_binding_upgrade_required`로 영구 무효화한다.

확인 질문 TTS가 끝나거나 barge-in으로 취소돼도 pending confirmation은
유지한다. pending 상태에서는 transcript를 LLM에 다시 보내지 않고 정확한 로컬
문법으로만 처리한다.

| 입력 | 결과 | pending | 실행 |
|---|---|---:|---:|
| `네`, `응`, `승인` 등 | `confirmation_approval_recorded_no_execution` | 종료 | 없음 |
| `아니요`, `거절` 등 | `confirmation_denial_recorded` | 종료 | 없음 |
| `취소`, `그만` 등 | `confirmation_cancelled` | 종료 | 없음 |
| 모호하거나 복합인 답 | `confirmation_response_unrecognized` | 유지 | 없음 |
| 서버 시각이 deadline 이상 | `confirmation_expired` | 종료 | 없음 |

음성 응답은 trusted speaker/source와 현재 `capture_epoch`를 검사한다. 웹 UI
응답은 오디오 epoch를 받지 않는 별도 strict DTO를 사용하며, client가 보낸
`user_id`, Tool 인자, 실행 ID를 거절한다. UI identity는 향후 인증 adapter가
서버에서 만들어야 하며 현재 Python 타입 자체를 인증 증거로 취급하지 않는다.

동일 response ID와 동일 payload는 같은 결과를 반환하며, 같은 ID를 변조하면
conflict다. unresolved confirmation의 response claim은 replay cache 크기와
무관하게 보존하고 capacity 초과는 fail-closed한다. approve와 deny가 경쟁하면
SQLite `BEGIN IMMEDIATE`에서 먼저 terminal CAS한 하나만 승리한다. 요청한
disposition도 durable row에 저장하므로 같은 ID·fingerprint를 approve에서
deny로 바꾼 replay는 conflict다.

서버 만료 ID는 `confirmation-expiry-` namespace로 예약한다. voice/UI가 이
ID를 사용하면 Store가 거절하고, `server_expiry` writer는 Store가 계산한
response ID·fingerprint·provenance가 모두 정확히 일치해야 한다. pre-fix DB에
충돌이 이미 있더라도 sweep 전체를 rollback하지 않고 해당 row를 typed
invalidation으로 닫는다.

confirmation request는 assistant turn과 같은 transaction에서 저장된다.
response 처리도 다음을 한 write transaction에서 수행한다.

```text
현재 conversation session 조회
 → instance/generation/revision/status 검사
 → issued_at/deadline/fresh server clock 검사
 → response owner/replay 검사
 → terminal row 1회 갱신
 → commit
```

reset, close, session expiry, 다음 일반 turn은 기존 pending을 invalidated로
바꾸고, conversation delete는 개인정보 삭제 정책에 따라 intent row도 제거한다.
프로세스 재시작 시 Store가 due row를 bounded batch로 모두 drain한다. 다른
프로세스가 먼저 terminal을 기록하면 음성 coordinator는 그 durable winner를
다시 읽어 process-local pending mirror도 종료한다.

음성 세션은 공개 conversation ID뿐 아니라 생성 시 받은 private
`session_instance_id`에 결속된다. conversation을 삭제하고 같은 ID로 새로
만들어도 오래된 음성 세션은 SQLite `begin_turn` transaction에서 Provider 호출
전에 차단된다.

## 5. 현재 포함하지 않는 것

다음 기능은 아직 구현 완료가 아니다.

- wake/continuous listening/VAD/AEC, production microphone config install,
  actual hardware smoke와 Agent-bound STT sink
- 실제 speaker TTS
- 실제 인증 adapter/verifier가 보증하는 UI·speaker identity
- 실제 요청자의 인증 principal을 Homecam owner/device에 결속하는 production adapter
- authoritative battery/e-stop/zone source를 collector에 공급하는 adapter
- Homecam camera/privacy evidence의 실제 Aurora·GStreamer frame 환경 검증과
  SROS2 또는 동등한 ROS publisher 신뢰 경계
- 실제 인증 principal을 발급하는 production verifier
- 실제 AWS CDK diff/deploy, 401·400·403·200 smoke와 secret rotation
- 음성 confirmation에서 simulation consume으로 가는 자동 bridge/API
- 물리 실행용 1회 capability와 proposal/execution/outbox ledger
- 실제 ROS/Nav2 port와 coverage/camera/Agent wiring
- Homecam/KVS mission adapter
- `session_ready`, `producer_live`, `viewer_live` 증거
- 물리 mission feedback evidence와 conversation feedback bridge
- 구현된 simulation TTS outbox를 실제 speaker에 연결하는 dispatcher,
  downstream dedupe/observe, durable playback arbitration,
  barge-in/reset/close cancellation reconciliation과 audible evidence

과거 실험의 테스트 수와 성공 결과를 현재 구현 증거로 재사용하지 않는다.

## 6. 영상 상태 계약

향후 Homecam 연동에서는 다음 상태를 합치지 않는다.

| 상태 | 의미 |
|---|---|
| `session_ready` | 브라우저가 미디어 세션 접속을 시도할 수 있음 |
| `producer_live` | 로봇 producer가 실제 frame을 전송 중임 |
| `viewer_live` | 특정 브라우저가 실제 frame을 디코딩함 |

P2P에서는 viewer 접속이 producer 연결을 유발할 수 있으므로
`producer_live`를 viewer 접속 전에 강제하지 않는다. 현재 브라우저의 decoded
frame 상태는 서버 ACK로 올라오지 않으므로 Agent는 `viewer_live=true`를 말할
수 없다.

### 6.1 이미 있는 Homecam·Nav2 자산과 연결되지 않은 부분

- `homecam_media_agent`: ROS Image/CameraInfo/Odometry, H.264/Opus, KVS
  P2P/Storage producer, session lease와 heartbeat를 담당한다.
- `homecam_detector`: image/odom/monitoring state를 받아 사람·개·고양이·motion
  event와 detector health를 만든다.
- `homecam_web`: device session API, 단기 KVS credential, 사용자 권한,
  viewer join과 PWA 재생 UI가 있다.
- `cloud_robot_sync`: map/TF/Nav2 상태와 인증된 web command queue를 운반한다.
- `robot_web_server`: 좌표 목표의 path preview, map/zone/costmap 재검증,
  Nav2 start/cancel/feedback가 있다.

strict 자연어 방 resolver와 고정 server-owned user/device용 signed Homecam
semantic fetch MVP, request-scoped RobotState UDS client, ROS 독립 collector core,
read-only Nav2/localization observer, applied Homecam camera/privacy evidence와
server-owned room allowlist wiring은 구현되어 있다. media evidence는 exact device
heartbeat, 적용 generation, strict image shape, GStreamer frame 수락과 software
privacy gate를 짧은 CLOCK_BOOTTIME TTL에 묶고, observer가 camera/privacy를 같은
receipt로 원자 commit한다. 동일 sequence 재발행은 TTL을 연장하지 않고 source
restart·sequence/generation rollback·A→B→A replay를 차단한다. Homecam은 설정된 Cognito
subject/email의 활성 Web session, owner membership, finalized map을 하나의 DB
snapshot에서 검사한다. Agent는 Safety 뒤 Homecam target의 device/map/revision을
같은 RobotState evidence와 exact-match한다. 그러나 실제 요청 principal을 묶는
production authority, battery/e-stop/zone safety source adapter, `monitor_room`
mission contract, coverage planner와 이 서비스들의 결과를 하나의 trusted mission
evidence로 묶는 adapter는 없다. 특히
`robot_web_server`의 단일 좌표 이동은 방 전체 coverage가 아니다.

이 media evidence는 카메라가 현재 generation에서 frame을 생산할 수 있는지와
software gate가 적용됐는지만 말한다. 물리 셔터, 방 전체 촬영, KVS producer 또는
특정 브라우저의 decoded frame 증거가 아니며, 현재 빌드 환경에서는 GStreamer
개발 패키지와 실제 Aurora/backend가 없어 physical frame branch의 장치 검증도
남아 있다.

영상 연결 순서도 transport 특성에 따라 다르다.

```text
P2P: session_ready → viewer join 시도 → producer 연결 → decoded frame
Storage: session_ready → producer/storage 연결 → viewer playback
```

`mediaHealthy=true`는 storage 또는 producer 연결의 근사치일 수 있지만 특정
가족 브라우저가 영상을 봤다는 증거는 아니다. 브라우저가 video track live,
unmuted, `HAVE_CURRENT_DATA`를 확인한 뒤 서버에 signed ACK를 보내는 계약이
추가돼야 mission이 `viewer_live`를 주장할 수 있다.

## 7. 검증 결과

2026-08-16 현재 다음을 실행했다.

```bash
cd /home/shin/ros2_ws/src/malbut/malbut_agent_server
python3 -m pytest -q
python3 -m flake8 malbut_agent_server test
python3 -m pydocstyle malbut_agent_server test

cd /home/shin/ros2_ws/src/malbut/homecam_web
npm run test:unit
npm run lint
npx tsc --noEmit
npm run build

cd infra/cdk
npm test
git diff --check
```

결과:

```text
Agent pytest: 1073 passed
malbut_voice M0 package: 85 passed, 3 skipped
Gazebo monitor-room durable store + injected Nav2 adapter focused: 86 passed
Gazebo trusted RobotState observer focused: 49 passed
Homecam media agent C++: 55 passed
Interfaces + media agent + Agent + observer isolated colcon build: pass
malbut_voice isolated colcon install / ros2 run --help: pass
malbut_gazebo packages-up-to isolated colcon build / installed store+adapter import: pass
Installed Homecam message / observer import: pass
Health-only `/homecam/media_evidence` ROS smoke: pass
Observer isolated colcon install / ros2 run / SIGINT cleanup: pass
flake8: pass
pydocstyle: pass
Homecam Web unit: 78 passed
Homecam Web ESLint / TypeScript / production build: pass
Homecam CDK: 12 passed
CDK prepare / dual / cutover / cleanup synth: pass
CDK npm audit: 0 vulnerabilities
git diff --check: pass

30/30 concurrent legacy-open stress iterations: pass
```

observer 자체는 격리 install에서 generated `HomecamMediaEvidence` import, entry
point 발견, 실제 UDS 생성, SIGINT exit 0과 own socket cleanup을 확인했다. 전체
workspace의 Gazebo test는 설치되지 않은 `malbut_description`·perception·roaming·
tracking 의존성 때문에 격리 `colcon test`의 대상이 아니며, 이 단면은 focused
source test와 4-package build로 검증했다. Homecam은 이 머신에 GStreamer 개발
패키지가 없어 health-only로 빌드됐고, GStreamer 활성 branch와 실카메라는 별도
장치 검증 게이트로 남는다.

검증에는 strict semantic room parser와 exact resolver, target/effects digest,
private persistence round-trip과 tamper matrix, server-rendered informed-consent
prompt, resolver 누락·오류 fail-closed, blocking resolver 중 barge-in epoch fence,
storage v1→v2 migration과 v2 pending 영구 tombstone, prompt TTS terminal,
barge-in, exact replay, 변조 replay, 잘못된
binding, SQLite 재시작 후 pending/terminal replay, current context와 deadline의
원자 검사, server-expiry namespace 선점 방지, 시계 역행, schema CHECK/index/
metadata/trigger 변조 차단, legacy DB 동시 migration, 외부 writer 결과와 메모리
상태 수렴, delete/recreate 뒤 stale speech session 차단, approve/deny 동시 경쟁과
Provider 추가 호출 0회 검사가 포함된다. 또한 simulation ledger의 activation
marker, pre-activation approval tombstone, HMAC actor·target evidence,
first/fresh 시각의 session·target 만료, exact restart replay, exact deadline,
target/effects A→B→A tombstone, reset/close/delete fence, 16개 별도 SQLite
connection 동시 소비, terminal row `INSERT OR REPLACE`·UPDATE·DELETE 및 물리
flag 변조 차단을 검증한다. v4에서는 Polygon/hole/MultiPolygon strict-interior
500 mm lattice, 모든 component 대표, subprocess 결정성, pre-loop CPU/size budget,
순수 무부작용을 검사한다. v3 terminal의 `legacy_unplanned` audit-only migration,
중간 DDL fault rollback, planner failure, exact no-replan replay와 전체 terminal
`receipt_digest`도 검증한다. count·target/effects·arguments·owner·timestamp·ID·
result/receipt digest를 바꾼 뒤 exact trigger를 복구해도 reopen이 fail-closed한다.
conversation trusted result와 fixed-template TTS event의 same-transaction
append/rollback, activation 이전 no-backfill, stable event ID, claim/ACK exact
replay, lease takeover·fence·최대 5회, 동시 claim 단일 winner,
reset/close/expiry/delete lifecycle, append-only claim·ACK와 schema/row/trigger/
activation-anchor drift도 함께 검증한다. 이 outbox는 원문 TTS message를
저장하지 않지만 source binding, delivery state와 lease credential은 저장한다.
scripted speech bridge는 normal/notification slot 상호 배제, generic terminal의
durable ACK 차단, restart live-claim 복구·ACK 후 no-redelivery, store I/O 동안
speech lock 미보유, claim commit 직후 reset/delete, terminal ACK↔lifecycle race,
lease 경계와 `lease_expired` cancel→local cancel-terminal→fence takeover 순서를
검증한다. HTTP body의 user/conversation/text/result/claim token 주입도
거절하고 모든 결과의 `physical_audio_verified=false`를 고정한다.

추가로 request-scoped RobotState의 UDS peer UID·nonce·device/boot/instance/sequence·
field freshness·TTL, incomplete/stale/timeout/clock failure, cached evidence exact
revalidation과 uint64 sequence 경계를 검증한다. client RobotState는 Provider에서
unknown marker로 바뀌며 Safety가 post-inference evidence만 사용하는 HTTP 주입
차단도 포함한다. signed Homecam HTTPS transport의 status/MIME/encoding/크기/TLS/HMAC/
TTL/content-digest 오류, semantic JSON 깊이·노드·크기 제한, active Cognito
subject/email과 owner membership 결속, 동일 finalized PUT idempotency,
A→B→A generation 증가, 2^53 초과 BIGINT 정밀도를 검증한다. opt-in scripted
HTTP는 client user/RobotState/Tool 주입 차단, 독립 인증 실패 제한, 정상 bearer
lockout 방지, 진행 요청 종료 후 store close를 실제 socket 경로로 검증한다.

## 8. 다음 구현 게이트

다음 단계는 durable approval 기록을 바로 실행으로 바꾸는 것이 아니다. 아래
순서로 새로운 권한 경계를 추가한다.

1. 실제 authenticated principal과 현재 robot/device를 서버에서 선택한다.
2. signed Homecam semantic MVP의 고정 user/device 결속을 실제 principal·owner
   membership 및 non-reusable revision 계약으로 교체한다.
3. 구현된 RobotState UDS client·collector core·Nav2/localization·Homecam
   camera/privacy evidence에 authoritative battery/e-stop/zone source를 연결하고,
   GStreamer 활성 실장치와 SROS2 배치 경계에서 운영 freshness를 검증한다.
4. 현재 구현된 semantic-plan terminal-only simulation ledger를 production verifier와
   연결한다. 음성·HTTP client가 `VerifiedSimulationApproval`을 직접 만들 수
   없게 한다.
5. 구현된 순수 Python 결과(`semantic_sample_plan_created`,
   `physical_effects=false`, `viewer_live=false`, `coverage_achieved=false`)와
   conversation trusted result·fixed-template TTS outbox의 same-transaction
   연결을 유지한다. 둘 다 비실행이며 outbox ACK도 audible proof가 아니다.
6. map/source/device의 인증된 monotonic revision을 도입한다. 변경을 한 번
   관측하면 현재 ledger의 durable tombstone으로 예전 상태가 복원돼도
   되살아나지 않게 한다.
7. 물리 execution용 outbox·lease·fence·observe/reconcile 원장을 별도로 만든다.
8. 구현된 fixed-template TTS outbox의 scripted claim/cancel/terminal bridge를
   실제 dispatcher에 연결하기 전에 stable event ID 기반 downstream
   idempotency/observe, playback arbitration, barge-in/reset/close cancellation
   reconciliation과 audible evidence를 추가한다. fake user/assistant turn으로
   대체하지 않는다.

시뮬레이션 결과도 반드시 다음처럼 표현한다.

```text
simulation=true
physical_effects=false
viewer_live=false
nav2_validated=false
camera_coverage_validated=false
coverage_achieved=false
```

실제 Nav2와 Homecam은 위의 durable gate와 독립 감사를 통과한 뒤 연결한다.

현재 confirmation lifecycle은 target/effects까지 SQLite에서 conversation
lifecycle과 직렬화한다. simulation ledger도 같은 connection과
`BEGIN IMMEDIATE`에서 current session·deadline·target/effects를 재검증하고
terminal row 하나를 만든다. 다만 production verifier와 자동 bridge가 없고,
receipt는 `simulation_only`이므로 confirmation 결과는 계속
`execution_authorized=false`다. 미래 물리 실행기는 이 receipt를 ROS 권한으로
승격하지 않고 별도 실행 원장을 통해 새 계약을 만족해야 한다.

순수 planner는 DB transaction 안에서 짧고 bounded한 계산만 수행한다. 계산 뒤 commit
전에 process가 죽으면 함수 계산 자체는 재호출될 수 있지만 외부 부작용이 없고,
durable terminal row와 ID는 하나만 남는다. 이를 물리 adapter의 crash-safe
exactly-once로 확대 해석하면 안 된다.

TTS claim/ACK의 exact replay도 SQLite request/state 멱등성일 뿐 audio
exactly-once가 아니다. 재생 뒤 ACK 전 crash 구간은 stable event ID를 사용하는
downstream dedupe/observe 없이는 같은 알림을 다시 전달할 수 있다.

v4 receipt에는 geometry나 sample 좌표를 저장하지 않고 planner/profile/plan/result
digest, sample/component 수와 terminal row 전체의 content-free `receipt_digest`만
저장한다. `receipt_digest`는 coherent-row 검증용 SHA-256이며 keyed MAC이나 외부
append-only 감사 서명이 아니다. `completed_at`은 planner의 실제 wall-clock 종료
시각이 아니라 같은 transaction에서 planner 직전에 고정한 terminal-decision 시각이다.
storage v3 terminal은 coverage 증거를 만들어 붙이지 않고 `legacy_unplanned`로
보존하며, API는 typed contract-upgrade-required로 닫는다.

`consume_request_id`는 사용자 전체 범위가 아니라 하나의 confirmation/approval
범위에서만 멱등성 키다. 서로 다른 confirmation은 같은 문자열을 사용할 수 있지만,
같은 confirmation의 payload를 바꾸면 conflict로 닫힌다. terminal receipt가 이미
있으면 동일한 HMAC-bound approval과 request로 재조회할 수 있으며, 이때 만료된
target freshness 때문에 simulator를 다시 실행하지 않는다. 반대로 terminal row가
없는 신규 소비는 first/fresh 시각 모두에서 current target evidence를 요구한다.

activation 이전 proposal은 raw SQLite rowid나 시각으로 구분하지 않는다. activation
transaction이 기존 proposal의 SHA-256 fingerprint를 immutable denylist에 snapshot하고,
그 fingerprint는 이후 승격할 수 없다. 이 방식은 confirmation 삭제 뒤 SQLite rowid가
재사용되거나 wall clock이 역행해도 새 proposal을 잘못 막지 않는다.

conversation delete는 원문 confirmation을 제거하지만, 이미 생성된 terminal simulation
receipt와 pre-activation fingerprint denylist는 재실행·승격 방지용 content-minimized
감사 기록으로 남는다. 여기에는 opaque ID, digest와 timestamp가 포함되므로 완전한
익명화는 아니다. production 전에는 보존 기간, 사용자 삭제 요청, 가명화·키 폐기
(crypto-erasure), 관리자 감사 접근 범위를 별도 정책으로 확정해야 한다.

### 8.1 그 다음 실제 연결 순서

```text
semantic room resolver
 → Nav2 preview/path/zone/costmap 재검증
 → one-time start + cancel/reconcile
 → coverage waypoint + camera health
 → Homecam session create
 → browser join 시도
 → producer evidence / viewer evidence 별도 수집
 → mission terminal + feedback outbox
 → conversation trusted result + durable TTS outbox event
 → idempotent speaker dispatcher terminal ACK
 → (별도 evidence가 있을 때만) playback/audible status
 → 다음 wake
```

물리 adapter는 DB transaction과 원자화할 수 없으므로 stable operation ID,
fence epoch, idempotent start/cancel, observe/reconcile가 있어야 한다. 이 계약이
없으면 SQLite에 “시작됨”을 쓴 뒤 Nav2 호출 직전에 crash하는 구간을 안전하게
복구할 수 없다.

### 8.2 trusted RobotState 선행 조건

과거 process-global `trusted_robot_state` production 경로는 제거했다. 현재
factory는 다음 server-owned 설정이 모두 있을 때만 request-scoped UDS client를
만든다.

```text
MALBUT_ROBOT_STATE_SOCKET_PATH       absolute fixed UDS path
MALBUT_ROBOT_STATE_EXPECTED_UID      fixed Linux peer UID
MALBUT_ROBOT_STATE_DEVICE_ID         fixed robot/device identity
MALBUT_ROBOT_STATE_TIMEOUT_SECONDS   1~5초, 기본 2초
MALBUT_AGENT_MONITORABLE_ROOMS       exact comma-separated allowlist
```

path·UID·device는 all-or-nothing이고 room allowlist의 기본값은 비어 있다. 방을
하나라도 열려면 complete signed Homecam binding도 필요하며 Homecam과 RobotState의
device ID가 같아야 한다. 이 저장소에는 UDS protocol/parser/client, factory·
orchestrator wiring, ROS 독립 collector store/server core와 별도 read-only
`trusted_robot_state_observer`가 있다. core는 nullable 8개 field, per-field receipt,
immutable sequence body, atomic batch, map-generation token, uint64/TTL/크기 상한,
peer UID와 exact-own-inode cleanup을 강제한다. observer는 lifecycle·endpoint
readiness와 fresh TF로 `navigation_available`·`localization_ok`를 원자 갱신하고,
fixed `/homecam/media_evidence`에서 검증한 camera/privacy를 별도의 정확한 field
TTL로 함께 반영한다. goal/cancel/costmap command/velocity/zone API는 호출하지
않으며 battery/e-stop/docked/forbidden-zones는 `null`이다. 따라서 production
기본값은 계속 fail-closed다. crash 뒤 남은 socket path는 core가 추측해 삭제하지
않고 supervisor가 owner/type을 확인해 정리해야 한다.

현재 재사용 가능한 신호와 차단 이유는 다음과 같다.

| 신호 | 현재 자산 | 아직 신뢰할 수 없는 이유 |
|---|---|---|
| Nav2·localization·map | 별도 observer의 lifecycle·readiness·TF와 collector binding | read-only 관측 단면 구현; 다른 required field가 없어 단독으로는 Safety 불완전 |
| camera·privacy | exact device heartbeat + applied generation + strict frame/GStreamer 수락 + software gate 증거 | 코드·health-only smoke는 구현; 실제 Aurora/GStreamer와 trusted ROS 배치 검증이 남고 물리 셔터를 증명하지 않음 |
| battery | 없음 | authoritative `BatteryState` publisher 필요 |
| emergency stop | 없음 | e-stop/safety-controller heartbeat 필요 |
| forbidden zones | geometric zone/map 자산 | room-name tuple로 안전하게 변환하는 authority 없음 |

구현된 Agent client는 fixed socket path·peer UID·nonce·크기·content digest,
`device_id`, `map_id/revision`, host boot ID, collector instance ID, uint64 sequence,
per-field monotonic receipt time과 `valid_until`을 검증한다. Provider inference 뒤
`monitor_room`에 대해서만 fresh snapshot을 읽고, complete한 요청별 evidence에서
Safety용 RobotState를 만든다. semantic resolution 뒤에는 evidence의
device/map/revision을 Homecam `TargetBinding`과 exact-match한다. cached proposal도
같은 evidence binding을 다시 읽어야 하며 만료되거나 달라지면 새 요청을 요구한다.
HTTP body는 이 값을 선택하거나 덮어쓸 수 없다.

Provider에 client RobotState를 all-false 값으로 전달하지도 않는다. 모델 입력에는
`robot_state_untrusted={"availability":"unknown"}`을 넣어 unknown을 known-safe로
해석하지 않게 하고, Safety만 post-inference UDS evidence를 사용한다. public 결과는
scope와 opaque digest/current만 노출하며 device/map/boot/sequence 전체 binding은
private persistence에 둔다.

남은 blocker는 authoritative battery/e-stop/zone source와 실제 media 장치·ROS
신뢰 배치 검증이다. 현재 observer는 지원하지 않는 field를 `null`로 유지하고
`monitor_room`을 계속 차단한다. 이는 unknown을 safe로 바꾸지 않는 의도적인
안전 경계다. observer의
`physical_authority` 기본값은 false이고, true로 시작한 physical mode에서는
runtime을 포함해 `use_sim_time=true` 변경을 거절한다.

## 9. 실패·취소·재시작 시 의미

| 지점 | 처리 원칙 | 사용자에게 말할 수 있는 것 |
|---|---|---|
| STT confidence/provenance 부족 | Tool 제안 금지, 재질문 | “다시 말해 주세요” |
| Safety·RobotState 실패 | proposal 차단 | 구체적 안전 거절 이유 |
| 확인 답변이 모호함 | pending 유지, LLM 재호출 금지 | “네/아니요로 답해 주세요” |
| 확인 deadline 경과 | durable expired terminal | “요청이 만료됐어요” |
| session reset/close/next turn | pending invalidated | 이전 승인을 새 문맥에 사용하지 않음 |
| server crash | SQLite pending/terminal 복구, due sweep | 재실행 권한은 자동 발급하지 않음 |
| map/device/source 변경을 consume에서 관측 | durable simulation tombstone | 예전 상태 복원 뒤에도 실행하지 않음 |
| 변경이 A→B→A로 끝나 B를 한 번도 관측하지 못함 | 현재 revision만으로 검출 불가 | 인증된 비재사용 epoch 전까지 물리 실행 금지 |
| TTS claim lease 만료 | 같은 event ID, 새 fence/token으로 reclaim | 새 음성 사실을 만들지 않음 |
| reset/close/expiry 중 TTS 미전달 | pending/claimed event를 durable cancel | 이전 lifecycle의 알림을 재생하지 않음 |
| TTS terminal ACK | downstream request 완료만 기록 | 재생·청취 완료로 표현하지 않음 |
| Nav2 start 결과 불명 | 현재 pure controller는 `delivery_unknown`으로 차단; 향후 실제 port/supervisor가 같은 operation ID로 observe/reconcile | 성공이나 재시작을 추측하지 않음 |
| camera producer만 준비 | `producer_live`까지만 기록 | “브라우저에서 보인다”는 말 금지 |
| viewer ACK 확인 | 특정 session의 `viewer_live` 기록 | 그때만 실제 시청 가능 안내 |
| 사용자 취소 | fence 후 adapter cancel/reconcile | late success가 취소를 덮지 못함 |

재시도는 새 작업을 만드는 것이 아니라 동일 request/response/operation ID의
exact replay여야 한다. payload가 달라지면 conflict로 닫고, 외부 side effect의
결과가 불명확하면 자동으로 한 번 더 호출하지 않는다.

## 10. 현재 코드를 공부할 순서

1. `tools.py`, `safety.py`, `gateway.py`: 모델 제안, 정책, 실행 금지 경계를
   먼저 본다.
2. `orchestrator.py`: bounded conversation context가 Provider로 가고 safe public
   result가 저장되는 흐름을 본다.
3. `confirmation.py`: immutable request와 음성/UI response DTO, fingerprint를
   본다.
4. `speech.py`: scripted final transcript, TTS/barge-in, local confirmation,
   process-local UX mirror를 본다.
5. `monitor_room_target.py`: semantic snapshot, room, geometry, effects의
   immutable binding과 public/private DTO 경계를 본다.
6. `conversation.py`: turn+intent atomic commit, terminal CAS, lifecycle
   invalidation, restart sweep, schema fail-closed를 본다.
7. `trusted_results.py`, `trusted_result_tts.py`: terminal simulation 사실을
   다음 Provider context와 non-authorizing fixed-template delivery event로
   분리하고, claim·lease·fence·terminal ACK를 내구화한 경계를 본다.
8. `monitor_room_coverage.py`: geometry를 bounded deterministic semantic sample
   plan으로 바꾸되 Nav2·camera·coverage 성공을 주장하지 않는 경계를 본다.
9. `execution_ledger.py`: 승인과 현재 binding을 재검증해 좌표 없는 plan/result
   digest와 물리 효과 없는 v4 terminal receipt 하나로 소비하는 경계를 본다.
10. `homecam_agent/README.md`와 `homecam_web`: media producer/session/viewer의
   실제 경계를 본다.
11. `malbut_gazebo/malbut_gazebo/robot_web_server.py`,
   `cloud_robot_sync.py`, `homecam_web/db/robot-map.ts`: Nav2 command와 semantic
   room 데이터가 현재 어디까지 분리돼 있는지 본다.

IDE에 과거 `local_stt.py` 탭이 남아 있어도 현재 파일은 롤백되어 존재하지
않는다. 실제 오디오부터 다시 만들 때는 이 문서의 1→2 계약에 맞는 작은
adapter로 새로 추가하고, 일반 WAV를 microphone provenance로 승격하지 않는다.
