# SWM25-131 이중확인을 위한 승인 테스트 요청 과정 구현

## 1. 결론

SWM25-131은 다음 텍스트 폐루프를 구현한다.

```text
인증된 텍스트 요청
  -> 기존 Provider 또는 명시적으로 선택한 RAI sidecar 1회
  -> untrusted navigate({"location":"거실"}) 제안
  -> CapabilityRegistry strict argument schema 재검증
  -> LLM 호출 후 server-owned RobotStateEvidence
  -> revision이 명시된 deterministic Safety
  -> SWM25-130 active-map catalog의 이름 해석
  -> private state evidence·policy revision을 proposal fingerprint에 결속
  -> 30초 agreement window의 SQLite pending confirmation 원자 저장
  -> 네 / 아니요 / 취소의 로컬 판정
  -> durable request claim과 approved / denied / canceled 기록
```

여기서 `approved`는 사용자가 제안에 동의했다는 기록일 뿐 실행 허가가
아니다. 모든 성공·실패 응답과 SQLite 제약은 다음 값을 유지한다.

```text
execution_authorized=false
consume_once=false
tool_call_id=null
physical_authorized=false
nav2_start_count=0
nav2_cancel_count=0
```

Robot Web preview·start, ROS ActionClient, Nav2 goal과 action/outbox 생성은
호출하지 않는다. 승인된 작업을 실제 Gazebo 이동과 연결하는 책임은
SWM25-132에 있다.

## 2. 목표와 달성 조건

### 목표

자연어 텍스트 요청을 고수준 행동 제안으로 바꾸고, 서버가 현재 상태와
목적지를 검증한 뒤 사용자에게 한 번 더 확인한다. 확인 응답은 LLM이 아닌
결정론적 handler가 처리하며, 결과는 재시작 후에도 같은 의미로 남아야 한다.

### 달성 조건

- [x] 인증된 일반 텍스트 요청은 선택된 Agent backend를 최대 1회 호출한다.
- [x] 이동 제안은 post-LLM RobotState·Safety·active map target을 통과한
  경우에만 pending confirmation으로 원자 저장된다.
- [x] `네/아니요/취소`는 LLM 0회로 처리되고 모호한 응답은 pending을
  유지한 채 한 번 재질문한다.
- [x] Agent turn과 confirmation response의 `request_id`·`turn_id` namespace를
  교차 점유할 수 없고, 모든 response claim은 재시작 뒤에도 같은 payload로만
  replay된다.
- [x] duplicate·restart·expiry·wrong-session·target-change·동시 응답이
  해당 ticket 단위로 fail-closed한다.
- [x] LLM 제안 뒤 안전 검증에 사용한 private state evidence와 Safety policy
  revision이 durable
  proposal fingerprint에 포함되며 public 응답에는 노출되지 않는다.
- [x] Provider proposal expiry와 별개인 기본 30초 confirmation agreement
  window를 제공하되, 승인 자체는 계속 non-authorizing이다.
- [x] 승인 결과를 포함한 모든 경로에서 실제 Nav2 start/cancel과 물리
  authority 생성이 0회다.

## 3. 책임 구조

```text
POST /v1/text/turns
  -> Bearer auth + server-owned user_id
  -> TextTurnService
      |- durable request claim 있음
      |    -> exact full-envelope replay 또는 collision
      |- pending 있음
      |    -> exact phrase classifier
      |    -> current target re-resolution
      |    -> SQLite response claim + terminal CAS
      |
      `- pending 없음
           -> AgentOrchestrator
               -> Mock / OpenAI / isolated RAI sidecar
               -> CapabilityRegistry
               -> post-LLM RobotStateSource + private evidence
               -> revisioned SafetyPolicy
           -> CatalogNamedTargetResolver.resolve(name)
           -> conversation response + confirmation 한 transaction commit
```

RAI가 자연어 의도와 고수준 Tool을 제안한다. Malbut가 사용자·세션,
capability, arguments, 상태, 목적지, 승인과 영속 상태를 소유한다. RAI와
Malbut 어디에도 이번 Story의 Nav2 실행 adapter가 주입되지 않는다.

## 4. 구현 위치와 이유

### `text_confirmation.py`

- `ConfirmationDraft`: 원래 Agent turn, proposal, arguments digest와 target
  binding을 묶은 승인 질문
- `ConfirmationResolution`: 원문을 저장하지 않고 exact response 분류와
  fingerprint만 보존한 응답
- `ConfirmationRecord`: pending 또는 terminal 상태

세 타입은 `execution_authorized=False`, `consume_once=False`만 허용한다.
`ConfirmationDraft`의 private binding에는 `state_evidence_id`,
`state_observed_at`, `safety_policy_revision`과 target·arguments digest가 함께
들어간다. private JSON을 다시 읽을 때 arguments digest와 전체 proposal
fingerprint를 재계산하므로 arguments, state evidence, policy revision 중 하나만
바뀌어도 로드가 실패한다. 사용자에게 보이는 `message`, room name/category도
같은 fingerprint에 포함되므로 표시된 동의 의미만 바꾸는 조작도 실패한다.
이 세 provenance 필드와 target binding digest는 public confirmation JSON에
노출되지 않는다.

### `conversation.py`

기존 conversation table을 교체하지 않고 additive schema를 추가했다.

- schema metadata version 검증
- conversation 완료와 confirmation insert의 동일 transaction
- 사용자·session instance·generation·revision 결속
- pending 1개 unique index
- response ID owner unique index
- exact replay와 terminal CAS
- confirmation-side `text_turn_request_claims`의 full-envelope fingerprint와
  content-free outcome
- Agent turn과 confirmation response 사이의 user-scoped `request_id`, 그리고
  active session·generation 안의 `turn_id` 교차 충돌 방어
- exact confirmation 응답은 같은 session generation 안에서 후속 대화 turn이
  추가돼도 원래 결과로 replay
- reset·close·expiry 시 pending 무효화
- DB CHECK로 authority, consume, tool/mission ID 저장 금지

confirmation insert가 실패하면 conversation response commit도 rollback한다.
반대로 둘이 모두 commit된 경우에만 사용자에게 pending 상태를 돌려준다.
모호한 응답, pending 없는 늦은 응답, recognized terminal 응답과 응답 중 감지한
target invalidation도 durable claim을 남긴다. 따라서 같은 ID·같은 전체 envelope는
재시작 후에도 동일 결과를 replay하고, 같은 ID의 text·turn·conversation을
바꾸는 요청은 conflict다. 한 번 `글쎄`로 claim한 ID를 나중에 `네`로 바꿔
승인으로 승격할 수 없다.

### `orchestrator.py`와 `robot_state_source.py`

Provider가 응답한 뒤에 `RobotStateSource.read()`를 호출한다. HTTP body나
모델 prompt의 state는 실행 가능성 판단의 권한이 아니다. source가 없거나,
예외가 발생하거나, 표본이 stale이면 기존 Safety가
`untrusted_robot_state`로 차단한다. 정상 표본의 server-owned
`evidence_id`·`observed_at`과 적용한 `SafetyPolicy.policy_revision` 기본값
`malbut-safety-v1`은 confirmation의 private proposal binding에 보존된다.
RobotState evidence가 존재하는 orchestration response는 private
`safety_binding`을 가진 persisted schema v3로 저장되며 cached replay에서도
세 값이 그대로 복원된다. source response와 confirmation을 한 transaction에
넣을 때 store가 세 값을 다시 비교하므로 custom confirmation factory가 다른
provenance label을 끼워 넣을 수 없다. orchestrator도 callback 호출 전 provenance
snapshot을 보존하고 호출 뒤 값이 달라지면 turn 전체를 rollback한다. 미래 시각의
evidence는 신뢰하지 않고 provenance를 `None`으로 정규화해 최초 응답과 cached
replay가 모두 `untrusted_robot_state`로 일치한다. source가 없는 기존 Agent 기본
경로와 이 content-free 실패 경로는 schema v2 shape를 유지한다.

이 provenance는 LLM 제안 직후의 안전 검증 근거를 감사하기 위한 것이지, `네`를
실행 허가로 바꾸는 증명이 아니다. SWM25-131은 응답 시 target binding만 다시
해석하며 RobotState·Safety를 실행용으로 재사용하지 않는다. SWM25-132가 실제
dispatch 직전에 새 RobotState evidence, 현재 policy revision과 target을 다시
검사하고 별도의 1회성 execution authorization을 만들어야 한다.

기존 `/v1/agent/respond`는 source를 주입하지 않은 기본 구성에서 이전과
같이 동작한다. `confirmation_factory`도 optional이라 기존 호출자의 메서드
사용법과 응답 shape는 변하지 않는다.

### `named_target.py`와 `agent_named_target.py`

Agent package에는 room name/category와 private binding digest만 아는 Port를
둔다. 실제 active map catalog adapter는 Agent와 Gazebo가 만나는
`malbut_scenarios`에 둔다.

adapter는 `catalog.resolve(location)`만 호출한다. SWM25-130 façade의
`preview`, `start`, `status`, `cancel`과 Robot Web client를 생성하지 않는다.
공개 confirmation에는 device ID, map ID/revision, room ID, 좌표와 digest가
포함되지 않는다.

### `text_turn.py`와 HTTP

새 endpoint body는 다음 네 필드만 받는다.

```json
{
  "request_id": "request-001",
  "conversation_id": "demo",
  "turn_id": "turn-001",
  "text": "거실로 가줘"
}
```

`user_id`, `robot_state`, `approved`, `goal_id`처럼 권한을 넓힐 수 있는 필드는
unknown field로 거절한다. 사용자 ID는 HTTP server의 인증된
`allowed_user_id`에서만 온다. text service가 구성됐는데 Bearer token이 비어
있으면 server bind 자체가 실패한다.

pending 상태에서는 어떤 문장도 일반 Agent turn으로 fall-through하지 않는다.
정확한 승인·거절·취소 표현만 terminal로 만들고 그 외 문장은 같은 질문을
다시 보여준다. pending이 없는데 늦은 `네/아니요/취소`가 오면 LLM을 부르지
않고 `confirmation_not_pending`을 반환한다.

### RAI sidecar

RAI는 ROS overlay나 system Python에 설치하지 않는다. 선택 값은
`rai-sidecar`이며 기본 provider는 계속 `mock`이다. 검토·실측 환경은 ROS와
분리한 Python 3.10 전용 virtualenv이고, 2026-08-29 smoke에서 실제 interpreter는
Python 3.10.12였다.

```text
Agent process
  -> strict JSON v1
  -> dedicated venv/bin/python -I -m malbut_agent_server.rai_sidecar_runtime
  -> rai-core 2.12.1 create_structured_output_runnable
  -> explicit ChatOpenAI(max_retries=0)
  -> TextReply | ActionProposal 1개
```

sidecar는 neutral ToolSpec만 prompt에 투영한다. RAI ROS/shell Tool과 callback은
등록하지 않고 graph가 Tool을 실행하지 않는다. 출력은 Agent process에서
Tool name과 arguments schema를 다시 검사한다. subprocess는 `shell=False`,
고정 argv, isolated CWD, bounded deadline을 사용하며 서버 환경 전체를
상속하지 않는다. partial 처리 뒤 Mock/OpenAI로 fallback하거나 retry하지
않는다.

dynamic argument schema도 자유 형식 dict가 아니다. 요청의 neutral ToolSpec은
`additionalProperties=false`여야 하고, runtime은 그 요청에 실제로 포함된
property 이름만으로 Pydantic model을 매번 만든다. arguments field는
`extra=forbid`, `strict=True`이며, 선택하지 않은 Tool의 property는 `null`이어야
한다. 구조화 출력 후 runtime이 선택 Tool의 required/type schema에 다시
결속하고, Agent process가 `validate_tool_arguments()`로 같은 Tool name과
arguments를 다시 검증한다. 예를 들어 `navigate`는 정확히 문자열
`{"location":"..."}`만 허용하고 `approved` 같은 동적 추가 키는 어느
경계에서도 통과하지 못한다.

startup과 process 경계의 실제 검사는 다음과 같다.

- `MALBUT_RAI_SIDECAR_PYTHON`은 절대 경로인 실행 파일이어야 하고, 바로 위
  directory가 `bin` 또는 `Scripts`이며 그 virtualenv root에 `pyvenv.cfg`가
  있어야 한다.
- `MALBUT_RAI_SIDECAR_CWD`는 존재하는 절대 directory이고 `/`가 아니며
  virtualenv 밖이어야 한다. sidecar 자체도 `sys.prefix != sys.base_prefix`를
  다시 확인한다.
- RAI import 전에 distribution 이름 `rai-core`를
  `importlib.metadata.version()`으로 조회해 정확히 `2.12.1`인지 비교한다.
  누락, 구버전, 신버전과 `2.12.1+local`은 모두 content-free
  `runtime_unavailable`로 fail-closed한다.
- child environment는 allowlist로 새로 만들고 `PYTHONPATH`와 server token 등은
  전달하지 않는다. executable은 고정 argv
  `<dedicated-venv>/bin/python -I -m malbut_agent_server.rai_sidecar_runtime`로
  실행하며, child `PATH`는 정확히 `/usr/bin:/bin`으로 강제한다.
- 모델 입력 상한은 protocol의 65,536자를 넘을 수 없다. 구성에 존재하지만
  소비되지 않는 executable·CWD 설정은 두지 않는다.

직접 dependency 기준은 다음과 같다.

```text
rai-core==2.12.1
wheel SHA-256:
e38b90691710d1d2ddb00feabef8d5366d54c2de85cff32429fe73aecd8a05ab
```

전체 transitive artifact manifest와 ROS dependency 충돌 분석은
`SWM25-128_CLEAN_BASELINE.md`를 source of truth로 사용한다. 이 ROS package의
`install_requires`에는 `rai-core`를 추가하지 않는다. runtime이 위 wheel hash를
다시 계산하는 것은 아니므로, hash 검증은 reviewed wheel 설치 단계와 manifest의
책임이고 runtime은 설치된 distribution name과 exact version을 독립적으로
검증한다.

검토된 artifact를 다시 설치·검사하는 절차는 다음처럼 system/ROS Python과
분리한다. `<reviewed-lock>`은 모든 transitive wheel hash가 고정된 private
requirements lock이고 `<reviewed-wheel>`은 위 SHA-256을 직접 확인한 wheel이다.

```bash
python3.10 -m venv <rai-venv>
sha256sum <reviewed-wheel>/rai_core-2.12.1-py3-none-any.whl
<rai-venv>/bin/python -m pip install --require-hashes -r <reviewed-lock>
<rai-venv>/bin/python -m pip install --no-deps \
  <reviewed-wheel>/rai_core-2.12.1-py3-none-any.whl
<rai-venv>/bin/python -m pip install --no-deps <workspace>/malbut_agent_server
<rai-venv>/bin/python -m pip check
```

첫 `sha256sum` 출력이 문서의 digest와 다르면 설치하지 않는다. 실제 운영용
lock과 artifact 경로는 private recovery 영역에 두고 Git에는 넣지 않는다.

### 30초 confirmation agreement window

Provider proposal의 `execution.expires_at`과 confirmation의 `expires_at`은
서로 다른 deadline이다. Provider가 반환한 TTL은 conversation source response에
그대로 남는다. confirmation은 기본적으로 `issued_at + 30초`이며 설정 가능한
범위는 1~120초다. atomic insert는 두 deadline이 각각 `issued_at`보다 뒤인지
검증하지만 서로의 대소 관계를 강제하지 않는다. 따라서 1초 agreement window와
5초 Provider proposal TTL 같은 구성도 정상적으로 fail-closed 동작한다.

예를 들어 기본 Mock/RAI proposal TTL이 5초라면 source proposal은 5초 뒤
stale이 되지만 agreement 질문에는 30초 동안 답할 수 있다. 5~30초 사이의
`네`도 과거 제안에 대한 동의만 기록하며 stale proposal을 되살리거나 실행
authority를 만들지 않는다. 30초가 지나면 confirmation 자체가 `expired`가
된다. 이후 SWM25-132는 이 기록을 바로 실행하지 않고 fresh state·policy·target
재검사를 통과한 별도 dispatch intent만 실행한다.

## 5. confirmation 상태와 실패 의미

| 입력·사건 | 결과 | LLM | Nav2 |
| --- | --- | ---: | ---: |
| 안전한 `거실로 가줘` | `awaiting_confirmation` | 1 | 0 |
| pending 중 `네` | `approved` | 0 | 0 |
| pending 중 `아니요` | `denied` | 0 | 0 |
| pending 중 `취소` | `canceled` | 0 | 0 |
| pending 중 모호한 문장 | pending + 재질문 | 0 | 0 |
| pending 없는 늦은 `네` | `confirmation_not_pending` | 0 | 0 |
| 모호한 response ID를 나중에 `네`로 변경 | durable claim conflict | 0 | 0 |
| Agent/confirmation 사이 request·turn ID 재사용 | namespace conflict | 0 | 0 |
| 같은 response ID·같은 입력 | 동일 terminal replay | 0 | 0 |
| 같은 response ID·다른 입력 | `409` conflict | 0 | 0 |
| 다른 conversation의 응답 | 원 ticket 유지 | 0 | 0 |
| target map/binding 변경 | 해당 ticket invalidated | 0 | 0 |
| Provider proposal TTL 만료, 30초 이내 동의 | approved 기록만, authority 없음 | 0 | 0 |
| 30초 agreement window 만료 | expired | 0 | 0 |
| 두 worker의 동시 응답 | terminal winner 1개 | 0 | 0 |
| stale/missing/E-stop state | refusal, ticket 없음 | 1 | 0 |

`fail-closed`는 모든 대화나 모든 예정 작업을 지운다는 뜻이 아니다. 검증에
실패한 exact confirmation 하나만 승인되지 않거나 terminal이 된다.

## 6. 로컬 실행

먼저 SWM25-130 private map fixture를 준비한다.

```bash
run_root="$(mktemp -d /tmp/malbut-swm25-131.XXXXXX)"
ros2 run malbut_scenarios prepare_named_navigation_fixture -- \
  --destination "$run_root/map-store"
```

Git에서 제외되는 `.env.local`에 다음 최소 설정을 둔다.

```text
MALBUT_AGENT_PROVIDER=mock
MALBUT_AGENT_TOOL_MODE=proposal
MALBUT_AGENT_AUTH_TOKEN=<local-random-token>
MALBUT_AGENT_USER_ID=local-user
MALBUT_AGENT_DB=<private-path>/swm25-131.sqlite3
MALBUT_NAMED_NAVIGATION_MAP_STORE=<run_root>/map-store
MALBUT_ROBOT_DEVICE_ID=malbut-sim-01
```

구성과 `거실` target만 검사한다. 이 명령은 LLM·Robot Web·ROS·Nav2를
호출하지 않는다.

```bash
ros2 run malbut_scenarios malbut_text_agent_server -- \
  --env-file <private-path>/.env.local --check
```

`--check`를 빼고 server를 시작한 뒤 인증된 conversation을 만든다.

```bash
curl -X POST http://127.0.0.1:8765/v1/conversations \
  -H 'Authorization: Bearer <local-random-token>' \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"local-user","conversation_id":"demo"}'
```

요청과 확인을 각각 보낸다.

```bash
curl -X POST http://127.0.0.1:8765/v1/text/turns \
  -H 'Authorization: Bearer <local-random-token>' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"request-1","conversation_id":"demo",\
"turn_id":"turn-1","text":"거실로 가줘"}'

curl -X POST http://127.0.0.1:8765/v1/text/turns \
  -H 'Authorization: Bearer <local-random-token>' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"response-1","conversation_id":"demo",\
"turn_id":"turn-2","text":"네"}'
```

두 번째 응답이 `approved`여도 console과 JSON은 `nav2=off`,
`physical_authorized=false`, `nav2_start_count=0`을 유지한다.

## 7. 검증

핵심 검증 명령은 다음과 같다.

```bash
PYTHONPATH=malbut_agent_server:malbut_gazebo:malbut_scenarios \
python3 -m pytest -q \
  malbut_agent_server/test/test_text_confirmation.py \
  malbut_agent_server/test/test_text_confirmation_store.py \
  malbut_agent_server/test/test_text_turn.py \
  malbut_agent_server/test/test_robot_state_source.py \
  malbut_agent_server/test/test_rai_sidecar_protocol.py \
  malbut_agent_server/test/test_rai_sidecar_client.py \
  malbut_agent_server/test/test_rai_sidecar_runtime.py \
  malbut_agent_server/test/test_rai_runtime_config.py \
  malbut_agent_server/test/test_http_server.py \
  malbut_scenarios/test/test_agent_named_target.py \
  malbut_scenarios/test/test_text_agent_server.py
```

2026-08-29 최종 focused 명령은 `104 passed in 6.57s`였다. 여기에는 real service와
SQLite store를 사용해 인증된 HTTP navigation 요청과 `네`를 연속 전송하고,
Provider 1회·durable approved·execution authority 0을 확인하는 E2E 회귀 시험도
포함된다. 실 API smoke를 제외한 이 pytest 실행은 fake provider/sidecar를
사용해 유료 호출을 만들지 않았다.

같은 source에서 Agent Server 전체 `243 passed`, scenario package
`30 passed`를 확인했다. clean 임시 colcon overlay에서는 두 package build가
완료됐고 `273 tests, 0 errors, 0 failures, 0 skipped`였다. installed import와
`malbut_text_agent_server --check`도
`simulation=true, physical_authorized=false, nav2=off`로 통과했다.
같은 installed CLI를 loopback HTTP server로 실제 기동해 conversation 생성,
`거실로 가줘`, `네`를 순서대로 보냈고 `awaiting_confirmation -> approved`,
후속 일반 turn 뒤 같은 승인 envelope의 `approved/cached` replay,
`execution_authorized=false`, `nav2_start_count=0`을 확인한 뒤 정상 종료했다.
종료 후 SQLite에서도 confirmation `approved`, authority/consume `0`, Tool/Mission
ID `NULL`, source response schema v3 private safety binding을 확인했다.

기존 Gazebo 전체 회귀에서는 기능 테스트 `349 passed`였고 repo-wide PEP257
collector 1개만 main에서 변경하지 않은 기존 4개 파일의 style issue로
실패했다. SWM25-131 신규·수정 파일에 한정한 flake8·PEP257·diff check는 모두
통과했으며 이 baseline lint debt는 이번 Story에서 무관한 파일을 수정하지 않고
별도 이슈로 남긴다.

완료 보고에는 다음을 구분해 적는다.

- pure/domain·SQLite·HTTP·sidecar fake test
- source package 전체 pytest
- colcon build/test와 installed import/CLI smoke
- 실제 유료 RAI/OpenAI 1회 검증 여부
- 실제 Gazebo/Nav2 goal은 의도적으로 0회라는 사실

### 2026-08-29 실제 RAI/OpenAI smoke 증거

검토한 wheel로 새로 만든 별도 virtualenv에서 secret과 절대 경로를 출력하지
않는 검사만 남겼다.

| 검사 | 관측값 |
| --- | --- |
| sidecar Python | 3.10.12 |
| `sys.prefix != sys.base_prefix` | true |
| virtualenv root의 `pyvenv.cfg` | 있음 |
| installed distribution | `rai-core==2.12.1` |
| 검토한 wheel artifact SHA-256 | `e38b90691710d1d2ddb00feabef8d5366d54c2de85cff32429fe73aecd8a05ab` |
| sidecar child `PATH` | `/usr/bin:/bin` |

같은 날 `MALBUT_RAI_MODEL=gpt-4.1-mini`로 최종 성공 smoke 1회를 수행했다.
그 전에 경계를 진단한 호출은 inherited `PATH`와 OpenAI strict schema 문제를
드러내고 content-free `runtime_unavailable`로 fail-closed했으며 Tool 실행으로
이어지지 않았다. 두 문제를 고정 PATH와 per-request strict arguments model로
수정한 뒤 얻은 최종 결과는 다음 non-authorizing proposal이었다.

```text
ActionProposal(tool_name='navigate', arguments={'location': '거실'}, ...)
```

이 smoke는 RAI structured-output proposal 경계를 확인한 것이며 Tool을 실행하는
graph, Robot Web client, ROS ActionClient 또는 Nav2 adapter는 구성하지 않았다.
따라서 이 실 API 검증의 Nav2 goal/start/cancel은 모두 0회다. API key, 응답 ID,
token 사용량, private virtualenv/CWD 경로는 문서와 Git에 남기지 않는다.

## 8. SWM25-132 인계

SWM25-132는 `approved` record를 즉시 Nav2 호출로 바꾸면 안 된다. 별도
RobotAction, 1회 소비 authorization, durable dispatch intent, executor/outbox와
reconciliation을 추가해야 한다. dispatch 직전에 fresh RobotState evidence와
현재 Safety policy revision을 얻고, active-map target을 다시 해석한 뒤 그
결과에 새 authorization을 결속해야 한다. 그 후에만 SWM25-130 façade를 통해
실행한다. 이번 confirmation ID나 proposal fingerprint를 ROS goal ID로
재사용하지 않는다.

## 9. Jira 결론 문안

> 인증된 텍스트 요청을 기존 Provider 또는 Python 3.10 전용 virtualenv의
> 격리된 RAI sidecar가 단일 행동 제안으로 변환하고, server-owned
> RobotStateEvidence·revisioned Safety·active map target을 통과한 경우에만
> 30초 durable confirmation을 생성했다. private state provenance와 policy
> revision을 proposal fingerprint에 결속하고, request claim 및 Agent/
> confirmation 교차 namespace 충돌 방어로 restart·duplicate·동시 응답을
> fail-closed한다. 2026-08-29 `gpt-4.1-mini` 유료 smoke 1회는
> `navigate({'location':'거실'})` ActionProposal을 반환했다. 승인 결과도 실행
> 권한을 만들지 않고 Nav2 start/cancel은 0회다. fresh 재검사와 실제 이동은
> SWM25-132로 인계한다.
