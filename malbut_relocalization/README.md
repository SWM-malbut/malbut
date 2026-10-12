# 위치 보정

저장 지도 위에서 AMCL 위치 추정을 맞추는 `/relocalize` Action 서버다.
저장된 위치나 지정한 위치를 `/initialpose`로 보내고, AMCL이 그 위치에서 새 추정을
낸 것까지 확인한다. 저장 위치가 라이다와 맞지 않거나 `method=GLOBAL_SEARCH`(2)면
AMCL 전역 위치 탐색과 Nav2 Spin을 사용한다. 위치는 AMCL만 추정하며,
일치율이 95% 이상이면 회전을 취소하고 끝낸다. 탐색은 최대 10초다.

## 인터페이스

| 항목 | 내용 |
| --- | --- |
| Action | `/relocalize` (`malbut_interfaces/action/Relocalize`) |
| `method=AUTO`(0) | 저장 AMCL 위치·AutoSLAM `<지도>.pose.yaml` 확인, 없거나 맞지 않으면 전역 탐색 |
| `method=GIVEN_POSE`(1) | Goal의 `initial_pose`(map 좌표) |
| 결과 | `success`, `message`, 보정 후 AMCL 추정 `pose` |
| 피드백 | `WAITING`(지도 선택·AMCL 활성 대기) → `APPLYING` |

선택 지도는 Bringup 공통 실행기의 `/malbut/localization/state`에서 읽는다.
지도·AMCL·스캔을 사용할 수 없으면 `timeout_s`(기본 10초) 뒤 실패한다.
한 번에 하나의 Goal만 받는다. 관리자는 지도/비지도 모드로 이 기능을 제한하지 않는다.
스캔·TF 연결은 유지하고, Action 종료 때 사용한 스캔 상태만 비운다.
실행 중 구독 객체를 삭제하지 않는다.
AMCL pose 수신과 마지막 위치 저장은 유휴 상태에서도 유지한다.

전역 탐색에는 알려진 빈칸이 필요하다. 전부 미확인인 기본 지도나 전부 점유된 지도에서는
AMCL 전역 탐색 서비스를 호출하지 않고 이유를 반환한다. `GIVEN_POSE`로 위치를 직접
지정하는 기능과 다른 센서 기반 기능은 유지한다.

## 사용처

- **Bringup 공통 실행기**: 저장 지도로 바꿀 때 `AUTO`를 요청하고, 결과를
  `/malbut/localization/state`의 `message`로 알린다. Bringup의
  `restore_pose:=false`면 요청하지 않는다.
- **다른 클라이언트**: `relocalize` 기능(Capability Manifest)으로
  `/malbut/mission/execute`에 요청한다. AMCL을 먼저 활성화하되, 이 Action이
  만들 초기 위치를 준비 단계에서 미리 요구하지 않는다.

```bash
ros2 action send_goal /malbut/mission/execute malbut_interfaces/action/ExecuteMission \
  "{capability_id: relocalize, arguments_yaml: '{method: 0}'}"
```

## 위치 보정 중 파티클 증량

기존 AMCL의 파티클 범위를 Action 실행 중에만 기본 2000~8000개로 변경한다.
새 AMCL 노드를 실행하지 않는다. 변경 전에 기존 범위를 읽고 성공·실패·취소 후 복원한다.
Humble AMCL은 범위를 바꾸면 필터를 초기화하므로, 같은 지도의 마지막 위치로 시작하고
복원할 때도 AMCL의 마지막 위치와 공분산을 그대로 전달한다. 자체 위치 탐색이나
고정 공분산으로 AMCL 결과를 덮어쓰지 않는다.

`scan_match`는 일치율 계산만 한다. 지도 장애물에서 0.10 m 이내인 유효 측정점의 비율을
사용한다(`match_distance_m`, `match_ratio_min`). 95%는 조기 종료 기준이다.
10초 내 도달하지 못해도 마지막 AMCL 위치를 유지하고, 측정한 일치율과 함께 완료로 반환한다.
회전 종료 확인과 파티클·위치 복원은 탐색 제한 이후에도 정리될 때까지 수행한다.
`GIVEN_POSE`는 사용자가 지정한 위치를 적용하고 일치율을 보고하는 기존 동작을 유지한다.

## 위치 기억

AMCL이 활성인 동안 새 `/amcl_pose`를 **5초마다**
`~/.ros/malbut/localization/last_pose.yaml`에 저장한다(`save_period_s`, `pose_file`).
기록에는 지도 YAML·이미지 내용의 SHA-256을 함께 저장하므로, 같은 이름이라도 지도가
바뀌면 예전 위치를 쓰지 않는다. 사용자가 RViz **2D Pose Estimate**로 위치를 바꾸면
그 이전에 계산된 추정은 저장하지 않는다.

복원은 초기 추정일 뿐이다. 전원이 꺼진 동안 로봇을 옮겼다면 복원 위치가 틀리므로,
지도 위 위치가 실제와 맞는지 확인하고 필요하면 RViz로 다시 지정한다.
