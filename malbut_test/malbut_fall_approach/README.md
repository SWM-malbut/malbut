# 낙상 확인 이동

`malbut_fall_approach`는 Manager가 `fall_approach` 미션으로 실행하는 주행 노드 `fall_approach`다.
낙상 의심 장소로 다가가고, 몸 일부만 보이면 몸 전체가 보이는 자리로 옮기고, 확인 뒤 원래 자리로 돌아간다.
무엇을 언제 할지는 Agent의 [낙상 규칙](../malbut_agent_server/docs/fall/fall_rules.md)이 정하고,
이 노드는 요청받은 주행만 한다. 질문이나 알림은 하지 않는다.
2026-10-11(SWM25-238)까지는 `malbut_fall_coordinator` 패키지 안에 있었고 동작은 그대로다.

- Action `/malbut/falls/approach`(`FallApproach`): `phase` = `approach`(1 m 앞, 의심 장소를 바라봄),
  `view`(요청한 거리로 물러남), `return`(질문 ID별로 기억한 시작 위치로).
- ComputePathToPose(GridBased, 금지 구역 반영) → 1 m 원에서 경로를 자름(`standoff_route`) →
  FollowPath → Spin으로 바라봄. 이동마다 60초 상한.
- `view`: 의심 장소를 바라보고 있으면 Nav2 BackUp으로 그대로 후진한다(0.1 m/s, 지역 costmap으로
  뒤쪽 충돌 확인). 안 되면 의심 장소 둘레의 자리(바로 뒤, 좌우 45°·90°)를 차례로 경로 계획·주행하고,
  주행이 실패해도 다음 자리를 시도한다. 2026-10-12 침대 옆에서 등 뒤 자리로 가려고 돌아서다
  Nav2 진행 확인(10초에 0.5 m)에 걸려 첫 자리에서 포기했던 것을 고쳤다.
- 결과는 `/malbut/falls/approach/status`(래치 JSON)에도 낸다(웹 순찰 카드의 "낙상 확인으로 멈춤").
- 능력 정의: [`fall_approach.yaml`](../malbut_interfaces/capabilities/fall_approach.yaml)
  (`[BASE]`, `map_requirement: SELECTED`).

Bringup의 `fall.launch.py`가 `fall_approach:=true`(기본)일 때 시작한다.
