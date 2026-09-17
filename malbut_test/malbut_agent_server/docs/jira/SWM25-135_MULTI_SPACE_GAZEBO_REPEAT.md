# SWM25-135 거실·주방·침실 Gazebo Nav2 폐루프 3회 연속 완료

## 1. 결론

SWM25-135는 SWM25-133의 실제 폐루프와 SWM25-134의 campaign runner를 복제하지
않고, **서로 다른 세 semantic 공간을 대상으로 fresh full-stack case를 세 번
순서대로 실행**하는 정상 인수 시험이다.

```text
case-001: happy_living_room
  -> 거실 요청 -> 승인 -> RobotAction -> Robot Web -> actual Nav2
case-002: happy_kitchen
  -> 주방 요청 -> 승인 -> RobotAction -> Robot Web -> actual Nav2
case-003: happy_bedroom
  -> 침실 요청 -> 승인 -> RobotAction -> Robot Web -> actual Nav2
  -> 세 child success·서로 다른 target binding·cleanup 집계
  -> campaign PASSED 또는 fail-closed
```

이는 로봇 한 대가 한 runtime에서 세 목적지를 연속 순찰하는 제품 기능이 아니다.
각 case는 새 private runtime, SQLite, Gazebo와 child evidence를 사용한다. 따라서
한 case의 원장·ROS goal·process가 다음 case에 남지 않았다는 것까지 정상 반복의
일부로 검증한다.

## 2. 목표

기존 거실 한 곳만 성공한 결과를 세 번 복제하지 않고, 거실·주방·침실이라는
서로 다른 named destination이 같은 안전·승인·durable execution 경계를 통해
각각 정확히 한 번 실행되는지 확인한다. 세 공간 모두 known terminal success이고
모든 자원이 정리된 경우에만 전체 campaign을 합격시킨다.

legacy `happy_path`의 요청과 CLI 기본값은 유지한다. 다만 과거 private fixture의
거실 target은 실제 생활 공간을 표현하기보다 한 개 synthetic Room에서 얻은 시험
점이었다. SWM25-135 fixture는 기존 이름/API 호환은 유지하면서 실제 Small House
world의 소파, 조리대, 침대 구역에 대응하는 기존 vetted route waypoint를 각각의
새 target cell로 사용한다. 과거 SWM25-133/134 evidence는 과거 commit과 binding에
결속된 기록으로 그대로 남는다.

## 3. 달성 조건 5개

### 3.1 세 공간을 server-owned profile로 고정

- [x] `happy_living_room`, `happy_kitchen`, `happy_bedroom`을 bounded enum으로
  정의한다.
- [x] 각 profile의 요청 문장과 semantic location을 서버가 고정하고 raw 요청·위치·
  좌표 입력을 campaign CLI가 받지 않게 한다.

### 3.2 세 목적지의 의미와 안전성 검증

- [x] private Small House fixture에 거실·주방·침실의 겹치지 않는 target cell을
  만든다.
- [x] 각 cell 전체가 map bounds 안의 free occupancy이고 생성된 Zone/clearance
  mask가 0인지 fixture 생성 시 fail-closed한다.
- [x] 세 이름, 대표점과 target binding digest가 서로 다름을 contract test로
  고정한다.

이 검증은 simulation fixture에 한정된다. 실제 로봇의 authoritative forbidden
zone이나 hardware safety evidence를 주장하지 않으며 physical authority는 계속
OFF다.

### 3.3 기존 실행 경로를 세 번 fresh 실행

- [x] campaign이 profile을 installed SWM25-133 child runner에 명시적으로 전달한다.
- [x] child가 profile에 결속된 Agent 요청, confirmation, RobotAction, Robot Web과
  Nav2 경로를 그대로 사용한다.
- [ ] clean installed overlay에서 거실 -> 주방 -> 침실 순서의 actual Gazebo case
  3개가 모두 known terminal `SUCCEEDED`를 반환한다.

각 case의 성공 조건은 승인 전 Nav2 goal 0개, 승인 후 proposal·confirmation·
RobotAction·dispatch intent·verified target·start·distinct Nav2 goal·terminal
result 각각 1개와 replay 추가 효과 0개다.

### 3.4 profile·target·evidence를 강하게 결속

- [x] child evidence v3에 bounded `scenario_profile`과
  `target_binding_digest`를 기록한다.
- [x] 요청 profile과 child profile이 다르거나 한 semantic location의 target
  binding이 실행마다 바뀌면 전체를 거절한다.
- [x] 서로 다른 semantic location 둘이 같은 target binding digest를 재사용하면
  성공한 Nav2 결과가 있어도 campaign을 거절한다.

campaign aggregate v2에는 원문, 좌표, private ID와 host path를 기록하지 않는다.
과거 child v2/campaign v1 evidence를 현재 성공 증거로 추정 변환하지 않고 현재
commit에서 새 evidence를 생성한다.

### 3.5 3/3와 zero residue를 하나의 판정으로 증명

- [x] source unit/contract test에서 세 case 순서, profile 전달, mismatch 차단과
  target binding 유일성을 검증한다.
- [ ] post-commit clean build, installed `--check`, actual 3-case campaign과 종료
  후 process·ROS node·socket 0개를 검증한다.
- [ ] aggregate가 `case_count=3`, `passed_case_count=3`, `stopped_early=false`,
  `simulation=true`, `physical_authorized=false`로 원자 발행된다.

하나라도 실패, timeout, evidence 손상 또는 cleanup 불완전이면 전체는 불합격이고
안전한 새 증거 경로에서 case-001부터 다시 수행한다. 불명 결과를 성공으로 바꾸거나
자동 재전송하지 않는다.

## 4. 계층별 책임

```text
Campaign CLI
  -> 허용 profile과 순서만 받음
Campaign core
  -> 기대 결과, 순차 실행, 중단·전체 verdict
Installed SWM25-133 adapter
  -> child에 bounded scenario profile 전달
SWM25-133 acceptance runner
  -> profile에 고정된 텍스트 요청·승인·RobotAction·실제 Nav2
Semantic fixture
  -> active map에서 선택할 세 named target 제공
Child evidence v3
  -> exact-once 결과 + scenario/target binding
Campaign evidence v2
  -> 세 child provenance·binding·cleanup 교차 검증
```

LLM의 임의 출력이나 CLI 문자열이 목적지를 직접 만들지 않는다. scenario profile이
server-owned 요청과 location을 선택하고, 기존 active map resolver가 현재 target을
해석하며, 승인 뒤 fresh state와 Safety를 재검사한 다음에만 기존 façade가 Nav2를
시작한다.

## 5. 검증 상태

2026-08-30 현재 다음 구현과 source 검증을 완료했다.

- 세 공간 semantic fixture와 전체 cell 안전성 검사
- scenario/profile API와 legacy `happy_path` 기본 동작
- Agent/acceptance/campaign child profile 전달
- evidence v3 및 aggregate v2 target binding 검증
- invalid/raw profile, profile relabel, target binding 재사용의 fail-closed test
- 변경 경로 통합 subset 351개 통과
- pre-commit isolated overlay에서 dependency 11개 package build 성공
- generated ROS interface를 포함한 `malbut_scenarios` 기능 test 467개 통과

전역 PEP257 test는 이번 diff 밖에 이미 존재하던 docstring 4건 때문에 실패한다.
이번 변경 Python 파일만 대상으로 한 flake8와 pydocstyle은 통과했으며, 기준선 문제를
이번 Sub-task에서 임의 수정하지 않는다. clean installed build, non-actuating check
및 실제 Gazebo 3/3 campaign 결과는 아직 이 문서에서 완료로 주장하지 않는다.
변경을 local commit으로 고정한 뒤 같은 commit으로 isolated overlay를 build하고
최종 실행 결과를 이 문서와 Jira 결론에 추가한다.

## 6. 명시적 제외

- 한 runtime에서 거실·주방·침실을 도는 다중 waypoint 순찰
- fault, duplicate, worker 경쟁, stale state, E-stop과 unknown reconciliation
- 실제 로봇, STT/TTS, Homecam/AWS와 physical authority
- raw 좌표 또는 임의 ROS goal을 받는 public CLI
- 과거 evidence를 현재 schema의 성공 증거로 자동 변환

위 장애·중복·최종 혼합 campaign은 SWM25-136~139에서 같은 runner 경계를 확장해
검증한다.
