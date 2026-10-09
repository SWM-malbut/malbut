# 위치 보정

저장 지도 위에서 AMCL 위치 추정을 맞추는 `/relocalize` Action 서버다.
저장된 위치나 지정한 위치를 `/initialpose`로 보내고, AMCL이 그 위치에서 새 추정을
낸 것까지 확인한다. 저장 위치가 라이다와 맞지 않거나 `method=GLOBAL_SEARCH`(2)면
AMCL 전역 위치 탐색을 켜고 Nav2 Spin으로 제자리에서 한 바퀴 돈다.
찾은 위치는 아래 **위치 다듬기**로 지도에 맞춘 뒤 결과의 `match_ratio`로 알린다.

## 인터페이스

| 항목 | 내용 |
| --- | --- |
| Action | `/relocalize` (`malbut_interfaces/action/Relocalize`) |
| `method=SAVED_POSE`(0) | 이 지도에서 마지막으로 저장한 AMCL 위치, 없으면 AutoSLAM이 지도와 함께 저장한 `<지도>.pose.yaml` |
| `method=GIVEN_POSE`(1) | Goal의 `initial_pose`(map 좌표) |
| 결과 | `success`, `message`, 보정 후 AMCL 추정 `pose` |
| 피드백 | `WAITING`(지도 선택·AMCL 활성 대기) → `APPLYING` |

선택 지도는 시스템 관리자의 `/malbut/localization/state`에서 읽는다. 저장 지도
주행(`LOCALIZATION`) 중에만 동작하며, 지도 작성 중이거나 AMCL이 활성화되지 않으면
`timeout_s`(10초) 뒤 실패한다. 한 번에 하나의 Goal만 받는다.

## 사용처

- **시스템 관리자**: 저장 지도로 바꿀 때마다 `SAVED_POSE`를 요청하고, 결과를
  `/malbut/localization/state`의 `message`로 알린다. Bringup의
  `restore_pose:=false`면 요청하지 않는다.
- **다른 클라이언트**: `relocalize` 기능(Capability Manifest)으로
  `/malbut/mission/execute`에 요청한다. 저장 지도 선택 후에만 관리자가 받는다.

```bash
ros2 action send_goal /malbut/mission/execute malbut_interfaces/action/ExecuteMission \
  "{capability_id: relocalize, arguments_yaml: '{method: 0}'}"
```

## 위치 다듬기

한 바퀴 돌아 찾은 AMCL 위치는 수 cm·수 도씩 어긋나고, 시작이 무작위라 같은 자리에서도
매번 달라진다. 5 m 앞 벽은 3°만 틀어져도 26 cm 벗어나 일치율이 크게 떨어진다.
그래서 저장 위치를 확인할 때와 한 바퀴 찾기가 끝날 때마다, 그 위치 주변
±30 cm·±8°에서 라이다 점이 지도의 벽 면에 가장 가깝게 놓이는 위치를 찾는다
(5 cm·1° 간격 → 1 cm·0.2° 간격, `scan_match.refine`). 로봇청소기들이 대략 찾은 뒤 스캔을
지도에 맞추는 방식과 같다. 일치율이 오르면 그 위치를 AMCL에 다시 넣는다(약 5 cm·3° 퍼짐).
차체는 더 움직이지 않는다.

Gazebo `small_house` 지도에서 잡음 2 cm·가림 10%·빈 측정 10%를 넣은 스캔으로 60번 확인했다.
±25 cm·±7°에서 시작해도 다듬은 뒤 위치 오차는 최대 1.3 cm, 방향 오차는 최대 0.22°였다.
일치율 중앙값은 59%에서 91%로 올라 정답 위치의 값과 같았다. 한 번에 PC에서 약 90 ms 걸린다.

`GLOBAL_SEARCH`는 요청 때의 AMCL 위치도 다듬어 둔다. 찾기 결과가 그보다 낮으면 그 위치로
돌아가 다시 다듬고 `returned to the previous pose`라고 알린다. `SAVED_POSE`도 저장 위치보다 낮게 찾으면
저장 위치로 돌아간다(`returned to the saved pose`). 다시 찾기가 맞던 위치를 더 나쁜 위치로 바꾸지 않게 하려는 것이다.

## 위치 기억

AMCL이 활성인 동안 새 `/amcl_pose`를 **5초마다**
`~/.ros/malbut/localization/last_pose.yaml`에 저장한다(`save_period_s`, `pose_file`).
기록에는 지도 YAML·이미지 내용의 SHA-256을 함께 저장하므로, 같은 이름이라도 지도가
바뀌면 예전 위치를 쓰지 않는다. 사용자가 RViz **2D Pose Estimate**로 위치를 바꾸면
그 이전에 계산된 추정은 저장하지 않는다.

복원은 초기 추정일 뿐이다. 전원이 꺼진 동안 로봇을 옮겼다면 복원 위치가 틀리므로,
지도 위 위치가 실제와 맞는지 확인하고 필요하면 RViz로 다시 지정한다.
