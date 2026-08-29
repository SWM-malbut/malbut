# SWM25-134 기존 Gazebo 1회 테스트를 여러 조건으로 실행하고 결과를 하나로 모은다

## 1. 결론

SWM25-134는 SWM25-133에서 만든 실제 Gazebo 폐루프를 복사해 새로운 Agent,
RobotAction 또는 Nav2 실행 경로를 만드는 작업이 아니다. 설치된
`run_text_gazebo_acceptance`를 **하나의 독립적인 case runner**로 재사용하고,
허용된 case들을 정해진 순서로 실행한 뒤 결과와 정리 상태를 하나의 campaign
판정으로 모으는 상위 시험 도구를 만드는 작업이다.

```text
run_text_gazebo_campaign
  -> case plan 검증
  -> case-001: installed SWM25-133 runner 실행
       -> Agent -> confirmation -> RobotAction -> Robot Web -> actual Nav2
       -> child evidence + cleanup 결과
  -> case-002: installed SWM25-133 runner 실행       # 후속 Story에서 추가
       -> 독립 DB/runtime/evidence
       -> child evidence + cleanup 결과
  -> ...
  -> 각 제품 결과와 기대 결과 비교
  -> 모든 case의 시험 판정과 cleanup 집계
  -> content-free campaign evidence 1개 원자 발행
```

이 구조에서 SWM25-133은 한 번의 실제 제품 폐루프를 소유하고, SWM25-134는
어떤 case를 어떤 순서로 실행하며 전체를 어떻게 합격·불합격으로 판정할지만
소유한다. 따라서 campaign 계층에는 Agent 요청 처리, confirmation 소비,
RobotAction 상태 전이, Robot Web 호출 또는 ROS 2 Nav2 goal 전송 코드가 없다.

2026-08-30 현재 source·contract 구현, 격리 overlay build와 로컬 기능 시험은
완료했다. campaign-specific subset 139개와 이 subset을 포함한
`malbut_scenarios` 전체 기능 시험 436개가 통과했다. 최종 실제 Gazebo 인수는
변경을 commit한 뒤 **그 commit과 동일한 clean source에서 overlay를 다시 build**하고,
campaign을 통해 허용된 `happy_path`를 1/1 실행해야 한다. 아직 이 post-commit
clean installed smoke를 완료했다고 주장하지 않는다.

## 2. 목표

SWM25-133의 단일 실행을 여러 정상·장애 조건에서 반복 사용할 수 있는 공통 시험
계층으로 만든다. 각 case가 서로의 DB, runtime 자원과 evidence를 오염시키지
않으며, 한 case라도 실행 실패, timeout, evidence 누락 또는 cleanup 실패가 있으면
전체 campaign이 합격하지 않게 한다.

이 Sub-task의 직접 범위는 다음과 같다.

- 여러 case를 순차 실행할 수 있는 campaign 계약
- installed SWM25-133 runner를 호출하는 process adapter
- case 기대 결과와 실제 제품 결과를 분리한 판정
- case별 격리와 fail-closed 중단 규칙
- child evidence를 검증해 만든 content-free aggregate evidence
- `happy_path` 1회로 campaign 자체가 실제 runner를 호출하는지 확인하는 smoke

정상 3회, 중복·경쟁, Safety 차단, 불명 결과와 최종 정상·장애 campaign은 각각
SWM25-135~139에서 같은 기반을 확장해 검증한다. 정상 반복은 현 profile을 그대로
쓰고, fault case는 Story별 typed child scenario·evidence와 adapter를 추가한다.

## 3. Jira 달성 조건 5개

### 3.1 기존 runner 재사용

- [x] SWM25-133의 installed `run_text_gazebo_acceptance`를 child process로
  재사용한다.
- [x] campaign에 Agent, confirmation, RobotAction, Robot Web 또는 Nav2 실행
  코드를 복제하지 않는다.

campaign의 **제품 실행 경계**는 SWM25-133의 installed public CLI와 strict evidence
계약만 사용한다. process ownership과 ROS environment hygiene에는 기존 공용 helper를
재사용하지만, 내부 private supervisor를 import하거나 SQLite를 직접 수정하지 않는다.
이 경계를 지키면
SWM25-133의 exact-once와 Safety 보장이 바뀌었을 때 하나의 runner만 수정하면 되고,
반복 도구가 별도의 실행 권한 경로가 되는 것을 막을 수 있다.

### 3.2 case 식별·순서·격리

- [x] 모든 case에 campaign 안에서 유일한 ID, 고정된 실행 순서, 허용된 profile과
  명시적인 기대 결과가 있다.
- [x] case별 DB, runtime directory와 child evidence 파일을 분리하고 이전 case의
  산출물을 다음 case 입력으로 재사용하지 않는다.

case는 동시에 실행하지 않고 순서대로 실행한다. child가 성공을 반환해도 cleanup이
끝난 뒤에만 다음 case를 시작한다. campaign이 만든 bounded public `case-001` 형식의
case ID와 ordinal은 aggregate에 기록한다. conversation·RobotAction·ROS goal·child
run 같은 private 제품/runtime ID는 기록하지 않고 필요한 결속은 digest로 표현한다.

### 3.3 하나라도 불완전하면 전체 불합격

- [x] case 실행 실패, deadline 초과, strict child evidence 누락·손상·불일치 또는
  cleanup 실패 중 하나라도 발생하면 전체 campaign은 `PASSED`가 아니다.
- [x] cleanup 실패 시 공유될 수 있는 ROS/process/socket 상태를 신뢰하지 않고 후속
  case 실행을 중단한다.

부분 성공을 전체 성공으로 보정하지 않는다. child process 종료 코드만 0인 경우,
제품 상태만 성공인 경우 또는 evidence 파일만 존재하는 경우도 충분하지 않다.
종료 코드, strict manifest, 기대 결과 및 cleanup이 모두 일치해야 해당 case의 시험
판정이 합격이다.

### 3.4 content-free aggregate evidence

- [x] 각 case 판정과 전체 판정을 content-free aggregate evidence 한 개로 모은다.
- [x] 기존 파일 overwrite와 symlink 경로를 거절하고 owner-only 권한으로 원자 발행한다.

aggregate에는 제한된 상태, 기대·관측 결과, 순서, 횟수, duration, cleanup 집계와
child manifest digest만 기록한다. 요청·승인 원문, token, 원본 ID, 좌표, pose,
device/map 값, DB/runtime/evidence의 host 경로 및 child stdout/stderr는 저장하지
않는다. 임시 파일을 완전히 기록하고 동기화한 뒤 새로운 최종 파일로만 게시하며,
이미 존재하는 경로는 덮어쓰지 않는다.

### 3.5 default-OFF와 실행 provenance

- [x] campaign 실행은 기본 OFF이며 명시적인 simulation 실행 승인, 허용된 case
  profile, clean source와 installed artifact attestation을 모두 통과해야 한다.
- [x] `simulation=true`, `physical_authorized=false`를 유지하고 실로봇 authority를
  만들지 않는다.

`--check`는 설치와 설정 계약만 검사하고 Agent server, Gazebo 또는 Nav2 goal을
시작하지 않는다. `--run`만으로도 충분하지 않으며
`--execute-approved-simulation`이 함께 있어야 한다. 알 수 없는 profile, dirty
source, source commit 불일치, source와 installed artifact 불일치는 실행 전에
fail-closed한다.

위 다섯 조건의 source·contract 검증과 post-commit 실제 smoke가 모두 끝나야 Jira
Sub-task를 완료로 바꾼다.

## 4. 계층과 책임

```text
Campaign CLI
  책임: check/run gate, 허용 profile 선택, public-safe 결과
      |
      v
Campaign core
  책임: case 순서, 유일성, 기대 결과, stop/fail 규칙, 전체 verdict
      |
      v
Installed runner adapter
  책임: shell 없는 bounded child 실행, deadline, output 제한
      |
      v
SWM25-133 run_text_gazebo_acceptance
  책임: 실제 Agent -> RobotAction -> Robot Web -> Nav2 폐루프와 cleanup
      |
      v
Strict child evidence parser
  책임: evidence v2 형식·digest·exact-success·cleanup 검증
      |
      v
Campaign evidence writer
  책임: content-free 집계, 원자·no-overwrite 발행
```

campaign core는 ROS, Gazebo, SQLite와 HTTP를 import하지 않는 순수 application
계층이다. unit test에서는 fake executor를 사용해 순서, timeout, mismatch와 중단
정책을 빠르고 결정적으로 검증한다. fake가 실제 ROS/Nav2 동작을 증명하는 것은
아니므로 최종 인수에는 반드시 installed SWM25-133 runner를 통한 실제 Gazebo
smoke가 별도로 필요하다.

## 5. 제품 결과와 시험 판정의 분리

campaign은 다음 두 값을 같은 뜻으로 취급하지 않는다.

| 구분 | 질문 | 예시 |
| --- | --- | --- |
| 제품 결과 | 로봇 작업이 어떻게 끝났는가 | `SUCCEEDED`, `BLOCKED`, `UNKNOWN` |
| 시험 판정 | 그 결과가 이 case의 기대와 정확히 일치했는가 | `PASSED`, `FAILED` |

현재 `happy_path`는 제품 결과 `SUCCEEDED`를 기대한다. 따라서 actual Nav2 known
terminal success, exact-once count와 cleanup을 모두 만족해야 시험도 `PASSED`다.

후속 Safety case에서는 제품 결과가 `BLOCKED`이고 Nav2 goal이 0개인 것이 올바른
결과다. 이때 `BLOCKED`를 실패로 오해하지 않고 기대한 차단과 evidence가 정확하면
시험 판정은 `PASSED`가 된다. 반대로 stale state case에서 이동이 `SUCCEEDED`하면
제품 수행 자체는 terminal success여도 안전 요구와 반대이므로 시험은 `FAILED`다.

불명 결과 case도 마찬가지다. 제품 결과 `UNKNOWN`을 성공으로 바꾸거나 재전송하지
않았다는 것이 기대 조건이면 시험은 합격할 수 있다. 다만 SWM25-134의 installed
adapter와 child parser는 의도적으로 SWM25-133의 `exact_success`만 허용한다.
`BLOCKED`와 `UNKNOWN`의 실제 합격 evidence를 받으려면 SWM25-137/138에서 typed child
evidence와 profile-aware adapter/parser를 추가해야 한다. 이번에 도입한 제품 결과와
시험 판정의 분리는 그 확장을 위한 core 계약이다.

## 6. case 격리와 순차 실행

각 case는 최소한 다음 항목으로 구분한다.

```text
case_id
ordinal_from_ordered_tuple
case_profile
expected_product_outcome
expected_child_evidence_contract
private runtime/evidence binding
```

campaign 시작 전에 빈 case 목록, 중복 ID, 지원하지 않는 profile과 non-enum 기대
결과를 거절한다. public CLI는 allowlisted profile을 server-owned 기대 결과에
고정한다. 입력 tuple의 순서를 보존하고 ordinal을 `1..N`으로 부여한다. 실행 중에는
다음 규칙을 적용한다.

1. case마다 새로운 private child evidence 파일을 할당한다.
2. SWM25-133 runner가 각 실행에서 독립 SQLite와 runtime 자원을 만들게 한다.
3. child 완료 뒤 종료 코드와 strict evidence를 함께 읽는다.
4. evidence의 source/install 결속, simulation 경계, 제품 결과와 cleanup을 검증한다.
5. 완전한 cleanup 뒤에만 다음 순번으로 이동한다.
6. unsafe residue 또는 결과 불명확성이 campaign 격리를 깨면 즉시 중단한다.

현재 Sub-task에서 허용하는 **profile 종류**는 `happy_path` 하나이며 같은 profile을
최대 32개까지 순서대로 반복 지정할 수 있다. SWM25-134 실제 인수는 그중 1개만
실행한다. 한 case라도 campaign 계층을 통과시켜 보는 이유는 새로운 제품 경로를
시험하는 것이 아니라, 상위 도구가 정말로 installed SWM25-133을 실행하고 child
evidence를 검증·집계하는지 확인하기 위해서다. 동일 profile 실제 3회 반복은
SWM25-135가 담당하고 fault profile과 typed evidence는 후속 Story에서 추가한다.

## 7. aggregate evidence 계약

aggregate evidence는 campaign 실행 내용을 재현할 수 있는 입력 원문이 아니라,
정해진 계약을 통과했는지 감사할 수 있는 제한된 영수증이다. 형식은
`malbut.text-gazebo-campaign-evidence.v1`로 고정한다.

기록 대상은 다음과 같다.

- schema와 campaign 상태
- source/install provenance digest
- 허용된 profile과 case 수·순서
- case별 기대 제품 결과, 관측된 제한 상태와 시험 판정
- strict child manifest digest
- bounded monotonic duration
- 실행·완료·실패·중단 case 집계
- cleanup 완료 여부와 제한된 residue count
- `simulation=true`, `physical_authorized=false`

기록 금지 대상은 다음과 같다.

- 사용자 요청, confirmation과 Provider 응답 원문
- credential, token, Cookie, CSRF와 환경변수 값
- conversation, confirmation, RobotAction, operation, ROS goal 원본 ID
- pose, 좌표, private map/device/revision 원문
- source, install, DB, runtime과 evidence의 host 절대 경로
- child argv, stdout/stderr와 exception 원문

성공 aggregate는 모든 예정 case가 실행되고 각각 기대 결과, evidence와 cleanup을
통과한 경우에만 만들어진다. 실패를 기록하는 receipt가 있더라도 그것은 campaign
성공 evidence와 구분하며, 누락된 child evidence를 추정 값으로 채우지 않는다.

## 8. 확정된 실행 CLI 계약

아래 option 조합, default-OFF와 public-safe 출력은 source 통합 시험으로 확인했다.
실제 Gazebo 성공 여부만 post-commit clean installed smoke에서 확인한다.

먼저 검증하려는 source commit과 동일한 isolated overlay를 build하고 source한다.

```bash
cd <clean-source-worktree>
source_tree="$(pwd -P)"
source_commit="$(git rev-parse HEAD)"

source /opt/ros/humble/setup.bash
source <same-commit-isolated-overlay>/install/setup.bash
```

### 8.1 non-actuating check

```bash
ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --check \
  --case-profile happy_path \
  --source-commit "$source_commit" \
  --source-tree "$source_tree"
```

`--check`는 provenance를 검증하기 위한 bounded SWM25-133 child process 하나는
실행한다. 다만 Agent/server/Gazebo/Nav2 같은 제품 runtime과 ROS goal은 생성하지
않는다.

### 8.2 명시적으로 승인한 simulation smoke

```bash
campaign_evidence_root=<absolute-owner-private-directory>
ros_domain_id=86  # 현재 비어 있는 1~100 값을 운영자가 명시
install -d -m 0700 "$campaign_evidence_root"

ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --run \
  --execute-approved-simulation \
  --case-profile happy_path \
  --source-commit "$source_commit" \
  --source-tree "$source_tree" \
  --ros-domain-id "$ros_domain_id" \
  --evidence "$campaign_evidence_root/swm25-134-smoke.json"
```

runner는 사용자가 지정한 domain이 비어 있는지 확인하며 임의 domain을 조용히
선택하지 않는다. 현재 `happy_path` 외 profile은 허용하지 않는다. 실행 결과는
public-safe status와 digest만 stdout에 내보내고 private path나 child 출력을
포함하지 않는다.

## 9. 검증 계획과 현재 상태

### source·contract test

- case ID uniqueness와 ordered tuple position의 ordinal projection
- 빈 plan, unknown profile과 non-enum expectation 거절 및 CLI mapping 고정
- 선언 순서 실행과 완료 후 다음 case 진행
- child non-zero exit, timeout, bounded output 초과 처리
- evidence 누락, schema 오류, digest·상태 불일치 처리
- cleanup 실패 뒤 campaign 중단
- 실행 중 `Ctrl-C` 이후에도 child cleanup 수행 및 불완전 cleanup 우선
- Popen 이후 identity 확인 실패를 미시작·정리 완료로 오판하지 않음
- 제품 결과와 시험 verdict 분리
- aggregate evidence content redaction
- owner-only atomic create와 overwrite·symlink 거절
- `--check`의 제품 실행 effect·Nav2 goal 0회와 `--run` default-OFF

unit test의 fake runner는 위 campaign 로직을 실제 시간·ROS 상태와 무관하게
재현한다. 이것만 통과해서는 Jira 완료가 아니다.

2026-08-30 source 검증 결과는 다음과 같다.

- campaign-specific subset: 139개 통과
- campaign subset을 포함한 `malbut_scenarios` 전체 기능 test: 436개 통과
- 변경 production module 5개는 `ament_flake8`와 `ament_pep257` 모두 통과
- 격리 non-symlink overlay build, installed import와 CLI entry point: 통과
- 실행 승인 flag 누락 및 dirty source의 fail-closed: 통과
- workspace 전체 `ament_flake8` wrapper는 통과했다. 전체 `ament_pep257` wrapper의
  기존 파일 4개 위반은 이번 변경 밖 기준선 문제이며 변경 module에는 위반이 없다.

### 실제 installed smoke

- [ ] 구현 변경을 Git에 commit한다.
- [ ] 같은 commit의 clean source로 isolated overlay를 새로 build한다.
- [ ] installed campaign `--check`가 제품 실행 effect·Nav2 goal 0회로 통과한다.
- [ ] `happy_path` 1개를 campaign `--run`으로 actual Gazebo에서 1/1 실행한다.
- [ ] child SWM25-133 exact-once·known terminal evidence와 aggregate verdict가
  일치한다.
- [ ] 종료 뒤 소유 process, ROS node와 socket 잔류가 0개임을 확인한다.

이 실제 smoke가 끝나기 전에는 “SWM25-134가 실제 Gazebo에서 완료됐다” 또는
“clean installed acceptance가 통과했다”고 기록하지 않는다. 실행별 commit과
digest는 tracked 문서를 다시 바꿔 attestation을 무효화하지 않도록 immutable Jira
실행 기록과 owner-private evidence에 보관한다.

## 10. 후속 Story 인계

| Jira | 같은 campaign 기반에 추가할 검증 | SWM25-134에서 하지 않는 것 |
| --- | --- | --- |
| SWM25-135 | 같은 `happy_path`를 실제 Gazebo에서 3회 연속 실행 | 이번 smoke를 3회 성공으로 확대 주장하지 않음 |
| SWM25-136 | duplicate·동시성 전용 child scenario/evidence와 추가 goal 0 검증 | 새 중복 방지 제품 로직을 campaign에 구현하지 않음 |
| SWM25-137 | typed `BLOCKED/no-goal` child evidence와 profile-aware adapter | Safety를 fake하거나 우회하지 않음 |
| SWM25-138 | typed `UNKNOWN/reconcile/no-resend` evidence와 adapter | `UNKNOWN`을 성공·실패로 임의 보정하지 않음 |
| SWM25-139 | 정상·장애 profile과 cleanup을 묶은 최종 Gazebo 인수 | 하위 runner를 다시 만들지 않음 |

후속 Story는 새로운 독립 campaign runner를 만들지 않는다. SWM25-134의 case plan,
process/runtime 경계와 aggregate writer를 재사용한다. SWM25-135는 현
`exact_success` 경로를 반복하고, SWM25-136~138은 각 Story가 소유한 typed child
scenario·evidence 계약 및 profile-aware 해석을 기존 installed boundary에 확장한다.

## 11. Jira 결론 작성 기준

현재 Jira 상태는 **진행 중**으로 유지한다. source·contract test와 post-commit
clean installed `happy_path` 1/1 smoke가 모두 끝난 뒤에는 다음 의미로 결론을
작성한다.

> SWM25-133의 실제 Gazebo runner를 복제하지 않고 installed child로 재사용하는
> campaign 계층을 추가했다. case별 ID·순서·기대 결과와 DB/runtime/evidence를
> 격리하고, 실행 실패·timeout·evidence 누락·cleanup 실패 중 하나라도 있으면
> 전체를 fail-closed한다. `happy_path` 1개가 동일 commit의 clean installed
> overlay에서 actual Gazebo 폐루프와 content-free aggregate evidence를 1/1
> 완료했으며, `simulation=true`, `physical_authorized=false`와 zero-residue를
> 확인했다. 정상 반복과 fault profile은 SWM25-135~139로 인계한다.

위 문구는 실제 smoke가 성공한 뒤에만 사용한다. 현재 시점의 정확한 결론은
“campaign 구조와 source·contract 검증, 격리 overlay build는 완료했으며, 동일
commit clean rebuild 및 actual Gazebo 1/1 smoke는 대기 중”이다.
