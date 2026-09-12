# Malbut System Manager

Capability Manifest에 등록된 ROS Action·Service를 공통 진입점으로 실행하고,
전경·백그라운드 미션의 상태와 선점을 관리하는 패키지입니다.

## 공개 인터페이스

- 실행: `/malbut/mission/execute` (`malbut_interfaces/action/ExecuteMission`)
- 상태: `/malbut/state` (`malbut_interfaces/msg/SystemState`)

관리 대상은 설치된 `share/malbut_interfaces/capabilities/`의 Manifest만
사용합니다. `execution.resources`가 하나라도 겹치는 미션끼리만 충돌합니다.
전경·백그라운드 구분과 무관하며, 자원이 겹치지 않는 전경 미션도 병행합니다.
Action은 실행·취소·결과 전달, Service는 요청·응답 전달을 지원합니다.
두 방식 모두 같은 우선순위·자원 충돌 규칙을 사용합니다.

```yaml
execution:
  mode: FOREGROUND
  priority: NORMAL
  resources: [BASE]
```

자원은 `BASE`(차체 이동·회전), `SPEAKER`, `BUZZER`, `LED`, `DISPLAY`입니다.
여러 출력을 제어하면 모두 선언하며, 독점 출력이 없는 기능은 `resources: []`로
명시합니다. 센서 데이터를 함께 구독하는 것은 자원 충돌이 아닙니다.
현재 등록된 사람 추적·목적지 이동·순찰·자동 지도 만들기는 모두
`resources: [BASE]`입니다. 자동 지도 만들기의 기능 ID는 `autoslam`이며,
실시간 SLAM·Nav2와 `malbut_autoslam` 서버를 별도로 준비한 뒤 요청합니다.

새 요청의 우선순위가 모든 충돌 미션보다 높거나 같으면 해당 미션만 취소하고,
실제 종료를 확인한 후 실행합니다. 더 높은 우선순위의 충돌 미션이 하나라도
있으면 새 요청을 거부합니다. 관계없는 미션은 계속 실행합니다.
교체된 미션은 완전히 종료하며 보류하거나 자동 재개하지
않습니다. 다시 실행하려면 새 요청을 보내야 합니다. 보류 상태와 공개 필드는
유지하지만, 요청 교체에는 사용하지 않습니다.

직접 취소 요청의 상위 Action 결과는 `CANCELED`입니다. 관리자에 의한
교체는 `ABORTED`와 `message: mission preempted by a replacement request`를
반환해 실행 오류와 구분합니다. 하위 Action의 실제 종료 확인은 동일합니다.

하위 Action이 취소를 거부하면 해당 상위 요청은 실패로 끝내지만, 실제로
계속 실행 중인 하위 Action은 활성 미션으로 추적해 충돌 미션의 동시 실행을
막습니다. 수동 제어·비상 정지·충전 상태의 외부 입력 인터페이스는 별도
계약이 확정되기 전까지 이 패키지에서 공개하지 않습니다.

하위 Action의 Goal 응답과 취소 완료에는 각각 5초의 통신 watchdog을
사용합니다. `goal_response_timeout_s`, `cancel_completion_timeout_s` ROS
parameter로 조정할 수 있으며, 정상 실행 중인 미션의 전체 수행 시간에는
제한을 두지 않습니다.

## Service 실행 규칙

- 진입점은 동일한 `/malbut/mission/execute`입니다. Manifest의 `command.kind`
  를 `SERVICE`, `command.type`을 `package/srv/Type`으로 등록합니다.
- `arguments_yaml`로 Request를 만들고, 실제 Response를 `result_yaml`로
  그대로 반환합니다. Service는 진행 Feedback이 없으므로 관리자 상태만 전달합니다.
- 정상 응답을 받으면 상위 Action은 `SUCCEEDED`입니다. 이는 요청·응답 완료를
  뜻하며, 응답 안의 `success: false` 등 기능별 성공 여부는 그대로 확인해야 합니다.
- 아직 실행하지 않은 요청은 취소할 수 있습니다. 이미 보낸 Service는 ROS에서
  취소할 수 없으므로 응답까지 자원을 유지합니다. 취소·선점 요청 시 `CANCELING`으로
  표시하고, 응답 이후 기존 요청을 종료한 뒤 대기 중인 충돌 요청을 실행합니다.
  상위 취소 결과가 `CANCELED`여도 이미 수행된 Service의 효과를 되돌리지는 않습니다.
- Service에는 Action 취소 watchdog을 적용하거나 요청을 자동 재전송하지 않습니다.
  응답이 오지 않으면 종료를 확인할 수 없으므로 자원을 임의로 해제하지 않습니다.
  통신 결과가 불명확한 실패도 활성 기록을 유지하며 운영자가 서버 상태를 확인해야 합니다.
- 짧은 요청·응답에 사용합니다. `켜기` Service가 응답한 이후 계속되는 작업은
  해당 미션의 실행 시간에 포함되지 않습니다. 장시간 자원 소유·취소가 필요하면 Action을 사용합니다.

상시 YOLO·재식별처럼 Topic 데이터를 제공하는 노드는 Bringup에서 실행하며,
그 결과를 구독하기 위해 매니저 Manifest를 등록하지 않습니다.

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
