# 위치 보정

저장 지도 위에서 AMCL 위치 추정을 맞추는 `/relocalize` Action 서버다.
지금은 **기능을 Action으로 분리한 최소 구현**이다. 저장된 위치나 지정한 위치를
`/initialpose`로 보내고, AMCL이 그 위치에서 새 추정을 낸 것까지 확인한다.
차체를 움직이지 않으며 전역 위치 탐색·스캔 정합 같은 고도화는 아직 없다.

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

## 위치 기억

AMCL이 활성인 동안 새 `/amcl_pose`를 **5초마다**
`~/.ros/malbut/localization/last_pose.yaml`에 저장한다(`save_period_s`, `pose_file`).
기록에는 지도 YAML·이미지 내용의 SHA-256을 함께 저장하므로, 같은 이름이라도 지도가
바뀌면 예전 위치를 쓰지 않는다. 사용자가 RViz **2D Pose Estimate**로 위치를 바꾸면
그 이전에 계산된 추정은 저장하지 않는다.

복원은 초기 추정일 뿐이다. 전원이 꺼진 동안 로봇을 옮겼다면 복원 위치가 틀리므로,
지도 위 위치가 실제와 맞는지 확인하고 필요하면 RViz로 다시 지정한다.
