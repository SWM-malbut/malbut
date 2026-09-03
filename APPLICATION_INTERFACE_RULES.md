# 응용 기능 책임 및 인터페이스 규격

응용 기능의 내부 알고리즘과 폴더 구조는 각 담당자가 결정합니다. 공통으로 정하는 것은 다른 패키지가 기능을 호출하는 데 필요한 **공개 ROS 계약과 최소 등록 정보**입니다.

## 1. 단일 원본 원칙

| 원본 | 책임 |
|---|---|
| `.action` | 장시간 명령의 Goal·Result·Feedback 필드와 ROS 자료형 |
| `.srv` | 짧은 명령의 Request·Response 필드와 ROS 자료형 |
| `.msg` | 별도 상태 Topic의 필드와 ROS 자료형 |
| `capabilities/*.yaml` | 기능 책임, 인터페이스 연결 정보, 입력과 실행 형태 |

Capability Manifest는 ROS 표준을 대체하지 않는 **Malbut 기능 등록 규격**입니다. `.action/.srv/.msg`가 필드와 상수의 최종 기준입니다.

```text
<application_package>/
├── capabilities/
│   └── <capability_id>.yaml
└── ...
```

설치 시 `capabilities/`를 `share/<application_package>/capabilities/`에 포함합니다.

## 2. Capability Manifest 구조

다음은 값의 예시가 아니라 Manifest의 **자료형 정의**입니다.

```yaml
schema_version: 1                    # integer, 현재 규격은 1로 고정

capability:                          # object
  id: string                         # 기능을 구분하는 고유 이름, 영문 소문자 사용
  title: string                      # 짧은 기능명
  description: string                # 기능 책임 한 문장

command:                             # object
  kind: enum(ACTION, SERVICE)        # ROS 명령 방식
  name: string                       # ROS Action 또는 Service 이름
  type: string                       # package/action/Type 또는 package/srv/Type

input:                               # object
  fields:
    <field_name>:                    # 실제 Goal 또는 Request 필드명
      type: ros_type                 # uint8, float32, string, geometry_msgs/Pose 등
      description: string            # 입력 의미
      default: ros_value             # 선택, 없으면 필수 입력

execution:                           # object
  mode: enum(FOREGROUND, BACKGROUND, IMMEDIATE)
```

| 항목 | 의미 |
|---|---|
| `capability` | 기능의 고유 이름과 책임 |
| `command` | 호출할 Action·Service의 이름과 ROS 타입 |
| `input` | Goal·Request 필드의 타입, 의미와 기본값 |
| `execution.mode` | 전경 미션, 병행 백그라운드 작업, 즉시 요청 구분 |

`default`가 없으면 반드시 전달해야 하는 입력입니다. 선택 가능한 값은 `.action/.srv`에 선언된 상수를 직접 확인하며 Manifest에 중복하지 않습니다. 조건부 입력과 수치 범위는 해당 Action·Service 서버가 검사합니다.

`FOREGROUND`는 하나씩 수행할 전경 미션, `BACKGROUND`는 전경 미션과 병행할 기능, `IMMEDIATE`는 짧게 끝나는 요청을 의미합니다. 선점 우선순위, 중복 요청, 동시 실행 및 준비 조건 정책은 이 규격에 포함하지 않습니다.

## 3. ROS 인터페이스 작성 규칙

- 연속 데이터·상태는 Topic, 짧은 요청은 Service, 피드백·결과·취소가 필요한 장시간 기능은 Action을 사용합니다.
- 의미가 맞는 표준 ROS 2·Nav2 인터페이스는 재사용하고, 프로젝트 전용 인터페이스만 `malbut_interfaces`에 추가합니다.
- 거리·시간·각도·주기는 `_m`, `_s`, `_rad`, `_hz`로 단위를 표시하고 선택값은 인터페이스 상수로 정의합니다.
- 요청값은 Action·Service 서버가 검증하며, 반복 조정하는 안전값과 알고리즘 튜닝값은 ROS parameter로 관리합니다.
- 응용 패키지는 다른 응용 패키지의 내부 소스를 import하지 않고 공개 Topic·Service·Action으로 통신합니다.
- Action 서버는 취소를 하위 Action까지 전달하고 남은 목표와 내부 상태를 정리해야 합니다.

## 4. 최소 검증 규칙

1. 기능 ID와 명령 이름이 중복되지 않아야 합니다.
2. `command.kind`, `command.type`, `execution.mode` 조합이 맞아야 합니다.
3. `input`의 필드명과 타입이 실제 Goal·Request와 일치해야 합니다.
4. `default`가 실제 ROS 타입과 일치해야 합니다.

현재 `FollowPerson.action`처럼 이미 정의된 인터페이스는 이 형식에 맞춰 등록합니다. 인터페이스 변경이 필요한 부분만 해당 기능의 별도 작업에서 수정합니다.
