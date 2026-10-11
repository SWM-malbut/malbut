# 낙상 코디네이터

`malbut_fall_coordinator`는 Bringup이 VLM·Pose와 함께 켜는 상시 노드다.
시스템 관리자는 낙상 이벤트·영상 판정·동의 설정을 해석하지 않는다.

## 책임과 연결

- 홈캠의 서버 설정 → VLM `ApplyFallSettings` → 실제 적용 결과를 홈캠으로 회신.
- VLM 사건 이벤트의 실행 ID·근거 버전·중복을 검사하고 확인 대화 필요 여부를 처리.
- 확인이 필요하면 `/malbut/mission/execute`에 `fall_confirmation`을 요청.
- 관리자는 Manifest의 `URGENT`, `[BASE, SPEAKER]`로 충돌 미션 종료를 확인한 뒤
  기존 Agent의 `/malbut/agent/confirm_situation` Action을 실행.
- 코디네이터가 최종 결과를 검증하여 해당 VLM 사건에 돌려줌. 실패·취소는 사용자
  무응답이나 도움 요청으로 바꾸지 않음. 일반 활동 종결도 기존 규칙을 유지.

VLM 분석·사건 상태·저장·업로드는 기존 `malbut_agent_server` 런타임,
질문·청취·답변 판단은 기존 Agent가 계속 담당한다. 코어를 복제하지 않는다.
취소된 이전 주행은 관리자의 기존 규칙대로 자동 재개하지 않는다.

코디네이터 재시작 시에는 `/malbut/state`에 남은 확인 미션의 종료를 기다린다.
동일 질문 재전달이 진행 중인 대화를 선점하지 않으며, Agent의 기존 결과 캐시로
복구한다. 새 근거 버전은 이전 요청을 취소하고 오래된 결과를 무시한다.

## 실행과 호환성

실로봇에서는 기존 `ros2 launch malbut_bringup robot.launch.py`를 사용한다.
낙상 설정이 있는 경우 준비 확인 후 노드가 함께 실행되며 별도 실행은 필요 없다.
설정 전달·heartbeat·VLM 이벤트의 Topic/Service와 Agent Action 필드는 그대로다.

새 노드의 읽기 전용 파라미터는 `runtime_id`, `bridge_runtime_id`, `vlm_runtime_id`다.
기존 메시지와 홈캠의 `manager_runtime_id`/`fall_manager_runtime_id` 필드는
호환성을 위해 이름을 유지하되 **낙상 코디네이터 실행 ID**를 담는다.
시스템 관리자에는 이 파라미터나 낙상 전용 구독을 등록하지 않는다.

`fall_confirmation.yaml`만 일반 Capability로 등록한다. 동의 설정 변경 Service는
Agent가 임의로 호출할 수 있도록 Manifest에 노출하지 않는다.

## 다가가 확인하기 (2026-10-08)

사람인지 확실하지 않은 낙상 의심(Cloud만 찾았거나 Pose 박스가 약함)은 묻기 전에 먼저 가까이 가서 본다.
런타임이 `question_requested`에 `approach_target{x, y, frame: map}`을 붙였을 때만이다(런치 `fall_approach`,
2026-10-08부터 기본 켜짐. 지도 좌표가 필요해 깊이 스위치 `fall_depth_aligned_to_rgb`도 켜져 있어야 하며 이것도 기본 켜짐).

```
approach_target 있음 → fall_approach(phase=approach) 미션: 금지 구역을 피해 1 m 앞, 의심 장소를 바라봄
  ├ 못 감(no_map·no_path·timeout·failed, Manager 거부=rejected) → approach_result → 그 자리에서 fall_confirmation
  └ arrived → approach_result → 런타임이 가까이서 확인(Pose 3초, 없으면 Cloud 1회) → person_check_completed
       ├ person(애매·실패 포함) → fall_confirmation(같은 질문 ID)
       ├ not_a_person → 질문 없음. 런타임이 사건을 not_a_person으로 종료 → fall_approach(phase=return) → return_result
       └ 15초 안에 답 없음 → 그 자리에서 fall_confirmation
```

- 다가가기 노드 `fall_approach`(이 패키지): `/malbut/falls/approach` `FallApproach` 액션 서버.
  ComputePathToPose(GridBased, keepout 반영) → 1 m 원에서 자름(사람 따라가기 `path_to_standoff`와 같은 규칙인
  `standoff_route`. tracking 패키지에 의존하지 않아 Agent CI에 감지기 빌드가 끌려오지 않음) →
  FollowPath → Spin으로 바라봄. 이동 60초 상한. 시작 위치를 질문 ID별로 저장해 return에서 돌아간다.
  결과는 `/malbut/falls/approach/status`(래치 JSON)에도 낸다(웹 순찰 카드의 "낙상 확인으로 멈춤").
- 능력 `fall_approach.yaml`: URGENT, `[BASE]`, `map_requirement: SELECTED`. 지도가 없거나 위치 전환 중이면
  Manager가 거부하고 코디네이터는 그 자리에서 묻는다.
- 멈춘 순찰·따라가기는 자동으로 다시 시작하지 않는다(기존 규칙). 웹 순찰 카드가 멈춘 이유와 결과를 보여 준다.
- 결정(코디네이터 → 런타임): `approach_result`, `view_result`, `return_result` = `boot_id, incident_id,
  question_id, evidence_revision, outcome`.

## 몸 전체 보고 판단하기 (2026-10-11, 런치 `fall_full_view`, 기본 꺼짐)

카메라가 12 cm 높이라 침대에 걸터앉은 사람은 1 m 앞에서 다리만 보인다(10월 10일 20:43, 11일 03:39).
사람인데 몸 일부만 보이면 몸 전체가 보이는 자리로 옮겨 다시 보고, 바닥에 있지 않으면 묻지 않는다.

```
person_check_completed
  ├ partial_body(사람 · 몸 일부만) → fall_approach(phase=view, standoff 2.5 m): 뒤로, 안 되면 좌우 45°·90°
  │    └ view_result → 런타임이 자세 확인 → posture_check_completed
  ├ whole_body(1 m 앞에서 이미 몸 전체) → 옮기지 않고 자세 확인 → posture_check_completed
  └ posture_check_completed
       ├ sitting_off_floor · standing → 질문 없음. 런타임이 off_floor_verified로 종료 → return(reason=off_floor)
       ├ on_floor · body_unseen · judgements_differ · posture_unconfirmed → fall_confirmation
       └ 30초 안에 답 없음 → 그 자리에서 fall_confirmation
```

- 자세 확인(런타임): Pose가 어깨·엉덩이 4점이 보이는 사람을 3번 잡고, 몸통이 서 있고, 몸통 관절 높이(깊이)가
  `fall_off_floor_torso_m`(기본 0.60 m, 1.0 m 넘으면 서 있음) 이상이면 Cloud에 자세를 한 번 묻는다.
  Cloud도 `raised`일 때만 닫는다. 몸통이 낮으면 Cloud 없이 묻는다.
- 옮기기는 60초 안(사용자 결정 1분). 시작 위치는 다가가기 때의 것을 그대로 써서 return이 원래 자리로 간다.
- return의 `reason`(`not_a_person`/`off_floor`)은 순찰 카드 결과(`fall_result`)를 가른다.
- 켜기 전에 실기에서 침대에 앉기·바닥에 앉기·바닥에 눕기·서기를 20번쯤 해서 잘못 닫은 경우가 0번인지 본다.
