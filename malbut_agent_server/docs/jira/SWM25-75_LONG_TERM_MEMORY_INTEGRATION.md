# SWM25-75 장기 기억 연동

## 문서 정보

| 항목 | 내용 |
| --- | --- |
| Jira | SWM25-75 |
| 문서 목적 | 사용자별 장기 기억의 확인 기반 변경·멱등성·revision·감사 계약을 오프라인 SQLite core로 구현한 근거 |
| 기준일 | 2026-08-13 |
| 구현 위치 | `malbut_agent_server/memory.py` |
| 현재 상태 | **오프라인 core MVP 완료, 신뢰된 person identity·HTTP/ROS adapter·보존 정책은 미연동** |
| 상위 계약 | `SWM25-69_CONVERSATION_AGENT_CONTRACT.md` 2.4·12.1, `SWM25-69_INTERFACE_APPROVAL_GUIDE.md` 3.3 |

## 1. 완료 범위

이번 구현은 실제 사람 인식, 네트워크, OpenAI, ROS 및 외부
장치를 호출하지 않는 로컬 저장 계약이다. 다음 범위를 구현했다.

- 기존 `add`, `search`, `search_with_revision`, `delete`,
  `list_for_user`, `purge_expired` 호환성 유지
- 기억 레코드별 `revision`, `updated_at`,
  `evidence_conversation_id`, `evidence_turn_id` 저장
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
| `memory_audit_events` | 변경 유형·revision·근거·시각의 append-only 감사 기록 | 없음 |

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
변경을 시작하지 않는다. 다만 core store는 근거 식별자의 **형식**만
검증한다. 해당 turn이 완료된 동일 사용자의 실제 대화인지는 추후
service layer가 `SQLiteConversationStore`와 함께 검증해야 한다.

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
  `updated_at`을 `created_at`, evidence를 `NULL`로 migration한다.
- DB, WAL, SHM 파일은 기존처럼 `0600`을 유지한다.
- audit과 idempotency 결과에는 memory content·metadata를 저장하지
  않는다. idempotency table에는 canonical payload의 SHA-256 fingerprint만
  저장한다.
- 모델에 전달하는 기억은 계속 `memory_context_untrusted` JSON
  데이터이며 system instruction으로 승격하지 않는다.

## 5. 자동화 검증

### 5.1 SWM25-75 직접 검증

```bash
PYTHONPATH=. python3 -m pytest -q test/test_memory.py
```

2026-08-13 최종 결과: **17 passed**

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

### 5.2 전체 회귀

```bash
PYTHONPATH=. python3 -m pytest -q
```

2026-08-13 최종 통합 워크트리 결과: **237 passed in 8.25s**

```bash
python3 -m flake8 malbut_agent_server/memory.py test/test_memory.py
python3 -m pydocstyle malbut_agent_server/memory.py test/test_memory.py
```

결과: **둘 다 exit 0**

## 6. 남은 제품 결정과 blocker

아래는 core store에서 임의로 정하면 제품 안전·개인정보 정책을
바꾸므로, 이번 범위에서 시행하지 않았다.

### 6.1 신뢰된 person identity

- `person_id`를 발급하는 인식 계층과 최소 confidence가 없다.
- 미인식·저신뢰 사용자의 개인 기억 조회·변경 정책이 없다.
- 현재 `user_id` SQL 격리는 신뢰된 identity binding을 대체하지
  않는다.

### 6.2 확인 증거와 공개 adapter

- core의 `user_confirmed is True`와 evidence ID는 신뢰된 service caller를
  전제한다.
- HTTP/ROS로 공개하기 전에 동일 사용자의 완료된 evidence turn
  조회, 인증된 UI/음성 확인, 1회성 confirmation token, endpoint auth를
  확정해야 한다.
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

- revision fence는 모든 memory writer가 현재 `SQLiteMemoryStore` mutation
  경로를 사용한다는 전제다. raw SQL이나 이전 schema writer는 revision을
  올리지 않으므로 같은 DB에 동시에 쓰면 안 된다.
- schema inspection과 migration은 `BEGIN IMMEDIATE`로 직렬화하고 실패 시
  rollback한다. journal mode는 memory store가 first-open마다 변경하지 않으며
  전체 runtime의 DB 소유 계층이 정한다.
- mixed-version 배포 전에는 schema version writer gate 또는 DB trigger를
  추가하고 구버전 writer가 모두 내려갔다는 운영 증거가 필요하다.

## 7. 완료 판정

SWM25-75의 **오프라인 core service contract**는 구현·회귀 검증을
끝냈다. 다만 스크린샷의 `장기 기억 연동`을 실제 사람 인식과
운영 서비스까지 포함하는 의미로 `완료`처리하려면 6장의 identity,
확인, 보존, 삭제 정책과 adapter 통합을 먼저 닫아야 한다.
