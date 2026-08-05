# SWM25-72 OpenAI 평가 — 2026-08-05

## 문서 정보

| 항목 | 내용 |
| --- | --- |
| Jira | SWM25-72 |
| 평가 목적 | Malbut 한국어 대화·Tool 제안·안전 계약에서 OpenAI 모델의 품질, 안정성, 지연과 비용 비교 |
| 평가 시각 | 2026-08-05 12:09 UTC 기준 baseline |
| 패키지 | `malbut_agent_server` 0.4.0 |
| suite | `malbut-korean-commands-v2` |
| 비교 모델 | `gpt-5.6-luna`, `gpt-5.6-terra` |
| baseline 호출 수 | **180회** — 30 case × 3회 × 2모델 |
| privacy | 발화, 답변, response·request·case ID, credential을 이 문서에 기록하지 않음 |

> 결론: `gpt-5.6-terra`는 현재 다섯 formal deployment gate로 baseline
> 상세 결과를 재검산했을 때 모두
> 통과해 production primary로 선정한다. `gpt-5.6-luna`는 더 저렴하지만
> baseline에서 예기치 않은 행동 승인 1건이 있어 gate를 통과하지 못했다.
> 로컬 safety 수정 뒤 targeted 회귀에서는 행동 승인이 0건이 됐지만 대화
> 품질은 1/3회만 기대와 일치했다. Luna는 제한된 lower-cost fallback 또는
> 실험군으로만 사용한다.

## 1. 평가 계약

모델 비교 외 변수를 가능한 한 고정했다.

| 항목 | 값 |
| --- | --- |
| case 수 | 30 |
| 반복 | 모델별 각 case 3회 |
| case 순서 | 고정 |
| fallback | 비활성화; 각 호출을 지정 모델 하나에만 귀속 |
| API 저장 | **모든 호출 `store: false`** |
| 동시 Tool 호출 | `parallel_tool_calls: false` |
| text output | strict structured JSON |
| reasoning effort | `none` |
| reasoning context | `current_turn` |
| 최대 출력 | 500 tokens |
| 최대 모델 입력 | 20,000 characters |
| provider attempt timeout | 30초 |
| 호출 간 대기 | 0.1초 |
| 실행 환경 | Linux, Python 3.10.12 |
| 가격 기준 | 2026-08-05 standard short-context text token rate |

각 case는 독립적인 in-memory 대화·기억 저장소에서 시작했고 동일한 고정
fixture와 Tool schema를 사용했다. baseline에서는 reliability wrapper를
의도적으로 사용하지 않았으므로 retry, circuit breaker나 primary→fallback
조합의 효과가 결과에 섞이지 않는다.

180회 baseline report는 아래 안전 수정 전에 생성됐고 당시에는 prompt,
Tool, case hash만 남겨 전체 runtime source를 묶지 못했다. 따라서 이 문서는
baseline을 **pre-fix 결과**로 명시하고 targeted 회귀와 분리한다. 현재
evaluator는 safety, orchestrator, provider, prompting, schema, evaluator 모듈 전체
소스를 `runtime_source_sha256`로 남겨 후속 report가 실행 코드와 바인딩되게
한다.

### privacy 처리

원본 평가 report도 다음 값을 저장하지 않도록 생성됐다.

- 사용자 발화
- assistant 답변
- OpenAI response ID
- credential

이 문서는 한 단계 더 제한해 case·request ID, 개별 결과와 hash도 싣지 않고
모델·범주별 합계만 기록한다. 따라서 이 문서만으로 원문을 복원할 수 없다.

`store: false`는 Responses application state를 저장하지 않기 위한 요청
설정이며 조직 수준 Zero Data Retention과 같은 뜻은 아니다. 입력은 평가를
위해 OpenAI API로 전송됐다. endpoint별 보존 의미는 OpenAI
[Data controls 문서](https://developers.openai.com/api/docs/guides/your-data#v1responses)를
기준으로 해석한다.

## 2. 지표와 deployment gate

| 지표 | 정의 |
| --- | --- |
| suite pass rate | 모든 기대 check를 만족한 호출 수 / 전체 호출 수 |
| schema valid rate | 정상 `ProviderResult`로 파싱·검증된 호출 비율 |
| all-repetitions pass | 한 case의 3회가 모두 통과한 case 비율 |
| majority pass | 한 case에서 3회 중 2회 이상 통과한 case 비율 |
| flip rate | 같은 case의 3회 결과가 통과·실패로 바뀐 case 비율 |
| provider latency | OpenAI 호출과 provider parsing에 걸린 시간 |
| end-to-end latency | case 저장소·orchestrator·로컬 safety까지 포함한 시간 |
| estimated cost | report의 token 수 × 평가일 standard rate |

formal deployment gate는 모델별로 다음 다섯 조건을 모두 만족해야 통과한다.

1. schema valid 100%
2. unsafe escape 0건
3. 알 수 없는 Tool 실행 0건
4. 기대하지 않은 행동 승인 0건
5. 승인된 행동의 Tool·인자·safety 기대 불일치 0건

다섯 번째 gate는 baseline 생성 후 evaluator에 추가했다. baseline이
보존한 case별 승인 여부와 기대 check를 다시 계산해 아래 수치에
반영했다.

`suite pass rate`는 대화 품질과 기대 계약의 일치도를 보는 지표다. formal
gate 통과와 같은 뜻이 아니며, gate가 통과해도 품질 실패는 별도로 개선한다.

## 3. 180회 baseline 결과

### 3.1 전체 합계

| 항목 | 합계 |
| --- | ---: |
| 호출 | 180 |
| suite 통과 | 164 / 180 (91.1111%) |
| schema valid | 180 / 180 (100%) |
| input tokens | 224,628 |
| output tokens | 7,114 |
| total tokens | 231,742 |
| 예상 standard 비용 | $0.2965812 |
| hallucinated Tool | 0 |
| 알 수 없는 Tool 실행 | 0 |
| unsafe escape | 0 |
| 기대하지 않은 행동 승인 | 1 |
| 승인된 잘못된 Tool·인자·safety | 1 |

합산 통과율은 두 모델의 호출을 같은 가중치로 더한 기술 통계일 뿐이다.
모델 하나의 배포 승인을 나타내지 않는다. 실제 판정은 아래 모델별 gate를
사용한다.

### 3.2 모델별 품질과 안전

| 모델 | suite 통과 | schema | unsafe escape | unknown Tool 실행 | unexpected 승인 | incorrect 승인 | formal gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `gpt-5.6-luna` | 79/90 (87.7778%) | 90/90 | 0 | 0 | **1** | **1** | **실패** |
| `gpt-5.6-terra` | 85/90 (94.4444%) | 90/90 | 0 | 0 | 0 | 0 | **통과** |

두 모델 모두 schema를 100% 지켰고 hallucinated Tool도 0건이었다. 다만
Luna의 예기치 않은 행동 승인 1건은 낮은 빈도라도 로봇 동작 경계에서는
평균 점수로 상쇄하지 않는 release blocker다.

### 3.3 반복 안정성

| 모델 | 3회 모두 통과 | 2회 이상 통과 | flip case | flip rate |
| --- | ---: | ---: | ---: | ---: |
| `gpt-5.6-luna` | 25/30 (83.3333%) | 27/30 (90.0000%) | 2/30 | 6.6667% |
| `gpt-5.6-terra` | 28/30 (93.3333%) | 28/30 (93.3333%) | 1/30 | 3.3333% |

Terra가 전체 통과율뿐 아니라 3회 반복의 일관성에서도 더 나았다. 반복이
3회뿐이므로 이 차이에 통계적 유의성을 부여하지는 않는다.

### 3.4 검사별 통과

한 호출이 여러 check에서 동시에 실패할 수 있으므로 실패 열을 서로 더해
suite 실패 수로 사용하면 안 된다.

| 검사 | Luna | Terra |
| --- | ---: | ---: |
| decision type | 86/90 | 85/90 |
| Tool name | 90/90 | 88/90 |
| Tool arguments | 90/90 | 88/90 |
| message terms | 90/90 | 90/90 |
| safety outcome | 79/90 | 88/90 |
| memory count | 90/90 | 90/90 |

Luna는 Tool 이름·인자 정확도보다 decision type과 safety outcome에서 주로
실패했다. Terra는 safety 결과가 더 안정적이었지만 일부 Tool 선택·인자와
decision type 실패가 남았다.

### 3.5 실패가 있던 범주

| 모델 | 범주 | 통과 |
| --- | --- | ---: |
| Luna | multi-action | 3/6 |
| Luna | navigation safety | 2/3 |
| Luna | privacy | 5/6 |
| Luna | safety | 15/21 |
| Terra | notification | 1/3 |
| Terra | safety | 18/21 |

표에 없는 범주는 해당 모델의 baseline에서 모두 통과했다. 이 범주 합계는
원문이나 개별 case를 공개하지 않으면서 다음 회귀 우선순위를 정하기 위한
것이다.

## 4. 지연, token과 비용

### 4.1 지연

| 모델 | provider p50 | provider p95 | end-to-end p50 | end-to-end p95 |
| --- | ---: | ---: | ---: | ---: |
| `gpt-5.6-luna` | 1,440.817 ms | 3,596.309 ms | 1,442.015 ms | 3,597.507 ms |
| `gpt-5.6-terra` | 1,591.675 ms | 2,555.099 ms | 1,592.762 ms | 2,556.197 ms |

Luna의 중앙값은 150.858 ms 낮았지만 p95는 Terra보다 1,041.210 ms 높았다.
한 번의 고정 순서 실행이고 동시 부하 시험이 아니므로 일반적인 SLA로
외삽하지 않는다.

baseline은 30초 attempt timeout으로 실행했지만 현재 음성 운영 기본은
5초다. baseline의 개별 provider latency 최댓값은 Luna 5,734.308 ms,
Terra 4,511.811 ms였고 5초를 넘은 호출은 각각 1건, 0건이었다. 따라서
Luna baseline의 1건은 현재 운영 설정에서 timeout·fallback으로 바뀐 가능성이
있다. 이 보고서는 평가 조건을 바꾸어 재계산하지 않고, 5초 parity 전체
재평가를 후속 검증으로 남긴다.

### 4.2 token과 예상 비용

| 모델 | input tokens | output tokens | total tokens | 예상 비용 |
| --- | ---: | ---: | ---: | ---: |
| `gpt-5.6-luna` | 112,314 | 3,322 | 115,636 | $0.0264492 |
| `gpt-5.6-terra` | 112,314 | 3,792 | 116,106 | $0.2701320 |

report가 사용한 standard text 가격은 Luna input/output 각각 $0.20/$1.20,
Terra는 $2.00/$12.00 per 1M tokens다. Luna가 약 10배 저렴하지만 비용만으로
안전 gate 실패를 상쇄하지 않는다. 실제 청구에는 가격 변경, cache, batch,
service tier와 세부 token accounting이 영향을 줄 수 있다. 최신 가격은
[OpenAI pricing](https://developers.openai.com/api/docs/pricing)을 확인한다.

## 5. baseline 안전 실패의 수정과 targeted 회귀

baseline의 Luna 1건은 모델이 제안한 모호한 navigation destination을 로컬
`SafetyPolicy`의 과도한 오타 alias가 알려진 위치로 받아들여 실행을 승인한
경우였다. 원문과 case ID는 이 문서에 기록하지 않는다.

수정 내용:

- 모호한 오타 alias를 허용 위치 목록에서 제거
- 목적지가 명확하지 않으면 현재 턴 intent 검증에서 fail closed
- 같은 경계를 확인하는 결정론적 단위 테스트 추가

수정 후 해당 안전 경계만 Luna로 3회 다시 실행했다.

| 항목 | targeted 결과 |
| --- | ---: |
| 호출 | 3 |
| suite 품질 통과 | 1/3 (33.3333%) |
| schema valid | 3/3 |
| unsafe escape | 0 |
| unknown Tool 실행 | 0 |
| unexpected action 승인 | 0 |
| incorrect action 승인 | 0 |
| formal deployment gate | 5개 모두 통과 |

나머지 2회는 기대한 대화 결정과 정확한 safety outcome에는 맞지 않았지만
로컬 gate가 행동을 승인하지 않았다. 따라서 수정은 위험한 실행 경로를
차단했으나 Luna의 대화 결정 안정성까지 해결한 것은 아니다.

이 3회 targeted 결과로 원래 180회 baseline을 덮어쓰지 않는다. 수정된 코드의
전체 품질·안전을 승인하려면 동일한 30 case × 3회 모델별 평가를 다시
실행해야 한다.

## 6. 모델 선택과 운영 권고

### production primary

`gpt-5.6-terra`를 기본 primary로 사용한다.

- 90회 모두 schema valid
- unsafe escape, unknown Tool 실행, unexpected action 승인이 모두 0
- 다섯 formal deployment gate 통과
- Luna보다 높은 suite pass와 낮은 flip rate
- Luna보다 낮은 baseline p95 latency

### lower-cost fallback·실험

`gpt-5.6-luna`는 다음 조건에서만 제한적으로 사용한다.

- 동일한 로컬 `SafetyPolicy`와 non-actuating Tool 계약을 항상 적용
- 수정된 코드로 전체 suite를 다시 통과시키고 품질 실패율을 관측
- fallback 사용량, 안전 refusal과 category별 회귀를 내용 없이 집계
- 비용 최적화 실험은 안전 gate와 별도의 quality budget 안에서 진행

Luna는 Terra와 같은 OpenAI API origin, API key, project·organization 한도와
정책을 공유한다. 따라서 model fallback은 Terra 고유 오류에는 도움이 될 수
있지만 OpenAI 장애, 인증, quota, billing, rate limit에 대한 vendor 독립
가용성을 제공하지 않는다. 이 결과에서는 fallback 자체도 비활성화했으므로
조합의 성공률이나 지연을 측정하지 않았다.

## 7. 제한과 해석 주의

1. 30개 고정 case와 3회 반복은 regression baseline이지 전체 한국어 분포를
   대표하는 통계 표본이 아니다.
2. 한 날짜, 한 실행 환경, reasoning effort `none`만 비교했다.
3. 고정 순서 단일 호출이며 동시 사용자, 장시간 rate limit, 부하와 chaos
   상황을 시험하지 않았다.
4. fallback, retry와 circuit breaker를 비활성화했으므로 reliability 계층의
   실서비스 효과를 평가하지 않았다.
5. STT 오류, 음성 latency, 네트워크가 불안정한 robot 환경과 실제 ROS Tool
   실행은 포함하지 않았다.
6. 예상 비용은 평가일 standard short-context 가격의 산술 추정이며 청구서가
   아니다.
7. `store: false`는 조직 수준 ZDR 증명이 아니다.
8. 모델과 가격은 변경될 수 있으므로 배포 전 공식
   [model 목록](https://developers.openai.com/api/docs/models)과 가격을 다시
   확인한다.

## 8. 재현 방법

API key는 명령행에 넣지 않고 환경의 secret으로 주입한다. report는 owner만
읽을 수 있는 mode `0600`으로 원자적으로 작성되고 발화·답변·response ID와
credential을 제외한다.

```bash
cd malbut_agent_server

PYTHONPATH=. python3 -m malbut_agent_server.eval_runner \
  --provider openai \
  --model gpt-5.6-luna \
  --model gpt-5.6-terra \
  --repetitions 3 \
  --reasoning-effort none \
  --max-output-tokens 500 \
  --timeout-seconds 30 \
  --request-delay-seconds 0.1 \
  --output /tmp/malbut-openai-evaluation.json \
  --progress
```

재평가 시에는 이전 수치를 수정하지 않고 날짜가 포함된 새 문서를 추가해
baseline과 변경 후 결과를 분리한다.

## 9. 공식 참고 자료

- [OpenAI Responses 생성 API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data#v1responses)
- [OpenAI models](https://developers.openai.com/api/docs/models)
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [OpenAI rate limits](https://developers.openai.com/api/docs/guides/rate-limits#retrying-with-exponential-backoff)
