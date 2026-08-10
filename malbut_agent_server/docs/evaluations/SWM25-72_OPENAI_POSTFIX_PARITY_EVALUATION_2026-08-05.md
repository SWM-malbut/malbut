# SWM25-72 OpenAI post-fix parity 평가 — 2026-08-05

## 문서 정보

| 항목 | 내용 |
| --- | --- |
| Jira | SWM25-72 |
| 목적 | 안전 수정 후 전체 suite를 운영 기본 timeout으로 다시 검증 |
| 평가 시각 | 2026-08-05 13:54 UTC 기준 |
| 패키지 | `malbut_agent_server` 0.4.0 |
| suite | `malbut-korean-commands-v2` |
| 비교 모델 | `gpt-5.6-luna`, `gpt-5.6-terra` |
| 호출 수 | **180회** — 30 case × 3회 × 2모델 |
| 판정 | **안전 회귀 통과, 전체 배포 승인 보류** |

수정 전 수치는
[`SWM25-72 OpenAI baseline 평가`](SWM25-72_OPENAI_EVALUATION_2026-08-05.md)에
보존한다. 이 문서는 동일한 평가 계약에 안전 수정과 운영 기본 5초 timeout을
적용한 별도 결과다.

## 1. 평가 계약

| 항목 | 값 |
| --- | --- |
| case 수·반복 | 30 case, 모델별 각 case 3회 |
| 순서 | 고정 순서, 단일 요청 |
| fallback·retry | 모델 귀속을 분리하기 위해 비활성화 |
| provider attempt timeout | **5초** |
| 호출 간 대기 | 0.1초 |
| reasoning | effort `none`, context `current_turn` |
| 출력 상한 | 500 tokens |
| 입력 상한 | 20,000 characters |
| 저장·Tool | `store: false`, `parallel_tool_calls: false` |
| 출력 계약 | strict structured text와 strict Tool schema |

평가 runtime source SHA-256은
`b35e598a9e765aed0968f261258bf83977dd3a902901293a760eafb7db5c0f55`다.
이는 safety, orchestrator, provider, prompting, schema와 evaluator 구현을 함께
묶는다.

## 2. 전체 결과

| 모델 | suite 통과 | schema valid | 3회 모두 통과 | 2회 이상 통과 | flip case |
| --- | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-luna` | 81/90 (90.0000%) | 89/90 (98.8889%) | 26/30 (86.6667%) | 27/30 (90.0000%) | 2/30 |
| `gpt-5.6-terra` | 80/90 (88.8889%) | 86/90 (95.5556%) | 24/30 (80.0000%) | 28/30 (93.3333%) | 4/30 |

Luna는 한 호출, Terra는 네 호출에서 `TimeoutError`가 발생해 정상
`ProviderResult`를 만들지 못했다. 이 다섯 건 때문에 두 모델 모두 schema
valid 100% gate를 통과하지 못했고 평가 CLI는 실패 코드 `2`로 종료됐다.

schema가 유효한 결과의 품질 실패는 주로 다음 범주에 있었다.

| 모델 | 실패가 있던 범주 | 범주별 통과 |
| --- | --- | ---: |
| Luna | multi-action | 3/6 |
| Luna | navigation safety | 2/3 |
| Luna | privacy | 4/6 |
| Luna | safety | 18/21 |
| Terra | navigation safety | 2/3 |
| Terra | notification | 0/3 |
| Terra | perception | 2/3 |
| Terra | safety | 16/21 |

일부 범주 수치에는 provider timeout도 실패로 포함된다. 품질 실패는 주로
요구된 decision type, Tool 인자 또는 safety 결과보다 보수적으로
거절·비행동 응답을 선택한 경우였다. 어떤 실패도 실제 실행 승인으로
이어지지 않았다.

## 3. 안전 gate

| formal deployment gate | Luna | Terra |
| --- | --- | --- |
| schema valid 100% | **실패** — 89/90 | **실패** — 86/90 |
| unsafe escape 0건 | 통과 | 통과 |
| 알 수 없는 Tool 실행 0건 | 통과 | 통과 |
| 기대하지 않은 행동 승인 0건 | 통과 | 통과 |
| 잘못된 Tool·인자·safety 행동 승인 0건 | 통과 | 통과 |

baseline에서 Luna에 발생했던 예기치 않은 행동 승인 1건은 이번 전체
평가에서 0건이었다. 모호한 위치 alias를 제거하고 현재 턴 의도가 명확하지
않으면 fail closed하는 수정이 targeted 3회뿐 아니라 전체 suite에서도
위험 실행을 차단했다.

따라서 **안전 수정 자체의 회귀 검증은 통과**했다. 다만 schema gate는 안전
gate와 별개로 평균 점수로 상쇄할 수 없으므로 전체 배포 승인은 보류한다.

## 4. 지연과 timeout

| 모델 | provider p50 | provider p95 | end-to-end p50 | end-to-end p95 | end-to-end 최대 | 5초 초과 | timeout |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-luna` | 2,030.839 ms | 4,665.515 ms | 2,032.018 ms | 5,037.654 ms | 5,584.162 ms | 5/90 | 1/90 |
| `gpt-5.6-terra` | 2,537.335 ms | 6,543.854 ms | 2,600.421 ms | 6,718.352 ms | 9,007.238 ms | 13/90 | 4/90 |

`urllib`의 5초 값은 DNS부터 전체 요청을 강제 취소하는 hard wall-clock
deadline이 아니라 개별 blocking socket 동작에 적용되는 timeout이다. 그래서
성공한 응답 중에도 end-to-end 5초 초과가 있었고 Terra 최대 실측은 약
9.0초였다. 현재 `ReliableProvider`의 11초 값도 새 시도 시작을 제한하는
스케줄링 예산이다. 음성 대화 SLA를 보장하려면 취소 가능한 HTTP client와
hard deadline을 별도로 도입해야 한다.

이번 모델 단독 평가에서는 fallback을 의도적으로 끄고 timeout을 실패로
기록했다. 운영 경로는 같은 오류를 `ReliableProvider`가 다음 모델 또는 로컬
비행동 refusal로 변환하지만, 실제 Terra→Luna 조합의 성공률·전체 지연은
별도의 live reliability 평가가 필요하다.

## 5. token과 비용

timeout 응답에는 usage가 없어 전체 token과 정확한 평가 비용을 계산할 수
없다. 아래 값은 usage가 반환된 호출만 합한 **알려진 비용 하한**이다.

| 모델 | usage 확보 | input tokens | output tokens | 알려진 비용 하한 |
| --- | ---: | ---: | ---: | ---: |
| `gpt-5.6-luna` | 89/90 | 111,070 | 3,470 | $0.026378 |
| `gpt-5.6-terra` | 86/90 | 107,341 | 3,658 | $0.258578 |
| 합계 | 175/180 | 218,411 | 7,128 | **$0.284956** |

timeout 요청도 서버에서 처리됐다면 실제 청구액은 이 하한보다 클 수 있다.
가격은 평가일의 standard short-context text rate를 사용했으며 실제 청구서가
아니다.

## 6. baseline 대비 변화

| 모델 | 항목 | baseline | post-fix 5초 |
| --- | --- | ---: | ---: |
| Luna | suite 통과 | 79/90 | 81/90 |
| Luna | schema valid | 90/90 | 89/90 |
| Luna | 기대하지 않은 행동 승인 | **1** | **0** |
| Luna | provider p95 | 3,596.309 ms | 4,665.515 ms |
| Terra | suite 통과 | 85/90 | 80/90 |
| Terra | schema valid | 90/90 | 86/90 |
| Terra | 기대하지 않은 행동 승인 | 0 | 0 |
| Terra | provider p95 | 2,555.099 ms | 6,543.854 ms |

baseline은 30초 timeout, post-fix 평가는 운영 기본 5초 timeout이므로 품질과
지연 변화 전부를 코드 수정이나 모델 변화의 효과로 해석하면 안 된다. 한
날짜의 고정 순서 3회 반복만으로 일반 SLA나 통계적 우열도 확정하지 않는다.

## 7. 운영 판정과 후속 작업

1. **Terra를 primary 후보로 유지한다.** baseline 안전·품질과 이번 majority
   pass는 여전히 Terra 선택을 지지하지만, 5초 조건의 전체 승인은 보류한다.
2. **Luna는 제한된 lower-cost fallback으로만 둔다.** 안전 회귀는 해결됐지만
   품질 안정성과 same-vendor 장애 한계가 남아 있다.
3. 실제 orchestrator에서 Terra→Luna→safe refusal 순서를 운영 timeout으로
   반복 측정해 성공률, 전체 지연, fallback·refusal 비율과 비용을 기록한다.
4. 취소 가능한 hard wall-clock deadline, retry jitter와 구조화된 provider
   관측 지표를 별도 후속으로 구현한다.
5. 실제 ROS Tool 실행은 confirmation과 1회 소비 승인까지 검증되기 전에는
   계속 금지한다.

## 8. report 무결성과 privacy

원본 JSON은 owner-only mode `0600`으로 로컬 임시 경로에 작성했다. SHA-256은
`f2b5e80b1df3837ebdaedc3e50c791a514d021b9428c1eef6c9594d5b3e41d96`다.
발화, assistant 답변, response ID와 credential은 report에 저장하지 않았고
별도 secret pattern 검사도 통과했다. 원본 report 자체는 Git에 추가하지
않는다.

## 9. 재현 명령

API key는 명령행에 쓰지 않고 Git에서 제외된 env 파일이나 secret manager로
주입한다.

```bash
cd malbut_agent_server

PYTHONPATH=. python3 -m malbut_agent_server.eval_runner \
  --provider openai \
  --model gpt-5.6-luna \
  --model gpt-5.6-terra \
  --repetitions 3 \
  --reasoning-effort none \
  --max-output-tokens 500 \
  --timeout-seconds 5 \
  --request-delay-seconds 0.1 \
  --env-file .env.local \
  --output /tmp/malbut-openai-postfix-parity.json \
  --progress
```

## 10. 공식 참고 자료

- [OpenAI Responses 생성 API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data#v1responses)
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [OpenAI API errors](https://developers.openai.com/api/docs/guides/error-codes#api-errors)
