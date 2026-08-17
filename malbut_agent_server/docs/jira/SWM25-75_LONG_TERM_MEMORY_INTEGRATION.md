# SWM25-75 장기 기억 연동

## 문서 정보

| 항목 | 내용 |
| --- | --- |
| Jira | SWM25-75 |
| 문서 목적 | 사용자별 장기 기억의 확인 기반 변경·멱등성·revision·감사 계약을 오프라인 SQLite core로 구현한 근거 |
| 기준일 | 2026-08-13 |
| 구현 위치 | `malbut_agent_server/memory.py`, `malbut_agent_server/memory_service.py` |
| 현재 상태 | **오프라인 core와 완료-turn 증거 service boundary 완료, person identity·HTTP/ROS adapter·보존 정책은 미연동** |
| 상위 계약 | `SWM25-69_CONVERSATION_AGENT_CONTRACT.md` 2.4·12.1, `SWM25-69_INTERFACE_APPROVAL_GUIDE.md` 3.3 |

## 1. 완료 범위

이번 구현은 실제 사람 인식, 네트워크, OpenAI, ROS 및 외부
장치를 호출하지 않는 로컬 저장 계약이다. 다음 범위를 구현했다.

- 기존 `add`, `search`, `search_with_revision`, `delete`,
  `list_for_user`, `purge_expired` 호환성 유지
- 기억 레코드별 `revision`, `updated_at`, conversation/turn 식별자와
  검증된 `session_instance_id`, `generation`, `completed_at` provenance 저장
- 사용자별 revision과 전체 revision의 SQLite 영속화
- 모델 호출 전후 같은 사용자 revision과 검색 row·만료를 다시 확인하는
  owner snapshot fence
- 명시적 확인·근거 식별자가 필수인 create·update·delete
- `(user_id, request_id)` 단위 내구성 있는 멱등성
- update·delete의 `expected_revision` compare-and-swap
- 기억 본문을 포함하지 않는 변경 감사 이벤트
- 동일 DB를 여는 두 connection과 프로세스 재시작 후의
  revision·멱등성 복원
- 기존 version-one `memories` 테이블의 in-place migration
- memory 전용 schema metadata와 SQLite trigger 기반 writer protocol gate
- 동일 사용자의 실제 completed conversation turn을 검증하는 injectable
  service boundary
- 만료 경계에서 검색·조회 제외, purge revision·감사 기록

공개 HTTP endpoint와 ROS Service adapter는 추가하지 않았다. 신뢰된
`person_id`와 확인 증거를 누가 발급하는지 확정하지 않은 상태에서
원격 변경 API를 열면 caller가 `user_id` 또는 `user_confirmed`를 위조할
수 있기 때문이다.

## 2. 저장 계약

### 2.1 테이블

| 테이블 | 역할 | 문자열 본문 복제 |
| --- | --- | --- |
| `memories` | 기억 본문, 소유자, 만료, record revision, 최종 근거 저장 | 필수 원본 1건 |
| `memory_user_revisions` | 사용자별 변경 세대 저장 | 없음 |
| `memory_store_state` | 기존 orchestrator 호환용 전체 변경 세대 저장 | 없음 |
| `memory_mutation_requests` | request fingerprint와 content-free 응답으로 재시작 후 중복 변경 차단 | 없음 |
| `memory_audit_events` | 관리 API에서 추가만 하는 변경 유형·revision·근거·시각 감사 기록 | 없음 |
| `memory_schema_metadata` | memory schema와 허용 writer protocol 범위 | 없음 |

모든 변경은 `BEGIN IMMEDIATE`로 시작해 기억 row, user/global
revision, audit event, idempotency 결과를 하나의 SQLite transaction에서
commit한다. 중간 예외에서는 전체를 rollback한다.

### 2.2 revision

세 revision의 역할은 다음과 같다.

| revision | 범위 | 용도 |
| --- | --- | --- |
| record revision | 기억 1건 | update·delete의 stale write 차단 |
| user revision | `user_id` 1개 | 해당 사용자 기억 snapshot 변경 감지 |
| global revision | 저장소 전체 | 기존 `AgentOrchestrator` revision 계약 호환 |

이전의 프로세스 메모리 `_revision`을 제거하고 DB에 저장했다.
따라서 다른 connection의 변경과 재시작 후의 변경 세대를 읽을 수
있다. 기존 호환 API `search_with_revision`은 검색 결과와 global revision을,
Agent가 사용하는 `search_with_owner_revision`은 검색 결과와 해당 user revision을
같은 read transaction snapshot에서 읽는다. 모델 호출 뒤
`owner_snapshot_is_current`가 같은 사용자의 revision, 검색된 row revision과
현재 만료 여부를 한 read transaction에서 다시 검사한다. 다른 사용자의
독립적인 기억 변경은 현재 요청을 불필요하게 폐기하지 않는다.

## 3. 확인된 변경 계약

### 3.1 core method

| method | 필수 조건 | 결과 |
| --- | --- | --- |
| `commit_confirmed` | `request_id`, content, `user_confirmed is True`, conversation/turn 근거 | 서버 생성 memory ID, revision 1 |
| `update_confirmed` | 위 확인 증거, memory ID, `expected_revision`, 새 content | record revision + 1 |
| `delete_confirmed` | 위 확인 증거, memory ID, `expected_revision` | 논리 삭제, 최종 revision·audit 결과 |

응답 `MemoryMutationResult`는 기억 본문을 반환하지 않고 memory ID,
record/user/global revision, audit event ID, 시각, 멱등 재사용 여부만
반환한다. 본문은 소유자 범위의 `get_for_user`로 별도 읽는다.

`user_confirmed=False`이거나 근거 conversation/turn 식별자가 비어 있으면
변경을 시작하지 않는다. low-level store는 trusted adapter와 fixture 호환을
위해 근거 식별자의 형식만 검사한다. 제품-facing 내부 호출은
`ConfirmedMemoryService`를 사용해야 한다. 기본
`SQLiteConversationEvidenceValidator`는 conversation store에서 정확히 같은
`user_id`, conversation ID, turn ID의 **completed** turn을 찾은 경우에만
변경을 전달한다. pending·없는 turn·다른 사용자의 turn은 같은
`MemoryEvidenceError`로 fail closed 한다. validator 결과도 service가
owner·conversation·turn binding과 generation·완료 시각을 다시 검사하므로
잘못 구현된 injected validator가 mutation을 통과시키지 못한다.
검증은 페이지나 최근 history window를 순회하지 않고 current generation의
turn identity를 SQL로 직접 조회하므로 500개보다 오래된 완료 turn도 정확히
찾는다. 새 mutation에는 active session의 current generation만 허용한다.
reset된 이전 generation 및 closed·expired session의 turn은 새 증거로
재사용할 수 없다.

service가 검증한 `session_instance_id`, `generation`, `completed_at`은
canonical request fingerprint, 현재 memory row, 관리 API의 추가 전용 audit event,
idempotency response와 그 전용 column 모두에 하나의 memory transaction으로
결속한다. 따라서 동일한 conversation ID와 turn ID를 reset 뒤 다시 써도
generation으로, conversation 삭제·재생성 뒤 다시 써도 session instance로
서로 다른 근거임을 감사 기록에서 구분할 수 있다. idempotency row는 호출자가
evidence 조회 전에 제시한 payload fingerprint도 별도로 저장한다. 그래서
성공한 exact retry는 기존 provenance까지 그대로 재생하지만, 검증 뒤 실제
mutation을 시작할 때에는 provenance를 포함한 full fingerprint도 다시
비교한다.

low-level confirmed method는 기존 trusted fixture·adapter 호환을 위해 세
provenance 인자를 optional로 유지한다. 세 값은 모두 있거나 모두 없어야 하며,
없으면 row·audit·result에 `NULL`인 **unknown provenance**로 명시된다. 이 경로는
완료 turn을 검증했다는 주장이 아니며 제품-facing 호출은 반드시
`ConfirmedMemoryService`를 사용해야 한다. service는 unknown provenance로
저장된 low-level 결과를 cached hit로 받아도 검증 완료 결과로 승격하거나
반환하지 않고 `MemoryEvidenceError`로 차단한다.

성공한 mutation의 exact retry는 conversation reset·delete·expiry 뒤에도
기존 durable 결과를 재생해야 한다. 따라서 service는 store의 content-free
idempotency 결과와 canonical fingerprint를 먼저 확인한다. 같은 payload는
evidence를 다시 조회하지 않고 `cached=True` 결과를 반환한다. 같은
request ID의 operation 또는 payload가 다르면 evidence 조회 전에 conflict로
차단한다. request ID가 처음인 경우에만 completed-turn evidence를 검증한 뒤
실제 mutation을 시작한다.

### 3.2 멱등성과 충돌

- 멱등성 key는 `(user_id, request_id)`다.
- 같은 key와 같은 operation·payload는 최초의 content-free 결과를
  `cached=True`로 반환하며 revision과 audit를 다시 늘리지 않는다.
- 같은 key의 operation 또는 payload가 다르면
  `MemoryMutationConflictError`로 차단한다.
- update·delete의 record revision이 `expected_revision`과 다르면 실행하지
  않는다.
- 다른 사용자의 memory ID는 없는 memory ID와 같은
  `MemoryNotFoundError`로 다루어 소유 여부를 누출하지 않는다.

## 4. 만료·migration·보안

- 조회와 검색의 활성 조건은 `expires_at IS NULL OR expires_at > now`다.
  따라서 `expires_at == now`인 기억은 즉시 제외된다.
- 확인된 create와 명시적 expiry update는 과거·현재 만료 시각을
  거절한다. 기존 `add`는 만료 fixture·adapter 호환을 위해 과거 시각을
  허용한다.
- 이미 만료된 row는 새 expiry를 넣더라도 update로 되살리지 않는다. 새 확인
  증거로 별도 memory를 create해야 한다. 단, 만료 전에 성공한 mutation의
  exact retry는 시간이 지난 뒤에도 기존 멱등 결과를 반환한다.
- `purge_expired` 또한 user/global revision을 늘리고 content-free
  `expire_purge` audit event를 남긴다.
- version-one DB를 열면 기존 row의 revision을 1,
  `updated_at`을 `created_at`, evidence를 `NULL`로 migration한다. schema/writer
  version 2 DB는 `BEGIN IMMEDIATE` 아래 기존 DML gate를 내리고 provenance
  column과 새 fingerprint를 backfill한 뒤 version 3 gate를 재설치한다.
- memory 전용 `memory_schema_metadata`가 schema version과 허용 writer
  protocol 범위를 저장한다. SQLite 전역 `PRAGMA user_version`은 conversation
  등 같은 DB의 다른 subsystem과 충돌하므로 사용하거나 변경하지 않는다.
- memory 관련 여섯 table(`memories`, `memory_store_state`,
  `memory_user_revisions`, `memory_mutation_requests`, `memory_audit_events`,
  `memory_schema_metadata`)의 INSERT·UPDATE·DELETE에만 설치된 18개 trigger는
  현재 writer가
  connection-local protocol 함수를 등록했는지 검증한다. 함수가 없는 raw
  SQL connection과 다른 protocol writer는 mutation 전에 거절된다. 동일
  SQLite 파일의 conversation 및 기타 비-memory table 쓰기는 영향받지 않는다.
- 이 trigger는 accidental raw DML·구버전 binary를 탐지하는 compatibility
  fence이지 DB 관리자에 대한 인증 경계가 아니다. 파일 권한을 가진 코드는
  trigger를 DROP하거나 동일 이름 함수를 가장할 수 있으므로 OS의 `0600`
  권한과 단일 runtime 소유 원칙을 계속 지켜야 한다.
- DB, WAL, SHM 파일은 기존처럼 `0600`을 유지한다.
- audit과 idempotency 결과에는 memory content·metadata를 저장하지
  않는다. idempotency table에는 canonical payload의 SHA-256 fingerprint만
  저장한다.
- 모델에 전달하는 기억은 계속 `memory_context_untrusted` JSON
  데이터이며 system instruction으로 승격하지 않는다.

## 5. 자동화 검증

### 5.1 SWM25-75 직접 검증

```bash
PYTHONPATH=. python3 -m pytest -q \
  test/test_memory.py test/test_memory_service.py
```

2026-08-13 강화 후 결과:

- `test/test_memory.py`: **65 passed**
- `test/test_memory_service.py`: **22 passed**
- 합계: **87 passed**

검증한 항목:

1. 한국어 검색과 사용자 간 검색 격리
2. 정확한 만료 경계와 purge
3. 기존 trusted API의 scoped delete·저장 파일 권한
4. 명시적 확인·evidence·record CAS 전체 lifecycle
5. create·update·delete 중복 요청의 프로세스 재시작 후 재사용
6. 두 SQLite connection 사이 revision 보이기와 stale update 차단
7. get·search·update·delete·audit의 사용자 격리
8. version-one DB migration과 기존 본문 보존
9. audit·idempotency 테이블에 기억 본문이 복제되지 않음
10. 동시 first-open migration, 오류 rollback과 writer lock 해제
11. 다른 사용자 변경 비간섭, 같은 사용자 변경·시간 만료·provider의
    memory list 변조에 대한 Agent snapshot fence
12. 미래 schema·incompatible writer fail-closed와 lock rollback
13. raw SQL·legacy writer mutation 차단 및 공유 DB 비-memory 호환성
14. completed-turn 증거의 owner·status 검증과 잘못된 validator 차단
15. evidence 삭제·재시작 뒤 exact retry와 다른 payload conflict
16. 501개 완료 turn 뒤에도 page window 없이 exact evidence 조회
17. create·update·delete 모두 evidence reset 뒤 exact retry 유지
18. closed·expired session은 새 mutation 차단, 기존 exact retry만 허용
19. 두 service connection의 동시 동일 요청은 1 commit·1 replay
20. 두 service connection의 동시 다른 payload는 1 commit·1 conflict
21. 완료 근거 provenance의 row·audit·idempotency·result 영속 결속
22. reset generation 및 삭제·재생성 session instance의 감사상 구분
23. low-level trusted compatibility의 명시적 unknown provenance
24. schema/writer version 2에서 version 3으로 원자적 migration과 old writer 차단
25. 검증 직후 reset을 삽입한 deterministic TOCTOU 재현과 provenance 보존
26. 재시작 뒤 row·audit·idempotency column/response·replay provenance 일치
27. low-level unknown provenance 결과의 service 검증 결과 승격 차단

### 5.2 전체 회귀

```bash
PYTHONPATH=. python3 -m pytest -q
```

초기 오프라인 기준선은 **237 passed in 8.25s**였다. 이후 provenance,
cross-connection, schema 손상, strict 입력과 snapshot 경계를 보강한 결과
SWM25-75 영향 범위(memory, evidence service, conversation, context window,
runtime)의 집중 회귀는 **152 passed**다. 전체 통합 결과는 이번 작업의 상위
worklog와 hardening 보고서에 기록한다.

```bash
python3 -m flake8 malbut_agent_server/memory.py \
  malbut_agent_server/memory_service.py \
  test/test_memory.py test/test_memory_service.py
python3 -m pydocstyle malbut_agent_server/memory.py \
  malbut_agent_server/memory_service.py \
  test/test_memory.py test/test_memory_service.py
```

결과: **둘 다 exit 0**

두 connection을 동시에 시작하는 same-payload idempotency 및
different-payload conflict 시험은 package 내부 임시 DB로 추가 20회씩
반복해 **40/40 invocation, 0 failure**를 확인했다. 임시 디렉터리는 검증 후
삭제했다.

## 6. 남은 제품 결정과 blocker

아래는 core store에서 임의로 정하면 제품 안전·개인정보 정책을
바꾸므로, 이번 범위에서 시행하지 않았다.

### 6.1 신뢰된 person identity

- `person_id`를 발급하는 인식 계층과 최소 confidence가 없다.
- 미인식·저신뢰 사용자의 개인 기억 조회·변경 정책이 없다.
- 현재 `user_id` SQL 격리는 신뢰된 identity binding을 대체하지
  않는다.

### 6.2 확인 증거와 공개 adapter

- `ConfirmedMemoryService`는 동일 사용자의 completed evidence turn을
  검증하지만, 그 turn의 자연어가 제품 의미상 어떤 확인 문구인지 또는
  UI confirmation token이 진짜인지 판정하지 않는다.
- HTTP/ROS로 공개하기 전에 인증된 UI/음성 확인 의미, 1회성 confirmation
  token, endpoint auth를 확정해야 한다.
- LLM 출력만으로 confirmed mutation method를 호출하는 경로를
  만들지 않는다.

### 6.3 보존 기간과 만료 운영

- 기본·최대 보존 기간, 기억 kind별 TTL, 사용자 변경 UX가
  확정되지 않았다.
- `purge_expired` 주기 scheduler와 재부팅 후 운영 주기가 없다.
- 보존 정책이 확정되기 전에 core가 임의의 기본 TTL을 선택하지
  않는다.

### 6.4 삭제의 파생 데이터 범위

- memory row를 삭제해도 그 기억으로 이미 생성된 대화 assistant
  text, rolling summary, durable response cache는 별도 conversation 정책에
  남을 수 있다.
- idempotency fingerprint, SQLite free page·WAL, backup을 포함한 forensic
  erase는 보장하지 않는다.
- `기억만 삭제`, `파생 대화도 redact`, `사용자 전체 삭제` 중 어느
  범위를 제품이 보장할지 개인정보 정책으로 확정해야 한다.

### 6.5 대용량·페이지·열거형

- 기존 `list_for_user`는 내부 진단용 호환 API라 페이지 상한이 없다.
- 공개 API 전에 cursor pagination, 사용자별 용량, kind/source
  allowlist, 요청 크기 제한을 확정해야 한다.

### 6.6 writer와 multi-process 운영 전제

- SQLite trigger gate 18개는 memory 관련 6개 테이블 각각의
  `INSERT`·`UPDATE`·`DELETE`에서 unmanaged raw SQL과 다른 protocol writer를
  fail closed 한다. 현재 store의 모든 row mutation은 revision을 함께 올린다.
  이 trigger는 DDL(`DROP TABLE` 등), 파일 변조, current-protocol 함수를 가장한
  동일 OS principal의 SQL을 막는 보안 경계가 아니다. DB 파일 접근 권한을 가진
  동일 OS principal은 신뢰한다.
- schema inspection과 migration은 `BEGIN IMMEDIATE`로 직렬화하고 실패 시
  rollback한다. journal mode는 memory store가 first-open마다 변경하지 않으며
  전체 runtime의 DB 소유 계층이 정한다.
- protocol이 다른 mixed-version process는 memory mutation을 할 수 없으므로
  rolling upgrade 전에 새 schema의 reader 호환성 및 배포 순서를 별도
  검증해야 한다.

### 6.7 inference fence와 response commit의 원자성

- owner snapshot 검사는 여러 connection과 time expiry를 정확히 감지한다.
  하지만 검사 직후 conversation response를 별도 transaction으로 commit하기
  전 다른 process가 같은 사용자의 memory를 변경할 수 있는 작은 TOCTOU
  구간은 남는다.
- 같은 SQLite 파일에서 두 connection의 writer transaction을 중첩하면
  self-lock이 발생할 수 있으므로 단순히 memory lock을 오래 잡아 해결하지
  않는다. 운영 exactly-once를 위해서는 단일 DB unit-of-work 또는 response
  commit SQL에 expected memory user revision CAS를 포함하는 설계가 필요하다.

### 6.8 evidence 검증과 memory mutation의 원자성

- `ConfirmedMemoryService`가 conversation completed turn을 검증한 뒤 memory
  transaction을 시작하기 전 reset·delete·expiry가 일어날 수 있는
  cross-transaction TOCTOU 구간은 남는다. 이번 변경은 **검증해 얻은 정확한
  provenance를 유실하지 않고 영속화**하지만 이 시점 경쟁을 원자적으로
  제거했다고 주장하지 않는다.
- 같은 SQLite 파일이어도 conversation store와 memory store가 별도 connection을
  소유하므로 conversation read transaction을 잡은 채 memory writer를 중첩하면
  self-lock·lock-order 역전 위험이 있다. 졸속 중첩 lock을 추가하지 않았다.
- 완전한 보장을 위해서는 두 subsystem을 소유하는 단일 unit-of-work가 completed
  turn의 `(session_instance_id, generation, completed_at)`을 조건으로 검사하고
  memory INSERT·audit·idempotency와 같은 transaction에서 commit하거나, 소비
  가능한 confirmation token ledger에 CAS를 적용해야 한다. reset 직전 검증을
  강제로 멈춘 뒤 reset하고 mutation을 재개하는 deterministic race test와 함께
  그 설계를 검증해야 한다.

## 7. 완료 판정

SWM25-75의 **오프라인 core service contract**는 구현·회귀 검증을
끝냈다. 다만 스크린샷의 `장기 기억 연동`을 실제 사람 인식과
운영 서비스까지 포함하는 의미로 `완료`처리하려면 6장의 identity,
확인, 보존, 삭제 정책과 adapter 통합을 먼저 닫아야 한다.
