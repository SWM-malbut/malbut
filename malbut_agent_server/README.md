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
- WAV 또는 짧은 push-to-talk 녹음을 로컬에서 인식하는 선택형 STT CLI
- final transcript만 받는 비실행 음성 대화 경계와 TTS 취소 계약
- 주입형 wake·STT·speech-output adapter로 반복 사이클을 검증하는
  비실행 연속 음성 상태 기계와 replay·barge-in fence
- `monitor_room(location)` 고수준 제안과 서버가 주입하고 구조·geometry를
  검증한 semantic Room 계획만
  받는 시뮬레이션 전용 mission 계약
- 최종 안전 응답을 제한된 visual cue로 바꾸는 비실행 감정 표현 정책

장기 기억 변경 core는 구현했지만 신뢰된 person identity와 확인 token이 필요한
공개 HTTP/ROS CRUD adapter는 열지 않았다. 실제 ROS 부작용 Tool 실행기도 후속
스토리에서 연결한다. 모델이 추론한 내용을 자동 저장하는 경로는 없다. 현재 서버는
`trusted_robot_state=False`, `MALBUT_AGENT_TOOL_MODE=proposal`이 기본이라
OpenAI 또는 Mock이 반환한 Tool 제안을 물리 실행하지 않는다.

## 테스트

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server
PYTHONPATH=. python3 -m pytest -q test
```

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
    "robot_state": {},
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
시뮬레이션만 처리한다. 이 설명은 `/v1/tools/query` 경계에 한정된다. 별도
`room_mission.py`는 simulation 전용 process-local 확인·Tool ID·취소·feedback을
제공하지만, 실제 행동과 재시작 뒤에도 유지되는 1회 소비는 여전히 없다.

현재 query cache는 프로세스 내 최대 256건으로 제한된다. adapter 응답
deadline이 지나도 이미 시작된 Python thread를 강제로 중단하지 못하므로,
73에서는 자체 I/O timeout이 있고 부작용이 없는 adapter만 연결한다.

`/v1/agent/respond`의 `execution.proposal_authorized`는 로컬 정책을 통과한
제안이라는 뜻일 뿐이다. `execution.authorized`와 `consume_once`는
SWM25-74 전까지 항상 `false`이고 `tool_call_id`는 `null`이다.

## 임시 로컬 STT 실행

`malbut-stt`는 WAV 파일 하나 또는 짧은 push-to-talk 녹음 하나를
`faster-whisper`로 로컬 변환하는 개발용 CLI다. 기본값은 한국어, multilingual
`base` 모델, CPU `int8`이며 스트리밍·wake word·화자 인식 기능은 없다.
Agent Server의 기본 설치를 무겁게 만들지 않도록 STT 엔진은 선택 의존성으로
분리했다.

Ubuntu에서 전용 가상환경을 준비한다.

```bash
sudo apt install python3-venv alsa-utils

cd ~/ros2_ws/src/malbut/malbut_agent_server
python3 -m venv .venv-stt
.venv-stt/bin/python -m pip install -e '.[stt]'
```

최초 한 번만 모델 다운로드를 명시적으로 허용해 5초 녹음을 인식한다. 모델을
먼저 준비한 뒤에 녹음을 시작하므로 dependency나 모델 로딩 실패 때 불필요한
음성을 수집하지 않는다.

```bash
.venv-stt/bin/malbut-stt \
  --microphone \
  --seconds 5 \
  --allow-model-download
```

모델이 로컬 cache에 생긴 뒤에는 네트워크 허용 옵션 없이 실행한다.

```bash
.venv-stt/bin/malbut-stt --microphone --seconds 5
```

이미 있는 PCM16 WAV도 사용할 수 있다. 입력은 1~2채널, 8~48 kHz,
1 ms~30초·6 MiB 이하여야 한다. 입력 경로의 파일을 추론 엔진이 다시 열지
않도록 동일 file descriptor에서 읽은 내용을 비공개 snapshot으로 만든 뒤,
그 snapshot을 검증하고 추론한다.

```bash
.venv-stt/bin/malbut-stt \
  --wav ./sample.wav \
  --allow-model-download
```

성공 시 stdout에는 transcript만 출력한다. 마이크 녹음은 권한 `0600`인 임시
파일에 저장했다가 정상적으로 cleanup 가능한 성공·실패·Ctrl-C 경로에서
삭제한다. 삭제를 재시도한 뒤에도 파일이 남으면 성공을 반환하지 않지만,
SIGKILL, 프로세스 crash, 파일시스템 오류나 전원 차단 뒤의 완전 삭제는
보장하지 않는다. 공급한 WAV는 삭제하지 않으며 추론용 snapshot은 처리 뒤
삭제한다. 원시 오디오와 파일 경로는 `SpeechTranscriptEvent`, HTTP,
대화 DB 또는 audit 결과에 넣지 않으며, confidence는 보정된 확률이 아닌
Whisper token/segment 점수 기반의 임시 휴리스틱이다.

이 CLI는 아직 Agent HTTP 서버나 ROS Topic에 자동 연결되지 않는다. 안전 경계로
넘길 때는 `build_transcript_event()`가 만든 final text와 제한된 오디오
metadata만 사용한다. 일반 WAV에서 만든 이벤트의 capture origin은 기본
`unknown`이라 coordinator가 차단한다. 신뢰된 마이크 adapter만 실제 capture
직후 `microphone` origin을 명시해야 한다. 자세한 범위는
[`SWM25-34 기본 로컬 STT`](docs/jira/SWM25-34_BASIC_LOCAL_STT.md)와
[`SWM25-76 음성 계약`](docs/jira/SWM25-76_VOICE_CONVERSATION_PIPELINE.md)을
참고한다.

### STT에서 Agent까지 한 번 실행

`malbut-voice-demo`는 마이크를 한 번만 녹음하고, 로컬 STT의 final
transcript를 기존 `SpeechConversationCoordinator`와 Agent Safety 경계로
보낸다. 기본은 네트워크를 쓰지 않는 Mock provider다.

```bash
.venv-stt/bin/malbut-voice-demo --microphone --seconds 5
```

OpenAI provider를 쓰려면 승인된 현재 사용자 소유의 mode `0600`
env 파일을 **명시적으로** 선택한다. process environment의 key나
암묵적 `.env.local` 탐색은 허용하지 않는다.

```bash
.venv-stt/bin/malbut-voice-demo \
  --microphone \
  --provider openai \
  --env-file ./.env.local \
  --agent-model gpt-5.6-terra \
  --seconds 5
```

이 demo는 `small`/CPU `int8`과 confidence `0.60`을 쓴다. 해당 임계값은
합성 한국어 음성 24개의 임시 결과로 선택한 대화 demo 전용값이며,
전역 음성 정책 `0.75`를 바꾸지 않는다. Tool은 빈 목록이고
로봇 상태는 untrusted라 이동·카메라·알림을 실행할 수 없다.
실제 TTS도 없으며, stdout에는 최종 Safety 응답 텍스트만
출력한다. terminal 기록과 redirect 파일에 민감한 응답이 남을 수
있으므로 공유 환경에서는 주의한다.

현재 측정과 실제 API 평가 범위는
[`SWM25-34·35 Live Voice 평가`](docs/evaluations/SWM25-34_35_LIVE_VOICE_EVALUATION_2026-08-15.md)에
분리해 두었다. 실제 마이크, 소음·self-echo, 스피커 TTS와 ROS
adapter는 아직 완료 증거가 아니다.

### 연속 음성·방 모니터링 오프라인 경계

`continuous_voice.py`는 신뢰된 adapter를 주입해 wake 하나에 final
transcript 하나만 처리하고, message·refusal·clarification은 TTS
terminal 후 `awaiting_wake`로 복귀한다. Tool 결정은 실행하지 않고
별도 confirmation 계층에 넘길 불변 제안만 반환한다. 실제 wake
detector, VAD, 마이크 stream과 speaker TTS adapter는 아직 없다.

`room_mission.py`는 user-map centroid를 목표로 승격하지 않고,
서버가 제공한 map-frame navigation goal과 coverage viewpoint만 검증한다.
현재 controller는 시뮬레이션 전용이며 controller-instance-local single active
lease와 process-local evidence는 남기지만 SQLite durable authorization이 아니다.
실제 Nav2·Homecam·KVS adapter를
등록하거나 “사이트에 생중계 중”이라고 보고할 수 없다. Safety의
`monitorable_locations` 기본값도 빈 집합이고 기본 factory가 plan-backed Room을
주입하지 않으므로 production 제안 경로는 safe-off다. 목표 상태기계와
실제 완료 조건은
[`SWM25-78 거실 라이브 모니터링`](docs/jira/SWM25-78_ROOM_LIVE_MONITORING_SCENARIO.md)에
분리했다.

`room_live_scenario.py`는 scripted wake/STT의 immutable proposal을 위
simulation controller에만 결속한다. 확인 전 adapter 호출은 0회이고,
verifier가 별도로 발급한 `TrustedConfirmation` 뒤에만 fake phase가 실행된다.
확인 대기와 mission 실행 중에는 wake source를 다시 호출하지 않으며 terminal
뒤에만 다음 cycle로 복귀한다. terminal은 `simulation_succeeded`,
`viewer_live=false`다. 이는 실제 음성, 이동, 카메라 또는 사이트 live 증거가
아니다.

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

이 검증은 실제 사람 인식, 연속 STT/TTS, ROS, frontend renderer 또는 운영
성능 시험을 대신하지 않는다. 현재 완료 범위와 blocker는 각 스토리 문서와
300회 반복 보고서에 분리해 두었다. 위 `malbut-stt`도 별도 one-shot 개발
adapter이며 이 300회 음성 계약 검사의 일부가 아니다.

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
- [SWM25-78 거실 이동·라이브 모니터링 시나리오](docs/jira/SWM25-78_ROOM_LIVE_MONITORING_SCENARIO.md)
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
