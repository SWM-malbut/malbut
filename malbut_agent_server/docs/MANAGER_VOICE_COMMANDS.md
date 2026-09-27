# Manager를 통한 음성 명령

음성 실행을 켜면 STT 원문 → LLM 도구 제안 → 현재 요청·인자 검증 →
대화 저장 → ROS 스레드의 `ManagerClient` → `/malbut/mission/execute`로 연결한다.
Agent는 Nav2나 속도 토픽을 직접 호출하지 않는다. Manager가 기능 등록,
준비 상태, 지도 모드, 자원 충돌과 실행·취소를 관리한다.

| 발화 예 | LLM 도구 | Manager 요청 |
| --- | --- | --- |
| 거실로 가줘 | `request_navigation(location="거실")` | `navigate_to_pose`와 등록된 목적지 pose |
| 따라와 | `request_follow_person()` | `follow_person`, 첫 번째 보이는 사람, 거리 1 m |
| 순찰해 | `request_patrol(thoroughness="normal")` | `patrol`, 꼼꼼함 1 |
| 꼼꼼하게 순찰해 | `request_patrol(thoroughness="thorough")` | `patrol`, 꼼꼼함 2 |
| 멈춰 / 취소해 | `cancel_voice_mission()` | 이 음성 경로에서 시작한 미종료 동작의 Goal 취소 |

따라오기 대상은 발화자로 인증된 사람이 아니다. 임의 사람 ID·좌표·속도·
Behavior Tree·기능 ID를 모델이 정할 수 없다. 상대 이동(앞으로/뒤로/회전)은
현재 등록된 음성 도구에 없으며 장소 이동으로 바꾸어 추측하지 않는다.
인용·부정·가정·복합 작업은 실행하지 않는다. 현재 직접 요청과 도구 선택이
맞지 않으면 실행을 거절하고, 모호한 장소는 질문하거나 등록된 이름을 요청한다.

## 설정

실로봇 `robot.launch.py`의 `speech_manager_commands` 기본값은 `true`다.
`speech.launch.py`에서는 `manager_commands`에 해당한다. 단독
`agent_communication`은 기존 동작을 유지하며 `--enable-manager-commands`를
명시해야 한다. `--check`는 실행 노드·모델·Manager를 호출하지 않는다.

장소 이동에는 `speech_navigation_targets:=/절대/경로/voice-targets.yaml`
(단독 speech launch: `navigation_targets`, CLI: `--navigation-targets`)을 지정한다.
미지정 시 따라오기·순찰·취소만 모델에 제공한다. 실제 지도에 확인된 목적지를
관리자가 아래 형식으로 작성한다. 다음 좌표는 형식 설명용 예시이며 실제 장소가 아니다.

```yaml
map: /absolute/path/to/selected-map.yaml
frame_id: map
locations:
  거실:
    x: 1.25
    y: -0.5
    yaw: 0.0
```

`map`은 Manager가 선택한 Nav2 지도 YAML의 절대 경로와 일치해야 한다.
`yaw`는 라디안이다. 장소 이름은 공백 정리와 Unicode NFC 정규화 후 정확히
일치해야 하며 중복 이름은 거부한다. 이름·좌표·지도 파일이 잘못됐거나
`/malbut/localization/state`가 `LOCALIZATION` 상태가 아니면 이동하지 않는다.
시뮬레이션의 방 fixture나 금지 구역 중심점을 실제 목적지로 사용하지 않는다.

목적지를 준비할 때와 Manager 전송 직전에 설정·지도 YAML·지도 이미지의
해시를 비교한다. 지도 전환, 목적지 변경, 만료, 기억 변경, 대화 변경,
상황 확인 대화에 의한 선점이 발생하면 아직 보내지 않은 요청을 폐기한다.
장소 파일을 지정했다는 것만으로 실제 위치 추정이나 주행 가능성을 보장하지 않는다.
기존 Manager와 하위 기능의 실행 검사 및 로봇 안전 제어가 계속 적용된다.

## 결과·취소·중복

Manager 전송은 ROS 소유 스레드에서 수행하며 추적·순찰 종료를 기다리지 않는다.
그래서 실행 중에도 다음 발화로 취소를 요청할 수 있다. 접수·진행·종료는 기존
`MissionAnnouncer`가 Manager의 관측 결과로 안내한다. 모델의 완료 주장이나
취소 접수만으로 동작 완료·물리 정지를 선언하지 않는다.

취소 범위는 이 Agent 프로세스의 음성 명령으로 시작한 미종료 동작이다.
웹·개발 JSON·날씨·상황 확인 작업은 취소하지 않는다. 프로세스가 재시작되면
이전 Goal handle을 복원하지 않으므로 이전 프로세스의 동작은 운영자가 확인한다.

발화 ID는 기존 SQLite 수신 기록으로 중복을 막고, 전송 준비 결과는 한 번만
소비한다. 전송 오류·시간 초과·TTS 발행 실패에도 자동 재전송하지 않는다.
결과 불명 Goal은 같은 프로세스에서 늦은 결과 관측과 취소를 위해 유지한다.

## 검증 범위

정책·지도 바인딩·중복·만료·상황 선점·대화 저장·취소를 고정 Provider와
테스트용 Manager로 검증한다. ROS 통합 시험은 localhost의 격리된 도메인에서
실제 Manager 및 Agent와 시험용 하위 Action 서버를 사용한다.
이는 실물 로봇 주행이나 실제 마이크·LLM·스피커 품질 검증과 구분한다.
