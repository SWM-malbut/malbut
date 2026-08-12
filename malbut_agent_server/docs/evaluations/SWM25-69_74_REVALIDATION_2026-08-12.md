# SWM25-69~74 구현 재검증 및 300회 반복 기록 — 2026-08-12

## 문서 정보

| 항목 | 값 |
| --- | --- |
| 목적 | Jira의 `완료` 표시와 실제 구현·검증 범위를 다시 대조하고 반복 시험 결과를 보존 |
| 검증 시각 | 2026-08-12 21:27 KST 기준 |
| 로컬 HEAD | `f311cfe2a69a61079a0f893674d902f45abc490e` |
| 최신 `origin/main` | `984fcc5538969ecb726abaaa2e704c7ced92de72` |
| 양쪽 `malbut_agent_server` tree | `def94c0e21ff5c741ac778fffc4f35f198ba273d` — 동일 |
| 시험 모드 | 오프라인·비부작용, 실제 OpenAI 호출·ROS 물리 실행·외부 전송 없음 |
| 원시 결과 | [`artifacts/SWM25-69_74_300X_OFFLINE_2026-08-12.json`](artifacts/SWM25-69_74_300X_OFFLINE_2026-08-12.json) |
| 주 보고서 | [`SWM25-69_74_REVALIDATION_2026-08-12.html`](SWM25-69_74_REVALIDATION_2026-08-12.html) |
| 보고서 입력 | [`artifacts/SWM25-69_74_REVALIDATION_2026-08-12.artifact.json`](artifacts/SWM25-69_74_REVALIDATION_2026-08-12.artifact.json) |
| 전달 검증 | [`artifacts/SWM25-69_74_REVALIDATION_2026-08-12.delivery.json`](artifacts/SWM25-69_74_REVALIDATION_2026-08-12.delivery.json) |
| 원시 결과 SHA-256 | `c1153c3ef32d5fb55781e898fb0892be4d25a1172fa0fa94981f2b42a0136749` |
| 반복 harness SHA-256 | `72c625b921c8298466fd0ae8c4336d050b520e69a96632597b4ee7d109287340` |

로컬 브랜치는 저장소 전체로 보면 `origin/main`보다 뒤에 있지만, 이번 검증의
대상인 `malbut_agent_server` Git tree는 바이트 단위로 같다. 따라서 아래 코드
판정과 오프라인 시험은 최신 `origin/main`의 agent package에도 그대로 적용된다.

## 결론

Jira 화면의 여섯 `완료` 표시는 같은 뜻이 아니다. 계약 완료, 제한된 MVP 구현,
코드 병합, 운영 배포 승인을 분리하면 다음과 같다.

| Jira | 재검증 판정 | Jira 상태 권고 | 핵심 이유 |
| --- | --- | --- | --- |
| SWM25-69 | **계약 범위 완료** | `완료` 유지하되 `계약 전용` 표기 | 책임·인터페이스 승인 스토리이며 기능 구현 완료를 뜻하지 않는다고 문서가 명시 |
| SWM25-70 | **단일 프로세스 MVP 완료** | `완료` 유지 + 운영 후속 작업 분리 | 세션·순서·격리·멱등성은 구현됨. 실 LLM 멀티턴과 다중 worker는 미검증 |
| SWM25-71 | **내부 컨텍스트 경계 완료** | `완료` 유지 + 제품 연동 후속 작업 분리 | 요약·기억 격리·입력 제한은 구현됨. 공개 기억 CRUD와 신뢰된 사람 identity 연결은 없음 |
| SWM25-72 | **코드·오프라인 완료, 실 API 배포 보류** | `부분 완료` 또는 `배포 검증 중` | 5초 실 API 평가에서 두 모델 모두 schema 100% gate 실패, 현재 tree 실재평가도 없음 |
| SWM25-73 | **비부작용 Gateway 범위 완료** | `완료` 유지하되 `ROS 실행 아님` 표기 | registry·read-only·Mock 경계는 구현됨. production 실행 adapter는 0개 |
| SWM25-74 | **구현 근거 없음** | **`할 일`로 되돌림 권고** | 확인·실행·feedback 문서, 구현 커밋, PR, 실제 adapter가 없고 런타임은 의도적으로 실행을 차단 |

따라서 가장 큰 불일치는 **SWM25-74**다. 이번 `300/300`은 74 기능이
완성됐다는 뜻이 아니라, 아직 존재하지 않는 실행 권한을 현재 코드가 계속
차단했다는 **음성 증거(negative evidence)**다.

## 300회 반복 결과

한 스토리의 1회는 그 스토리에 선정한 대표 검사를 각각 한 번씩 실행한 것이다.
각 대표 검사도 개별적으로 정확히 300회 실행됐으며 모두 실패 0건이었다.

| Jira | 스토리 반복 | 세부 검사 실행 | p95/회 | 최대/회 | 해석 |
| --- | ---: | ---: | ---: | ---: | --- |
| SWM25-69 | 300/300 | 1,800/1,800 | 0.296 ms | 0.344 ms | schema·allowlist·현재 턴 intent·privacy gate 안정 |
| SWM25-70 | 300/300 | 1,800/1,800 | 17.109 ms | 27.755 ms | lifecycle·최근 10턴·격리·reset·만료·동시 예약 안정 |
| SWM25-71 | 300/300 | 1,800/1,800 | 34.836 ms | 52.070 ms | 요약 구간·하드 캡·untrusted 분리·기억 격리 안정 |
| SWM25-72 | 300/300 | 2,400/2,400 | 1.432 ms | 1.689 ms | fake HTTP 기반 payload/parser·retry·fallback 계약 안정 |
| SWM25-73 | 300/300 | 1,800/1,800 | 29.406 ms | 30.277 ms | registry·멱등성·동시 중복·차단·Mock·timeout 안정 |
| SWM25-74 | 300/300 | 900/900 | 3.205 ms | 4.151 ms | **실행 부재와 가짜 confirmation 차단만 확인** |
| **합계** | **1,800/1,800** | **10,500/10,500** | — | — | 실패한 story iteration·세부 검사 0건 |

전체 wall time은 `24,145.934 ms`였다. JSON은 `0600` 권한으로 기록했으며,
각 iteration의 성공 여부·지연, 각 세부 검사의 `attempted/passed/failed`, 실패가
있을 때 오류 타입·축약 메시지·traceback digest를 보존한다.

`10,500`은 검사 **호출 횟수**다. 선정 항목은 스토리 간 중복 하나를 포함한
35개 항목이고 고유 pytest/harness 함수는 34개다. JSON artifact 자체는
harness가 생성했지만, 시험 대상 애플리케이션은 사진·알림·Nav2 목표 같은
파일이나 외부 산출물을 만들지 않았다.

portable HTML은 canonical artifact validation과 payload 구조 검증을 통과했다.
환경에 호환 Chromium이 없어 browser interaction·desktop/narrow viewport QA는
실행하지 못했고 delivery receipt는 `structural_only`였다. HTML에는 같은
artifact에서 생성한 semantic 표·차트 데이터 fallback이 포함된다.

### 반복 검사 범위

- SWM25-69: request schema, 저수준 제어 Tool 부재, strict Tool schema,
  불신 state 실행 차단, 현재 턴 navigation intent, camera privacy
- SWM25-70: create/get/close/delete, 최근 10턴, 사용자·세션 격리, reset,
  정확한 idle expiry, 동시 pending 차단
- SWM25-71: raw window와 summary의 무공백·무중복, 전체 입력 하드 캡,
  untrusted JSON 분리, 과거 prompt injection 차단, 사용자별 기억, 기억 만료
- SWM25-72: strict Responses payload와 Tool parsing, structured text,
  credential 비노출, 완료 상태 강제, retry 범위, 인증 실패 시 fallback 금지,
  모든 provider 실패 시 비행동 refusal, primary/fallback factory 조립
- SWM25-73: 서버 소유 registry, strict read-only query, 동시 중복 1회,
  부작용·unknown Tool 차단, 명시적 비부작용 simulation, timeout·stale·오류 정규화
- SWM25-74: policy 통과와 실행 권한 분리, 부작용 query 차단,
  schema 밖의 가짜 confirmation 거부와 production 실행 capability 0개

### 300회가 증명하지 않는 것

이 시험은 전체 153개 pytest를 300번 실행한 것이 아니라, 스토리별 핵심 경계를
대표하는 3~8개 결정론적 검사를 같은 Python 프로세스에서 300번 반복한 것이다.
따라서 다음 항목의 증거로 사용하면 안 된다.

- OpenAI 모델 응답 품질·비용·실제 네트워크 지연의 300회 결과
- Terra → Luna → safe refusal 운영 조합의 신뢰성
- 여러 서버·worker·프로세스·재시작을 가로지르는 동시성
- Nav2, 카메라 파일 저장, 알림 전송 등 실제 ROS 부작용
- confirmation, 영속 exactly-once, Action feedback·cancel의 구현 성공

유료 OpenAI 300회 또는 물리 로봇 300회는 비용·외부 부작용·장비 안전 범위가
별도로 필요하므로 이번 재검증에 포함하지 않았다.

## 공통 빌드·회귀 근거

2026-08-12 로컬에서 agent package만 대상으로 다음을 다시 실행했다.

| 검사 | 결과 |
| --- | --- |
| `colcon build --symlink-install --packages-select malbut_agent_server` | 성공 |
| `colcon test --event-handlers console_direct+ --packages-select malbut_agent_server` | `153 tests`, 실패 0, 오류 0, skip 0 |
| 반복 harness `flake8` | 통과 |
| 반복 harness `pydocstyle` | 통과 |
| 최신 `origin/main` CI | [run 31575451086](https://github.com/SWM-malbut/malbut/actions/runs/31575451086), 성공 |

저장소 전체의 과거 test-result까지 합친 명령에서는 이번 agent 범위와 무관한
`malbut_gazebo/test_flake8.py`의 기존 lint 34건이 함께 보고됐다. 위의
`malbut_agent_server` package-scoped build/test는 독립적으로 성공했으며, 이
보고서는 Gazebo lint를 수정하거나 agent 결과로 숨기지 않는다.

## 스토리별 감사 근거

### SWM25-69 — 계약 완료이지 기능 완료가 아님

계약은 스스로 “책임·인터페이스 합의이며 각 기능의 구현 완료를 의미하지
않는다”고 명시한다. 승인 체크와 승인 댓글은 존재하므로 계약 스토리로서의
완료 판정은 가능하다.

- 범위 선언: [`SWM25-69_CONVERSATION_AGENT_CONTRACT.md`](../jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md#L16)
- 달성 조건과 승인: [`SWM25-69_CONVERSATION_AGENT_CONTRACT.md`](../jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md#L965)
- 실제 실행기·confirmation·cancel의 차이: [`SWM25-69_CONVERSATION_AGENT_CONTRACT.md`](../jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md#L771)
- 병합 근거: [PR #10](https://github.com/SWM-malbut/malbut/pull/10),
  [계약 검토 PR #15](https://github.com/SWM-malbut/malbut/pull/15),
  [승인 기록 PR #16](https://github.com/SWM-malbut/malbut/pull/16)

주의할 점은 여섯 도메인의 승인이 한 명의 위임 reviewer가 작성한 한 GitHub
댓글에 묶여 있다는 것이다. 저장소에서는 위임을 독립적으로 증명하는 별도
artifact를 찾지 못했으므로, 조직 절차상 필요하면 Jira 권한·위임 기록을
추가로 첨부해야 한다.

### SWM25-70 — 단일 프로세스 세션 MVP

SQLite session lifecycle, turn 순서, 최근 10턴, 사용자·세션 격리, reset,
expiry, durable request idempotency와 단일 프로세스 동시 요청 차단은 구현돼
있다. 문서도 운영 판정을 “단일 프로세스 MVP”로 한정한다.

- 범위와 운영 판정: [`SWM25-70_MULTITURN_CONVERSATION_SESSION.md`](../jira/SWM25-70_MULTITURN_CONVERSATION_SESSION.md#L7)
- 단일 프로세스 동시성 범위: [`SWM25-70_MULTITURN_CONVERSATION_SESSION.md`](../jira/SWM25-70_MULTITURN_CONVERSATION_SESSION.md#L307)
- 실 provider·다중 worker·sweeper·auth·ROS 후속: [`SWM25-70_MULTITURN_CONVERSATION_SESSION.md`](../jira/SWM25-70_MULTITURN_CONVERSATION_SESSION.md#L344)
- 병합 근거: [PR #11](https://github.com/SWM-malbut/malbut/pull/11),
  [stack 통합 PR #17](https://github.com/SWM-malbut/malbut/pull/17)

현재 live evaluator는 case마다 새 conversation의 첫 턴을 만들기 때문에,
대명사·후속 지시를 포함한 실제 모델의 장기 멀티턴 품질은 아직 검증하지 않는다.

### SWM25-71 — 내부 컨텍스트 경계

최근 원문과 rolling summary 분리, 사용자별 장기 기억, revision 확인, 전체
입력 문자 상한과 overflow fallback, history·summary·memory의 untrusted JSON
분리가 구현돼 있다.

- 내부 범위와 공개 CRUD 제외: [`SWM25-71_USER_CONTEXT_INTEGRATION.md`](../jira/SWM25-71_USER_CONTEXT_INTEGRATION.md#L84)
- 남은 삭제·다중 프로세스·실 LLM 위험: [`SWM25-71_USER_CONTEXT_INTEGRATION.md`](../jira/SWM25-71_USER_CONTEXT_INTEGRATION.md#L273)
- 병합 근거: [PR #12](https://github.com/SWM-malbut/malbut/pull/12),
  [stack 통합 PR #17](https://github.com/SWM-malbut/malbut/pull/17)

따라서 내부 context builder 스토리는 완료로 볼 수 있지만, 신뢰된
`person_id`, 동의 기반 memory CRUD, 다중 worker revision과 저장 매체 수준
삭제까지 끝났다고 보면 안 된다.

### SWM25-72 — Provider 코드는 있으나 배포 gate 실패

OpenAI Responses adapter, strict schema, `store:false`, 단일 Tool 호출,
endpoint 제한, 오류 정규화, bounded retry·circuit·same-vendor fallback과
safe refusal은 구현돼 있다.

- 현재 상태와 provider 한계: [`SWM25-72_LLM_PROVIDER_INTEGRATION.md`](../jira/SWM25-72_LLM_PROVIDER_INTEGRATION.md#L7)
- 남은 hard deadline·실 fallback·관측성: [`SWM25-72_LLM_PROVIDER_INTEGRATION.md`](../jira/SWM25-72_LLM_PROVIDER_INTEGRATION.md#L365)
- 병합 근거: [PR #14](https://github.com/SWM-malbut/malbut/pull/14),
  [stack 통합 PR #17](https://github.com/SWM-malbut/malbut/pull/17)

그러나 5초 실 API post-fix 평가는 Luna `89/90`, Terra `86/90`만 schema-valid라
둘 다 schema 100% gate에 실패했다. Terra end-to-end p95는 `6.718초`, 최대는
`9.007초`였다. 해당 보고서의 결론도 **전체 배포 승인 보류**다.

- 실평가 결과: [`SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md`](SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md#L41)
- formal gate: [`SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md`](SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md#L70)
- 지연과 hard deadline 공백: [`SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md`](SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md#L88)

그 실평가의 runtime SHA는 72 당시 코드에 대한 것이다. 73 통합 뒤 현재 agent
tree의 실제 OpenAI 재평가와 실제 Terra → Luna 조합 시험 기록은 없다. 이번
300회는 fake provider·fake HTTP 기반이므로 이 운영 공백을 닫지 않는다.

### SWM25-73 — 비부작용 Gateway 완료

서버 소유 registry, 요청 Tool과의 교집합, strict 입력·출력, read-only adapter
경계, timeout·freshness 검증, 프로세스 내 중복 억제와 명시적 Mock simulation은
구현돼 있다. PR CI도 성공했으므로 문서의 “구현 중”과 미체크 CI는 stale하다.

- 명시된 제한 범위: [`SWM25-73_AGENT_TOOL_GATEWAY.md`](../jira/SWM25-73_AGENT_TOOL_GATEWAY.md#L7)
- SWM25-74 이관 범위: [`SWM25-73_AGENT_TOOL_GATEWAY.md`](../jira/SWM25-73_AGENT_TOOL_GATEWAY.md#L286)
- 달성 조건과 stale CI checkbox: [`SWM25-73_AGENT_TOOL_GATEWAY.md`](../jira/SWM25-73_AGENT_TOOL_GATEWAY.md#L307)
- 병합·CI 근거: [PR #18](https://github.com/SWM-malbut/malbut/pull/18)

production registry는 실제 adapter를 하나도 연결하지 않고, simulation은
Nav2 goal·촬영 파일·외부 알림을 만들지 않는다. 메모리 cache는 재시작하면
사라지며 이미 시작한 Python thread를 강제 취소하지 못한다. 따라서 이 완료는
실제 ROS Tool 실행 완료가 아니라 **실행 전 안전 경계 완료**를 뜻한다.

### SWM25-74 — 완료로 볼 수 없음

최신 `origin/main`에서 다음 항목을 모두 다시 검색했지만 SWM25-74 구현 증거가
없었다.

- `SWM25-74` 전용 Jira 구현 문서 없음
- `SWM25-74` 제목·본문의 구현 커밋 없음
- `SWM25-74` 구현 PR 없음
- confirmation endpoint·증거 schema 없음
- 별도 `tool_call_id` 발급과 영속·원자적 1회 소비 없음
- 실제 ROS Service·Action adapter, lifecycle, feedback, cancel, 감사 로그 없음

반대로 현재 구현은 실행 부재를 명시적으로 고정한다.

- 모든 응답의 `authorized=false`, `consume_once=false`, `tool_call_id=null`:
  [`orchestrator.py`](../../malbut_agent_server/orchestrator.py#L95)
- production 기본 registry에 실행 adapter 없음:
  [`gateway.py`](../../malbut_agent_server/gateway.py#L640)
- 이 상태를 요구하는 회귀 테스트:
  [`test_orchestrator.py`](../../test/test_orchestrator.py#L345)
- 74가 소유해야 하는 정확한 목록:
  [`SWM25-73_AGENT_TOOL_GATEWAY.md`](../jira/SWM25-73_AGENT_TOOL_GATEWAY.md#L294)

따라서 Jira에서 74를 `완료`로 둔 근거가 저장소 밖에 따로 없다면 `할 일`로
되돌리고, confirmation → 최신 trusted state 재검사 → durable consume-once →
ROS 실행 → feedback/cancel → terminal audit의 end-to-end 구현과 검증을 새로
진행해야 한다.

## 재현 방법

전체 agent package 회귀:

```bash
cd ~/ros2_ws
colcon build --symlink-install --packages-select malbut_agent_server
colcon test --event-handlers console_direct+ \
  --packages-select malbut_agent_server
colcon test-result \
  --test-result-base build/malbut_agent_server \
  --all --verbose
```

스토리별 대표 경계 300회 반복과 mode `0600` JSON 기록:

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server
PYTHONPATH=. python3 scripts/run_swm25_69_74_stress.py \
  --iterations 300 \
  --progress-every 50 \
  --output \
    docs/evaluations/artifacts/SWM25-69_74_300X_OFFLINE_2026-08-12.json
```

결과 파일 무결성 확인:

```bash
sha256sum \
  docs/evaluations/artifacts/SWM25-69_74_300X_OFFLINE_2026-08-12.json
```

기대 digest는 문서 상단의 SHA-256과 같아야 한다. harness나 대상 코드가
바뀌면 과거 결과를 덮어쓰지 말고 날짜가 다른 새 artifact를 생성한다.
