# SWM25-71 사용자 컨텍스트 통합

## 문서 정보

| 항목 | 내용 |
| --- | --- |
| Jira | SWM25-71 |
| 문서 목적 | 긴 대화의 최근 원문·요약·장기 기억을 안전하고 제한된 모델 입력으로 통합한 구현 및 검증 근거 정리 |
| 작성 기준일 | 2026-08-04 |
| 구현 위치 | `malbut_agent_server` |
| 현재 상태 | **완료 — 달성 조건 9/9 구현 및 관련 자동화 테스트 통과** |
| 관련 작업 | SWM25-69 대화·에이전트 계약 정의, SWM25-70 멀티턴 대화 세션, SWM25-72 LLM provider 연결 |

> 이 문서의 완료 판정은 코드와 관련 자동화 테스트를 기준으로 한다. 모델별 실제 대화 품질과 토큰 비용 비교는 SWM25-72의 평가 범위다.

## 목표

긴 대화에서도 필요한 문맥만 모델에 전달하여 응답 품질, 속도, 비용을 관리한다.

구체적으로 다음 세 종류의 문맥을 서로 다른 수명 주기와 신뢰 수준으로 관리한다.

1. 현재 세션의 최근 대화 원문
2. 최근 원문보다 오래된 대화의 rolling summary
3. 사용자별 장기 기억

모델 입력은 전체 문자 예산 안에서 구성하며, 저장된 대화·요약·기억은 명령이 아닌 신뢰되지 않은 데이터로만 취급한다.

## 현재 구현 요약

- 최근 완료 대화는 기본 10턴만 원문으로 전달한다.
- 원문 창보다 오래된 완료 턴은 네트워크 요청이 없는 결정론적 로컬 요약으로 누적한다.
- 장기 기억은 사용자별로 검색하고, 만료되지 않은 관련 항목만 별도 영역에 포함한다.
- 전체 입력 상한을 넘으면 선택 문맥을 단계적으로 축소하되 현재 사용자 발화의 가능한 가장 긴 prefix를 보존한다.
- 입력에 포함된 각 문맥의 원본·포함 크기와 잘림 여부를 내용 노출 없이 응답 메트릭으로 제공한다.
- reset, 만료, 삭제 및 삭제 후 동일 ID 재생성 시 이전 대화와 요약이 다시 유입되지 않는다.

주요 구현 근거:

- [prompting.py](../../malbut_agent_server/prompting.py): 문맥 분리, 신뢰 경계, 문자 예산, 축소 fallback, 크기 측정
- [conversation.py](../../malbut_agent_server/conversation.py): 최근 N턴 조회, 요약 저장·출처 추적, 세션 생명주기 무효화
- [summarization.py](../../malbut_agent_server/summarization.py): 제한된 결정론적 extractive rolling summary
- [memory.py](../../malbut_agent_server/memory.py): 사용자 격리 장기 기억, 관련도 검색, 만료 제외
- [orchestrator.py](../../malbut_agent_server/orchestrator.py): 세션·기억·provider·안전 정책의 통합 순서
- [config.py](../../malbut_agent_server/config.py): 최근 N, 요약 길이, 전체 입력 상한의 환경 설정 및 범위 검증
- [README.md](../../README.md): 공개 실행·설정·응답 계약

## 컨텍스트 처리 흐름

```text
현재 요청
  └─ user_id + conversation_id + turn_id + current utterance
       │
       ▼
SQLiteConversationStore.begin_turn()
  ├─ 현재 session_instance_id/generation 확인
  ├─ 완료된 최근 N턴 원문 조회
  └─ 최근 N턴 이전 구간의 요약 조회
       │
       ▼
SQLiteMemoryStore.search_with_revision()
  └─ 같은 user_id의 활성·관련 장기 기억만 조회
       │
       ▼
AgentOrchestrator
  └─ 세 영역을 provider에 각각 전달
       │
       ▼
prepare_model_input()
  ├─ conversation_history_untrusted
  ├─ conversation_summary_untrusted
  ├─ memory_context_untrusted
  ├─ current_user_utterance
  ├─ 전체 문자 상한 적용 및 overflow fallback
  └─ content-free ContextMetrics 생성
       │
       ▼
LLM provider → 로컬 SafetyPolicy → 완료 턴 저장
  └─ 원문 창 밖으로 밀려난 완료 턴을 rolling summary에 반영
```

### 저장 경계

대화와 장기 기억은 같은 SQLite 파일을 사용할 수 있지만 논리 데이터는 분리되어 있다.

| 데이터 | 저장·조회 주체 | 범위와 수명 |
| --- | --- | --- |
| 최근 대화 | `conversation_turns` / `SQLiteConversationStore` | 현재 사용자·세션 인스턴스·generation의 완료 턴 |
| 대화 요약 | `conversation_summaries` / `SQLiteConversationStore` | 최근 원문 창 이전 prefix에서 파생되며 세션 생명주기에 종속 |
| 장기 기억 | `memories` / `SQLiteMemoryStore` | 사용자별 독립 레코드이며 별도 만료·삭제 정책 적용 |

`AgentOrchestrator`는 현재 세션의 최근 원문과 요약을 받은 뒤, 같은 사용자의 장기 기억을 별도로 검색한다. 기억 저장소 revision이 모델 추론 중 바뀌면 해당 결과를 거부하여 오래된 기억 snapshot으로 행동하는 것을 방지한다.

이번 스토리는 내부 저장·검색과 모델 입력 경계까지만 공개한다. 장기 기억의
HTTP 생성·수정·삭제 API와 사람별 학습 정책은 SWM25-36 담당 인터페이스가
확정된 뒤 별도 스토리에서 연결한다. 따라서 모델이 추론한 내용을 자동으로
기억에 저장하는 경로는 없다.

## 신뢰 경계와 안전 처리

### 명령으로 인정되는 입력

- 고정 `SYSTEM_INSTRUCTIONS`
- 현재 HTTP 요청의 `current_user_utterance`
- 서버가 구성한 로봇 상태와 허용 Tool 목록

### 신뢰되지 않은 참고 데이터

- `conversation_history_untrusted`
- `conversation_summary_untrusted`
- `memory_context_untrusted`

저장된 세 영역은 JSON 값으로 직렬화되고 system instruction과 분리된다. 내부에 `SYSTEM`, `developer`, 역할 변경, Tool 호출 또는 안전 규칙 우회 문장이 있어도 과거 데이터일 뿐 현재 명령으로 승격하지 않는다. 프롬프트 규칙과 별개로 로컬 안전 정책이 현재 발화에서 행동 의도와 대상을 다시 확인하므로, 과거 문맥만으로 로봇 동작을 허가하지 않는다.

검증 근거:

- `test_untrusted_sources_remain_separate_json_data`
- `test_conversation_instructions_are_rendered_as_untrusted_data`
- `test_malicious_history_cannot_authorize_benign_current_turn`
- `SYSTEM_INSTRUCTIONS`의 과거 대화·요약·기억 명령 실행 금지 규칙

## 제한값과 측정값

### 설정 가능한 제한

| 환경 변수 | 기본값 | 허용 범위 | 의미 |
| --- | ---: | ---: | --- |
| `MALBUT_AGENT_CONVERSATION_HISTORY_LIMIT` | 10턴 | 10~50턴 | 모델에 원문으로 전달할 최근 완료 턴 N |
| `MALBUT_AGENT_CONVERSATION_SUMMARY_MAX_CHARS` | 2,000자 | 256~8,000자 | SQLite에 저장하는 rolling summary 상한 |
| `MALBUT_AGENT_MAX_MODEL_INPUT_CHARS` | 20,000자 | 4,096~1,000,000자 | system instruction과 직렬화된 문맥을 합한 입력 상한 |
| `MALBUT_AGENT_MEMORY_LIMIT` | 5개 | 1~10개 | 요청당 검색할 장기 기억 수 |

### provider 입력 내부 상한

| 영역 | 상한 |
| --- | ---: |
| 최근 대화 전체 | 6,000자 |
| 대화 메시지 한 개 | 300자 |
| provider에 포함되는 요약 | 2,000자 |
| 장기 기억 전체 | 3,000자 |
| 장기 기억 한 개 | 1,200자 |

전체 상한을 초과하면 장기 기억, 요약, 최근 대화 등 선택 문맥을 축소한다. 그래도 크면 선택 영역과 부가 상태를 비우고, 마지막에는 JSON escaping 크기까지 계산하여 현재 발화의 들어갈 수 있는 prefix를 보존한다. 따라서 과도한 문맥 크기만을 이유로 모델 입력 생성이 실패하지 않는다.

> 현재 하드 캡은 tokenizer에 종속되지 않는 **문자 수 기준**이다. 실제 provider token 사용량은 `provider.usage`의 input/output/total token으로 별도 관찰한다.

### 응답 메트릭

각 응답의 `provider.context`는 원문을 노출하지 않고 다음 항목을 제공한다.

- 최근 대화: 원본·포함 턴 수, 원본·포함 문자 수
- 대화 요약: `summary_id`, 원본 턴 수, 원본·포함 문자 수
- 장기 기억: 원본·포함 레코드 수, 원본·포함 문자 수
- 현재 발화: 원본·포함 문자 수
- 전체 입력: 실제 문자 수, 설정 상한, 잘린 영역, `overflow_fallback`

`ContextMetrics`는 완료 응답과 함께 저장되므로 동일 요청의 내구성 있는 재시도에서도 최초 입력 측정값을 그대로 반환한다.

## 달성 조건 및 근거

### 1. 최근 대화와 장기 기억을 서로 다른 데이터로 관리한다.

- [x] 완료

`SQLiteConversationStore`가 최근 대화·요약을, `SQLiteMemoryStore`가 사용자별 장기 기억을 각각 관리한다. 모델 입력에서도 `conversation_history_untrusted`, `conversation_summary_untrusted`, `memory_context_untrusted`로 분리한다.

**코드:** `conversation.py`, `memory.py`, `orchestrator.py`, `prompting.py`

**테스트:** `test_latest_ten_raw_turns_and_summary_have_no_gap_or_overlap`, `test_korean_memory_retrieval_and_user_isolation`, `test_untrusted_sources_remain_separate_json_data`, `test_conversation_lifecycle_does_not_delete_long_term_memory`

### 2. 최근 N개의 대화 턴만 원문으로 모델에 전달한다.

- [x] 완료

기본 N은 10이며 10~50 범위로 설정할 수 있다. 저장소가 현재 generation의 최신 완료 N턴을 순서대로 선택하고, provider가 이 선택을 임의로 더 줄이지 않는다.

**코드:** `SQLiteConversationStore.history_limit`, `begin_turn()`, `prepare_model_input()`

**테스트:** `test_latest_ten_raw_turns_and_summary_have_no_gap_or_overlap`, `test_provider_preserves_store_selected_recent_n`, `test_conversation_context_is_latest_ten_and_data_only`

### 3. 오래된 대화는 요약하여 컨텍스트에 포함한다.

- [x] 완료

최근 N턴 이전의 연속 prefix를 `ExtractiveConversationSummarizer`가 로컬에서 요약한다. 요약은 최근 원문과 중복되거나 누락되지 않게 별도 영역으로 전달되며, 서비스 재시작 후 N 변경 시에도 경계를 재정렬한다.

**코드:** `conversation.py`의 `_advance_summary_locked()`·`_summary_for_window_locked()`, `summarization.py`

**테스트:** `test_latest_ten_raw_turns_and_summary_have_no_gap_or_overlap`, `test_changed_recent_n_rebuilds_summary_without_gap_or_overlap`, `test_rolling_state_preserves_prior_salient_turns`, `test_orchestrator_passes_generated_summary_to_provider`

### 4. 최대 토큰 또는 문자 제한을 설정한다.

- [x] 완료

전체 모델 입력을 기본 20,000자로 제한하며 최소 4,096자부터 설정할 수 있다. 최근 대화, 요약, 기억에도 별도 문자 상한을 적용한다.

**코드:** `config.py`의 `MALBUT_AGENT_MAX_MODEL_INPUT_CHARS`, `prompting.py`의 영역별 상수와 `prepare_model_input()`

**테스트:** `test_hard_cap_and_content_free_metrics_cover_all_sources`, `test_memory_context_has_a_total_character_budget`

### 5. 컨텍스트 제한을 초과해도 요청이 실패하지 않는다.

- [x] 완료

선택 문맥을 단계적으로 줄이는 fallback과 현재 발화 prefix 보존 로직을 구현했다. JSON escape로 실제 직렬화 크기가 커지는 경우도 전체 상한 안에서 처리한다.

**코드:** `prompting.py`의 `_shrink_optional_context()`·`_bounded_current_utterance_context()`

**테스트:** `test_hard_cap_and_content_free_metrics_cover_all_sources`, `test_escaped_utterance_overflow_keeps_a_nonempty_prefix`, `test_oversized_and_malformed_text_cannot_escape_resource_bounds`

### 6. 대화 요약의 생성 시점과 원본 세션을 추적할 수 있다.

- [x] 완료

요약에 `summary_id`, `user_id`, `conversation_id`, `session_instance_id`, `generation`, `summary_revision`, 원본 ordinal 범위·턴 수·digest, summarizer, fallback 여부, `created_at`, `updated_at`을 저장한다.

**코드:** `ConversationSummary`, `conversation_summaries`, `_advance_summary_locked()`

**테스트:** `test_summary_provenance_persists_across_restart`, `test_summary_is_deterministic_bounded_and_has_provenance`

### 7. 대화 기록과 기억 안의 명령문을 시스템 명령으로 실행하지 않는다.

- [x] 완료

과거 대화·요약·기억은 이름에 `_untrusted`가 붙은 서로 다른 JSON 데이터 영역에만 포함한다. 현재 사용자 명령은 `current_user_utterance` 하나로 구분하며, 로컬 safety/intent gate가 과거 문맥만으로 제안된 행동을 차단한다.

**코드:** `SYSTEM_INSTRUCTIONS`, `_render_context()`, `SafetyPolicy`, `AgentOrchestrator`

**테스트:** `test_untrusted_sources_remain_separate_json_data`, `test_conversation_instructions_are_rendered_as_untrusted_data`, `test_malicious_history_cannot_authorize_benign_current_turn`

### 8. 모델에 전달한 대화·기억 크기를 측정할 수 있다.

- [x] 완료

`ContextMetrics`가 최근 대화·요약·장기 기억·현재 발화의 원본 및 포함 크기와 전체 입력 크기, 잘린 영역, fallback 여부를 기록한다. 공개 응답의 `provider.context`에는 내용 없이 측정값만 노출한다.

**코드:** `prompting.py`의 `_measure_context()`, `schemas.py`의 `ContextMetrics`, `orchestrator.py`의 응답 저장·복원

**테스트:** `test_hard_cap_and_content_free_metrics_cover_all_sources`, `test_durable_retry_preserves_context_metrics`, `test_http_context_metrics_do_not_expose_conversation_content`

### 9. 삭제되거나 만료된 대화가 다시 컨텍스트에 포함되지 않는다.

- [x] 완료

reset과 만료 시 generation을 변경하고 현재 요약과 진행 중인 턴을
무효화한다. 과거 완료 턴은 감사·보존 정책을 위해 DB에 남을 수 있지만,
현재 generation 조회 조건에서 제외되어 모델 입력으로 다시 들어오지 않는다.
세션을 명시적으로 삭제하면 FK cascade가 관련 턴·요약을 물리 삭제하며, 동일
`conversation_id`를 재생성해도 새로운 `session_instance_id`를 사용한다.

**코드:** `reset()`, `delete()`, `_expire_due_locked()`, `_select_summary_locked()`

**테스트:** `test_reset_expiry_and_delete_recreate_invalidate_summary`, `test_delete_recreate_during_inference_rejects_old_instance`, `test_idle_expiry_is_exact_and_reads_do_not_extend_it`

## 자동화 검증 결과

2026-08-04에 전체 패키지 테스트를 실행했다.

```text
88 passed
```

검증 범위:

- 컨텍스트 원문 창·요약 경계 및 재시작 복원
- 문자 하드 캡과 overflow fallback
- 컨텍스트 크기 메트릭 및 재시도 보존
- 프롬프트 인젝션 문자열의 비신뢰 데이터 격리
- 요약의 결정성·리소스 상한·복구 경로
- 사용자별 장기 기억 격리 및 만료 제외
- 세션 reset·만료·삭제·재생성 무효화
- orchestrator의 세션·기억·provider 통합

## 남은 위험과 후속 작업

완료 조건은 충족했지만 운영 전 다음 항목은 계속 관찰해야 한다.

1. **문자 수와 토큰 수 차이**

   문자 하드 캡은 provider 독립적이지만 모델별 tokenizer의 실제 토큰 수와 일치하지 않는다. SWM25-72 평가에서 `provider.usage`와 문자 수의 상관관계를 기록해 모델별 권장 상한을 정한다.

2. **Extractive summary의 정보 손실**

   현재 요약은 작은 환경에서 결정적으로 실행되는 장점이 있지만 함축적 관계나 긴 시간축의 의미를 놓칠 수 있다. 한국어 멀티턴 회귀 데이터로 대명사 해소와 중요 사실 보존율을 측정한다.

3. **프롬프트 방어는 단독 보안 경계가 아님**

   `_untrusted` 분리와 system instruction만으로 모델 행동을 신뢰하지 않는다. Tool 실행은 계속 로컬 schema 검증, 현재 턴 intent gate, safety policy와 1회 소비 실행기를 거쳐야 한다.

4. **장기 기억 삭제의 저장 매체 수준 보장**

   논리 삭제·만료된 기억은 검색되지 않지만 SQLite 파일의 forensic erase까지 보장하지 않는다. 민감정보 정책에 따라 DB 암호화, 보존 기간, VACUUM 또는 저장소 폐기 절차를 별도 정의한다.

5. **운영 관측성**

   `overflow_fallback`, 잘린 영역, 입력 문자 수, 실제 토큰 수, 지연과 요약 fallback 비율을 대시보드 또는 구조화 로그로 집계한다. 원문 내용은 로그에 남기지 않는다.

6. **다중 프로세스 기억 변경 감지**

   현재 기억 revision은 프로세스 내부 변경을 감지한다. 여러 agent worker가
   같은 DB에 쓸 때는 DB revision 테이블이나 분산 잠금으로 확장해야 한다.

7. **실제 LLM 프롬프트 검증**

   Mock은 입력 경계와 안전 게이트를 결정론적으로 검증하지만 실제 모델의
   지시 추종 특성까지 증명하지 않는다. SWM25-72에서 같은 인젝션 fixture를
   주력·fallback provider에 반복 실행한다.

## 재검증 명령

패키지 디렉터리에서 실행한다.

```bash
cd malbut_agent_server
PYTHONPATH=. python3 -m pytest -q test
```

ROS 2 패키지 빌드 후 설치 공간을 기준으로 전체 패키지 테스트를 실행하려면 다음 명령을 사용한다.

```bash
# 저장소 루트에서
source /opt/ros/humble/setup.bash

colcon build --symlink-install --packages-select malbut_agent_server
source install/local_setup.bash
colcon test --packages-select malbut_agent_server
colcon test-result --verbose
```

> 위 ROS 2 전체 테스트 명령은 재검증용이다. 이 문서에 기록한 실행 결과
> `88 passed`는 앞의 전체 패키지 pytest 명령에 대한 결과다.
