# Malbut System Manager

Capability Manifest에 등록된 ROS Action을 공통 진입점으로 실행하고,
전경·백그라운드 미션의 상태와 선점을 관리하는 패키지입니다.

## 공개 인터페이스

- 실행: `/malbut/mission/execute` (`malbut_interfaces/action/ExecuteMission`)
- 상태: `/malbut/state` (`malbut_interfaces/msg/SystemState`)

관리 대상은 설치된 `share/malbut_interfaces/capabilities/`의 Manifest만
사용합니다. 현재 안전한 기본 정책은 모든 전경 미션이 서로 충돌하고,
백그라운드 미션은 병행하는 것입니다.
현재 실행 대상으로 허용하는 명령 방식은 취소 가능한 ROS Action입니다.

하위 Action이 취소를 거부하면 해당 상위 요청은 실패로 끝내지만, 실제로
계속 실행 중인 하위 Action은 활성 미션으로 추적해 충돌 미션의 동시 실행을
막습니다. 수동 제어·비상 정지·충전 상태의 외부 입력 인터페이스는 별도
계약이 확정되기 전까지 이 패키지에서 공개하지 않습니다.

하위 Action의 Goal 응답과 취소 완료에는 각각 5초의 통신 watchdog을
사용합니다. `goal_response_timeout_s`, `cancel_completion_timeout_s` ROS
parameter로 조정할 수 있으며, 정상 실행 중인 미션의 전체 수행 시간에는
제한을 두지 않습니다.

```bash
ros2 launch malbut_system_manager system_manager.launch.py
```

```bash
ros2 action send_goal \
  /malbut/mission/execute \
  malbut_interfaces/action/ExecuteMission \
  "{capability_id: follow_person, arguments_yaml: '{target_mode: 0, target_person_id: \"\", desired_distance_m: 1.0}'}" \
  --feedback
```
