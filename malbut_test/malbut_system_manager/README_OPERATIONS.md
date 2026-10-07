# Malbut System Manager 운영 가이드

실행 명령·ROS parameter·Service 처리·기존 단계형 복구의 상세 안내입니다.
관리자의 역할과 현재 실행 구조는 [설계 개요](README.md)를 참고합니다.

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
현재 작업별 우선순위와 독점 자원은 다음과 같습니다.

| 우선순위 | 기능 | 자원 |
|---|---|---|
| `URGENT` | 낙상 확인 (`fall_confirmation`), Bringup 복구 (`recovery`) | `BASE`, `SPEAKER` |
| `HIGH` | 수동 조작 (`manual_drive`) | `BASE` |
| `NORMAL` | 사람 추적, 목적지 이동, 자동 지도 만들기, 위치 보정 | `BASE` |
| `LOW` | 순찰 (`patrol`) | `BASE` |

순찰 중 추적·이동 요청은 순찰의 종료를 확인한 뒤 실행합니다. 추적·이동 중 순찰
요청은 기존 작업을 유지하고 거부합니다. 같은 등급의 충돌 요청은 새 요청으로 교체하므로,
사람 추적과 목적지 이동은 서로 전환할 수 있습니다. 날씨 조회·지역 설정은
`resources: []`여서 이동 작업과 병행하며 우선순위 경쟁을 하지 않습니다.

수동 조작 `manual_drive`는 Nav2 `AssistedTeleop`을 실행하는 기능이라, 실행 중인
NORMAL·LOW 이동을 취소하고 수동 조작 중 NORMAL·LOW 이동 요청은 거부합니다.
자동 지도 만들기의 기능 ID는 `autoslam`이며,
Nav2와 `malbut_autoslam` 서버를 준비한 뒤 요청합니다. 현재 실로봇 Bringup에서는
이 요청이 관리자에게 SLAM 시작·종료를 연결하므로 SLAM을 별도로 중복 실행하지 않습니다.

새 요청의 우선순위가 모든 충돌 미션보다 높거나 같으면 해당 미션만 취소하고,
실제 종료를 확인한 후 실행합니다. 더 높은 우선순위의 충돌 미션이 하나라도
있으면 새 요청을 거부합니다. 관계없는 미션은 계속 실행합니다.
단, Bringup 복구가 진행 중일 때는 기존 복구 보호 규칙에 따라 새 미션을 거부합니다.
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

- `ready_topic`: 기본값은 빈 문자열이며 관리자 초기화 후 미션을 받습니다.
  별도로 지정하면 해당 Topic의 첫 READY를 받을 때까지 `BOOTING`으로 미션을 거부합니다.
  현재 실로봇 `robot.launch.py`는 빈 값을 전달합니다. 통합 Bringup의 연결 관측기는
  웹 표시용이며 관리자 전체의 미션 허용 조건으로 연결하지 않습니다.
- `localization_control`: 관리자 내부 모듈(`localization.py`)이 map→odom을 내는 위치
  추정을 하나만 유지합니다. 현재 Bringup은 저장 지도 미지정 시에도 기본 미확인 지도를
  `initial_map`으로 전달해 map_server·AMCL을 켭니다. SLAM은 지도 작성 요청 때
  자식 프로세스로 실행하며, 완료·취소·실패 후 기본 지도·AMCL로 돌아옵니다.
  관리자를 따로 구성하면서 `initial_map`을 비우면 기존 SLAM 시작 경로를 사용합니다.
  전환은 `/malbut/localization/load_map`(`nav2_msgs/srv/LoadMap`),
  `/malbut/localization/start_mapping`, `/malbut/localization/stop_mapping`
  (`std_srvs/srv/Trigger`)으로 요청하고 상태는
  `/malbut/localization/state`(`std_msgs/String` JSON)로 발행합니다. 지도 선택은
  `BASE` 미션이 실행·대기 중이면 거부합니다. AutoSLAM 자신의 SLAM 시작·종료는
  다른 활성 `BASE` 미션이 없으면 허용해 종료 정리가 가능하게 합니다.
  Manifest의 `execution.map_requirement`는 위치 추정 모드로 판정하며,
  `SELECTED`는 `LOCALIZATION`, `NOT_SELECTED`는 `MAPPING`에서 받습니다.
  기본 미확인 지도도 `LOCALIZATION`이므로 실제 저장 지도·위치 정확성을 보증하는 검사는 아닙니다.
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

## 기존 단계형 Bringup 복구

**현재 모듈형 Bringup에서는 이 복구 경로를 지원하지 않습니다.**
`recovery` Manifest와 실행 소유자는 남아 있지만, 모듈형 실행의 복구 요청은 거절합니다.
아래는 기존 단계형 실행기의 상세 동작이며, 현재 웹에서의 복구 성공을 설명하는 것이 아닙니다.

웹에서 시작한 Bringup은 이 패키지의 `managed_bringup`이 실행과 자식 프로세스
종료를 추적합니다. 전체 준비를 통과했거나 시작 단계의 준비 검사가 실패한 뒤
**Bringup 복구** 버튼을 누르면
관리자의 기존 `/malbut/mission/execute`에 `capability_id: recovery`를 요청합니다.
내부 `/malbut/bringup/recover`도 기존 `ExecuteMission` 형식이며 새 메시지는 없습니다.

- 기존 Bringup 단계마다 실제 종료가 확인된 프로세스만 원래 설정으로 한 번 실행하고,
  같은 준비 검사를 새로 수행합니다. 정상 프로세스는 그대로 둡니다.
- Nav2 구성 노드는 단일 컨테이너이므로 컨테이너 종료 시 전체 구성 요소를 다시 로드합니다.
  저장 지도와 마지막 AMCL 위치를 초기값으로 복원하고 새 센서·TF 준비 검사를 통과해야 합니다.
- 살아 있는 Nav2도 Lifecycle을 조회합니다. 모두 `inactive`면 공식 관리자의 `RESUME`,
  모두 `unconfigured`면 지도·Zone 설정을 복원하고 `STARTUP`을 호출합니다.
  `inactive`/`unconfigured`만 섞여 있으면 미설정 노드만 configure한 뒤 `RESUME`합니다.
  이미 활성화된 그룹은 조회만 하며, mapping 중 의도적으로 꺼 둔 AMCL은 활성화하지 않습니다.
- Nav2의 bond 고장 감지는 유지하되 자동 재연결 활성화는 끕니다. 수동 복구와 자동
  활성화가 동시에 실행되지 않으며, 다시 로드한 컨테이너도 수동 복구가 순서대로 활성화합니다.
- 꺼 둔 옵션과 완료된 일회성 초기화는 대상이 아닙니다. 서비스 미발견과 응답 시간 초과를
  구분해 `UNRESPONSIVE`로 기록하고, PID·프로세스 상태·Lifecycle 관측 결과를
  기존 Action 피드백/결과에 남깁니다. 읽기 전용 조회는 기존 `sensor_timeout_s`마다
  재조회하므로 한 번의 응답 유실만으로 재시작하지 않습니다.
  이는 DDS 장애나 교착의 원인을 확정한 것은 아닙니다.
- 기존 단계의 `startup_timeout_s` 안에 ROS 응답을 받지 못한 **소유 Nav2 컨테이너만**
  수동 요청당 한 번 종료 후 재실행할 수 있습니다. ROS launch의 `ShutdownProcess`로
  SIGINT → SIGTERM → SIGKILL 절차를 맡기며, 실제 종료 이벤트 확인 전에는 새 프로세스를
  만들지 않습니다. 다른 정상 프로세스나 외부에서 실행한 Nav2는 종료하지 않습니다.
  활성/비활성 상태가 섞였거나 전환 중인 그룹은 정상 노드를 초기화하지 않고 실패를 알립니다.
- 재활성화 성공 응답만으로 완료하지 않고 실제 ACTIVE 상태와 최신 costmap·TF·Action을
  재검사합니다. 새 컨테이너도 응답하지 않으면 반복 재시작하지 않고 실패합니다.
  전환 명령은 ROS 서비스라 취소할 수 없으므로, 시간 초과·취소 후에도 응답이 오기 전까지
  (또는 해당 컨테이너의 종료를 확인하기 전까지) 중복 복구를 거부합니다.
- 웹 관리 실행의 초기 준비 검사 실패는 남은 단계만 보류하고 이미 실행한 프로세스를
  보존합니다. 수동 복구가 도달한 단계들을 재검증한 다음 미실행 단계들을 이어서 시작합니다.
  관리자가 아직 켜지지 않은 초기 센서 단계 실패는 웹 Bringup 종료 후 재시작이 필요합니다.
  프로세스 생성 자체 실패·launch 예외·음성 내부 초기화 예외까지 모두 복구하는 것은 아닙니다.
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

시뮬레이션 실험은 전체 저장소의 원본 패키지에서 실행합니다. 시퀀스, 확인 항목과 로그 형식은
[실험 안내](../../malbut_system_manager/experiments/README.md)를 참고합니다.
