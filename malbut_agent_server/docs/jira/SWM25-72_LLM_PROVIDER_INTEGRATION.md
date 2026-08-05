# SWM25-72 LLM provider 통합

## 문서 정보

| 항목 | 내용 |
| --- | --- |
| Jira | SWM25-72 |
| 문서 목적 | 실제 LLM provider 연결의 구현 범위, 보안 경계, 장애 처리와 검증 근거 정리 |
| 작성 기준일 | 2026-08-05 |
| 대상 패키지 | `malbut_agent_server` 0.4.0 |
| 실제 LLM 범위 | **OpenAI Responses API만 지원** |
| 운영 기본 모델 | `gpt-5.6-terra` |
| 선택 fallback 모델 | `gpt-5.6-luna` |
| 현재 상태 | 구현 및 자동화 검증 완료, Terra를 운영 primary로 선정 |

> `mock`은 네트워크 없는 개발·회귀 테스트용 provider다. 실제 네트워크
> provider는 OpenAI 하나뿐이며, Luna fallback도 같은 OpenAI API, 자격 증명,
> 계정 한도를 공유한다. 따라서 이 구현은 다중 vendor 또는 다중 cloud
> 가용성을 제공하지 않는다.

## 1. 목표와 범위

SWM25-72는 SWM25-69의 대화·Tool 제안 계약과 SWM25-70·71의 세션·컨텍스트
처리를 유지하면서 실제 모델 호출을 연결한다. 모델 응답은 바로 로봇을
작동시키지 않고 기존 로컬 스키마 검증과 `SafetyPolicy`를 통과한 제안으로만
취급한다.

### 포함

- 공식 `https://api.openai.com/v1/responses`를 호출하는 OpenAI adapter
- Responses API function calling과 strict JSON text output의 공통
  `AgentDecision` 변환
- 입력 문자 수, 출력 token 수, timeout과 응답 body 크기의 상한
- 안전하게 정규화한 오류 분류
- 제한된 재시도, provider별 circuit breaker, 순서가 있는 모델 fallback
- 모든 원격 호출 실패 시 로컬에서 생성하는 비행동 refusal
- API key와 로컬 HTTP bearer token의 분리
- 내용 없는 provider 사용량·지연·컨텍스트 메트릭
- 고정 한국어 fixture의 실제 OpenAI 평가

### 제외

- Anthropic, Google, AWS Bedrock, Azure OpenAI 등 다른 vendor·배포면
- OpenAI-compatible proxy, 자체 호스팅 endpoint와 임의 base URL
- provider SDK 교체, streaming, Realtime API, 음성 입출력
- 서로 다른 계정·region·vendor를 이용한 재해 복구
- 모델이 제안한 ROS 2 Tool의 실제 실행
- `store: false`보다 강한 조직 수준 Zero Data Retention 보장
- 다중 프로세스에 공유되는 circuit state와 분산 rate limiter

`AgentProvider` 추상화는 이후 adapter를 추가할 수 있는 코드 경계일 뿐이다.
현재 `SUPPORTED_PROVIDERS`는 `mock`, `openai` 두 값이고, 그중 실제 LLM은
OpenAI만 구현되어 있다.

## 2. 구성과 처리 흐름

```text
로컬 HTTP 요청
  -> AgentRequest / 세션 / 사용자 격리 검증
  -> 최근 대화 + 요약 + 장기 기억을 제한된 입력으로 구성
  -> ReliableProvider
       -> OpenAIResponsesProvider(primary: gpt-5.6-terra)
            -> HTTPS POST api.openai.com/v1/responses
       -> 선택 OpenAIResponsesProvider(fallback: gpt-5.6-luna)
       -> 모두 실패하면 로컬 safe-non-action refusal
  -> ProviderResult와 AgentDecision 재검증
  -> 로컬 SafetyPolicy
  -> 대화 결과 저장 및 HTTP 응답
```

| 구성 요소 | 책임 |
| --- | --- |
| [`config.py`](../../malbut_agent_server/config.py) | 환경 변수 로딩, 범위 검증, 공식 origin 강제, 운영 기본값 |
| [`factory.py`](../../malbut_agent_server/factory.py) | primary·fallback adapter와 `ReliableProvider` 조립 |
| [`openai_responses.py`](../../malbut_agent_server/providers/openai_responses.py) | 요청 payload, HTTPS 전송, 응답 크기 제한, OpenAI 응답 파싱 |
| [`reliable.py`](../../malbut_agent_server/providers/reliable.py) | 오류 정규화, 재시도, circuit breaker, fallback, 안전 refusal |
| [`prompting.py`](../../malbut_agent_server/prompting.py) | 신뢰되지 않은 문맥 분리와 전체 입력 문자 상한 |
| [`orchestrator.py`](../../malbut_agent_server/orchestrator.py) | 세션·기억·provider·로컬 안전 정책의 실행 순서 |
| [`safety.py`](../../malbut_agent_server/safety.py) | 현재 턴 intent, Tool 인자, 로봇 상태를 이용한 fail-closed 검증 |

`build_provider()`는 네트워크 요청 없이 객체만 만든다. CLI의 `--check`도
설정과 저장소 초기화만 확인하고 OpenAI를 호출하지 않는다.

## 3. OpenAI Responses API 계약

adapter는 모든 호출에 다음 정책을 적용한다.

| 필드·동작 | 구현 계약 |
| --- | --- |
| endpoint | 공식 origin의 `POST /v1/responses`만 허용 |
| `model` | 설정된 primary 또는 fallback 모델 |
| `instructions` | 저장소가 소유하는 고정 system instruction |
| `input` | 현재 발화와 `_untrusted` 대화·요약·기억을 문자 예산 안에서 직렬화한 값 |
| `tools` | 현재 요청에서 허용된 Tool의 JSON schema만 전달 |
| `tool_choice` | Tool이 있을 때 `auto`, 없으면 필드 자체를 생략 |
| `parallel_tool_calls` | 항상 `false`; 한 응답에 Tool 호출 하나만 허용 |
| `text.format` | 비행동 답변을 strict JSON schema로 제한 |
| `reasoning.effort` | 기본 `none`, 설정으로 제한된 허용값만 선택 |
| `reasoning.context` | `current_turn`; 과거 대화 상태는 애플리케이션이 직접 전달 |
| `max_output_tokens` | 기본 500, 설정 범위 64~4,096 |
| `store` | **항상 `false`** |
| `safety_identifier` | 원래 사용자 ID 대신 SHA-256 기반 가명값 전달 |
| `X-Client-Request-Id` | 원래 로컬 요청 ID 대신 모델명과 ID의 SHA-256 기반 추적값 전달 |

응답 parser는 다음을 거부한다.

- 완료 상태가 아닌 응답
- 배열이 아닌 `output`
- 둘 이상의 function call
- function call과 terminal text가 섞인 응답
- JSON object가 아닌 Tool 인자
- strict text schema의 필드가 빠지거나 추가된 응답
- `NaN`, `Infinity` 같은 비유한 JSON 숫자
- 음수·불일치 token 수와 비정상 provider metadata
- 4 MiB를 넘는 response body

Tool 호출은 OpenAI 문서의 application-side 실행 흐름을 따르되, Malbut에서는
추가로 로컬 `SafetyPolicy`가 현재 턴의 의도와 실행 조건을 재검증한다. 관련
공식 설명은 [Function calling 흐름](https://developers.openai.com/api/docs/guides/function-calling#the-tool-calling-flow)과
[Responses 생성 API](https://developers.openai.com/api/reference/resources/responses/methods/create)를
참조한다.

## 4. 데이터와 보안 경계

### 4.1 `store: false`의 정확한 의미

모든 primary·fallback·재시도 요청은 같은 payload의 **`store: false`**를
사용한다. 애플리케이션은 OpenAI의 저장된 Response를 대화 상태로 사용하지
않으며, 필요한 최근 대화와 요약을 로컬 SQLite에서 다시 구성한다.

`store: false`를 다음과 같이 과장해서 해석하면 안 된다.

- 입력이 장치 밖으로 나가지 않는다는 뜻이 아니다. 입력은 OpenAI API로
  전송된다.
- 조직에 Zero Data Retention이 자동 적용된다는 뜻이 아니다.
- abuse monitoring, 조직별 데이터 제어, 법적 보존 정책까지 이 코드가
  결정한다는 뜻이 아니다.

즉 `store: false`는 Responses의 application state 저장을 사용하지 않기 위한
요청 설정이고, 더 강한 보존 보장은 별도의 OpenAI 조직 설정과 계약을
확인해야 한다. endpoint별 보존 조건은 OpenAI의
[Data controls 문서](https://developers.openai.com/api/docs/guides/your-data#v1responses)를
운영 시점에 다시 확인한다.

### 4.2 자격 증명과 네트워크

- `OPENAI_API_KEY`는 환경 또는 로컬 env 파일에서만 읽고 CLI 인자로 받지
  않는다.
- `Settings`와 `OpenAIResponsesProvider`의 `repr`은 key와 로컬 bearer token을
  `<redacted>`로 표시한다.
- base URL은 scheme `https`, host `api.openai.com`, 기본/443 port, path `/v1`인
  경우만 허용한다. 사용자 정보, query, fragment도 거부한다.
- redirect를 따르지 않아 `Authorization` header가 다른 origin으로 전달되지
  않는다.
- 서버는 loopback 주소에서만 실행한다. OpenAI 모드는 별도의
  `MALBUT_AGENT_AUTH_TOKEN` 없이는 시작하지 않는다.
- 운영 API key와 로컬 HTTP bearer token은 서로 다른 자격 증명이다.

API key는 저장소나 이미지에 넣지 않고 배포 환경의 secret manager로
주입해야 한다. 현재 코드는 secret manager 생성·회전·폐기 자체를 구현하지
않는다.

### 4.3 프롬프트와 출력

- 저장된 대화·요약·기억은 `_untrusted` JSON 데이터로 system instruction과
  분리한다.
- 전체 모델 입력은 기본 20,000자로 제한한다.
- raw 사용자 ID와 로컬 요청 ID 대신 단방향 가명값만 OpenAI header/body에
  보낸다.
- provider 오류는 내용 없는 범주로 정규화하고 사용자에게 원문 오류나
  credential을 반환하지 않는다.
- 모델의 Tool 이름·인자·결정과 provider metadata를 다시 검증한다.
- 운영 orchestrator는 외부에서 받은 robot state를 신뢰하지 않는 설정으로
  생성된다.
- 모든 provider가 실패하면 Tool이 없는 로컬 refusal을 반환한다.

이 계층은 모델 출력이 직접 모터나 ROS action을 실행하지 못하게 한다. 실제
Tool 실행기는 별도의 1회 소비 승인, timeout, 취소와 결과 계약을 가져야 한다.

## 5. 재시도, circuit breaker와 fallback

### 5.1 오류 분류와 재시도

| 오류 범주 | 예 | 재시도 | circuit 반영 |
| --- | --- | --- | --- |
| transient | timeout, network, 408·409·425, 429, 5xx | 제한 안에서 재시도 | 예 |
| authentication | 401, 403 | 재시도하지 않음 | 예, fallback도 중단 |
| invalid request | 대부분의 다른 4xx | 재시도하지 않음 | 아니요 |
| invalid response | schema·JSON·metadata 불일치 | 재시도하지 않음 | 예 |
| unknown | 분류할 수 없는 오류 | 재시도하지 않음 | 아니요 |

운영 기본값은 지연을 예측할 수 있도록 추가 재시도를 0회로 둔다. 설정으로
재시도를 켜면 delay는 250 ms에서 시작해 2배씩 증가하고 1,000 ms 상한을
넘으면 더 기다리지 않는다. 숫자형
`Retry-After`가 있으면 이를 최소 대기 시간으로 존중하지만, 설정한 최대
delay보다 크면 음성 UX를 오래 막지 않고 해당 모델 시도를 종료한다.

각 OpenAI 시도의 timeout은 기본 5초이고 primary, 대기와 fallback을 합한
provider 스케줄링 예산은 11초다. 다음 시도를 온전히 수행할 5초가
남지 않으면
재시도나 다음 모델을 시작하지 않고 안전 refusal로 종료한다. 재시도 횟수를
늘려도 이 전체 예산을 우회하지 못한다.

이 값은 새 시도를 시작하지 않게 하는 예산이지 process를 강제 취소하는
hard wall-clock deadline은 아니다. 현재 `urllib` 호출의 DNS·socket 동작이
개별 5초 timeout을 초과하면 전체 실측도 11초를 초과할 수 있다. 운영 hard
deadline에는 취소 가능한 HTTP client·실행 모델이 추가로 필요하다.

OpenAI는 rate limit 재시도에 지수 backoff와 작은 random jitter를 권장한다.
현재 구현은 bounded exponential backoff와 `Retry-After`는 지원하지만
**jitter는 구현하지 않았다**. 동시 robot이 늘어나기 전 jitter를
추가하고, 11초 스케줄링 예산이 실제 부하에서도 지켜지는지 검증해야
한다. 공식 기준은
[Rate limits 문서](https://developers.openai.com/api/docs/guides/rate-limits#retrying-with-exponential-backoff)와
[API errors 문서](https://developers.openai.com/api/docs/guides/error-codes#api-errors)를
따른다.

재시도는 provider-side exactly-once를 보장하지 않는다. 첫 요청이 처리된 뒤
응답만 유실되면 다음 시도가 별도의 생성과 비용을 만들 수 있다.
`X-Client-Request-Id`는 추적용이며 idempotency 보장으로 간주하지 않는다.
Malbut 세션의 내구성 있는 `request_id` 처리는 완료 결과의 클라이언트
재전송을 줄이지만 이 네트워크 모호성을 없애지는 않는다.

### 5.2 circuit breaker

- circuit은 provider adapter, 즉 모델별로 프로세스 메모리에 하나씩 있다.
- 기본 2회의 연속 request-level 실패 후 `open`이 된다.
- `open` 동안 해당 모델을 건너뛰고 다음 모델 또는 안전 refusal로 간다.
- 기본 30초 뒤 한 요청만 `half_open` probe로 허용한다.
- probe 성공 시 `closed`가 된다. circuit에 영향을 주는 실패면 다시
  `open`으로 돌아가고, invalid request처럼 circuit 비대상 실패면 닫는다.
- 성공은 연속 실패 수를 0으로 초기화한다.

프로세스 재시작 시 상태가 사라지고 worker끼리 공유되지 않는다. 따라서
circuit은 짧은 장애의 연쇄 호출을 줄이는 로컬 보호 장치이지 분산 장애
조정기가 아니다.

### 5.3 모델 fallback과 한계

운영 primary는 평가에서 모든 formal deployment gate를 통과한
`gpt-5.6-terra`다. `OPENAI_FALLBACK_MODEL`을 설정하면 lower-cost
`gpt-5.6-luna`를 두 번째로 시도할 수 있다. 둘 다 동일한 입력 계약과 로컬
안전 검증을 사용한다.

이 fallback은 모델별 일시 오류나 circuit open에는 도움이 될 수 있지만
**same-vendor fallback**이다. 다음 장애는 두 모델이 함께 겪을 가능성이 높다.

- 같은 API origin의 네트워크·서비스 장애
- 같은 API key·project·organization의 인증 실패
- 공유 rate limit, quota, billing 또는 spend limit
- 공통 정책 차단과 계정·region 가용성 문제

특히 인증 오류는 동일 credential 문제일 가능성이 높으므로 구현도 Luna로
넘어가지 않고 즉시 안전 refusal로 종료한다. Luna fallback을 켰다고
multi-vendor 고가용성 또는 재해 복구가 달성됐다고 표시해서는 안 된다.

## 6. 설정

| 환경 변수 | 코드 기본값 | 허용 범위·운영 의미 |
| --- | --- | --- |
| `MALBUT_AGENT_PROVIDER` | `mock` | 실제 호출은 `openai`로 명시 |
| `OPENAI_API_KEY` | 없음 | OpenAI 모드 필수, CLI 전달 금지 |
| `OPENAI_MODEL` | `gpt-5.6-terra` | 운영 primary |
| `OPENAI_FALLBACK_MODEL` | 빈 값 | 선택 사항; 제한된 Luna fallback·실험에 사용 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 이 공식 origin만 허용 |
| `OPENAI_REASONING_EFFORT` | `none` | `none`, `low`, `medium`, `high`, `xhigh`, `max` |
| `OPENAI_MAX_OUTPUT_TOKENS` | 500 | 64~4,096 |
| `MALBUT_AGENT_MAX_MODEL_INPUT_CHARS` | 20,000 | 4,096~1,000,000 |
| `MALBUT_AGENT_TIMEOUT_SECONDS` | 5초 | 1~120초, OpenAI 시도 하나의 timeout |
| `MALBUT_AGENT_PROVIDER_TOTAL_TIMEOUT_SECONDS` | 11초 | 1~300초, attempt timeout 이상 |
| `MALBUT_AGENT_PROVIDER_MAX_RETRIES` | 0 | 0~3, 최초 호출 제외 재시도 횟수 |
| `MALBUT_AGENT_PROVIDER_RETRY_BASE_DELAY_MS` | 250 ms | 0~5,000 ms |
| `MALBUT_AGENT_PROVIDER_RETRY_MAX_DELAY_MS` | 1,000 ms | 0~10,000 ms, base 이상 |
| `MALBUT_AGENT_PROVIDER_FAILURE_THRESHOLD` | 2 | 1~20회 |
| `MALBUT_AGENT_PROVIDER_RECOVERY_TIMEOUT_SECONDS` | 30초 | 1~3,600초 |
| `MALBUT_AGENT_AUTH_TOKEN` | 없음 | OpenAI 모드의 로컬 HTTP 인증에 필수 |

예시는 실제 secret 값을 문서나 명령 이력에 넣지 않는다.

```dotenv
MALBUT_AGENT_PROVIDER=openai
MALBUT_AGENT_AUTH_TOKEN=<secret-manager에서 주입>
OPENAI_API_KEY=<secret-manager에서 주입>
OPENAI_MODEL=gpt-5.6-terra
OPENAI_FALLBACK_MODEL=gpt-5.6-luna
OPENAI_BASE_URL=https://api.openai.com/v1
```

네트워크 호출 없이 설정을 확인한다.

```bash
cd malbut_agent_server
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --provider openai \
  --check
```

## 7. 검증

### 자동화 테스트

다음 영역을 네트워크 없는 테스트로 검증한다.

- Responses payload의 `store: false`, strict schema와 단일 Tool 계약
- Tool call·structured text·refusal의 정상 변환과 비정상 출력 거부
- 비공식 origin, redirect와 credential 노출 차단
- timeout·429·5xx와 비재시도 오류의 분류
- bounded backoff, `Retry-After`, circuit open·half-open·복구
- 인증 실패 시 same-vendor fallback 중단
- 전체 실패 시 내용 없는 safe non-action refusal
- 설정 상한, loopback bind, API key·HTTP auth 필수 조건
- evaluator의 고정 suite, 최소 반복 수, redaction과 파일 권한
- evaluator의 attempt timeout·호출 간격 메타데이터 기록
- provider 장애 뒤에도 HTTP 서버가 안전 refusal을 반환하고 계속 동작하는지

재현 명령:

```bash
cd malbut_agent_server
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
python3 -m pytest -q -p no:cacheprovider test
```

### 실제 OpenAI 평가

30개 고정 case를 두 모델에 각각 3회 실행해 총 180회를 비교했다. fallback은
비활성화하여 어느 모델이 결과를 만들었는지 섞이지 않게 했다. 현재
다섯 개의 formal deployment gate로 상세 결과를 재검산했을 때 Terra는
모두 통과했다. Luna baseline은 예기치 않은
행동 승인 1건으로 gate 하나를 통과하지 못했다.

그 원인이 된 과도한 로컬 위치 오타 alias를 제거하고 fail-closed 단위
테스트를 추가했다. 수정 후 같은 안전 경계를 Luna에 3회 재검증한 결과 실제
행동 승인은 0건이었지만, 기대한 대화 결정까지 맞춘 것은 1/3회였다. 따라서
안전 회귀는 차단됐지만 Luna 품질 안정성이 입증된 것은 아니다.

상세 수치와 판정은
[`SWM25-72 OpenAI 평가`](../evaluations/SWM25-72_OPENAI_EVALUATION_2026-08-05.md)에
기록한다.

## 8. 운영 판정

1. **Terra를 production primary로 사용한다.** 180회 baseline에서 모든
   deployment gate를 통과했고 현재 코드 기본값도 이에 맞췄다.
2. **Luna는 lower-cost fallback 또는 실험군으로만 제한한다.** 항상 동일한
   로컬 safety wrapper를 적용하고, post-fix 전체 suite 재실행과 품질
   모니터링을 거친 뒤 범위를 넓힌다.
3. **Luna fallback을 고가용성으로 계산하지 않는다.** OpenAI의 공통 장애,
   인증, quota와 rate limit을 공유한다.
4. **`store: false`를 유지한다.** 이 설정을 제거하는 변경은 별도의 privacy
   검토와 회귀 테스트가 필요하다.
5. **모델 출력은 계속 non-actuating으로 취급한다.** 실제 ROS 실행은 로컬
   안전·승인 계층의 별도 책임이다.

## 9. 남은 위험과 후속 작업

1. Luna를 포함한 post-fix 180회 전체 평가를 다시 실행한다.
2. retry backoff에 jitter를 추가하고 11초 스케줄링 예산을
   부하·chaos test로 검증하며, 취소 가능한 hard deadline을 도입한다.
3. provider·모델별 retry, fallback, circuit state와 safe refusal 비율을 원문
   없이 구조화해 관측한다.
4. 여러 worker를 운영하기 전에 공유 rate limiting과 circuit 전략을 정한다.
5. 실제 로봇 Tool 실행 전 confirmation, 1회 소비, timeout, 취소와 감사 로그를
   end-to-end로 검증한다.
6. 조직의 OpenAI 데이터 제어와 API key rotation 정책을 배포 checklist에
   연결한다.
7. vendor 독립 장애 복구가 요구되면 별도 스토리에서 다른 provider와
   credential·network failure domain을 설계한다.

## 10. 공식 참고 자료

- [OpenAI Responses 생성 API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling#the-tool-calling-flow)
- [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data#v1responses)
- [OpenAI rate limits와 exponential backoff](https://developers.openai.com/api/docs/guides/rate-limits#retrying-with-exponential-backoff)
- [OpenAI API error codes](https://developers.openai.com/api/docs/guides/error-codes#api-errors)
- [OpenAI models](https://developers.openai.com/api/docs/models)
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
