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
현재 등록된 사람 추적·목적지 이동·순찰·자동 지도 만들기·위치 보정(`relocalize`)은 모두
`resources: [BASE]`, `NORMAL`입니다. 수동 조작 `manual_drive`는 Nav2
`AssistedTeleop`을 실행하는 `HIGH`·`[BASE]` 기능이라, 실행 중인 NORMAL 이동을
취소하고 수동 조작 중 NORMAL 이동 요청은 거부합니다. 자동 지도 만들기의 기능 ID는 `autoslam`이며,
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
막습니다. `/malbut/state`의 `control_mode`는 AssistedTeleop 미션(`manual_drive`)이
실행 중이면 `MANUAL`, 아니면 `AUTONOMOUS`입니다. 따로 설정하는 값이 아니며, 수동
조작 중 다른 미션을 막는 것은 우선순위·자원 규칙입니다. 비상 정지·충전 상태의 외부 입력 인터페이스는 별도 계약이 확정되기
전까지 이 패키지에서 공개하지 않습니다.

## 실로봇 Bringup에서 켜는 기능

아래 parameter는 기본값이 꺼져 있어 단독 실행·시뮬레이션 실험은 이전과 같습니다.
실로봇 Bringup이 켭니다.

- `ready_topic`: 준비 검사기의 `/malbut/bringup/status`가 READY를 보내기 전까지
  `BOOTING`으로 미션을 거부합니다.
- `localization_control`: 관리자 내부 모듈(`localization.py`)이 map→odom을 내는 위치
  추정을 하나만 유지합니다. 저장 지도가 없으면 slam_toolbox를 자식 프로세스로 실행하고,
  선택되면 SLAM을 끄고 `lifecycle_manager_localization`으로 map_server·AMCL을 켠 뒤
  지도를 로드합니다. 전환은 `/malbut/localization/load_map`(`nav2_msgs/srv/LoadMap`),
  `/malbut/localization/start_mapping`(`std_srvs/srv/Trigger`)으로 요청하고 상태는
  `/malbut/localization/state`(`std_msgs/String` JSON)로 발행합니다. `BASE` 미션이
  실행·대기 중이면 전환을 거부합니다. Manifest의 `execution.map_requirement`에 따라
  `SELECTED` 기능은 저장 지도 선택 후, `NOT_SELECTED` 기능은 지도 작성 중에만 받습니다.
  전환 중(`SWITCHING`)에는 둘 다 거부하고, 위치 보정이 로봇을 회전시킬 수 있으므로
  `BASE`를 쓰는 다른 미션(수동 조작 포함)도 거부합니다. 다른 저장 지도로 바꿀 때는
  AMCL을 RESET해 이전 지도의 위치를 넘기지 않습니다.
- `relocalize_action`: 저장 지도를 불러올 때마다 이 Action(Bringup에서는
  `/relocalize`, `malbut_relocalization`)을 `AUTO`로 요청하고, 결과가 나올 때까지
  `SWITCHING`을 유지합니다. 결과는 위치 추정 상태의 `message`로 알립니다.
  `relocalize_timeout_s`(90초)가 지나면 취소하고 수동 지정을 안내합니다.
- `manual_control` 노드: `/cmd_vel_teleop`에 움직임 명령이 들어오면 `manual_drive`를
  요청하고, 조작(명령 또는 보드 Joy의 스틱 기울기)이 5초 없으면 `/preempt_teleop`으로
  AssistedTeleop을 끝냅니다. `idle_timeout_s`로 조정합니다.

하위 Action의 Goal 응답과 취소 완료에는 각각 5초의 통신 watchdog을
사용합니다. `goal_response_timeout_s`, `cancel_completion_timeout_s` ROS
parameter로 조정할 수 있으며, 정상 실행 중인 미션의 전체 수행 시간에는
제한을 두지 않습니다.

## 웹에서 Bringup 수동 복구

웹에서 시작한 Bringup은 이 패키지의 `managed_bringup`이 실행과 자식 프로세스
종료를 추적합니다. 처음 전체 준비를 통과한 뒤 **Bringup 복구** 버튼을 누르면
관리자의 기존 `/malbut/mission/execute`에 `capability_id: recovery`를 요청합니다.
내부 `/malbut/bringup/recover`도 기존 `ExecuteMission` 형식이며 새 메시지는 없습니다.

- 기존 Bringup 단계마다 실제 종료가 확인된 프로세스만 원래 설정으로 한 번 실행하고,
  같은 준비 검사를 새로 수행합니다. 정상 프로세스는 그대로 둡니다.
- Nav2 구성 노드는 단일 컨테이너이므로 컨테이너 종료 시 전체 구성 요소를 다시 로드합니다.
  저장 지도와 마지막 AMCL 위치를 초기값으로 복원하고 새 센서·TF 준비 검사를 통과해야 합니다.
- 꺼 둔 옵션과 완료된 일회성 초기화는 대상이 아닙니다. 응답이 없더라도 프로세스가
  살아 있으면 강제 종료하거나 복제 실행하지 않고 해당 준비 단계에서 실패를 알립니다.
- 복구는 BASE·SPEAKER 자원을 점유하며 진행 중 새 미션을 거부합니다. 이전 미션은
  자동 재개하지 않습니다. 실패한 실행을 무한 반복하지 않으며 다시 누를 때만 재시도합니다.
- 관리자나 Bringup 소유 프로세스 자체가 종료됐으면 이 기능은 사용할 수 없습니다.
  관리자 소유 SLAM이 종료되어 잃어버린 미저장 지도는 재실행으로 복원할 수 없으므로,
  지도를 조용히 초기화하지 않고 저장 지도 선택 또는 새 지도 작성을 안내합니다.

`ros2 launch ... robot.launch.py`로 직접 실행한 경우에는 기존 종료 정책을 유지합니다.
웹의 `cloud.launch.py` 연결 및 Bringup 시작 경로를 사용해야 수동 복구 소유자가 실행됩니다.

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
