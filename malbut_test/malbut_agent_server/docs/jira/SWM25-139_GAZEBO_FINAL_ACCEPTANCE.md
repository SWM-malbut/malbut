# SWM25-139 Gazebo 정상·장애 campaign과 자원 정리 결과를 봉인

## 1. 이 Sub-task의 결론

SWM25-139는 새로운 Agent, RobotAction, Robot Web 또는 Nav2 실행 경로를 만드는
기능 개발이 아니다. SWM25-133의 actual Gazebo runner와 SWM25-134의 ordered
campaign에 SWM25-135~138이 추가한 정상·경쟁·Safety·불명 결과 profile을 하나의
clean source/install provenance에서 다시 실행하는 **최종 Gazebo 인수 작업**이다.

```text
clean committed source
  -> isolated non-symlink install
  -> non-actuating 15-case check
  -> existing run_text_gazebo_campaign
       -> 15 fresh child runtimes in fixed order
       -> child v6 evidence 15개
       -> campaign v5 evidence 1개
  -> exact outcome/effect/cleanup audit
  -> public manifest digest만 Jira와 PR에 기록
```

기존 generic campaign은 전달받은 case 목록을 정확히 실행하고 봉인하지만, 한 개의
정상 case만으로도 campaign 자체는 `PASSED`가 될 수 있다. 따라서 SWM25-139는
"campaign이 통과했다"만 확인하지 않고, 이 문서의 15개 profile과 순서가 모두
포함됐는지 별도로 감사한다.

하위 runner, fault adapter와 evidence schema는 이미 필요한 표현력을 갖고 있으므로
복제하거나 version을 올리지 않는다. actual run에서 결함이 발견될 때만 그 원인과
최소 수정 범위를 별도 계획으로 확정한다.

## 2. 목표

정상 이동, 중복·동시성 압력, dispatch 직전 Safety 차단과 Nav2 결과 불명을 같은
commit에서 순서대로 실행한다. 모든 case의 기대 제품 결과, 실제 ROS 효과,
no-resend와 cleanup이 일치할 때만 하나의 content-free campaign evidence를 최종
Gazebo 인수 결과로 인정한다.

제품 결과와 시험 판정은 계속 분리한다.

| 제품 결과 | 의미 | 시험이 합격하는 조건 |
| --- | --- | --- |
| `SUCCEEDED` | known terminal 성공 | start·goal·terminal이 각각 정확히 1개 |
| `BLOCKED` | Safety가 실행 전 차단 | exact block code와 start·goal·terminal이 각각 0개 |
| `UNKNOWN` | 외부 결과를 확정할 수 없음 | exact unknown code, no-resend와 독립 ROS 관측 일치 |

기대한 `BLOCKED`나 `UNKNOWN`은 제품 성공이 아니지만 안전 시험의 올바른 결과이므로
case의 `test_verdict`는 `PASSED`가 될 수 있다.

## 3. Jira 달성 조건 5개

1. 최신 `main` 기반의 clean commit에서 source와 installed build의 provenance를 일치시킨다.
2. 기존 runner와 campaign을 재사용해 정상·중복·Safety·응답 유실 15개 case를 고정된 순서로 실행한다.
3. 각 case의 `SUCCEEDED·BLOCKED·UNKNOWN` 결과와 RobotAction·Nav2 goal·재전송 횟수가 기대값과 정확히 일치한다.
4. 모든 실행 후 process·ROS node·socket·thread가 정리되고 중복 효과와 forced termination이 0개임을 확인한다.
5. `15/15 passed`, `stopped_early=false`, `simulation=true`, `physical_authorized=false` 결과를 content-free evidence 하나로 봉인한다.

## 4. 고정된 최종 15-case suite

legacy alias인 `happy_path`는 `happy_living_room`과 같은 의미이므로 중복 실행하지
않는다. 서로 다른 12개 의미 profile을 모두 포함하고, 각 fault group 뒤에
`happy_living_room` 대조군을 추가해 총 15회 실행한다.

| 순서 | profile | 기대 결과 | exact block/unknown code | Robot Web start | Nav2 goal/terminal |
| ---: | --- | --- | --- | ---: | ---: |
| 1 | `happy_living_room` | `SUCCEEDED` | `none` | 1 | 1/1 |
| 2 | `happy_kitchen` | `SUCCEEDED` | `none` | 1 | 1/1 |
| 3 | `happy_bedroom` | `SUCCEEDED` | `none` | 1 | 1/1 |
| 4 | `duplicate_request` | `SUCCEEDED` | `none` | 1 | 1/1 |
| 5 | `concurrent_approval` | `SUCCEEDED` | `none` | 1 | 1/1 |
| 6 | `competing_workers` | `SUCCEEDED` | `none` | 1 | 1/1 |
| 7 | `happy_living_room` | `SUCCEEDED` | `none` | 1 | 1/1 |
| 8 | `stale_state` | `BLOCKED` | `robot_state_stale` | 0 | 0/0 |
| 9 | `emergency_stop` | `BLOCKED` | `safety_emergency_stop` | 0 | 0/0 |
| 10 | `map_revision_changed` | `BLOCKED` | `target_binding_changed` | 0 | 0/0 |
| 11 | `happy_living_room` | `SUCCEEDED` | `none` | 1 | 1/1 |
| 12 | `nav2_unavailable` | `UNKNOWN` | `navigation_start_outcome_unknown` | 1 | 0/0 |
| 13 | `start_response_lost` | `UNKNOWN` | `navigation_start_outcome_unknown` | 1 | 1/1 |
| 14 | `terminal_status_response_lost` | `UNKNOWN` | `navigation_status_outcome_unknown` | 1 | 1/1 |
| 15 | `happy_living_room` | `SUCCEEDED` | `none` | 1 | 1/1 |

7번은 duplicate·동시 승인·worker 경쟁 뒤에 실행해 request claim, confirmation CAS와
worker lease/fence가 다음 fresh runtime에 남지 않았음을 확인한다. 11번은 Safety fault
주입이 정리됐음을, 15번은 execution proxy fault와 응답 drop이 정리됐음을 확인한다.

전체 기대 수량은 다음과 같다.

- 제품 결과: `SUCCEEDED=9`, `BLOCKED=3`, `UNKNOWN=3`
- Agent proposal·confirmation·approved confirmation·RobotAction: 각각 15개
- durable dispatch intent·Robot Web start: 각각 12개
- distinct Nav2 goal·known ROS terminal: 각각 11개
- preapproval Nav2 goal: 0개
- approval replay와 late approval의 추가 effect: 0개
- Safety 차단 case의 dispatch intent·start·goal: 모두 0개

같은 거실 target은 같은 target binding digest를 사용해야 한다. 거실·주방·침실은
서로 다른 target binding digest를 가져야 한다. 실제 goal이 있는 서로 다른 실행은
서로 다른 goal-set digest를 가져야 하며, 차단과 Nav2 unavailable의 empty goal-set
digest 반복은 허용한다.

## 5. 재사용하는 구현 경계

```text
run_text_gazebo_campaign
  책임: fixed order, expected outcome, fail/stop, aggregate
      |
      v
installed run_text_gazebo_acceptance
  책임: Agent -> confirmation -> RobotAction -> Robot Web -> actual Nav2
      |
      v
child evidence v6
  책임: product state, exact effect, fault observation, cleanup
      |
      v
campaign evidence v5
  책임: order, expected/observed result, child digest, provenance, cleanup
```

SWM25-139는 다음을 추가하지 않는다.

- 별도 `SWM139Executor`나 두 번째 campaign runner
- Agent 또는 RobotAction 상태 전이 복제
- SQLite 직접 수정이나 Safety 우회 test hook
- caller가 임의 좌표·ROS action endpoint·fault payload를 넣는 입력
- child v7 또는 campaign v6 같은 불필요한 schema 변경

현재 child/campaign parser는 exact-key 계약이므로 필드 하나를 추가해도 format bump와
전체 evidence 재생성이 필요하다. 최종 인수에 필요하지 않은 suite ID, thread count나
total count 필드를 schema에 추가하지 않고, fixed suite의 완전성은 이 문서와 post-hoc
audit가 검증한다.

## 6. fail-closed와 cleanup 판정

각 case는 fresh SQLite, runtime directory, child evidence, Agent/Gazebo process와 ROS
관측 window를 사용한다. child cleanup이 끝난 뒤에만 다음 case를 시작한다.

다음 조건은 unsafe residue로 간주하고 즉시 후속 case를 중단한다.

- child process 예외, deadline 또는 output limit 초과
- child evidence 누락·손상·non-canonical·digest 불일치
- source/install provenance 불일치
- process·ROS node·listener/socket 잔류
- proxy close 또는 observer close/join 실패
- forced termination 발생

기대 제품 결과만 다르고 cleanup이 완전한 경우에는 진단을 위해 후속 case를 실행할 수
있지만 전체 campaign verdict는 반드시 `FAILED`다. 실패·중단 실행의 일부 child를 다른
실행의 child와 합쳐 `15/15`로 만들지 않으며, 재시험은 신규 owner-private evidence
root에서 `case-001`부터 다시 시작한다.

public cleanup schema에는 thread 전용 숫자가 없지만 proxy close와 observer join 실패는
child `cleanup.completed=false`로 귀속된다. 따라서 Jira에는 thread 수를 추정해서 쓰지
않고 `close/join 성공`과 aggregate cleanup 완료를 구분해 기록한다.

## 7. evidence와 공개 경계

성공한 한 실행은 같은 private root에 다음을 보존한다.

- canonical child v6 evidence 15개
- canonical campaign v5 evidence 1개
- campaign이 참조하는 child manifest digest 15개
- commit, source-tree digest와 installed digest

aggregate만 남기면 child의 세부 effect count를 다시 감사할 수 없으므로 private child를
함께 보존한다. 반대로 Git, Jira와 PR에는 다음을 넣지 않는다.

- 실제 evidence 파일과 host path
- 요청·승인 원문
- conversation, confirmation, RobotAction, run, ROS goal과 claim의 private ID
- 좌표, pose, map geometry 또는 device binding 원문
- DB/runtime path와 stdout/stderr
- credential, cookie, token과 environment 값

Jira와 PR에는 bounded case 요약, commit, test 수치, cleanup 집계와 public manifest
SHA-256만 기록한다. evidence parent는 `0700`, 최종 파일은 `0600`이어야 하고 기존
파일 overwrite와 symlink 경로는 거절한다.

## 8. exact commit 실행 순서

tracked 문서에 actual 결과 digest를 기록하고 다시 commit하면 검증한 commit과 HEAD가
달라지는 provenance 순환이 생긴다. 따라서 이 계약 문서를 먼저 local commit한 뒤 그
SHA를 build·campaign 입력으로 사용한다. actual 결과와 public digest는 Jira와 PR
본문에만 기록하며 검증 뒤 tracked 파일을 수정하지 않는다.

### 8.0 문서-only local commit과 SHA 고정

```bash
git add -- \
  malbut_agent_server/docs/jira/SWM25-139_GAZEBO_FINAL_ACCEPTANCE.md
git diff --cached --check
git diff --cached --name-only
git commit -m \
  "SWM25-139 Gazebo 정상·장애 campaign과 자원 정리 결과를 봉인"

git status --porcelain=v1
git rev-parse HEAD
```

stage와 commit에는 위 문서 한 개만 있어야 하며 마지막 status 출력은 비어 있어야 한다.
이후 commit amend, rebase나 tracked 파일 수정이 발생하면 build와 15-case campaign을
새 HEAD에서 처음부터 다시 실행한다.

### 8.1 clean isolated build

source 밖의 신규 canonical directory를 `<attestation-root>`로 사용하고 symlink install을
사용하지 않는다. 현재 terminal에 남은 ROS overlay를 상속하지 않도록 먼저 고정된 최소
환경의 subshell을 시작한다.

```bash
env -i \
  HOME="${HOME}" \
  USER="${USER}" \
  LOGNAME="${LOGNAME:-${USER}}" \
  PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  LANG="C.UTF-8" \
  TERM="${TERM:-dumb}" \
  bash --noprofile --norc

test -z "${AMENT_PREFIX_PATH:-}"
test -z "${CMAKE_PREFIX_PATH:-}"
test -z "${COLCON_PREFIX_PATH:-}"
test -z "${PYTHONPATH:-}"
test -z "${LD_LIBRARY_PATH:-}"
```

```bash
source /opt/ros/humble/setup.bash

colcon --log-base "<attestation-root>/log" build \
  --base-paths "<clean-source-tree>" \
  --build-base "<attestation-root>/build" \
  --install-base "<attestation-root>/install" \
  --packages-up-to malbut_scenarios
```

build 뒤에도 같은 오염 없는 shell에서 ROS base와 방금 만든 install만 다음 순서로
source한다. check와 actual run도 반드시 이 shell을 사용한다.

```bash
source /opt/ros/humble/setup.bash
source "<attestation-root>/install/setup.bash"

ATTESTATION_INSTALL_ROOT="<attestation-root>/install" python3 - <<'PY'
import os
from pathlib import Path

from ament_index_python.packages import get_package_prefix

root = Path(os.environ['ATTESTATION_INSTALL_ROOT']).resolve(strict=True)
for package in (
    'malbut_interfaces',
    'malbut_agent_server',
    'malbut_gazebo',
    'malbut_scenarios',
):
    Path(get_package_prefix(package)).resolve(strict=True).relative_to(root)
print('installed-prefix-preflight: ok')
PY
```

생성된 `malbut_interfaces`까지 방금 만든 exact install에서만 가져오도록, 이 shell에서
focused regression을 실행한다. source package는 `PYTHONPATH` 앞에 두되 ROS setup이
추가한 기존 값을 보존한다.

```bash
cd "<clean-source-tree>"

PYTHONPATH="malbut_agent_server:malbut_gazebo:malbut_scenarios${PYTHONPATH:+:${PYTHONPATH}}" \
python3 -m pytest -q \
  malbut_agent_server/test/test_approved_action_worker.py \
  malbut_agent_server/test/test_sqlite_action_repository.py \
  malbut_gazebo/test/test_robot_web_server.py \
  malbut_gazebo/test/test_small_house_nav2_testbed_launch.py \
  malbut_scenarios/test/test_counting_robot_web_proxy.py \
  malbut_scenarios/test/test_text_gazebo_scenario.py \
  malbut_scenarios/test/test_text_gazebo_runtime.py \
  malbut_scenarios/test/test_text_gazebo_acceptance.py \
  malbut_scenarios/test/test_text_gazebo_evidence.py \
  malbut_scenarios/test/test_text_gazebo_campaign_core.py \
  malbut_scenarios/test/test_text_gazebo_campaign_runtime.py \
  malbut_scenarios/test/test_text_gazebo_campaign_evidence.py
```

같은 build/install을 지정해 세 package test와 결과 집계를 실행한다.

```bash
colcon --log-base "<attestation-root>/test-log" test \
  --base-paths "<clean-source-tree>" \
  --build-base "<attestation-root>/build" \
  --install-base "<attestation-root>/install" \
  --packages-select \
    malbut_agent_server malbut_gazebo malbut_scenarios

colcon test-result \
  --test-result-base "<attestation-root>/build" \
  --all --verbose
```

### 8.2 non-actuating check

다음 profile 목록은 actual run과 byte-for-byte 같은 순서를 사용한다.

```bash
ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --check \
  --case-profile happy_living_room \
  --case-profile happy_kitchen \
  --case-profile happy_bedroom \
  --case-profile duplicate_request \
  --case-profile concurrent_approval \
  --case-profile competing_workers \
  --case-profile happy_living_room \
  --case-profile stale_state \
  --case-profile emergency_stop \
  --case-profile map_revision_changed \
  --case-profile happy_living_room \
  --case-profile nav2_unavailable \
  --case-profile start_response_lost \
  --case-profile terminal_status_response_lost \
  --case-profile happy_living_room \
  --source-commit "<full-lowercase-commit>" \
  --source-tree "<clean-source-tree>"
```

합격 조건은 `status=ok`, `case_count=15`, `nav2_start_count=0`,
`simulation=true`, `physical_authorized=false`다.

### 8.3 actual headless Gazebo campaign

source와 Git 밖에 있는 기존 owner-private `<private-recovery-root>` 아래에 매 실행마다
새 parent를 만든다. CLI는 이 parent가 absolute canonical non-symlink directory이고,
현재 사용자 소유의 exact `0700`이며 destination이 아직 없을 때만 실행한다.

```bash
campaign_evidence_root="$(mktemp -d "<private-recovery-root>/swm25-139-evidence.XXXXXX")"
chmod 0700 "$campaign_evidence_root"
campaign_evidence_path="${campaign_evidence_root}/swm25-139-acceptance.json"

test "$(realpath -e "$campaign_evidence_root")" = "$campaign_evidence_root"
test "$(stat -c %u "$campaign_evidence_root")" -eq "$(id -u)"
test "$(stat -c %a "$campaign_evidence_root")" = "700"
test ! -e "$campaign_evidence_path"
```

```bash
campaign_public_output="$(
ros2 run malbut_scenarios run_text_gazebo_campaign -- \
  --run \
  --execute-approved-simulation \
  --case-profile happy_living_room \
  --case-profile happy_kitchen \
  --case-profile happy_bedroom \
  --case-profile duplicate_request \
  --case-profile concurrent_approval \
  --case-profile competing_workers \
  --case-profile happy_living_room \
  --case-profile stale_state \
  --case-profile emergency_stop \
  --case-profile map_revision_changed \
  --case-profile happy_living_room \
  --case-profile nav2_unavailable \
  --case-profile start_response_lost \
  --case-profile terminal_status_response_lost \
  --case-profile happy_living_room \
  --source-commit "<full-lowercase-commit>" \
  --source-tree "<clean-source-tree>" \
  --ros-domain-id "<isolated-domain-id>" \
  --evidence "$campaign_evidence_path"
)" || exit 1

campaign_manifest_digest="$(
CAMPAIGN_PUBLIC_OUTPUT="$campaign_public_output" python3 - <<'PY'
import json
import os
import re


class DuplicateKeyError(ValueError):
    pass


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError
        result[key] = value
    return result


raw = os.environ['CAMPAIGN_PUBLIC_OUTPUT']
if not raw or '\n' in raw or '\r' in raw or len(raw.encode()) > 4096:
    raise SystemExit(1)
try:
    value = json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda unused: (_ for _ in ()).throw(ValueError()),
    )
except (TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1) from None
expected_keys = {
    'case_count',
    'manifest_digest',
    'mode',
    'physical_authorized',
    'simulation',
    'status',
    'stopped_early',
    'test_verdict',
}
if (
    type(value) is not dict
    or set(value) != expected_keys
    or raw != json.dumps(value, ensure_ascii=True, sort_keys=True)
    or type(value['case_count']) is not int
    or value['case_count'] != 15
    or value['mode'] != 'run'
    or value['physical_authorized'] is not False
    or value['simulation'] is not True
    or value['status'] != 'succeeded'
    or value['stopped_early'] is not False
    or value['test_verdict'] != 'passed'
    or type(value['manifest_digest']) is not str
    or re.fullmatch(r'[0-9a-f]{64}', value['manifest_digest']) is None
):
    raise SystemExit(1)
print(value['manifest_digest'])
PY
)" || exit 1
```

합격 조건은 `15/15 passed`, `stopped_early=false`, child v6 15개, campaign v5,
expected/observed product result와 exact code 일치, 전체 effect count 일치와 cleanup
complete다. canonical JSON, receipt digest와 full manifest digest를 다시 계산해 저장된
값 및 runner가 출력한 public digest와 비교한다.

### 8.4 post-hoc evidence와 최종 Git 상태 감사

aggregate의 `receipt.cases[].profile`이 4절의 15개 tuple과 정확히 같은지 확인한다.
`case_count=15`, `SUCCEEDED=9`, `BLOCKED=3`, `UNKNOWN=3`, clean case 15개와 zero
residue를 다시 집계한다. private child 15개는 모두 v6, aggregate는 v5여야 하며 각
child digest가 aggregate의 같은 ordinal과 일치해야 한다.

canonical single-line JSON과 receipt SHA-256을 검증한 뒤 trailing newline을 제외한
full manifest SHA-256을 runner가 출력한 public digest와 비교한다. aggregate 파일은
`0600`, private parent는 `0700`이어야 한다. 검증 도중 raw JSON이나 private 경로를
Jira·PR·일반 로그로 복사하지 않는다.

다음 auditor는 exact install에서 import한 strict child parser와 campaign constructor로
aggregate를 재구성한다. raw evidence나 private 경로는 출력하지 않으며 합격한 경우에만
bounded public JSON 한 줄을 출력한다.

```bash
python3 - --evidence "$campaign_evidence_path" \
  --manifest-digest "$campaign_manifest_digest" <<'PY'
"""Content-free post-hoc auditor for the exact SWM25-139 campaign."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from malbut_scenarios.text_gazebo_campaign_core import (
    CampaignProfile, SafetyBlockCode, UnknownResultCode,
    campaign_profile_binding,
)
from malbut_scenarios.text_gazebo_campaign_evidence import (
    CAMPAIGN_EVIDENCE_FORMAT, CampaignCaseEvidence,
    CampaignCleanupAggregate, CampaignTestVerdict, CaseCleanupState,
    CaseErrorCode, CaseTestVerdict, ProductOutcome,
    TextGazeboCampaignManifest, TextGazeboCampaignReceipt,
    parse_child_manifest,
)
from malbut_scenarios.text_gazebo_campaign_runtime import _read_manifest
from malbut_scenarios.text_gazebo_scenario import (
    execution_contract, scenario_spec,
)

DIGEST = re.compile(r'[0-9a-f]{64}\Z')
CHILD_DIR = re.compile(r'\.swm25-134-cases-[a-z0-9]+\Z')
EMPTY_GOALS = hashlib.sha256(b'[]').hexdigest()
CASES = (
    ('happy_living_room', 'succeeded', 'none', 'none'),
    ('happy_kitchen', 'succeeded', 'none', 'none'),
    ('happy_bedroom', 'succeeded', 'none', 'none'),
    ('duplicate_request', 'succeeded', 'none', 'none'),
    ('concurrent_approval', 'succeeded', 'none', 'none'),
    ('competing_workers', 'succeeded', 'none', 'none'),
    ('happy_living_room', 'succeeded', 'none', 'none'),
    ('stale_state', 'blocked', 'robot_state_stale', 'none'),
    ('emergency_stop', 'blocked', 'safety_emergency_stop', 'none'),
    ('map_revision_changed', 'blocked', 'target_binding_changed', 'none'),
    ('happy_living_room', 'succeeded', 'none', 'none'),
    ('nav2_unavailable', 'unknown', 'none',
     'navigation_start_outcome_unknown'),
    ('start_response_lost', 'unknown', 'none',
     'navigation_start_outcome_unknown'),
    ('terminal_status_response_lost', 'unknown', 'none',
     'navigation_status_outcome_unknown'),
    ('happy_living_room', 'succeeded', 'none', 'none'),
)
TOTALS = {
    'agent_proposal_count': 15, 'approved_confirmation_count': 15,
    'confirmation_count': 15, 'dispatch_intent_count': 12,
    'nav2_goal_count': 11, 'preapproval_nav2_goal_count': 0,
    'replay_additional_effect_count': 0, 'robot_action_count': 15,
    'robot_web_start_count': 12, 'robot_web_verified_target_count': 15,
    'terminal_result_count': 11,
}


class AuditError(RuntimeError):
    pass


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        del message
        raise AuditError


def need(condition):
    if not condition:
        raise AuditError


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result)
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      sort_keys=True, separators=(',', ':'))


def parse_line(raw):
    try:
        text = raw.decode('utf-8', errors='strict')
        need(text.endswith('\n') and '\n' not in text[:-1] and '\r' not in text)
        value = json.loads(text[:-1], object_pairs_hook=unique,
                           parse_constant=lambda unused: need(False))
        need(type(value) is dict and text == canonical(value) + '\n')
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise AuditError from None


def private_dir(path):
    try:
        meta = os.lstat(path)
        need(path.is_absolute() and path.resolve(strict=True) == path)
        need(stat.S_ISDIR(meta.st_mode) and not stat.S_ISLNK(meta.st_mode))
        need(meta.st_uid == os.getuid() and stat.S_IMODE(meta.st_mode) == 0o700)
    except (OSError, RuntimeError, ValueError):
        raise AuditError from None


def read_private(path, limit):
    private_dir(path.parent)
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid())
        need(stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1)
        need(1 <= before.st_size <= limit)
        raw = os.read(fd, limit + 1)
        after, named = os.fstat(fd), os.lstat(path)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                 item.st_mtime_ns)
        need(len(raw) == before.st_size and identity(before) == identity(after))
        need(identity(after) == identity(named) and stat.S_ISREG(named.st_mode))
        need(named.st_uid == os.getuid() and stat.S_IMODE(named.st_mode) == 0o600)
        need(named.st_nlink == 1)
        return raw
    except OSError:
        raise AuditError from None
    finally:
        if fd >= 0:
            os.close(fd)


def reconstruct(path):
    raw = read_private(path, 256 * 1024)
    stored = parse_line(raw)
    need(set(stored) == {'format', 'receipt', 'receipt_digest'})
    need(stored['format'] == CAMPAIGN_EVIDENCE_FORMAT)
    receipt_value = stored['receipt']
    need(type(receipt_value) is dict and type(receipt_value.get('cases')) is list)
    case_values = receipt_value['cases']
    hidden = [item for item in os.scandir(path.parent)
              if item.name.startswith('.')]
    need(len(hidden) == 1 and hidden[0].is_dir(follow_symlinks=False))
    child_dir = Path(hidden[0].path)
    need(CHILD_DIR.fullmatch(child_dir.name) is not None)
    private_dir(child_dir)
    names = {item.name for item in os.scandir(child_dir)
             if item.name.startswith('case-')}
    need(names == {f'case-{i:03d}.json' for i in range(1, len(case_values) + 1)})

    typed_cases, child_receipts = [], []
    for ordinal, value in enumerate(case_values, 1):
        need(type(value) is dict)
        child_path = child_dir / f'case-{ordinal:03d}.json'
        try:
            summary = _read_manifest(child_path)
            child_raw = read_private(child_path, 64 * 1024)
            need(summary == parse_child_manifest(child_raw))
            child = parse_line(child_raw)
            need(type(child.get('receipt')) is dict)
            child_receipts.append(child['receipt'])
            typed_cases.append(CampaignCaseEvidence(
                ordinal=value['ordinal'], case_id=value['case_id'],
                profile=value['profile'],
                expected_outcome=ProductOutcome(value['expected_outcome']),
                observed_outcome=ProductOutcome(value['observed_outcome']),
                test_verdict=CaseTestVerdict(value['test_verdict']),
                error_code=CaseErrorCode(value['error_code']),
                child_manifest=summary, duration_seconds=value['duration_seconds'],
                cleanup=CaseCleanupState(value['cleanup']),
                expected_block_code=SafetyBlockCode(value['expected_block_code']),
                observed_block_code=SafetyBlockCode(value['observed_block_code']),
                expected_unknown_result_code=UnknownResultCode(
                    value['expected_unknown_result_code']),
                observed_unknown_result_code=UnknownResultCode(
                    value['observed_unknown_result_code']),
            ))
        except (KeyError, TypeError, ValueError):
            raise AuditError from None
    try:
        cleanup = CampaignCleanupAggregate(**receipt_value['cleanup'])
        receipt = TextGazeboCampaignReceipt(
            campaign_id=receipt_value['campaign_id'], commit=receipt_value['commit'],
            source_tree_digest=receipt_value['source_tree_digest'],
            installed_digest=receipt_value['installed_digest'], cases=tuple(typed_cases),
            test_verdict=CampaignTestVerdict(receipt_value['test_verdict']),
            stopped_early=receipt_value['stopped_early'],
            total_duration_seconds=receipt_value['total_duration_seconds'],
            cleanup=cleanup,
        )
        manifest = TextGazeboCampaignManifest(receipt)
    except (KeyError, TypeError, ValueError):
        raise AuditError from None
    need(manifest.canonical_json().encode() + b'\n' == raw)
    need(stored['receipt_digest'] == manifest.receipt_digest)
    return manifest, tuple(child_receipts)


def audit(path, runner_digest):
    need(type(runner_digest) is str and DIGEST.fullmatch(runner_digest) is not None)
    manifest, children = reconstruct(path)
    receipt = manifest.receipt
    need(manifest.digest() == runner_digest and len(receipt.cases) == 15)
    need(len(children) == 15 and receipt.test_verdict is CampaignTestVerdict.PASSED)
    need(not receipt.stopped_early and receipt.simulation and not receipt.physical_authorized)
    observed = tuple((case.profile, case.observed_outcome.value,
                      case.observed_block_code.value,
                      case.observed_unknown_result_code.value)
                     for case in receipt.cases)
    need(observed == CASES)
    need(tuple(case.case_id for case in receipt.cases) ==
         tuple(f'case-{i:03d}' for i in range(1, 16)))
    cleanup = receipt.cleanup
    need(cleanup.completed and cleanup.clean_case_count == 15)
    need(cleanup.incomplete_case_count == cleanup.not_observed_case_count == 0)
    need(cleanup.owned_processes_remaining == cleanup.ros_nodes_remaining == 0)
    need(cleanup.owned_sockets_remaining == cleanup.forced_termination_count == 0)

    totals = dict.fromkeys(TOTALS, 0)
    for child in children:
        counts = child.get('counts')
        need(type(counts) is dict and child.get('simulation') is True)
        need(child.get('physical_authorized') is False)
        for key in totals:
            need(type(counts.get(key)) is int and counts[key] >= 0)
            totals[key] += counts[key]
    need(totals == TOTALS)

    targets, reverse, goals = {}, {}, []
    for case in receipt.cases:
        child = case.child_manifest
        need(child is not None)
        binding = campaign_profile_binding(CampaignProfile(case.profile))
        room = scenario_spec(binding.scenario_profile).location
        need(targets.setdefault(room, child.target_binding_digest) ==
             child.target_binding_digest)
        need(reverse.setdefault(child.target_binding_digest, room) == room)
        goal_count = (0 if case.observed_outcome is ProductOutcome.BLOCKED else
                      execution_contract(binding.execution_profile).expected_nav2_goal_count)
        empty = child.goal_set_digest == EMPTY_GOALS
        need((goal_count == 0) is empty)
        if not empty:
            goals.append(child.goal_set_digest)
    need(set(targets) == {'거실', '주방', '침실'} and len(reverse) == 3)
    need(len(goals) == len(set(goals)) == 11)
    outcomes = Counter(case.observed_outcome.value for case in receipt.cases)
    need(outcomes == Counter(succeeded=9, blocked=3, unknown=3))
    return {
        'blocked_count': 3, 'case_count': 15, 'cleanup_complete': True,
        'goal_count': 11, 'manifest_digest': manifest.digest(),
        'receipt_digest': manifest.receipt_digest, 'status': 'passed',
        'succeeded_count': 9, 'test_verdict': 'passed', 'unknown_count': 3,
    }


def main(argv=None):
    parser = SafeParser()
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--manifest-digest', required=True)
    try:
        args = parser.parse_args(argv)
        print(canonical(audit(args.evidence, args.manifest_digest)))
        return 0
    except BaseException:
        print('{"error_code":"audit_failed","status":"failed"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
PY
```

마지막으로 다음 두 값이 8.0에서 고정한 SHA와 같고 status가 비어 있는지 확인한다.

```bash
git rev-parse HEAD
git status --porcelain=v1
```

검증한 SHA만 원격 branch로 push한다. PR 생성 뒤 head SHA가 달라졌다면 기존 evidence를
PR-head 결과로 주장하지 않고 새 head에서 전체 절차를 다시 실행한다.

## 9. 이번 범위에서 제외하는 것

- 실제 로봇과 physical authority
- 실제 hardware E-stop·sensor provenance·localization fault matrix
- STT/TTS, Homecam과 AWS
- 여러 fault를 한 case에 동시에 넣는 조합 시험
- randomized fuzz, 장시간 soak와 Gazebo 500회
- 성능·latency SLO
- stable operation ID 기반 `UNKNOWN -> known terminal` 승격
- cancel race와 실제 로봇 restart/reconciliation

SWM25-139가 합격하면 SWM25-122의 Gazebo 폐루프 Story를 닫을 수 있다. 다음 제품
개발은 SWM25-120 텍스트/음성 입력 연결, SWM25-123 실제 RobotState와 SWM25-124 실제
Nav2 start/status/result/cancel 경계로 이어간다.

## 10. Jira 결과 작성 기준

actual exact-commit campaign이 완료된 뒤 Jira에는 다음 형식으로 기록한다. angle bracket
값은 Jira에서만 실제 bounded 값으로 치환하고 이 tracked 문서는 다시 수정하지 않는다.

> SWM25-133 runner와 SWM25-134 campaign을 재사용해 거실·주방·침실 정상 이동,
> duplicate request·concurrent approval·competing workers, stale state·E-stop·map
> revision 변경, Nav2 unavailable·start response loss·terminal status response loss와
> group별 정상 복구 대조군을 clean commit `<commit>`에서 순서대로 실행했다. 제품 결과는
> `SUCCEEDED=9`, `BLOCKED=3`, `UNKNOWN=3`으로 기대값과 일치했고 RobotAction 15개,
> Robot Web start 12개, actual Nav2 goal/terminal 11개, preapproval 및 replay 추가
> effect 0개를 확인했다. child v6 15개와 campaign v5 aggregate의 canonical digest를
> 재검증했으며 public manifest digest는 `<manifest-digest>`다. campaign은 `15/15
> passed`, `stopped_early=false`, process·ROS node·socket·forced termination 0,
> proxy/observer close·join 성공으로 끝났고 전 과정은 `simulation=true`,
> `physical_authorized=false`다.
