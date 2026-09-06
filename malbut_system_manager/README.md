# Malbut System Manager

Capability Manifest에 등록된 ROS Action을 공통 진입점으로 실행하고,
전경·백그라운드 미션의 상태와 선점을 관리하는 패키지입니다.

## 공개 인터페이스

- 실행: `/malbut/mission/execute` (`malbut_interfaces/action/ExecuteMission`)
- 상태: `/malbut/state` (`malbut_interfaces/msg/SystemState`)

관리 대상은 설치된 `share/malbut_interfaces/capabilities/`의 Manifest만
사용합니다. `execution.resources`가 하나라도 겹치는 미션끼리만 충돌합니다.
전경·백그라운드 구분과 무관하며, 자원이 겹치지 않는 전경 미션도 병행합니다.
현재 실행 대상으로 허용하는 명령 방식은 취소 가능한 ROS Action입니다.

```yaml
execution:
  mode: FOREGROUND
  priority: NORMAL
  resources: [BASE]
```

자원은 `BASE`(차체 이동·회전), `SPEAKER`, `BUZZER`, `LED`, `DISPLAY`입니다.
여러 출력을 제어하면 모두 선언하며, 독점 출력이 없는 기능은 `resources: []`로
명시합니다. 센서 데이터를 함께 구독하는 것은 자원 충돌이 아닙니다.
현재 등록된 사람 추적·목적지 이동·순찰은 모두 `resources: [BASE]`입니다.

새 요청의 우선순위가 모든 충돌 미션보다 높거나 같으면 해당 미션만 취소하고,
실제 종료를 확인한 후 실행합니다. 더 높은 우선순위의 충돌 미션이 하나라도
있으면 새 요청을 거부합니다. 관계없는 미션은 계속 실행합니다.
선점당한 미션은 선점한 미션이 끝나고 자원이 비면 같은 입력으로 다시
실행합니다. 중단 지점을 복원하는 것은 아닙니다.

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

## 실제 응용 기능 연결 실험

저장소 루트에서 아래 스크립트를 실행하면 Small House 시뮬레이션과 관리자를
띄우고, 사람 추적·순찰·목적지 이동 요청을 시간 간격을 두고 전달합니다.
실험 종료 시 자신이 실행한 프로세스만 종료합니다.

```bash
bash malbut_system_manager/experiments/run_mission_sequence.sh
```

시퀀스, 확인 항목과 로그 형식은 [실험 안내](experiments/README.md)를 참고합니다.
