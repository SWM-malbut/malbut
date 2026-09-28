# Manager 음성 명령 공개 인터페이스 정의

## 1. 적용 기준과 단일 원본

이 문서는 음성으로 요청한 이동·따라오기·순찰·취소를 패키지 사이에서 연결하는 공개 ROS 계약과 최소 등록 정보를 정리한다.
기능 책임과 동작 범위는 [기능 명세](MANAGER_VOICE_SPEC.md)에 따른다.
작성 기준은 사용자가 제공한 [응용 패키지 책임 및 인터페이스 확정](https://app.notion.com/p/3cfc6013131b8077a8fcd3bbab52d039)이다.

| 최종 원본 | 이 기능에서의 역할 |
| --- | --- |
| `.action` | Manager 요청과 하위 장시간 기능의 Goal·Result·Feedback 필드 및 ROS 자료형 |
| `.srv` | 짧은 요청·응답 계약. 취소에는 ROS Action의 표준 취소 서비스를 사용 |
| `.msg` | 발화·음성 출력 Topic의 필드·상수와 Action에서 사용하는 표준 메시지 구조 |
| `malbut_interfaces/capabilities/*.yaml` | 기능 ID·책임, 호출할 ROS 명령, 실제 입력 필드·기본값, 기본 실행 형태·우선순위·자원 |

아래 표는 기존 원본을 읽기 위한 요약이다. 문서의 표나 LLM 도구 스키마가 ROS 자료형과 상수를 재정의하지 않는다.
Capability Manifest는 Malbut 기능 등록 규격이며 ROS IDL을 대체하지 않는다.

## 2. 공개 연결 목록

| 제공 → 사용 | 방식 | ROS 이름 | ROS 타입 | 역할 |
| --- | --- | --- | --- | --- |
| STT → Agent | Topic | `/malbut/speech/transcript` | `malbut_interfaces/msg/SpeechTranscript` | 최종 사용자 발화 전달 |
| Manager ← Agent | Action | `/malbut/mission/execute` | `malbut_interfaces/action/ExecuteMission` | 등록된 기능의 실행·진행·결과·취소 |
| Manager → Agent | Topic | `/malbut/localization/state` | `std_msgs/msg/String` | 현재 지도 모드와 선택된 지도 전달 |
| Agent → TTS | Topic | `/malbut/speech/response` | `malbut_interfaces/msg/SpeechRequest` | 답변과 작업 상태 안내 |
| Nav2 ← Manager | Action | `/navigate_to_pose` | `nav2_msgs/action/NavigateToPose` | 지도상의 pose로 이동 |
| Tracking ← Manager | Action | `/follow_person` | `malbut_interfaces/action/FollowPerson` | 선택된 사람 추적 |
| Patrol ← Manager | Action | `/patrol` | `malbut_interfaces/action/Patrol` | 선택된 저장 지도 순찰 |

장시간 기능은 피드백·결과·취소를 제공하는 Action으로 호출한다.
발화와 음성 출력은 기존 Topic 계약을 사용하며, 지도 상태는 상태 Topic으로 구독한다.
응용 패키지는 서로의 내부 Python·C++ 모듈을 import하지 않고 위 공개 경로로 통신한다.

### 2.1 발화와 음성 출력

| 원본 | 필드 | ROS 타입 | 의미 |
| --- | --- | --- | --- |
| [SpeechTranscript.msg](../../malbut_interfaces/msg/SpeechTranscript.msg) | `utterance_id` | `string` | 최종 발화 고유 ID. 동일 발화 재수신을 구분 |
| 동일 | `text` | `string` | 최종 인식한 발화 원문 |
| 동일 | `session_id` | `string` | Agent가 먼저 연 확인 대화의 세션. 일반 호출어 대화는 빈 값 |
| [SpeechRequest.msg](../../malbut_interfaces/msg/SpeechRequest.msg) | `text` | `string` | 재생할 안내 텍스트 |
| 동일 | `request_type` | `uint8` | 음성 요청 종류. 선택값은 해당 `.msg` 상수 참조 |
| 동일 | `playback_id` | `string` | 재생 ID. 빈 값이면 TTS가 생성 |
| 동일 | `interim` | `bool` | 최종 답변 전에 보내는 진행 안내 여부 |

두 Topic에서 Agent가 사용하는 QoS는 `KEEP_LAST`, depth 10, `RELIABLE`, `VOLATILE`이다.
Topic 발행 성공은 음성 재생 완료를 뜻하지 않는다.

### 2.2 Manager 실행 Action

필드의 최종 원본은 [ExecuteMission.action](../../malbut_interfaces/action/ExecuteMission.action)이다.

| 구분 | 필드 | ROS 타입 | 의미 |
| --- | --- | --- | --- |
| Goal | `capability_id` | `string` | 중앙 Manifest에 등록된 기능 ID |
| Goal | `arguments_yaml` | `string` | 해당 기능의 실제 Goal·Request 필드 값을 직렬화한 YAML mapping |
| Result | `mission_id` | `string` | 이 ROS Goal UUID의 하이픈 없는 32자리 hex |
| Result | `result_yaml` | `string` | 하위 기능의 Result·Response를 직렬화한 YAML |
| Result | `message` | `string` | Manager가 제공하는 진단 메시지 |
| Feedback | `mission_id` | `string` | 같은 Goal의 미션 ID |
| Feedback | `state` | `string` | 기존 IDL 주석에 정의된 진행 상태 |
| Feedback | `feedback_yaml` | `string` | 하위 Action Feedback을 직렬화한 YAML |

입력 필드를 생략하면 Manifest의 `default`를 적용하고, `default`가 없으면 필수 입력으로 검사한다.
ROS 타입·필드·기본값 형식은 Manager가 대조하며, 조건부 입력과 수치 범위는 해당 기능 서버가 검사한다.
Goal 접수 후 검증 실패로 종료될 수 있으므로 접수와 실행 성공을 구분한다.

종료 상태는 Result 본문의 새 필드가 아니라 ROS Action 결과 wrapper의 `status`다.
`action_msgs/msg/GoalStatus` 상수에 따라 성공·실패·취소를 판정한다.
현재 Agent는 ROS의 `ABORTED`를 내부 상태 `FAILED`로 표시한다.
하위 기능의 상세 성공 여부·진단은 `result_yaml`을 확인하며, 하위 `message`가 상위 `message`에도 복사된다고 가정하지 않는다.
Agent의 중복 방지용 `request_id`는 프로세스 내부 값으로 ROS Goal 필드에 추가하지 않는다.

### 2.3 취소 계약

| 항목 | 계약 |
| --- | --- |
| 대상 | 같은 Agent 프로세스의 음성 경로가 시작한 미종료 `ExecuteMission` Goal |
| 표준 서비스 | `/malbut/mission/execute/_action/cancel_goal` |
| ROS 타입 | `action_msgs/srv/CancelGoal` |
| 호출 | 해당 Goal handle을 통해 취소. 음성 입력으로 임의 UUID나 전체 취소를 받지 않음 |
| 전파 책임 | Manager가 응용 서버에 전달하고, 응용 서버도 실행 중인 하위 Goal에 전달 |
| 완료 조건 | 취소 접수와 최종 종료를 구분. 서버가 내부 목표를 정리하고 하위 종료를 확인한 뒤 최종 결과 반환 |

취소는 Action 프로토콜을 사용하므로 별도 취소 Capability를 등록하지 않는다.
취소 접수 실패나 통신 결과 불명은 작업 종료·물리 정지로 해석하지 않는다.

### 2.4 지도 상태 계약

기존 `/malbut/localization/state`는 `std_msgs/msg/String.data`에 JSON 문자열을 담는다.
아래 내부 키는 현재 Manager 발행 계약이며 `std_msgs/String` 자체가 정의한 필드는 아니다.

| JSON 키 | 자료형 | 의미 |
| --- | --- | --- |
| `mode` | string | 현재 위치 추정 모드. 현재 발행값은 `LOCALIZATION`, `MAPPING`, `SWITCHING`, `ERROR` |
| `map` | string 또는 null | 선택된 지도 YAML의 절대 경로 또는 미선택 |
| `message` | string | 상태 설명 |

QoS는 depth 1, `RELIABLE`, `TRANSIENT_LOCAL`이며 상태 전환 시 발행한다.
Agent는 목적지 설정이 있을 때 이 Topic을 구독하고, `LOCALIZATION`인 지도와 설정의 지도 연결을 확인한다.
주기적인 heartbeat나 위치 추정 정확도 지표로 취급하지 않는다.

이 JSON 계약을 전용 `.msg`와 상수로 옮기려면 발행자·구독자를 함께 변경하는 별도 인터페이스 작업이 필요하다.
이번 음성 연결에서는 기존 wire 형식을 유지한다.

## 3. Capability 등록 정보

### 3.1 등록 원본과 호출 대상

| 등록 원본 | 기능 ID | 기능 책임 | `command.name` | `command.type` |
| --- | --- | --- | --- | --- |
| [navigate_to_pose.yaml](../../malbut_interfaces/capabilities/navigate_to_pose.yaml) | `navigate_to_pose` | 지정한 지도 좌표로 이동 | `/navigate_to_pose` | `nav2_msgs/action/NavigateToPose` |
| [follow_person.yaml](../../malbut_interfaces/capabilities/follow_person.yaml) | `follow_person` | 선택한 사람과 희망 거리를 유지하며 추적 | `/follow_person` | `malbut_interfaces/action/FollowPerson` |
| [patrol.yaml](../../malbut_interfaces/capabilities/patrol.yaml) | `patrol` | 저장 지도에서 접근 가능한 구역을 요청한 꼼꼼함으로 관측 | `/patrol` | `malbut_interfaces/action/Patrol` |

세 등록의 `schema_version`은 `1`, `command.kind`는 `ACTION`이다.
`capability.title`과 설명의 원문은 해당 YAML을 따른다.
`ExecuteMission`은 Manager의 공통 진입점이므로 하위 기능으로 다시 등록하지 않는다.
음성 해석 도구도 응용 ROS 기능과 중복 등록하지 않는다.

### 3.2 입력 필드와 기본값

| 기능 | 실제 Goal 필드 | Manifest ROS 타입 | `default` | 의미와 최종 원본 |
| --- | --- | --- | --- | --- |
| 이동 | `pose` | `geometry_msgs/PoseStamped` | 없음: 필수 | 목적지 위치·방향. 표준 `nav2_msgs/action/NavigateToPose` 및 `geometry_msgs/msg/PoseStamped` |
| 이동 | `behavior_tree` | `string` | `""` | Nav2 Behavior Tree 경로. 표준 `NavigateToPose` |
| 따라오기 | `target_mode` | `uint8` | `0` | 대상 선택 방식. [FollowPerson.action](../../malbut_interfaces/action/FollowPerson.action) 상수 참조 |
| 따라오기 | `target_person_id` | `string` | `""` | 등록된 사람 선택 시 식별자. 동일 Action 참조 |
| 따라오기 | `desired_distance_m` | `float32` | `1.0` | 희망 거리, 단위 m. 조건과 범위는 해당 서버가 검사 |
| 순찰 | `thoroughness` | `uint8` | `1` | 순찰 꼼꼼함. [Patrol.action](../../malbut_interfaces/action/Patrol.action) 상수 참조 |

필드명과 타입은 생성된 Goal 정의와 일치해야 한다.
선택값 목록은 Action 상수를 직접 참조하며 Manifest에 별도 `enum`이나 숫자 대응표로 중복 등록하지 않는다.
표준 Nav2 인터페이스를 재사용하며 프로젝트 전용 이동 Action을 새로 만들지 않는다.
현재 Humble의 `NavigateToPose` Result는 `std_msgs/Empty result`이며, 이 문서에서 다른 버전의 `error_code`를 추가하지 않는다.

### 3.3 실행 등록과 설치

| Manifest 항목 | 세 기능의 현재 등록 | 해석 |
| --- | --- | --- |
| `execution.mode` | `FOREGROUND` | 시스템의 주 임무 상태에 영향을 주는 기능 |
| `execution.priority` | `NORMAL` | 기본 우선순위. 실제 요청 정책은 Manager 책임 |
| `execution.resources` | `[BASE]` | 차체 이동·회전 자원 사용 |
| `execution.map_requirement` | `SELECTED` | 현재 저장소의 선택적 확장. 저장 지도 선택 조건 |

`map_requirement`는 제공받은 기본 Manifest 구조에 없는 기존 저장소 확장이다.
정의와 적용 범위는 [공통 인터페이스 규격](../../../APPLICATION_INTERFACE_RULES.md)을 따른다.
병행·충돌·선점 관계를 음성 도구나 Manifest에 별도 재정의하지 않는다.
현재 등록기는 `resources` 필드를 요구하므로 독점 자원이 없는 기능도 `resources: []`를 명시한다.

등록 파일은 `malbut_interfaces/capabilities/<capability_id>.yaml`에 둔다.
설치 경로는 `share/malbut_interfaces/capabilities/`이며 현재 [CMakeLists.txt](../../malbut_interfaces/CMakeLists.txt)의 설치 규칙을 재사용한다.

## 4. 음성 입력을 기존 Goal로 연결하는 규칙

이 절은 Agent의 입력 변환 설명이다. 다른 응용 패키지가 호출하는 공개 ROS API나 별도 Capability 규격이 아니다.

| 현재 발화의 요청 | Manager에 전달할 값 |
| --- | --- |
| 등록된 장소로 이동 | `capability_id=navigate_to_pose`, 설정에서 해석한 `pose`, 빈 `behavior_tree` |
| 따라오기 | `capability_id=follow_person`, `target_mode=FollowPerson.Goal.VISIBLE_PERSON`, 빈 `target_person_id`, `desired_distance_m=1.0` |
| 순찰 | `capability_id=patrol`, 요청에 맞는 `Patrol.Goal` 상수의 `thoroughness`; 기본 `NORMAL` |
| 취소 | 음성 경로가 소유한 기존 Goal에 Action 취소 요청 |

Agent 내부 LLM 도구 스키마는 [`tools.py`](../malbut_agent_server/tools.py)가 관리한다.
모델이 좌표·속도·사람 ID·Behavior Tree·기능 ID·취소 UUID를 임의로 결정하도록 노출하지 않는다.
LLM은 표현 변형·사투리·인식 오탈자를 포함한 현재 요청의 의미로 실제 제공된 도구 하나를 선택한다.
바로 앞의 목적지 확인 질문에 대한 현재의 명시적 답변도 연결할 수 있지만, 기억이나 과거 대화만으로 새 작업을 시작하지 않는다.
인용·부정·가정·복합 요청의 의미 구분은 LLM 책임이며 실제 음성 경로에는 표현 정규식·키워드 허용 목록이 없다.
서버는 활성화된 도구, 인자 스키마, 유효 시간, 지도 연결, 대화 저장·변경, 취소 소유권과 중복 전송을 검사한다.

`SpeechTranscript`는 발화자 ID·방향·지도 좌표를 제공하지 않는다. `FollowPerson`은 사람을 계속 따라가는 Action이며
일회성 발화자 접근 계약이 아니다. 따라서 `와바라`, `이리 오너라`에 목적지가 없으면 등록된 장소를 확인하며,
임의 pose를 생성하거나 지속 추적으로 대체하지 않는다. 이 의미 해석 변경으로 공개 ROS 필드·상수·Manifest는 바꾸지 않는다.
목적지 설정, launch 인자, CLI 사용법은 [운영 안내](MANAGER_VOICE_COMMANDS.md)를 참조한다.

## 5. 최소 검증과 변경 경계

| 검증 항목 | 확인 기준 |
| --- | --- |
| 식별자 | 기능 ID와 정규화한 명령 이름이 중복되지 않음 |
| 명령 형식 | `ACTION`은 `/action/`, `SERVICE`는 `/srv/` ROS 타입과 일치 |
| 입력 | Manifest 필드명·타입이 실제 Goal·Request와 일치 |
| 기본값 | `default`가 실제 ROS 타입과 일치하며, 없으면 필수 입력 |
| 실행 등록 | mode·priority·resources가 허용된 형식과 값인지 검사 |
| 선택값·수치 의미 | IDL의 상수와 조건을 사용하고 실제 요청을 기능 서버에서 검사 |
| 취소 | Manager에서 응용 서버와 그 하위 Goal까지 취소가 전달되고 최종 종료 확인 |
| 설치 | 중앙 Manifest가 `share/malbut_interfaces/capabilities/`에 포함 |

등록 검증 근거는 `malbut_system_manager/test/test_manifest_registry.py`,
실제 Manager 연결 검증 근거는 `malbut_agent_server/test/test_ros_speech_missions.py`다.
mock의 표현 정규식은 `providers/mock_speech_intent.py`의 오프라인 시험 전용이다.
고정 Provider로 통과한 호출·취소 시험이 실제 LLM의 의미 판정 정확도를 보장하지는 않는다.

새 프로젝트 전용 인터페이스가 필요해질 때는 `malbut_interfaces`에 정의하고, 필드마다 의미·단위를 주석으로 남긴다.
거리·시간·각도·주기는 `_m`, `_s`, `_rad`, `_hz`로 표시하고 선택값은 IDL 상수로 둔다.
Action에는 성공·실패·취소 조건을 명시한다. 기존 표준 필드명과 이번에 재사용하는 IDL은 호환성을 유지한다.

현재 `ExecuteMission.state`는 문자열과 주석 계약이고 지도 상태는 String JSON이다.
이를 상수·전용 메시지로 정리하거나 기존 IDL의 누락된 주석을 보완하는 작업은 각 인터페이스의 별도 변경으로 다룬다.
이번 음성 연결은 기존 `.action/.srv/.msg` 및 세 Capability Manifest를 재사용한다.
