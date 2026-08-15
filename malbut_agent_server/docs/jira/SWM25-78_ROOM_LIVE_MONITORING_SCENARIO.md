# SWM25-78 거실 이동·라이브 모니터링 시나리오

## 문서 정보

| 항목 | 값 |
| --- | --- |
| Jira 스토리 | SWM25-78 거실 이동·라이브 모니터링 시나리오 |
| 작성 기준일 | 2026-08-15 |
| 대상 저장소 | `malbut`의 Agent, Nav2, Homecam 경계 |
| 목표 | 음성 명령으로 검증된 거실 coverage mission을 시작하고 Malbut 웹에서 live-only 영상을 볼 수 있게 한 뒤, 실행 결과를 같은 대화로 돌려준다. |
| 1단계 미디어 정책 | **P2P live-only, recording off** |
| 현재 판정 | Tool/Safety·연속 음성 offline core·semantic Room resolver·신뢰 확인을 소비하는 process-local simulation controller와 전체 scripted trace 구현. 실제 음성 adapter, durable execution, 전체 physical preflight, ROS/Nav2·Homecam/KVS·브라우저·대화 feedback은 미구현 또는 미검증 |

이 문서는 다음 사용자 시나리오를 구현 가능한 계약으로 구체화한다.

> 사용자가 Malbut에게 거실로 가서 거실을 잘 볼 수 있게 움직이라고 말한다.
> Malbut은 안전한 고수준 mission인지 판단하고 명시적 확인을 받은 뒤,
> 검증된 경로만 Nav2로 실행한다. 카메라 영상은 녹화하지 않고 Malbut 웹에
> live-only로 준비한다. mission이 끝나거나 실패하면 결과를 같은 대화로
> 설명하고 다음 wake word를 기다린다.

이 목표는 단순한 `navigate(location="거실")` 한 번이 아니다. 음성 수명주기,
사용자 확인, 영속적인 1회 실행, Room 해석, Nav2 coverage, Homecam 상태,
브라우저 권한, Tool 결과의 대화 복귀를 하나의 추적 가능한 mission으로 묶어야
한다.

## 1. 1단계에서 고정하는 제품 결정

### 1.1 고정 사항

- 모델에는 저수준 좌표나 `/cmd_vel`을 만들게 하지 않는다.
- 모델은 `거실` 같은 사용자 용어와 고수준 mission만 제안한다.
- Room resolver가 현재 map과 정확히 일치하는, 사전에 검증된 coverage plan을
  선택한다. LLM이 좌표·yaw·waypoint를 생성하거나 수정하지 않는다.
- 이동을 포함하므로 mission 위험 등급은 L3다. 실행 전 명시적 확인과 최신
  trusted ROS state 재검사가 반드시 필요하다.
- 카메라와 live 상태도 프라이버시 경계다. `cameraEnabled=true`,
  `monitoringEnabled=false`가 owner가 이미 설정한 desired state에서 확인될 때만
  1단계 mission을 시작한다.
- 1단계 Agent는 Homecam 설정을 바꾸지 않는다. 카메라가 꺼져 있거나 녹화
  모드이면 실행하지 않고 owner가 Malbut 웹에서 설정을 확인하도록 안내한다.
- 1단계 성공은 `P2P`, `mediaHealthy=true`, fresh heartbeat와 검증된 active
  session을 요구한다. `storage` mode를 성공으로 간주하지 않는다.
- privacy, e-stop, localization, map revision 또는 media mode가 실행 중 바뀌면
  mission을 중단하고 성공으로 기록하지 않는다.
- 실패 시 초기 위치로 자동 복귀하지 않는다. 자동 복귀도 별도의 물리 이동이기
  때문에 새 안전 판단과 확인 없이 rollback으로 실행할 수 없다.
- `stop`과 privacy 차단처럼 위험을 줄이는 취소는 추가 확인 없이 즉시 처리한다.

### 1.2 명시적으로 미결정인 사항

다음 항목은 구현 중 임의로 정하지 않는다.

| 미결정 | 선택지 | 1단계 처리 |
| --- | --- | --- |
| 장기 미디어 모드 | P2P live-only / KVS Storage+7일 녹화 | P2P만 허용. Storage 요청은 미지원으로 종료 |
| “거실 전체”의 의미 | 한 최적 관측 지점 / 검증된 다중 waypoint / 시간 기반 roaming | runtime 생성은 금지. 승인된 coverage plan이 있는 경우만 실행 |
| coverage 종료 조건 | 모든 waypoint 완료 / 가시 영역 비율 / 사용자 중지 | plan에 고정된 완료 조건만 사용. 임의 추론 금지 |
| live 유지 시간 | mission 종료와 함께 종료 / owner 설정에 따라 유지 | Homecam owner desired state를 변경하지 않으며 mission은 live readiness만 보고 |
| 브라우저 자동 열기 | 사용자가 직접 열기 / push·deep link / kiosk 자동 전환 | 1단계는 사용자가 인증된 웹에서 직접 연다. 자동 열기 없음 |
| 음성 확인 identity | 단일 owner 음성 / 앱 확인 / 물리 버튼 | 신뢰 identity가 정해지기 전 physical 실행 금지 |

Storage mode는 단순한 성능 옵션이 아니다. 녹화, 7일 보존, 개인정보 처리와
삭제 정책이 추가되는 별도 제품 동의다. 따라서 `monitoringEnabled=true`를
“live가 잘 될 것 같아서” Agent가 자동 선택해서는 안 된다.

## 2. 목표 사용자 흐름 (TARGET · production)

```text
awaiting_wake
  -> wake detected
  -> trusted microphone capture
  -> final STT transcript
  -> Agent decision + local Safety
  -> monitor_room proposal
  -> explicit confirmation
  -> durable consume-once authorization
  -> Room resolver
  -> trusted ROS preflight
  -> Nav2 verified coverage plan
  -> Homecam mediaHealthy + active P2P session
  -> live_ready
  -> trusted Tool execution result
  -> same-conversation feedback
  -> TTS terminal
  -> awaiting_wake
```

실패·취소·timeout도 반드시 `trusted Tool execution result`로 수렴한다. 실패한
Nav2 goal, stale heartbeat 또는 실제 영상이 없는 transport를 `live_ready`나
`succeeded`로 바꾸어 기록하지 않는다.

### 2.1 권장 고수준 제안

모델이 볼 수 있는 입력은 최소화한다.

```json
{
  "tool_name": "monitor_room",
  "arguments": {
    "location": "거실"
  }
}
```

다음 값은 모델 인자가 아니라 trusted resolver와 서버 설정이 소유한다.

- `device_id`
- canonical `room_id`
- `map_id`와 map content digest
- `coverage_plan_id`와 plan digest
- map-frame poses와 yaw
- Nav2 Action 이름과 timeout
- Homecam backend 주소와 인증 정보
- 사용자·장치 membership

현재 prompt는 여러 행동을 한 턴에 요청하면 하나를 임의로 선택하지 않고
clarification하도록 한다. 따라서 기존 `navigate`와 별도 camera Tool을 모델이
순서대로 호출하게 만드는 대신, 제품에서 승인한 하나의 고수준 mission으로
등록해야 한다. 고수준 mission의 내부 단계는 모델 호출 순서가 아니라 deterministic
mission runner가 소유한다.

## 3. 상태 기계 (TARGET · production)

### 3.1 Speech·dialog 상태

| 상태 | 진입 조건 | 허용 이벤트 | 종료 조건 |
| --- | --- | --- | --- |
| `awaiting_wake` | 세션 시작 또는 이전 TTS terminal | 신뢰된 wake event | `capturing` |
| `capturing` | wake 승인 | VAD, audio frames, cancel | final audio -> `transcribing` |
| `transcribing` | capture terminal | final STT / no-speech / low-confidence | 성공 -> `deciding`, 실패 -> `awaiting_wake` |
| `deciding` | trusted final transcript | Agent result | message/refusal/clarification 또는 `awaiting_confirmation` |
| `awaiting_confirmation` | L3 mission proposal | 같은 identity의 confirm/cancel/expiry | confirm -> `authorized`, cancel/expiry -> feedback |
| `mission_wait` | durable authorization 완료 | progress, cancel, terminal result | terminal -> `feedback` |
| `feedback` | safe terminal result | TTS request/terminal | `awaiting_wake` |

부분 transcript, self-echo와 active TTS 중 transcript는 provider 또는 mission
runner로 전달하지 않는다. confirmation 발화도 pending proposal이 정확히 하나이고
사용자·conversation·전체 인자·만료가 모두 일치할 때만 소비한다.

### 3.2 Mission 상태

```text
proposed
  -> awaiting_confirmation
  -> authorized
  -> resolving_room
  -> preflight
  -> navigating
  -> covering
  -> waiting_media_ready
  -> live_ready
  -> succeeded

모든 실행 상태
  -> cancelling -> cancelled
  -> failed
  -> timed_out
```

| 상태 | 필수 불변식 | 대표 실패 |
| --- | --- | --- |
| `proposed` | immutable decision digest, short TTL | `proposal_expired` |
| `awaiting_confirmation` | user/session/turn/tool/arguments가 결속됨 | `confirmation_mismatch` |
| `authorized` | confirmation을 DB에서 원자적으로 1회 소비하고 `tool_call_id` 발급 | `already_consumed` |
| `resolving_room` | 이름이 현재 map의 canonical Room 하나에만 대응 | `room_ambiguous`, `room_not_found` |
| `preflight` | e-stop off, localization fresh, battery 충분, plan/map digest 일치, camera on, privacy 허용, P2P desired | `stale_state`, `privacy_mode`, `recording_mode_mismatch` |
| `navigating` | Nav2 Action goal은 verified plan의 pose만 사용 | `goal_rejected`, `navigation_failed` |
| `covering` | plan 순서·yaw·dwell과 현재 privacy를 계속 검증 | `coverage_cancelled`, `plan_revision_changed` |
| `waiting_media_ready` | fresh heartbeat, camera health, active P2P session 대기 | `media_timeout`, `media_unhealthy` |
| `live_ready` | `streamMode=p2p`, `mediaHealthy=true`, session/device 일치 | `session_mismatch` |
| terminal | terminal 상태는 한 번만 기록되고 뒤늦은 성공이 덮어쓰지 못함 | `late_result_discarded` 감사 이벤트 |

`live_ready`는 장치 영상이 KVS에 송출 가능한 상태라는 뜻이다. 인증된 브라우저가
실제로 frame을 표시했다는 `viewer_live`와 구분한다. 현재 웹은 브라우저 내부에서
video `loadeddata`와 live track을 확인하지만 그 증거를 mission runner로 다시
보내는 trusted feedback API는 없다.

### 3.3 terminal 결과와 대화 복귀

다음 JSON은 실제 adapter와 durable ledger가 갖춰진 뒤의 **TARGET** 계약이다.
현재 simulation controller의 public 결과와 혼동하면 안 된다.

```json
{
  "tool_call_id": "server-issued-uuid",
  "decision_id": "proposal-uuid",
  "conversation_id": "bound-conversation",
  "tool_name": "monitor_room",
  "status": "succeeded",
  "started_at": "2026-08-15T00:00:00Z",
  "completed_at": "2026-08-15T00:00:20Z",
  "result": {
    "room_id": "living-room",
    "coverage_plan_id": "living-room-plan-v1",
    "coverage_completed": true,
    "media_mode": "p2p",
    "live_ready": true,
    "viewer_live": false
  },
  "error": null
}
```

`viewer_live=false`는 실패를 숨기기 위한 값이 아니다. 브라우저가 아직 열리지
않았거나 현재 계약으로 검증할 수 없음을 뜻한다. 제품 문구도 “웹에서 볼 준비가
됐어”와 “웹 화면에 영상이 나오고 있어”를 구분해야 한다.

terminal result는 일반 사용자 발화나 untrusted conversation history로 위장해
주입하지 않는다. trusted executor 전용 입력 경계가 결과를 같은
`conversation_id`에 결속하고, 결과 전용 turn을 durable commit한 뒤 최종 사용자
메시지를 만든다. 같은 terminal event의 재전송은 provider와 TTS를 두 번
호출하지 않아야 한다.

현재 offline controller는 room, pose와 plan을 노출하지 않는 다음과 같은
content-free 결과만 반환한다.

```json
{
  "status": "succeeded",
  "phase": "terminal",
  "code": "simulation_succeeded",
  "runtime_mode": "simulation",
  "simulated": true,
  "physical_effects": false,
  "viewer_live": false,
  "durability": "process_local"
}
```

simulation의 `live_ready` phase는 adapter 상태기계 계약을 시험한 이름일 뿐,
production의 Nav2 도착, KVS frame 송출 또는 브라우저 표시 증거가 아니다.

## 4. 신뢰 경계

```text
Untrusted / user-controlled
  microphone sound
  STT text
  short "응" confirmation text
  LLM decision
  HTTP robot_state
  browser query/body
              |
              v
Trusted local boundaries
  speech binding + capture epoch
  explicit-confirmation verifier
  durable authorization ledger
  server-owned capability registry
  Room resolver + signed/versioned coverage plan
  ROS state adapter + Nav2 Action adapter
  Homecam read-only status adapter
              |
              v
External trusted owners
  Nav2 owns motion execution/cancel/feedback
  Homecam backend owns device desired state and membership
  homecam_media_agent owns camera/KVS device session
  authenticated browser owns viewer connection and autoplay
```

### 4.1 절대 공유하지 않는 credential

- Agent에 Homecam 장치 bearer token을 복사하지 않는다.
- Agent에 브라우저 Cognito token, opaque session cookie 또는 owner 비밀번호를
  전달하지 않는다.
- 웹 frontend에 Agent/OpenAI API key를 전달하지 않는다.
- LLM prompt, conversation DB, Tool result와 로그에 KVS STS credential, channel
  endpoint 또는 bearer token을 넣지 않는다.

향후 Agent가 Homecam 상태를 읽어야 한다면 device·user binding이 있는 별도
least-privilege server-to-server identity와 read-only endpoint를 사용한다.
1단계 offline Mock은 이를 실제 HTTP 호출로 가장하지 않는다.

### 4.2 confirmation과 exactly-once

확인은 다음 전체 값의 digest에 결속한다.

```text
authenticated user/person
+ speech identity evidence
+ conversation_id
+ proposal turn_id
+ decision_id
+ tool_name
+ normalized full arguments
+ target device binding
+ issued_at / expires_at
```

확인 성공 뒤 별도 `tool_call_id`를 발급하며 다음 상태를 한 transaction에서
저장해야 한다.

```text
confirmation consumed_at
decision_id -> unique tool_call_id
execution status=pending
immutable request digest
```

동일 confirmation, `decision_id` 또는 `tool_call_id`가 동시에 재전송되거나
프로세스 재시작 뒤 다시 들어와도 physical adapter 호출 수는 1회여야 한다.
현재 `ToolGateway`의 process-local LRU cache는 read-only·Mock query 중복 억제일
뿐 이 요구사항의 증거가 아니다.

## 5. 데이터 흐름

### 5.1 음성에서 mission proposal까지

```text
wake/VAD adapter
  -> PCM capture (adapter-local, bounded)
  -> final transcript + metadata only
  -> SpeechConversationCoordinator
  -> AgentRequest
  -> Provider
  -> AgentDecision
  -> local SafetyPolicy
  -> non-executable mission proposal
```

현재 개발 CLI는 local STT와 coordinator를 통과하지만 한 번만 녹음하고 종료하며
Tool 목록도 비워 둔다. 별도 `ContinuousVoiceSession`과 scripted integration test는
injected wake를 반복 처리하고 `monitor_room` proposal을 simulation controller에
결속하지만, 실제 wake detector·연속 audio capture 또는 physical mission 실행을
증명하지 않는다.

### 5.2 Room·Nav2 coverage

```text
room="거실"
  -> canonical alias lookup
  -> current User Map의 unique room_id
  -> map content digest 검증
  -> approved coverage_plan_id
  -> ordered map-frame poses/yaw/dwell
  -> Nav2 plan check
  -> NavigateToPose 또는 승인된 coverage Action
  -> feedback + terminal result
```

현재 User Map Room은 이름, category, polygon과 centroid를 가질 수 있다. 하지만
centroid가 충돌 없이 도달 가능하거나 카메라 coverage를 보장한다는 뜻은 아니다.
centroid를 그대로 Nav2 goal로 보내지 않는다. coverage plan은 실제 map·robot
footprint·camera FOV로 별도 검증한 artifact여야 한다.

### 5.3 카메라에서 Malbut 웹까지

현재 코드가 의도하는 영상 노선은 다음과 같다.

```text
Gazebo /rgbd_camera/image
  -> ros_gz_bridge /camera/color/image_raw
  -> homecam_media_agent
  -> GStreamer H.264 + Opus
  -> device-authenticated POST /api/device/v1/session
  -> AWS KVS P2P master
  -> authenticated owner/family POST /api/devices/{deviceId}/live-session
  -> short-lived VIEWER credentials
  -> connectAuthorizedDeviceViewer()
  -> browser MediaStream
  -> <video> current frame
```

Homecam backend의 owner 설정은 heartbeat 응답으로 장치에 전달된다.

```text
PATCH /api/devices/{deviceId}/settings       (owner web identity)
  -> DB desired state
  -> POST /api/device/v1/heartbeat           (device bearer identity)
  -> desiredState.cameraEnabled
  -> desiredState.microphoneEnabled
  -> desiredState.monitoringEnabled
  -> homecam_media_agent applies generation fence
```

`monitoringEnabled=false`이면 media agent가 P2P session을 선택하고,
`monitoringEnabled=true`이면 Storage session을 선택한다. 1단계 mission은 전자만
허용한다. Agent가 `PATCH settings`를 호출하거나 owner를 가장해서는 안 된다.

## 6. 현재 코드 근거와 실제 gap

### 6.1 현재 저장소에서 확인되는 구현

| 책임 | 현재 근거 | 정확한 해석 |
| --- | --- | --- |
| final transcript 대화 경계 | [`speech.py`](../../malbut_agent_server/speech.py) | binding, confidence, final-only, self-echo, capture epoch와 text-only TTS 계약이 있다. 실제 wake/STT/TTS ROS adapter는 아니다. |
| one-shot local STT demo | [`local_voice_demo.py`](../../malbut_agent_server/local_voice_demo.py) | 마이크 한 번→STT→Agent→text 결과. `available_tools=()`이며 한 번 뒤 종료한다. |
| 연속 음성 offline core | [`continuous_voice.py`](../../malbut_agent_server/continuous_voice.py), [`test_continuous_voice.py`](../../test/test_continuous_voice.py) | injected wake·STT·speech output으로 반복 cycle, replay, barge-in과 immutable non-authorizing confirmation handoff를 검증한다. Tool 제안은 `awaiting_confirmation`, 확인 뒤에는 `mission_wait`에 머물며 terminal 전 다음 wake를 소비하지 않는다. 실제 detector·VAD·마이크 stream·TTS adapter는 아니다. |
| Tool schema | [`tools.py`](../../malbut_agent_server/tools.py) | `monitor_room(location)` 고수준 제안과 기존 단일 Tool이 있다. 현재 제안은 실행 권한이 아니다. |
| local monitor Safety | [`safety.py`](../../malbut_agent_server/safety.py), [`test_agent_contract.py`](../../test/test_agent_contract.py) | 닫힌 whole-utterance 문법, competing action·secret·금지 Room 차단과 기본 빈 monitorable allowlist를 검증한다. plan-backed Room을 trusted runtime이 주입하기 전에는 safe-off다. |
| Agent single-action policy | [`prompting.py`](../../malbut_agent_server/prompting.py) | 여러 행동은 clarification으로 돌린다. 승인된 고수준 mission 없이는 이동+live를 자동 조합하지 않는다. |
| proposal metadata | [`orchestrator.py`](../../malbut_agent_server/orchestrator.py) | `decision_id`는 있지만 `authorized=false`, `consume_once=false`, `tool_call_id=null`이다. |
| Tool Gateway | [`gateway.py`](../../malbut_agent_server/gateway.py) | production side-effect Tool은 proposal-only이고 query는 read-only/Mock 전용이다. |
| runtime trust | [`factory.py`](../../malbut_agent_server/factory.py) | 기본 orchestrator는 `trusted_robot_state=False`이며 실제 ROS adapter를 주입하지 않는다. |
| Room geometry source | [`user_map_builder.py`](../../../malbut_gazebo/malbut_gazebo/user_map_builder.py), [`room_editor.py`](../../../malbut_gazebo/malbut_gazebo/room_editor.py) | Room polygon·name·category·centroid가 있다. 실제 map용 승인된 coverage plan은 아직 없다. |
| semantic Room simulation | [`room_mission.py`](../../malbut_agent_server/room_mission.py), [`test_room_mission.py`](../../test/test_room_mission.py) | provided map snapshot의 unique alias, topology, explicit navigation goal·coverage viewpoints를 검증하고 centroid를 승격하지 않는다. server authority·verifier-issued confirmation·controller-instance-local single active lease·bounded fake phases는 process-local이며 physical adapter를 거절한다. |
| voice→mission scripted trace | [`room_live_scenario.py`](../../malbut_agent_server/room_live_scenario.py), [`test_room_live_scenario.py`](../../test/test_room_live_scenario.py) | wake→STT→proposal→trusted confirmation→simulation terminal→다음 wake를 연결한다. 확인·mission 중 wake 소비는 0회이고 terminal 뒤에만 재무장한다. 실제 음성·ROS·카메라·network 호출은 0회이며 terminal은 항상 `viewer_live=false`다. |
| Nav2 stack | [`navigation.launch.py`](../../../malbut_gazebo/launch/navigation.launch.py), [`nav2_params.yaml`](../../../malbut_gazebo/config/nav2_params.yaml) | simulation navigation 구성은 있다. Agent proposal을 받는 Action adapter는 없다. |
| Gazebo camera bridge | [`bridge.yaml`](../../../malbut_gazebo/config/bridge.yaml) | simulator RGB가 `/camera/color/image_raw`으로 연결된다. |
| Homecam ROS launch | [`homecam_sim.launch.py`](../../../homecam_agent/homecam_media_agent/launch/homecam_sim.launch.py) | 기본 RGB topic을 media/detector node에 연결한다. |
| desired state·heartbeat | [`heartbeat_client.cpp`](../../../homecam_agent/homecam_media_agent/src/heartbeat_client.cpp) | device bearer로 heartbeat하고 정확히 세 boolean desired state를 검증한다. |
| media session state | [`media_agent_node.cpp`](../../../homecam_agent/homecam_media_agent/src/media_agent_node.cpp) | camera health, pipeline, P2P/Storage 선택, session refresh와 privacy generation fence가 있다. |
| device session contract | [`session_client.cpp`](../../../homecam_agent/homecam_media_agent/src/session_client.cpp) | 단기 KVS credential과 device/mode/session을 검증한다. |
| 전체 구조의 기존 판정 | [`MALBUT_ONE_PAGE_SYSTEM_MAP.md`](../../../docs/MALBUT_ONE_PAGE_SYSTEM_MAP.md) | Agent→Nav2/Homecam은 현재 GAP이라고 명시한다. |

별도 Homecam Web 저장소에서 정적 확인한 현재 API 소유 파일은 다음과 같다.
이 파일들은 이 저장소의 변경·테스트 범위가 아니며 이 문서가 배포 상태를
증명하지 않는다.

```text
homecam_web/app/api/device/v1/heartbeat/route.ts
homecam_web/app/api/device/v1/session/route.ts
homecam_web/app/api/devices/[deviceId]/settings/route.ts
homecam_web/app/api/devices/[deviceId]/live-session/route.ts
homecam_web/app/components/homecam-app.tsx
homecam_web/app/lib/kvs-client.ts
homecam_web/db/homecam.ts
```

여기에는 device bearer, owner/family web authorization, P2P/Storage session과
browser viewer 연결 코드가 있다. 그러나 Agent mission endpoint, trusted mission
status callback 또는 browser `viewer_live` callback은 없다.

### 6.2 아직 없는 핵심 구현

- 실제 wake word detector, VAD, microphone stream과 TTS adapter
- 실제 speaker/person identity
- production `monitor_room` execution adapter 연결
- 실제 confirmation endpoint·voice/person verifier. 현재 코드는 verifier-issued
  `TrustedConfirmation`을 소비하는 offline 계약만 제공한다.
- durable confirmation ledger와 exactly-once execution DB
- trusted ROS state provenance·freshness adapter
- 실제 저장 map과 승인된 living-room coverage plan artifact
- battery, forbidden Room, recording/P2P mode와 device identity를 포함한 전체
  physical preflight
- Agent mission을 Nav2에 전달하는 Action adapter
- Agent가 읽을 수 있는 least-privilege Homecam status adapter
- ROS/Homecam 외부 progress·cancel·terminal callback
- trusted Tool result를 conversation에 넣는 전용 저장 schema
- terminal result 뒤 Agent/TTS를 한 번만 재개하는 feedback coordinator
- 브라우저가 실제 video frame을 표시했다는 server-verifiable callback
- physical robot, AWS KVS와 authenticated browser를 함께 사용한 E2E 증거

## 7. Offline Mock 구현 경계

1단계 offline 개발은 실제 이동·촬영·웹 요청 없이 다음 protocol만 구현할 수
있다.

```text
RoomResolver
  resolve(room_alias, trusted_map_revision) -> RoomResolution

RobotStateReader
  snapshot() -> TrustedRobotState(source, sequence, observed_at, ...)

CoverageExecutor
  start(tool_call_id, verified_plan) -> acceptance
  cancel(tool_call_id) -> terminal result
  events(tool_call_id) -> ordered progress/terminal events

HomecamStatusReader
  snapshot(bound_device_id) -> HomecamStatus(
    observed_at, camera_enabled, monitoring_enabled,
    stream_mode, media_healthy, active_session_id
  )

ToolResultFeedback
  commit_terminal_once(bound conversation, execution result)
  -> one safe follow-up response
```

Mock adapter는 다음을 명시해야 한다.

- `simulated=true`
- `nav2_goal_published=false`
- `camera_setting_changed=false`
- `network_request_sent=false`
- `browser_opened=false`
- deterministic clock와 event sequence

Mock 성공은 physical 성공이 아니다. 실제 adapter가 연결되기 전 production
registry의 실행 가능 capability 수는 계속 0이어야 한다.

### 7.1 권장 durable record

| record | 핵심 키·제약 |
| --- | --- |
| `mission_proposals` | `decision_id` PK, user/conversation/turn, request digest, expiry |
| `mission_confirmations` | `confirmation_id` PK, decision FK, evidence digest, `consumed_at` |
| `mission_executions` | `tool_call_id` PK, `decision_id UNIQUE`, status, plan/map digest, timestamps, terminal payload |
| `mission_events` | `(tool_call_id, sequence) UNIQUE`, phase, source, observed_at, bounded payload |
| `mission_feedback` | `tool_call_id UNIQUE`, conversation revision, response commit ID |

`mission_executions`와 confirmation consume은 같은 transaction이어야 한다.
adapter를 호출하기 전 durable `pending` row가 있어야 하며, crash recovery는 이
row를 조회해 기존 실행을 reconcile한다. 새 `tool_call_id`를 발급해 다시 움직이지
않는다.

## 8. Acceptance matrix

상태 값은 다음 의미다.

- **구현**: 현재 코드와 자동화 evidence가 해당 범위를 직접 증명한다.
- **부분**: 재사용 가능한 코어는 있으나 이 시나리오 경로는 끊겨 있다.
- **미구현**: 요구 경계를 만족하는 코드가 없다.
- **실기 대기**: 코드가 있어도 physical/AWS/browser 증거가 없다.

| # | 요구사항 | Offline Mock 합격 증거 | Physical·browser 합격 증거 | 현재 |
| ---: | --- | --- | --- | --- |
| 1 | wake 이후에만 capture 시작 | wake 전 transcript 1,000건이 provider 0회, wake replay가 capture 0회 | 소음·TV·로봇 TTS 환경 false wake 실측 | 부분: injected wake/replay fence 구현, 실제 detector 없음 |
| 2 | final STT만 Agent에 전달 | partial, low-confidence, self-echo, stale epoch가 provider 0회 | 실제 마이크·speaker echo 시험 | offline 구현·실기 대기 |
| 3 | 명확한 단일 고수준 mission 제안 | 거실 live 요청이 정확히 한 proposal, 모호한 방은 clarification | 실제 모델 반복 평가와 한국어 변형 | offline Mock 구현·실제 모델 평가 대기 |
| 4 | L3 explicit confirmation | 미확인·만료·다른 user·변조 args가 executor 0회 | 승인 UX와 실제 identity 검증 | 부분: verifier-issued 확인 계약 구현, 실제 UX/person identity 없음 |
| 5 | durable exactly-once | 동시 10,000회와 process restart replay에서 adapter start 1회 | ROS adapter crash/restart fault injection | 미구현 |
| 6 | canonical Room resolver | alias unique match, ambiguous/not-found/map mismatch fail-closed | 실제 저장 map과 room label 검증 | offline 구현 |
| 7 | verified coverage plan만 사용 | 임의 좌표, centroid 직접 사용, digest mismatch가 Nav2 0회 | map별 waypoint·yaw·camera FOV 실기 승인 | 부분: explicit pose·centroid 차단 구현, plan ID/digest/FOV 실기 승인 없음 |
| 8 | trusted ROS preflight | stale state, e-stop, bad localization, low battery가 start 0회 | 실물 상태 source/sequence/freshness 시험 | 부분: freshness/map/e-stop/privacy/nav/localization/camera/stream 구현. battery·forbidden·recording/device 미구현 |
| 9 | Nav2 feedback·cancel | ordered progress, timeout, cancel, late success discard | closed course에서 goal/cancel 100회 | 부분: bounded fake timeout/cancel/late-result fence, 실제 Nav2 feedback 없음 |
| 10 | privacy-safe live-only | camera off 또는 monitoring true면 start 0회; Agent settings mutation 0회 | owner 설정 변경이 heartbeat를 통해 장치에 반영 | 부분 |
| 11 | media live readiness | fresh P2P session+healthy만 `live_ready`; idle/storage/stale는 실패 | AWS KVS 장치 송출과 frame transport 실측 | 부분·실기 대기 |
| 12 | authenticated viewer | unauthorized/family revoked/device mismatch credential 0회 | Cognito owner/family 브라우저에서 영상 frame 확인 | 부분·실기 대기 |
| 13 | `viewer_live`를 과장하지 않음 | browser evidence 없으면 결과가 false/unknown이고 문구도 “준비됨” | `<video>` live track+current frame callback을 server가 검증 | offline 구현·browser callback 대기 |
| 14 | terminal result가 대화로 복귀 | success/failure/cancel 각각 follow-up 1회; duplicate terminal도 1회 | 실제 실행 종료 후 같은 음성 session TTS | 미구현 |
| 15 | feedback 뒤 awaiting wake | confirmation/mission terminal 전 wake source 호출 0회, terminal 뒤에만 다음 cycle | 실제 mission feedback TTS·wake·barge-in loop | 부분: offline lifecycle fence 구현, feedback TTS 미구현 |
| 16 | 비밀·원시 미디어 비저장 | DB/log/result에 audio, cookie, token, STS, endpoint 원문 0건 | 운영 log·trace privacy audit | 부분 |
| 17 | 전체 시나리오 E2E | 모든 adapter가 `simulated=true`인 deterministic trace | 음성→로봇 이동→coverage→KVS→인증 웹 frame→음성 feedback | 부분: scripted offline trace 구현, actual E2E 미구현 |

### 8.1 필수 offline fault-injection cases

다음 case가 모두 통과하기 전 실제 adapter를 registry에 등록하지 않는다.

1. confirmation 없는 proposal은 adapter 호출 0회다.
2. 만료된 confirmation, 다른 user, 다른 conversation과 한 글자라도 바뀐 인자는
   adapter 호출 0회다.
3. 동일 확인의 순차·동시·재시작 replay가 같은 `tool_call_id`를 반환하고 adapter
   start는 정확히 1회다.
4. Room alias가 0개 또는 2개 이상과 일치하면 clarification 또는 terminal
   failure이고 Nav2 호출은 0회다.
5. map digest와 coverage plan digest가 다르면 Nav2 호출은 0회다.
6. e-stop, stale localization, unknown battery, forbidden Room은 Nav2 호출 0회다.
7. camera off, privacy mode, `monitoringEnabled=true`, stale heartbeat는 Nav2와
   Homecam mutation 모두 0회다.
8. navigation accept 실패, waypoint 실패와 timeout은 성공으로 바뀌지 않는다.
9. 실행 중 privacy/camera/recording mode가 바뀌면 active goal을 cooperative
   cancel하고 terminal 상태는 `cancelled` 또는 `failed`다.
10. cancel 뒤 늦은 Nav2 success가 terminal 상태를 덮어쓰지 않는다.
11. P2P가 아닌 Storage session은 1단계 성공으로 인정하지 않는다.
12. browser callback이 없으면 `viewer_live=true`를 만들지 않는다.
13. terminal event를 100회 재전송해도 conversation commit, provider 후속 호출과
    TTS request는 각각 1회다.
14. conversation이 reset·close·delete된 뒤 terminal event가 오면 새 대화를
    만들지 않고 감사 가능한 orphan terminal로 보존한다.
15. Mock trace에 transcript 원문, API key, bearer, cookie, KVS STS credential,
    절대 credential path가 포함되지 않는다.

## 9. 실제 E2E 실행 전 blocker

다음 조건은 offline 코드만으로 해결되거나 증명되지 않는다.

### 9.1 Physical robot blocker

- 실물 base/Aurora driver와 camera topic 이름
- 실제 map과 localization 안정성
- 거실 Room label과 검증된 coverage waypoint/yaw
- camera FOV, 가구·사각지대와 이동 중 영상 품질
- 사람·반려동물과 함께 있는 폐쇄 시험 공간의 충돌 안전
- Nav2 cancel latency와 e-stop 독립 경로
- 배터리·localization·privacy state의 trusted provenance

### 9.2 AWS·Homecam blocker

- KVS-enabled build와 실제 장치별 P2P channel
- 장치 provisioning과 유효 device credential
- backend heartbeat·session의 실제 endpoint
- 실제 `mediaHealthy`가 최근 frame write 성공을 반영하는지
- P2P credential refresh와 viewer reconnect 중 mission status 일관성
- Agent용 read-only Homecam status identity/API

### 9.3 Browser blocker

- Cognito/opaque session 로그인과 owner/family membership
- 실제 `/live-session` credential 발급
- 지원 브라우저의 H.264/Opus WebRTC 수신
- autoplay·background tab·모바일 네트워크 전환
- live video track이 unmuted이고 `<video>`가 current frame을 가진다는 증거
- 그 증거를 mission feedback으로 결속하는 인증된 callback

실제 브라우저 frame 증거가 없으면 “Malbut 사이트에서 현재 생중계 중”이라고
완료 보고하지 않는다. 장치 측 P2P readiness까지만 확인했다면 “사이트에서 볼
준비가 됨”이라고 보고한다.

## 10. 구현 순서

1. 기존 production registry를 그대로 non-executing 상태로 유지한다.
2. 고수준 mission schema, strict validator와 위 acceptance matrix의 case를 먼저
   추가한다.
3. in-memory가 아닌 SQLite durable authorization/execution ledger와 concurrent
   replay 시험을 구현한다.
4. Fake Room resolver, Fake trusted state, Fake coverage executor와 Fake Homecam
   status를 사용해 전체 상태기계 trace를 만든다.
5. terminal Tool result 전용 conversation/feedback 경계와 중복 억제를 구현한다.
6. simulation에서만 map-bound coverage plan과 Nav2 adapter를 연결한다.
7. Homecam은 설정 mutation 없이 read-only P2P readiness만 연결한다.
8. 실제 장치·AWS·브라우저는 별도 승인된 실기 단계에서 하나씩 연결한다.
9. physical enablement는 confirmation, exactly-once, cancel, audit와 모든 negative
   case가 통과한 뒤에만 별도 runtime profile로 추가한다.

## 11. 완료 정의

SWM25-78을 완료라고 부르려면 다음 증거가 모두 있어야 한다.

- 위 전체 상태기계와 durable exactly-once가 코드·DB migration·자동화 test로
  존재한다.
- 실제 user identity에 결속된 explicit confirmation이 있다.
- 실제 current map의 거실 coverage plan이 검토·버전·digest와 함께 승인됐다.
- Agent가 아닌 trusted adapter가 Nav2 Action을 실행·취소하고 모든 terminal
  결과를 반환한다.
- Homecam은 owner가 설정한 `cameraEnabled=true`, `monitoringEnabled=false`를
  사용하며 Agent가 credential이나 설정 mutation 권한을 갖지 않는다.
- 장치가 P2P media ready임을 확인하고, 인증된 Malbut 웹 브라우저가 실제 frame을
  표시한 E2E evidence가 있다.
- 성공·실패·취소 결과가 같은 conversation으로 정확히 한 번 돌아오고 TTS 뒤
  `awaiting_wake`로 복귀한다.
- 실제 이동, 카메라, AWS와 browser를 포함한 시험에서 미확인 실행, 중복 이동,
  privacy 우회, Storage 녹화와 성공 오보가 모두 0건이다.

현재 코드는 닫힌 `monitor_room` 제안, injected 연속 음성 core, semantic Room
resolver, verifier-issued 확인을 소비하는 process-local simulation controller와
그 전체 scripted trace를 제공한다. 일반 Agent 응답의
`authorized=false`, `tool_call_id=null`은 그대로이며 실제 Tool ID는 별도 trusted
confirmation 경계 안에서 simulation용으로만 발급된다. durable DB, 실제 음성
adapter, full physical preflight, ROS/Nav2·Homecam/KVS·browser와 trusted
conversation feedback은 여전히 없다. 이 문서는 offline simulation 통과를 실제
이동·촬영·사이트 생중계 완료로 재분류하지 않는다.
