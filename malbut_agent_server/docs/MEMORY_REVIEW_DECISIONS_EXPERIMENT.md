# 기억 원문 검토와 Decisions API 비교

이 실험은 **개인 사실 기억의 원문 검토**만 비교한다. 이야기 기억 추출, 사용자 답변 생성,
기억 후보 추출은 비교 범위 밖이다. Decisions 비교 코드는 운영 factory에 연결하지 않았다.
후속 `ownership-v2`에서는 기존 규칙의 소유주·부분 인용 검사와 OpenAI의 원문 검토 지시문을
보완했다. 운영의 검토 API는 계속 Responses다.

실제 168회 호출의 결과는 [최초 비교](evaluations/MEMORY_REVIEW_DECISIONS_2026-10-14.md),
소유주·혼합 취향 보완 후 384회 호출은
[후속 비교](evaluations/MEMORY_REVIEW_DECISIONS_OWNERSHIP_V2_2026-10-14.md)에 기록했다.

## 소유주·혼합 취향 후속 비교

`--suite ownership-v2`는 기존 사례 38개에 새로운 사례 20개를 더한다. 입력·기대판정을
실행 전에 고정하고, 기존 사례와 새 사례 결과를 `regression`/`holdout`으로 나눠 기록한다.
새 사례도 이 실험에서 작성한 가상 사례이며 독립적인 사람의 정답 검수는 아니다.

1. `responses_current`: 보완 전 Responses 지시문, gpt-5.6-luna.
2. `responses_refined`: 개인 기억의 소유주 범위와 후보별 독립 판정을 추가한 Responses.
3. `decisions`: 최초 Decisions 지시문과 후보 배열.
4. `decisions_refined`: 같은 범위 규칙을 추가하고 질문마다 후보 하나를 명시한 Decisions.

기존 Responses 비교군에서는 새로 추가된 지시문만 제거해 이전 기준을 재현한다. 네 경로는
모두 **보완한 공통 규칙을 거친 같은 후보**를 받는다. 보완 전 규칙을 포함한 전체 파이프라인과
비교한 결과는 아니다. 보완 전 규칙 우회는 별도의 재현과 오프라인 테스트로 확인했다.

반려동물은 소유관계가 명확하거나 제삼자 소유 단서가 없는 명시적 정정 요청일 때만 규칙으로
처리한다. 제삼자 소유 단서, 입양 등 변화, 잘라낸 근거 밖의 정정·부정은 원문 검토로 보낸다.
이것은 모호한 후보를 일괄 거절하는 규칙이 아니다. 같은 문장에 이웃이 나와도 사용자의 동물
정보가 명확하면 검토에서 승인할 수 있다. 검토기가 없는 경로는 미확인 후보를 저장하지 않는다.

검토 지시문은 혼합 취향을 대상·절별로 분리하고 후보마다 독립적으로 판단하도록 한다.
Decisions에서는 공통 입력을 전체 사용자 원문으로 제한하고 각 질문에 해당 후보를 직접
넣는다. 확률 기준은 결과에 맞춰 바꾸지 않고 원래 선택값과 0.9 기준을 함께 보고한다.

```bash
python3 -m malbut_agent_server.memory_review_benchmark --suite ownership-v2
python3 -m malbut_agent_server.memory_review_benchmark \
  --suite ownership-v2 --live --repeats 2 --output /tmp/memory-review-ownership-v2.json
```

## 최초 비교와 공통 측정 방법

- 사람이 검토할 수 있는 가상 한국어 원문과 후보, 정답을
  `malbut_agent_server/data/memory_review_cases.json`에 고정하였다.
- 실제 `PersonalMemory.prepare_source_review()`로 검토할 후보만 고른다. 후보가 없거나
  규칙으로 확인되거나 동의·출처 조건에 맞지 않는 경우 API를 호출하지 않는다.
- 최초 비교는 같은 원문·후보 묶음을 세 경로에 넣는다. 후속 비교는 위의 네 경로를 사용한다.
  후보가 여럿이면 각 경로에서도 한 요청으로 묶는다.

| 경로 | 모델 | 처리 |
| --- | --- | --- |
| `responses_current` | `gpt-5.6-luna` | `MemorySourceReviewer`와 보완 전 Responses 지시문 |
| `responses_control` | `gpt-6-luna` | 같은 검토 코드에서 모델만 변경한 비교군 |
| `decisions` | `gpt-6-luna` | 후보별 `choice`: supported / contradicted / insufficient |

Responses는 현재 설정 기본값인 `reasoning_effort=low`, 출력 상한 1,024를 사용한다.
실험은 요청당 30초, 재시도 없이 실행한다. 이는 운영 기본 요청 제한 5초와 다르므로
5초를 초과한 건수도 별도로 남긴다. 생성된 결과의 품질을 비교하려고 이 실험에서만 여유를 둔다.
지연은 검토 호출 시작부터 검증 종료까지의 실제 경과 시간이며, 별도 네트워크 시간도 저장한다.

각 사례는 기본 2회 반복하고 사례와 경로 순서를 고정 seed로 섞는다. 동시 요청은 보내지 않는다.
첫 호출도 포함하며 별도 준비 호출은 제외하지 않는다. 시작 시의 접근 확인 요청은 본 결과와
분리한다. 동일 입력 반복과 공통 프롬프트의 캐시 효과가 비용·지연에 영향을 줄 수 있다.

현재 검토는 승인/거절만 반환하므로 공통 품질 지표는 이진 판정이다.

- 잘못된 승인: 정답이 `contradicted` 또는 `insufficient`인데 승인한 후보 수 / 해당 후보 수.
- 잘못된 거절: 정답이 `supported`인데 승인하지 않은 후보 수 / 해당 후보 수.
- Decisions의 3분류 정답률과 거부 응답 수는 별도로 남긴다.
- 오류·누락·중복 응답·형식 오류는 승인하지 않으며, 정상적인 거절로 점수에 섞지 않는다.
  실패 묶음 수를 따로 보고하고 비용 정보가 없으면 0원으로 간주하지 않는다.
- 후속 비교의 `paired`는 한 경로라도 실패한 사례·반복 묶음을 모든 경로에서 제외해,
  같은 후보 수로 비교한다. 전체 호출 결과와 오류 수는 별도로 보존한다.
- Decisions의 원래 선택과, `supported` 선택 및 그 선택지의 확률 0.9 이상을 함께 요구하는
  보수적 기준을 같은 호출 결과에서 비교한다. 0.9는 실험 전 고정한 탐색용 기준이다.

두 API는 동일 원문·후보를 보지만 출력 형식과 지시문 크기가 다르다. 따라서 속도 차이는
**검토 구현 전체의 비교**이며 endpoint만의 효과라고 단정할 수 없다.
고정 후보 실험은 추출 정확도나 실제 트래픽에서 검토가 얼마나 필요한지를 측정하지 않는다.
또한 일반 자동 기억은 백그라운드이므로 검토 시간 감소가 일반 답변 시간 감소와 같지 않다.

## 실행

패키지 루트에서 실행한다. 키 값을 인자나 결과 파일에 쓰지 않는다.

```bash
# 원격 호출 없이 실제 규칙으로 분기와 예정 호출 수 확인
python3 -m malbut_agent_server.memory_review_benchmark

# 이미 설정한 OPENAI_API_KEY 사용. 결과 경로가 이미 있으면 덮어쓰지 않는다.
python3 -m malbut_agent_server.memory_review_benchmark \
  --live --repeats 2 --output /tmp/memory-review-comparison.json

# 기존 환경 파일을 명시적으로 사용하려면 --env-file 경로 추가
```

최대 400회로 호출을 제한한다. 인증·권한·endpoint·할당량 오류는 부분 결과를 남기고 중단한다.
결과에는 후보 ID, 정답, 선택, 토큰 수, 추정 비용, 지연 및 오류 종류만 기록한다.
원문은 고정 가상 데이터 파일에 있으며 운영 대화나 키·헤더·원격 오류 본문은 저장하지 않는다.

## 운영 호출 비율 확인

기존 `automatic_memory_jobs`는 `review_ms`와 검토 토큰 수를 저장한다. 다음 명령은 기존
DB를 읽기 전용으로 열고 원문이나 사용자 식별자를 읽지 않은 채 집계한다.

```bash
python3 -m malbut_agent_server.memory_review_benchmark \
  --database /absolute/path/to/existing-agent.sqlite3
```

- `review_ms > 0`: 검토기 실행이 기록된 작업. HTTP 시도 수와 정확히 같지는 않다.
- `review_ms = 0`: 검토 생략이 명시된 작업.
- `review_ms IS NULL`: 미처리·조기 종료·실패·구버전 등으로 알 수 없는 작업.
- 전체 보존 작업 대비 관측 비율과, 생략/검토 여부가 확인되는 작업만의 비율을 따로 표시한다.

후보 없이 끝난 작업도 NULL일 수 있다. 호출 후 취소·실패한 작업은 검토 시간이 남지 않을 수
있다. 삭제된 작업과 명시적 저장의 동기 검토도 포함하지 않으므로 이 집계는 **전체 대화의
정확한 API 호출률이 아니다.** 이를 정확히 재려면 기간이 일치하는 완료 대화 수와 실제 검토
HTTP 시도 수를 따로 수집해야 한다. 이번 작업공간에서는 운영 DB를 찾지 못해 운영 수치는
측정하지 못했다. 가상 사례의 분기 비율을 운영 호출률로 보고하지 않는다.

## 가격과 해석

2026-10-14 공식 표의 Standard, 짧은 컨텍스트 가격을 사용한 추정치다.
Responses는 입력·캐시 읽기·캐시 쓰기·출력 사용량을 반영하고, Decisions는 전체 입력에
100만 토큰당 $0.10을 적용한다. 지역 할증과 계정별 청구 차이는 포함하지 않는다.
실제 응답 사용량이 없는 호출의 비용은 미상으로 남긴다.

- [Decisions 공식 안내](https://developers.openai.com/api/docs/guides/decisions)
- [공식 API 가격](https://developers.openai.com/api/docs/pricing)

소규모 가상 사례에서 오류가 없더라도 실사용 오류율이 0이라는 뜻은 아니다. 같은 사례의
반복은 독립적인 새로운 사례가 아니다. 운영 전환 판단에는 별도의 정답 검수와 미사용 사례,
실제 트래픽 비율, 더 넓은 표현·오류 사례가 필요하다.
