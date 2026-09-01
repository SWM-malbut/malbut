# SWM25-149 모호한 텍스트 이동 요청을 한 번의 질문·응답으로 확정한다

## 1. 결론

SWM25-149는 모든 자연어가 문법적으로 명확한지 판정하는 범용 언어 이해
기능이 아니다. 현재 지도에 등록된 공간으로 이동하려는 의도는 분명하지만 목적지만
`여기`, `저기`, `저쪽 방`처럼 빠진 요청을 한 번 질문하고, 바로 다음 답변이 정확한
공간 이름 하나일 때만 기존 이동 제안·안전 검사·확인 절차로 연결한다.

```text
"여기로 가줘"
  -> strict server predicate
  -> server clarification: "등록된 공간 이름 하나를 말해 주세요."
  -> Provider 0회, RobotState 조회 0회
  -> confirmation 0, RobotAction 0, Nav2 0

"거실"
  -> 서버가 직전 질문과 현재 답을 검증
  -> Provider/Safety용 발화만 "거실로 이동해줘"로 구성
  -> LLM navigate(location="거실") 제안
  -> fresh RobotState + deterministic Safety
  -> "거실로 이동할까요?" confirmation 1개
  -> RobotAction 0, Nav2 0

별도의 다음 입력 "네"
  -> 기존 SWM25-131 승인 절차
```

두 번째 `거실`은 목적지를 채우는 답이지 이동 승인이 아니다. 따라서 이번 Story가
성공해도 RobotAction이나 Nav2 goal은 생성되지 않는다. 실제 승인과 실행은 기존
confirmation 응답과 후속 실행 계층의 책임으로 유지한다.

## 2. 목표

- 명확한 이동 요청의 기존 동작을 바꾸지 않는다.
- 목적지만 빠진 이동 요청에는 실행 없이 질문한다.
- 같은 사용자·대화·세션·generation의 바로 다음 답변만 결합한다.
- 최초 질문은 모델에 맡기지 않고 Tool 필드가 없는 서버 정책으로 만든다.
- 재질문은 한 번으로 제한하고 실패 시 새 요청을 안내한다.

## 3. 달성 조건 5개

1. [x] `거실로 가줘`처럼 등록된 목적지가 있는 요청은 기존
   `navigate -> fresh Safety -> confirmation` 흐름을 그대로 사용한다.
2. [x] `여기로 가줘`처럼 목적지가 빠진 요청은 clarification 한 번을 반환하며,
   Provider·confirmation·RobotAction·Nav2 start/cancel은 모두 0회다.
3. [x] 같은 사용자·conversation·session의 바로 다음 답이 정확한 등록 공간 이름
   하나이면 Provider를 정확히 한 번 호출하고 confirmation을 정확히 하나 만든다.
4. [x] unknown·복수 목적지·부정 표현·주입 문구·변조된 기록·다른 conversation은
   결합하지 않으며, LLM이 Tool을 억지로 제안해도 실행 효과는 0회다.
5. [x] restart·exact replay·중간 입력 barrier가 유지되고, 동시 답변은 한 turn만
   승리하며, 두 번째 clarification은 refusal로 닫혀 자동 질문 loop가 생기지 않는다.

## 4. 구현 구조

### 4.1 말하기와 행동 제안을 구분

`TextDecisionPolicy`는 이제 clarification을 일반 답변과 별도의
`CLARIFICATION_REQUIRED` route로 분류한다. 이 route는 질문이 필요하다는 대화
상태만 나타내며 confirmation이나 실행 권한을 갖지 않는다.

```text
AgentDecision
  |- message/refusal      -> DIRECT_REPLY
  |- clarification       -> CLARIFICATION_REQUIRED
  `- navigate tool_call  -> CONFIRMABLE_ACTION_PROPOSAL
```

이는 ReSpAct의 speak/act 분리 아이디어를 Malbut의 typed decision에 작게 적용한
것이다. ReSpAct 연구 runtime이나 자유로운 reasoning trace, 무제한 반복 loop는
가져오지 않는다.

최초 입력이 허용된 deictic 이동 형식과 exact-match하면 `TextTurnService`가
`ServerClarification`을 전달한다. 이 DTO에는 질문·정책 code·revision만 있고
Tool 이름, arguments, RobotState 또는 실행 권한 필드는 없다. Orchestrator는 기존
`begin_turn -> complete_turn` 원장을 그대로 사용하되 외부 Provider와 memory/state
I/O를 생략한다. 따라서 Provider가 message·refusal·Tool call을 내놓을 가능성이나
timeout과 관계없이 같은 non-action 질문이 즉시 저장된다.

### 4.2 직전 질문을 서버가 검증

`NavigationClarificationResolver`는 다음 조건을 모두 만족할 때만 목적지를
복원한다.

- 현재 request와 `BeginTurnToken`의 user·conversation·turn·request fingerprint가
  일치한다.
- 직전 완료 turn이 같은 session instance와 generation에 있고 ordinal이 정확히
  하나 앞선다.
- 직전 clarification이 완료된 revision에 다른 text-turn request claim이 없다.
- 직전 persisted response가 strict schema로 복원되며 non-action clarification이다.
- 직전 사용자의 원문이 허용된 deictic navigation 형식이다.
- 현재 답이 정확한 등록 공간 이름 하나로 `NamedTargetResolver`에서 해석된다.
- 현재 route에서 `navigate` capability가 제공된다.

최초 질문은 서버가 고정하고, 직전 persisted clarification의 자유로운 표시 문구는
목적지 복원의 authority로 쓰지 않는다. 이전 response에 예상하지 않은 필드가
추가되거나 execution authority가 조작되어도 복원을 거부한다.

`네`, `아니요`, `취소`처럼 pending confirmation 없이 먼저 들어온 입력은 기존
content-free `text_turn_request_claims`에 기록된다. 이후 답변은 같은 revision의
claim을 발견하면 앞 clarification과 결합하지 않는다. 따라서
`여기로 가줘 -> 네 -> 거실`에서 마지막 `거실`은 늦은 clarification 답이 아니라
독립된 새 입력으로 처리되며, 이 barrier는 process restart 뒤에도 유지된다.
답변 Provider 호출 중 `네` 같은 새 claim이 경쟁하는 경우에는 같은 SQLite writer
transaction에서 pending Agent turn을 확인해 한쪽만 선형화한다. 답변 turn이 먼저면
새 claim을 conflict로 막고, claim이 먼저면 기존 revision barrier가 답변 결합을 막는다.

### 4.3 raw 발화와 effective 발화를 분리

`AgentOrchestrator`의 optional `utterance_resolver`는 현재 request·history·token의
deep copy만 받는다. 성공하면 새 `AgentRequest`에서 utterance 하나만 바꾸고, 사용자
ID·conversation·request ID·Tool 목록·RobotState는 원본에서 다시 구성한다.

```text
DB conversation user_content     = "거실"
request fingerprint              = raw "거실" 기준
memory search query              = raw "거실"
Provider/Safety current utterance = "거실로 이동해줘"
```

따라서 서버가 대화 의미를 조립할 수는 있어도, resolver가 identity·capability·상태
권한을 바꿀 수 없다. exact replay는 raw request fingerprint로 기존 결과를 반환하므로
resolver와 Provider를 다시 호출하지 않는다.

### 4.4 한 번만 질문하고 fail-closed

직전 turn이 유효한 navigation clarification인데 답을 등록 공간 하나로 확정하지
못하면, 후속 모델 결과가 Tool call이어도 `clarification_answer_invalid`로 차단한다.
후속 모델이 다시 clarification을 반환하면 `clarification_limit_reached` refusal로
바꾸고 더 질문하지 않는다.

```text
pending clarification + valid target + navigate proposal
  -> fresh RobotState/Safety -> confirmation 가능

pending clarification + invalid target + any action proposal
  -> refusal -> confirmation 0 -> RobotAction 0 -> Nav2 0

pending clarification + second clarification
  -> bounded refusal -> 자동 loop 없음
```

## 5. 테스트가 증명하는 것

- resolver 단위 계약은 deictic 표현, exact target, persisted schema, fingerprint,
  session/generation/ordinal, 변조, 부정·복수·주입 입력을 검사한다.
- orchestrator 계약은 canonical 발화가 Provider와 Safety에만 전달되고 DB 원문과
  fingerprint는 raw 입력으로 남는지 검사한다.
- TextTurn 통합 계약은 명확한 요청, 질문만 하는 첫 turn, 답변 결합, restart,
  replay, intervening input, wrong conversation, invalid 답변, 악의적 Tool 제안,
  모델의 target 바꿔치기와 동시 응답을 검사한다.
- 최초 deictic 요청에서는 adversarial Provider가 message·refusal·Tool call을
  반환하도록 준비돼 있어도 실제 Provider 호출이 0회인지 검사한다.
- 답변 처리 중 confirmation 단어가 경쟁하는 반대 방향 race도 재현해 confirmation과
  claim이 동시에 성공하지 못하는지 검사한다.
- 모든 clarification 단계에서 `execution_authorized=false`,
  `physical_authorized=false`, `nav2_start_count=0`, `nav2_cancel_count=0`을 확인한다.

## 6. 의도적으로 하지 않은 것

- clarification 전용 DB table, ticket, `reply_to`, 독립 TTL과 CAS는 추가하지 않았다.
  현재 상태는 같은 session/generation의 바로 전 완료 conversation turn에서 파생하고,
  중간 입력 여부만 기존 durable text-turn claim으로 차단한다.
- public status를 `awaiting_clarification`으로 바꾸지 않았다. 기존 wire shape를
  유지하므로 client는 `decision.type=clarification`을 확인한다.
- 다른 conversation의 `거실`은 결합되지 않지만 독립된 일반 입력이므로 Provider가
  호출될 수 있다. 중요한 보안 불변식은 confirmation·Action·Nav2가 0회라는 점이다.
- 범용 slot filling, 다중 질문, 대명사 전체 해석, STT 보정은 이번 범위가 아니다.
- 마이크, 실제 로봇과 Nav2 실행은 이번 노트북 전용 Story에서 다루지 않는다.

별도 durable clarification ticket과 명시적 만료가 필요해지는 시점은 UI가 여러
동시 질문을 표시하거나 비동기 채널·장시간 세션을 지원할 때다. 그 전까지는
한 turn짜리 경계를 작게 유지한다.

## 7. 검증 결과

- Agent Server 전체 source test: `419 passed`
- clarification·race·server-policy 집중 test: `110 passed`
- Inspector + Text Agent Server 집중 test: `27 passed`
- isolated dependency-complete build: `11 packages` 통과
- 최종 변경 package rebuild/import/entry point: 통과
- isolated overlay에서 Scenario 전체 source test: `690 passed`
- 기존 named-navigation façade 집중 회귀: `53 passed`
- source compile, 실제 credential·private path 검사와 `git diff --check`: 통과
  (credential 회귀 검사용 가짜 `ghp_...` canary 한 건은 의도된 test fixture다.)

참고로 변경하지 않은 `malbut_gazebo` 전체 source suite는 `358 passed`, `9 failed`였다.
실패는 현재 underlay에 없는 LiDAR/Gazebo plugin package, 오래된 generated
`FollowPerson` interface와 기존 launch contract에서 발생했으며 SWM25-149 변경 파일을
통과하는 실패는 없었다. 이번 laptop text Story의 gate에는 포함하지 않고, 관련
named-navigation 회귀와 isolated Scenario suite를 별도로 통과시켰다.

## 8. 직접 문장을 입력하는 Text Inspector

SWM25-149의 모호한 문장과 오타를 개발자가 직접 시험할 수 있도록 로컬 전용
`malbut_text_agent_inspector`를 함께 추가했다. Inspector는 별도의 축약된 판단기를
만들지 않고 `build_simulation_text_runtime -> TextTurnService` 제품 경로를 그대로
사용한다. 이 runtime에는 Action repository, worker, Robot Web과 Nav2 adapter가 없고
각 응답에서도 실행 authority와 start/cancel count가 false/0인지 다시 검사한다.

```bash
ros2 run malbut_scenarios malbut_text_agent_inspector -- \
  --env-file <private-path>/.env.local
```

기본 Provider는 ambient 환경과 관계없이 `mock`이다. 실제 자연어 모델을 시험할 때만
두 개의 명시적 옵션을 함께 사용한다.

```bash
ros2 run malbut_scenarios malbut_text_agent_inspector -- \
  --env-file <private-path>/.env.local \
  --provider openai --allow-live-provider
```

Inspector는 입력마다 다음 content-free allowlist 정보만 표시한다.

```text
입력 문자 수와 effective 발화의 변환 여부
Provider 호출 여부와 고정된 Provider 분류
모델 decision type, Tool과 허용된 argument key
서버 route와 Safety code
confirmation status/result code
RobotAction=0, Nav2 start/cancel=0
```

`/new`, `/stateful`, `/isolated`, `/history`, `/quit`를 지원한다. full effective 발화와
모델 decision은 Provider 경계를 관찰하는 한 turn 동안만 사용하고 즉시 버린다.
Inspector report와 `/history`에는 원문, 변환문, message, argument value, proposal과
conversation payload를 담을 필드 자체가 없다. 알 수 없는 Provider/model/Tool/argument
key도 고정 placeholder로 바꾼다. 대화 DB는 owner-only 임시 디렉터리에 만들고 정상
종료 시 삭제한다. prompt, credential, target digest, 좌표와 RobotState provenance는
출력하지 않는다.

설치된 entry point에서 `여기로 가줘 -> 거실 -> 네`와 `거실루 가줘`를 직접 입력해
effective 발화의 변환 여부, route, Tool, argument key, Safety와 effect 0을 확인했다.
`/quit` 뒤 Inspector process와 `malbut-text-inspector-*` 임시 디렉터리는 남지 않았다.
