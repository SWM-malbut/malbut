# SWM25-75~77 야간 자율 작업 기록

## 목표

사진에서 `할 일`로 표시된 다음 세 스토리를, 실제 외부 장치나 유료 API를
호출하지 않는 안전한 오프라인 계약과 검증 가능한 MVP 범위로 구현한다.

- SWM25-75 장기 기억 연동
- SWM25-76 음성 대화 파이프라인
- SWM25-77 감정 표현 연동

스토리 제목만으로 제품 방향을 임의 결정하지 않는다. 이미 승인된
[`SWM25-69_CONVERSATION_AGENT_CONTRACT.md`](../jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md)와
[`SWM25-69_INTERFACE_APPROVAL_GUIDE.md`](../jira/SWM25-69_INTERFACE_APPROVAL_GUIDE.md)의
경계 안에서만 구현한다. 실제 ROS adapter, STT/TTS provider, 표정 renderer가
필요한 항목은 계약·검증 경계까지만 만들고 미구현 상태를 명확히 기록한다.

## 작업 안전 경계

- 전용 로컬 브랜치: `overnight/swm25-75-77-20260813`
- push, PR, merge, deploy 금지
- OpenAI 및 다른 유료 API 호출 금지
- 실제 로봇 이동·촬영·알림·마이크·스피커·표정 renderer 호출 금지
- `.env*`, `owner-login.txt`, 자격 증명 파일 읽기 금지
- 사용자 기존 수정과 untracked 파일 삭제·덮어쓰기 금지
- 인메모리 SQLite, Mock/fake adapter, 순수 데이터 계약만 실행 허용

## 시작 상태

- 시작 HEAD: `f311cfe2a69a61079a0f893674d902f45abc490e`
- 확인한 `origin/main`: `984fcc5538969ecb726abaaa2e704c7ced92de72`
- 기존 변경: `README.md`, `setup.py` 수정 및 이전 감사·trace 산출물과
  저장소의 다른 untracked 파일이 존재한다. 모두 사용자 작업으로 보고 보존한다.
- 별도 worktree에는 이 미커밋 상태가 전달되지 않아 앞선 감사 산출물과의
  통합 검증이 끊긴다. 따라서 파일을 복사하거나 stash하지 않고 현재 checkout을
  전용 로컬 브랜치로 전환했다. 브랜치 전환은 파일 내용을 변경하지 않았다.
- 기준 회귀: `PYTHONPATH=. python3 -m pytest -q`
- 기준 결과: `153 passed in 7.75s`

## 진행 기록

### 2026-08-13 — 범위 감사 시작

세 스토리를 병렬로 감사한다. 우선 확인할 계약은 다음과 같다.

1. 장기 기억은 사용자/사람별로 격리하고 명시적 확인 없이 변경하지 않는다.
2. 음성은 final transcript만 대화 입력으로 만들며 partial transcript와
   self-echo는 실행 문맥으로 승격하지 않는다.
3. 감정 표현은 제한된 enum·강도·시간만 허용하고 이동 제어와 분리하며,
   긴급 상태에서는 neutral fallback을 적용한다.

아직 세 항목 모두 제품 완료로 판정하지 않는다. 구현·테스트·문서와 전체
회귀 증거를 모은 뒤 각각의 완료 범위와 남은 외부 통합을 구분한다.

### 2026-08-13 — 감사 판정

#### SWM25-75

기존 `SQLiteMemoryStore`는 사용자별 검색, 만료 제외, 내부 add/delete와
`0600` 파일 권한을 제공한다. 그러나 외부 제품 연동에서 필요한 확인 증거,
수정, 영속 revision, mutation idempotency, 내용 없는 audit가 없다. 프로세스
내 `_revision`만 있어서 재시작이나 두 DB 연결 사이의 변경을 감지하지 못한다.

오늘 밤 구현 범위는 기존 API 호환을 유지하는 core memory mutation 계약,
영속 사용자/record revision, 확인된 create/update/delete, request idempotency와
content-free audit다. 신뢰된 `person_id` 발급, 보존 기간 기본값, 기억 삭제 시
파생 대화까지 지울지는 제품 결정 blocker로 남긴다.

#### SWM25-76

현재 저장소에는 STT/TTS/VAD나 ROS 음성 타입이 없다. 기존 계약은 final
transcript만 사용하고 신뢰된 사용자·세션에 결속하며, TTS 끼어들기·취소와
self-echo 차단을 요구한다.

오늘 밤 구현 범위는 원시 오디오를 받지 않는 strict transcript schema,
서버 소유 identity binding, partial/저신뢰/mismatch/self-echo 사전 차단,
기존 orchestrator의 최종 안전 응답을 이용한 취소 가능 TTS 요청과 순수 Python
상태기계다. 실제 마이크·STT·TTS·ROS adapter는 호출하거나 구현 완료로
주장하지 않는다.

#### SWM25-77

`express_emotion`은 문서상 제안일 뿐 `TOOL_SPECS`, SafetyPolicy, Gateway,
renderer에는 없다. 현재 AgentDecision은 일반 답변과 Tool 호출 혼합도 금지하므로
일반 Tool로 단순 추가하면 “답변 + 표정” 요구와 충돌한다.

오늘 밤 구현 범위는 별도의 비실행 visual cue 계약과 결정론적 mapper,
긴급·privacy 우선 억제, TTL·빈도·idempotency, neutral fallback 및 recording
renderer 경계다. 사용자나 반려동물의 심리·의료 상태는 추론하지 않는다.
실제 얼굴 frontend와 ROS Action은 타입과 renderer 소유자 승인이 없어 blocker다.

### 2026-08-13 — SWM25-75 오프라인 core 완료

`SQLiteMemoryStore`에 기존 schema를 보존하는 migration과 세 종류의 영속
revision(record/user/global), 확인 기반 create·update·delete, `(user_id,
request_id)` 멱등성, update/delete CAS, content-free audit를 구현했다. 기억 row,
revision, audit, 멱등 응답은 한 `BEGIN IMMEDIATE` transaction에서 함께
commit한다. 두 SQLite connection과 재시작, 사용자 격리, 정확한 만료 경계,
v1 migration을 전용 테스트로 확인했다.

중간 검증 결과:

- `PYTHONPATH=. python3 -m pytest -q test/test_memory.py` → `13 passed`
- 통합 worktree 전체 회귀 → `196 passed`
- 대상 `flake8`, `pydocstyle`, `compileall`, `git diff --check` → 통과

현재 완료 표기는 **오프라인 core**에만 적용한다. `user_confirmed=True`는
그 자체로 신뢰 증명이 아니며, 동일 사용자의 실제 완료 turn 조회와 1회성
confirmation token, person identity, 보존·파생 삭제 정책이 정해지지 않아
HTTP/ROS mutation endpoint는 열지 않았다. 이 판단과 상세 근거는
[`SWM25-75_LONG_TERM_MEMORY_INTEGRATION.md`](../jira/SWM25-75_LONG_TERM_MEMORY_INTEGRATION.md)에
기록했다.

### 2026-08-13 — SWM25-76 오프라인 음성 계약 구현

`malbut_agent_server/speech.py`에 원시 오디오를 받지 않는 strict transcript
계약과 순수 Python coordinator를 추가했다. 서버가 소유한
speech-session/conversation/speaker/user 결속, final-only·confidence·sequence·
capture-epoch gate, 안정적인 Agent/TTS 상관 ID, 중복·변조 차단, self-echo
fence, barge-in cancel, terminal·close 이후 늦은 이벤트 차단을 구현했다.
Orchestrator의 raw Tool 제안이 아니라 SafetyPolicy를 거친 최종 문구만
interruptible TTS 요청에 들어간다.

독립 검토에서 거절된 높은 sequence가 정상 후속 발화를 막는 문제와 bounded
activity cache에서 제거된 옛 이벤트가 새 TTS를 취소할 수 있는 문제를 재현했다.
거절 이벤트는 accepted high-water mark를 전진시키지 않도록 바꾸고,
activity event에 현재 `capture_epoch`를 요구해 수정했다. 같은 conversation에
두 live speech session을 결속해 한 세션 close가 다른 세션을 깨뜨리는 문제도
프로세스 내 중복 binding 거절로 닫았다.

실제 STT·TTS·ROS, provider 호출 중 즉시 barge-in, 다중 프로세스 lease와
재시작 뒤 TTS exactly-once는 구현하지 않았고 blocker로 남겼다. 상세 계약은
[`SWM25-76_VOICE_CONVERSATION_PIPELINE.md`](../jira/SWM25-76_VOICE_CONVERSATION_PIPELINE.md)에
기록했다.

### 2026-08-13 — SWM25-77 비실행 시각 표현 계약 구현

`malbut_agent_server/expression.py`에 5개 allowlist emotion의 strict cue,
본문이나 사용자·반려동물 상태를 읽지 않는 결정적 mapper, 긴급 > privacy >
renderer availability > freshness 정책, dispatch/display TTL, 빈도 제한,
bounded process-local idempotency와 neutral fallback을 추가했다. 제공 renderer는
아무 동작도 하지 않거나 메모리에 호출만 기록하며 ROS·frontend·장치를
호출하지 않는다.

독립 검토에서 emergency 상태가 request conflict보다 늦게 적용되던 문제,
미래 `issued_at` cue, caller가 mapper 출처를 가장할 수 있던 문제, 일반 neutral
반복과 neutral 실패 재시도 문제를 재현했다. 긴급 clear를 conflict보다 먼저
적용하고, 미래 cue를 거절하며, 프로세스 내부 authority를 가진 mapper 결과만
submit하도록 바꿨다. 이미 neutral이면 renderer 호출을 coalesce하고 neutral
실패는 재귀 fallback 없이 renderer를 비활성화한다. cache eviction 뒤 영속
exactly-once와 blocking renderer timeout은 해결한 것처럼 주장하지 않고 실제
연동 blocker로 문서화했다.

상세 계약은
[`SWM25-77_EMOTION_EXPRESSION_INTEGRATION.md`](../jira/SWM25-77_EMOTION_EXPRESSION_INTEGRATION.md)에
기록했다.

### 2026-08-13 — 교차 검토에서 발견한 장기 기억 결함 수정

초기 단위 시험 통과 뒤 별도의 적대적 fixture로 다음 문제를 재현해 수정했다.

- provider가 전달받은 mutable memory list나 metadata를 바꾸면 inference 뒤
  revision·만료 fence를 우회할 수 있던 문제: 서버 snapshot과 provider copy 분리
- mutation의 exact retry 전에 현재 시각 기준 expiry를 검사해, 최초 성공 뒤
  시간이 지나면 같은 retry가 실패하던 문제: durable replay를 먼저 확인
- 만료된 기억을 update로 되살릴 수 있던 문제: 새 confirmed create만 허용
- 여러 connection이 legacy DB migration을 동시에 시작할 때 duplicate ALTER가
  발생하던 문제: `BEGIN IMMEDIATE` 안에서 schema를 재확인하고 오류 rollback

revision fence는 모든 writer가 현재 `SQLiteMemoryStore` 경로를 쓴다는 운영
전제까지 문서화했다. raw SQL이나 구버전 writer와 혼합하는 배포는 schema writer
gate 또는 DB trigger 없이는 허용할 수 없다.

### 2026-08-13 — 기능별 300회 반복 검증

첫 전체 실행은 SWM25-75 `276/300`, SWM25-76 `300/300`, SWM25-77
`300/300`이었다. SWM25-75 실패 24건은 모두 동시 first-open migration에서
간헐적으로 발생한 `database is locked`였다. 원인은 각 memory connection이
migration 전에 `PRAGMA journal_mode=WAL` 전환을 경쟁한 것이었다.

memory store의 first-open journal-mode 전환을 제거하고, schema migration을
단일 write transaction으로 직렬화했다. migration 실패 rollback 회귀를 추가한
뒤 동시 first-open을 별도로 100회 반복해 전부 통과했다. 이어 전체 matrix를
처음부터 다시 실행한 최종 결과는 다음과 같다.

| Story | story 반복 | 대표 subcheck | 실패 |
| --- | ---: | ---: | ---: |
| SWM25-75 | 300/300 | 4,200/4,200 | 0 |
| SWM25-76 | 300/300 | 8,400/8,400 | 0 |
| SWM25-77 | 300/300 | 12,300/12,300 | 0 |
| 합계 | 900/900 | 24,900/24,900 | 0 |

결과는
[`SWM25-75_77_300X_OFFLINE_2026-08-13.md`](../evaluations/SWM25-75_77_300X_OFFLINE_2026-08-13.md)와
mode `0600` JSON artifact에 남겼다. 합성 데이터·Mock·Noop/Recording adapter만
썼으며 실제 음성·화면·ROS·유료 API 호출은 0이다.

한 가지 절차상 이탈도 숨기지 않는다. 초기 migration 원인 분리 중 한 번의
진단 명령이 pytest 기본 임시 경로(`/tmp/pytest-of-user`)와 `/tmp`의 진단 출력
파일을 사용했다. 사용자 파일을 읽거나 삭제하지 않았고 제품 데이터도 쓰지
않았다. 이후 반복·최종 검증의 모든 임시 DB와 출력은 package 내부 경로로
제한했다.

### 2026-08-13 — 최종 검증

최종 소스와 문서를 기준으로 다음을 다시 실행했다.

- `pytest`: **237 passed in 8.25s**
- 집중 시험: memory 17, speech 28, expression 41, 합계 **86 passed**
- `flake8`: package·전체 test·scripts·`setup.py` 통과
- `pydocstyle`: 변경한 production module·새 test·stress runner 통과
- `git diff --check`: 통과
- package-contained colcon build: **1 package finished**
- package-contained colcon test: **237 tests, 0 errors, 0 failures, 0 skipped**
- 최종 300회 artifact: source SHA-256 **9/9 일치**, mode `0600`

build·test 임시 산출물은 정확한 package 내부 임시 경로만 대상으로 검증 뒤
정리했다. 기존 사용자 변경과 저장소 밖의 untracked 파일은 건드리지 않았다.

## 초기 아침 인계 판정 — 아래 후속 강화 기록으로 대체됨

SWM25-75~77은 모두 **외부 부작용이 없는 오프라인 안전 계약 MVP**까지
구현·문서화·반복 검증을 마쳤다. 스크린샷의 항목을 실제 제품 연동 의미로
`완료`로 바꾸면 안 된다. 남은 핵심은 다음과 같다.

1. SWM25-75: 신뢰된 person identity, evidence turn 검증과 1회성 확인 token,
   공개 HTTP/ROS adapter, 보존·파생 삭제 정책, mixed-version writer gate
2. SWM25-76: 실제 STT/TTS/ROS, source timestamp freshness, provider 추론 중
   즉시 barge-in, 다중 프로세스 lease, 재시작 뒤 TTS ledger, 실음성 품질·지연
3. SWM25-77: 실제 frontend/renderer/ROS Action, 비동기 timeout·cancel,
   재시작 뒤 영속 exactly-once, 제품 intensity·빈도·접근성 값

이 항목들은 제품 방향이나 외부 소유 인터페이스를 임의로 결정해야 하므로
오늘 밤 구현하지 않았다. push, PR, merge, deploy도 수행하지 않았다.

로컬 커밋에는 이번 75~77 변경과 함께, 작업 시작 때 이미 package 안에 있던
69~74 재검증·합성 trace 산출물을 포함한다. `README.md`와 `setup.py`가 그
파일들을 함께 참조·패키징하고 있어 일부만 커밋하면 새 checkout의 source
package가 불완전해지기 때문이다. 저장소 루트의 기존 benchmark/report,
Gazebo map과 `malbut_vision` untracked 파일은 staging하지 않았다.

## 2026-08-13 — 후속 완성도 강화 최종 기록

위 `237 passed`, `24,900` subcheck와 초기 blocker 목록은 첫 오프라인 MVP
시점의 이력이다. 그 상태를 별도 적대 검토한 뒤 발견한 결함을 수정하고 전체
검증을 다시 수행했다. 아래 결과가 이 작업의 최종 인계 기준이다.

### 구현 강화

- SWM25-75: persistent evidence provenance, v1/v2→v3 원자 migration,
  18개 row-DML writer gate, exact completed-turn validator, restart-safe
  idempotency와 cross-connection CAS를 추가했다.
- SWM25-76: provider 호출 밖의 per-session locking, result-aware completion
  guard, DB 응답과 로컬 TTS 예약의 선형화, 외부 session 소실의 typed 결과,
  barge-in·close 경합을 추가했다.
- SWM25-77: renderer를 lock 밖 bounded worker로 격리하고 generation/cancel
  fence, worker-start 오류, 긴급 lost-update, neutral timeout fail-closed와
  pending dispatch 공유를 추가했다.
- 공통: 같은 대화만 직렬화하고 서로 다른 대화는 병렬화했다. provider가
  memory snapshot을 변조하는 우회, 영문 후행 이동 금지문 우회, Gateway의
  boolean 정수 설정과 무제한 executor queue도 재현 후 수정했다.

### 최종 검증

- 전체 `pytest`: **584 passed**, 실패·skip 0
- package-contained `colcon build`: **1 package finished**
- package-contained `colcon test`: **584 tests**, 오류·실패·skip 0
- Mock 고정 suite: **90/90**, schema 100%, 5개 safety gate 모두 통과
- 전체 package coverage: line **93.18%**, branch **88.95%**
- 승인 문서가 지정한 핵심 모듈 aggregate: line **95.65%**, branch **93.16%**
- 이번 production 변경 executable line coverage: **96.89% (748/772)**. 남은 미실행
  방어문은 public 선행 검사가 차단하는 중복 guard와 희귀 private invariant다.
- `flake8`, `pydocstyle`, `compileall`, `git diff --check`: 모두 통과
- 최종 반복 artifact: **900/900 story iteration**,
  **40,500/40,500 subcheck**, 실패 0
- artifact manifest: runner·선택 test·production Python **30/30 SHA-256 일치**,
  `source_unchanged_during_run=true`, 생성 시 mode `0600`

### 최종 판정과 남은 blocker

SWM25-75~77의 **비부작용 오프라인 계약과 정책 경계**는 강화 구현·검증을
완료했다. Jira 제목을 실제 제품 연동까지 완료한 뜻으로 바꾸면 안 된다.

1. SWM25-75: evidence read→memory write 및 memory 재검사→conversation commit의
   cross-transaction TOCTOU, 신뢰 person identity, 1회성 confirmation-token
   CAS, 보존·파생 삭제 정책과 인증된 공개 adapter
2. SWM25-76: 실제 STT/TTS/ROS, source-clock freshness, durable TTS outbox,
   multi-process session lease, 실음성 WER·echo·latency
3. SWM25-77: 실제 frontend/ROS renderer, receiver-side generation CAS,
   uncooperative renderer process 격리, restart-safe execution ledger와 제품 UX값
4. 공통: completion guard post-yield 실패의 durable DB rollback 불가, 반환하지
   않는 Gateway adapter worker의 프로세스 종료 저해, lexical intent의 열린
   자연어 한계. 실제 실행에는 outbox/UoW, adapter deadline·격리, closed grammar
   또는 인증된 1회성 confirmation이 필요하다.
4. LLM 전체: 실제 OpenAI 5초 post-fix gate, hard wall-clock deadline,
   Terra→Luna→safe-refusal 운영 조합과 실제 Tool 실행 loop는 여전히 별도 단계

push, PR, merge, deploy, 유료 API와 실제 로봇·카메라·마이크·스피커·알림
호출은 수행하지 않았다. 저장소 루트 benchmark/report, Gazebo map,
`malbut_vision`의 기존 untracked 파일도 수정하거나 staging하지 않았다.
