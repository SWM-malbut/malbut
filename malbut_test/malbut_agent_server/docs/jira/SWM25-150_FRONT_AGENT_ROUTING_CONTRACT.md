# SWM25-150 Front Agent가 요청을 다섯 종류로 분기하는 계약을 만든다

## 1. 결론

SWM25-150은 답변까지 생성하는 원격 Front LLM이 아니라, 빠른 Router가 요청을
다섯 종류 중 하나로 고확신 분류하거나 내부적으로 abstain하는 pure 계약을 만든다.

```text
사용자 텍스트
  -> 기존 confirmation / replay / SWM25-149 fast path
  -> FrontRouterPort.try_route()
     |- FrontRouteMatch(route)  -> 선택된 handler/Provider
     `- None                    -> 기존 강한 범용 Provider
```

공개 route는 다음 다섯 개뿐이다.

```text
general_conversation
clarification_required
robot_status_query
current_action_query
robot_action_request
```

`None`은 여섯 번째 route가 아니라 서버 내부의 정상적인 "빠른 분류 포기"다. 외부
JSON의 `abstain`, `null`, confidence, 응답 문장, Tool과 실행 권한은 모두 거절한다.

이번 작업은 실제 Router, Chat Provider, Robot Planner 또는 production TextTurn을
연결하지 않는다. 따라서 제품 API·DB·ROS runtime과 사용자가 보는 동작은 바뀌지
않는다. 계약을 사람이 직접 확인할 수 있도록 실제 OpenAI 5-way 후보를 보여주는
`observe-only` Inspector를 추가했지만, 그 후보는 `FrontRouteMatch`로 승격되지 않는다.
`parse_front_route_match()`의 strict 검증 과정에서 내부 임시 객체는 생성되지만 외부
결과로 반환하거나 production `FrontRouterPort`에 주입하지 않는다.

## 2. 변경 이유

기존 초안은 Front 결과에 `route + response_text`를 함께 넣었다. 이 구조를 그대로
연결하면 로봇 행동 요청에서 다음 두 원격 호출이 직렬로 발생할 수 있다.

```text
Front LLM이 분류·문장 생성
  -> Robot Planner LLM이 ActionProposal 생성
```

현재 Orchestrator는 Provider 호출 동안 요청을 직렬화하므로 두 호출은 해당 요청뿐
아니라 대기 중인 다른 요청의 latency도 늘린다. 또한 기존 초안의 4096자 Front
응답은 기존 `AgentDecision`과 conversation 저장 한도 2000자보다 커 persistence에서
실패할 수 있었다.

따라서 SWM25-150을 다음처럼 축소했다.

```text
Front Router = 분류만 수행
선택된 Provider = 답변 또는 제안 생성
Malbut 서버 = Safety·승인·실행 권한 소유
```

이 구조의 후속 목표 호출 수는 다음과 같다.

| route | 후속 처리 | 원격 모델 목표 |
|---|---|---:|
| 일반 대화 | 빠른 Chat Provider | 1회 |
| clarification | 서버 질문 또는 전문 질문 | 0~1회 |
| 로봇 상태 | trusted state handler | 0회 |
| 현재 작업 | durable action read model | 0회 |
| 로봇 행동 | 기존 강한 Planner | 1회 |
| 내부 abstain | 기존 강한 범용 Provider | 1회 |

## 3. 목표

- 사용자 요청 종류를 모델·adapter 구현과 독립된 Python 3.10 타입으로 고정한다.
- 고확신 fast route와 정상적인 내부 abstain을 구분한다.
- Router가 답변·Tool·confidence·RobotState·실행 권한을 만들지 못하게 한다.
- 요청당 Router dependency 호출을 최대 한 번으로 고정하고 retry·fallback을 하지
  않는다.
- 후속 Story가 일반 대화·로봇 행동·불확실한 요청을 원격 모델 최대 한 번으로
  처리할 수 있는 경계를 제공한다.

## 4. 달성 조건 5개

1. [x] 공개 `FrontRoute`는 정확히 다섯 개이며 `abstain`은 enum과 wire result에서
   모두 거절한다.
2. [x] `FrontRouteMatch`는 route 한 필드만 가지며 응답·Tool·confidence·승인·상태·
   Action·ROS goal 필드가 없다.
3. [x] `FrontRouteRequest`는 128자 request ID, 최대 2000자 현재 문장과 bounded
   user/assistant history만 가진다.
4. [x] application service는 Router를 요청당 최대 한 번 호출하고 `None`을 정상
   abstain으로 반환하며 exception·wrong type에서 retry나 fallback을 수행하지 않는다.
5. [x] raw JSON duplicate key·unknown route·extra/authority field·non-finite value를
   거절하고 Stage A dependency rule과 전체 회귀 test를 통과한다.

### 4.1 수동 실험 도구 추가 판정

사용자가 직접 다양한 한국어 문장을 시험해 볼 수 있도록 다음 실행기를 추가했다.

```text
stdin 문장
  -> OpenAI strict 5-way candidate 1회
  -> candidate_route와 latency 표시
  -> production_route=None
  -> RobotAction=0, Nav2=0
```

이 실행기는 분류 후보 관찰용이지 production Router adapter가 아니다. 모델의 결과를
고확신이라고 간주하거나 production `FrontRouterPort`에 주입하지 않는다. 실제 승격
threshold와 정확도 평가는 여전히 SWM25-152가 담당한다.

Inspector leaf import가 기존 Orchestrator와 SQLite adapter를 eager load하지 않도록
root/outbound compatibility export를 lazy façade로 바꿨다. 기존 import 경로와 실제
class symbol identity는 그대로 유지한다.

## 5. 계약 구조

### 5.1 입력

```text
FrontRouteRequest
  request_id: opaque correlation, 최대 128자
  user_message: 현재 요청, 최대 2000자
  recent_messages: immutable tuple, 최대 16개

FrontMessage
  role: user | assistant
  content: untrusted history, 메시지당 최대 300자·전체 최대 4000자
```

Router에는 다음 입력을 주지 않는다.

```text
system role
user credential
RobotState
ToolSpec
DB·ROS 객체
confirmation·Action·goal identifier
physical authority
```

Router history는 의도 파악을 위한 신뢰되지 않은 데이터다. 선택된 Chat/Planner의
실제 history·summary·memory는 기존 Provider 경계에서 별도로 전달한다. 이번 Front
DTO에 같은 ContextBuilder와 telemetry 타입을 복제하지 않는다.

### 5.2 출력과 abstain

```text
FrontRouteMatch
  route: FrontRoute

FrontRouterPort.try_route(request)
  -> FrontRouteMatch | None
```

`FrontRouteMatch`는 고확신 분류 결과다. `None`은 서버가 관리하는 abstain이며 JSON
결과가 아니다. Router의 score·threshold·artifact revision은 실제 local classifier를
연결하는 SWM25-152의 adapter/config 책임으로 남긴다. 모델이 자기 confidence를
출력한다고 해서 고확신 route로 인정하지 않는다.

### 5.3 strict raw JSON

향후 격리 sidecar나 model adapter가 사용하는 raw wire shape는 다음 하나다.

```json
{"route":"robot_action_request"}
```

`parse_front_route_match()`은 UTF-8 transport가 정상적으로 decode한 문자열의 size를
제한하고 JSON duplicate key와 NaN·Infinity를 Python `dict`로 덮어쓰기 전에
거절한다. invalid UTF-8 byte 거절은 실제 adapter의 decode 경계에서 수행한다. 이후
strict decoder는 `route` 외 필드가 하나라도 있으면 실패한다.

## 6. 기존 정책과의 관계

새 `FrontRoute`와 기존 `TextDecisionRoute`는 서로 다른 경계다.

```text
FrontRoute
  Provider 선택 전
  어떤 handler/Provider가 요청을 처리할지 고름

TextDecisionRoute
  Planner/Provider 결과 후
  message·clarification·read-only·action proposal의 권한 정책 검증
```

예를 들어 `거실로 가줘`의 후속 production 흐름은 다음과 같다.

```text
FrontRoute.ROBOT_ACTION_REQUEST
  -> 기존 Robot Planner가 navigate(location="거실") 제안
  -> TextDecisionPolicy의 Tool·arguments 검사
  -> LLM 이후 fresh RobotState
  -> deterministic Safety
  -> confirmation
```

Front Route만으로 Tool 호출이나 confirmation을 만들 수 없다.

## 7. 논문·사례 판정

- Anthropic Routing과 OpenAI triage/handoff 사례처럼 route와 전문 handler를 분리한다.
- RouteLLM·Arch-Router처럼 route 정확도와 비용은 실제 분포로 측정하며 enum 존재를
  성능 증거로 취급하지 않는다.
- MAC 원칙에 따라 Front는 전역 모호성, 전문 Agent는 domain slot 부족을 담당한다.
- CLARA의 ambiguous/infeasible 구분을 참고하되 물리 가능성은 Front가 아니라
  post-LLM fresh state와 deterministic Safety가 판단한다.
- CaMeL의 control/data separation 원칙에 따라 Router 결과에 실행 capability를 넣지
  않는다.

관련 자료:

- https://www.anthropic.com/engineering/building-effective-agents
- https://openai.github.io/openai-agents-python/multi_agent/
- https://arxiv.org/abs/2406.18665
- https://arxiv.org/abs/2506.16655
- https://aclanthology.org/2025.iwsds-1.7.pdf
- https://aclanthology.org/anthology-files/anthology-files/pdf/iwsds/2026.iwsds-1.1.pdf
- https://arxiv.org/abs/2306.10376
- https://arxiv.org/abs/2503.18813

## 8. Story 인계

### SWM25-151

- 기존 Provider 위치에 `RoutedAgentProvider`를 연결한다.
- `begin_turn`과 cached replay 확인 이후에만 Router를 실행한다.
- match된 route는 해당 handler/Provider로, abstain은 기존 범용 Provider로 넘긴다.
- persisted history에 빈 assistant content가 있으면 bounded FrontMessage로 만들지 않고
  projection 단계에서 건너뛴다.
- request ID는 모델 prompt나 일반 로그에 넣지 않고 transport correlation에는 hash만
  사용한다.
- 한 logical request의 선택된 원격 Provider 호출이 최대 한 번인지 검증한다.
- 준비되지 않은 status/current-action route는 추측하지 않고 bounded unavailable로
  처리한다.

### SWM25-152

- 실제 deterministic/local classifier와 server-owned threshold를 구현한다.
- 빠른 Chat 모델과 강한 Robot Planner 모델을 분리한다.
- 고정 한국어 route corpus로 precision·recall·action false-positive를 측정한다.
- route별 p50/p95 latency, token, 비용, abstain/fallback 비율을 측정한다.
- logical Provider 1회뿐 아니라 실제 HTTP retry/fallback 호출 수도 검증한다.
- 이번 observe-only Inspector의 candidate를 고정 corpus에서 평가하고, server-owned
  threshold를 통과한 경우에만 `FrontRouteMatch`로 승격하는 adapter를 구현한다.

## 9. 수동 실험 방법

실험기는 입력 문장을 command argument로 받지 않는다. shell history와 process list에
원문을 남기지 않도록 stdin에서만 읽는다. 입력은 OpenAI API로 전송되지만 이 도구는
원문을 파일이나 일반 로그에 저장하지 않으며 request payload는 `store=false`다.
API key도 argument로 받지 않는다.

source worktree 실행:

```bash
cd <SWM25-150-worktree>/malbut_agent_server
PYTHONPATH=. python3 -m malbut_agent_server.front_route_inspector \
  --allow-live-provider \
  --env-file <private-agent-server>/.env.local \
  --model gpt-4.1-mini \
  --timeout-seconds 5
```

설치 후 실행:

```bash
ros2 run malbut_agent_server malbut-front-route-inspect \
  --allow-live-provider \
  --env-file <private-agent-server>/.env.local \
  --model gpt-4.1-mini \
  --timeout-seconds 5
```

종료 명령은 `/quit`이다. 출력은 입력 원문·prompt·raw response·credential을 포함하지
않고 다음처럼 후보와 content-free 측정값만 보여준다.

```text
candidate_route : robot_action_request
production_route: -
outcome         : candidate_observed
promoted        : false
latency_ms      : 755.583
classifier_calls: 1
authority       : false
error_code      : none
```

`--json`을 추가하면 같은 정보를 JSONL로 출력한다. `--check`는 key·model·endpoint
설정만 확인하고 network call은 0회다. live 요청은 명시적인
`--allow-live-provider` 없이는 시작되지 않는다.

여기서 `production_route: -` 또는 JSON `null`은 calibrated abstain이 아니라 아직
승격 평가 자체를 하지 않았다는 뜻이다. `--timeout-seconds`도 hard wall-clock
deadline이 아니라 blocking network I/O timeout이며 production deadline과 threshold는
SWM25-152에서 별도로 검증한다.

## 10. 의도적으로 하지 않은 것

- `text_turn.py`, Orchestrator, HTTP, SQLite schema와 production factory를 변경하지
  않았다.
- 실제 규칙·embedding·로컬 모델 기반 production Router를 만들지 않았다.
- OpenAI candidate 실험기는 만들었지만 `FrontRouterPort`, Chat Provider 또는 Robot
  Planner adapter로 연결하지 않았다.
- `get_robot_status`, `get_current_action`을 실행하지 않았다.
- RobotState·Safety·confirmation·RobotAction·Nav2를 호출하지 않았다.
- 수동 smoke latency는 관찰했지만 production latency SLO나 route 정확도 수치로
  간주하지 않는다.

## 11. 검증 결과

- Front Router pure contract focused test: `58 passed`
- Front Router + SWM25-149/권한 집중 regression: `161 passed`
- 전체 Agent Server source regression: `544 passed`
- 새 production/test 파일 pycodestyle·pyflakes·pydocstyle: 통과
- Python 3.10 compile/import smoke와 `git diff --check`: 통과
- clean 임시 colcon install의 `malbut-front-route-inspect --help/--check`: 통과,
  check network call `0회`

observe-only Inspector 추가 검증:

- Front 계약 + candidate adapter + Inspector focused test: `125 passed`
- 실제 `gpt-4.1-mini` forced 5-way smoke: 공개 route 5종 `5/5` 반환
- 별도 사투리형 이동 문장: `robot_action_request`, `755.583ms`
- 2초 socket-timeout 설정의 cold attempt: `provider_timeout`, 관찰 경과 약 `2.065초`,
  retry `0회`, production route `0개`
- 5-way smoke latency 범위: `818.983~2861.563ms`
- 모든 live 결과: `promoted=false`, `authority=false`, RobotAction/Nav2 `0회`
- `여기로 가줘`는 grounding 예시 추가 전 action 후보가 한 번 관찰되어 production
  승격 금지 필요성을 확인했다. 명시 규칙 추가 후 반복 smoke `3/3`에서
  `clarification_required`였으며 latency는 `819.099~1580.070ms`였다. 이는 수동
  관찰일 뿐 정확도 KPI나 production threshold 통과 증거가 아니다.
