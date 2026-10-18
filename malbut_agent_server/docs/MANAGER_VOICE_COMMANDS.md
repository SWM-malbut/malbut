# Manager를 통한 음성 명령

음성 실행을 켜면 STT 원문 → LLM 의미 해석·도구 제안 → 서버의 도구·인자·전송 조건 검사 →
대화 저장 → ROS 스레드의 `ManagerClient` → `/malbut/mission/execute`로 연결한다.
Agent는 Nav2나 속도 토픽을 직접 호출하지 않는다. Manager가 기능 등록,
자원 충돌과 실행·취소를 관리하며, 기존 Bringup이 기능별 준비를 담당한다.

| 발화 예 | LLM 도구 | Manager 요청 |
| --- | --- | --- |
| 거실로 가줘 | `request_navigation(location="거실")` | `navigate_to_pose`와 등록된 목적지 pose |
| 따라와 | `request_follow_person()` | `follow_person`, 첫 번째 보이는 사람, 거리 1 m |
| 순찰해 | `request_patrol(thoroughness="normal")` | `patrol`, 꼼꼼함 1 |
| 꼼꼼하게 순찰해 | `request_patrol(thoroughness="thorough")` | `patrol`, 꼼꼼함 2 |
| 새 지도 만들어줘 | `request_mapping(map_name="new_home")` | `autoslam` |
| 위치 다시 찾아줘 | `request_relocalization(method="auto")` | `relocalize`, method 0 |
| 수동 조작 켜줘 | `request_manual_control()` | `manual_drive` (속도 명령 없음) |
| Bringup 복구해줘 | `request_recovery()` | 기존 `recovery` Action |
| 멈춰 / 취소해 | `cancel_voice_mission()` | 관측된 전면 작업과 음성 접수 대기 Goal의 표준 취소 |
| 왼쪽으로 90도 돌아 | `request_rotation(direction="left", angle_deg=90)` | 기존 Nav2 `/spin` |
| 30 cm 뒤로 가 | `request_backup(distance_m=0.3)` | 기존 Nav2 `/backup` |
| 지도 좌표 (1, 2)로 가 | `request_navigation_point(map_revision=…, x=1, y=2, yaw_deg=null)` | 기존 `navigate_to_pose` |
| 거실 다음 주방으로 가 | `request_waypoints(map_revision=…, waypoints=[…])` | 기존 Nav2 `/follow_waypoints` 한 번 |

따라오기 대상은 발화자로 인증된 사람이 아니다. 임의 사람 ID·속도·
Behavior Tree·기능 ID를 모델이 정할 수 없다. 사용자 요청 범위 안에서 RGB와 현재 위치·방향,
등록 장소 좌표를 함께 보고 회전각·이동거리·지도 목표·경유점을 선택할 수 있다.
사용자가 수치를 지정하면 우선하며, 없더라도 현재 정보로 정할 수 있으면 합리적인 추정값을 사용한다.
회전은 1~360도, 후진은 0.05~1 m이며 속도(후진 0.1 m/s)와 시간 제한은 서버가 정한다.
직접 전진 도구나 사진 픽셀을 지도 좌표로 바꾸는 기하 변환은 추가하지 않는다.
실제 음성 명령의 의미는 LLM이 판단한다. 서버가 정규식이나 키워드 허용 목록으로
표현을 제한하지 않으며, 바꿔 말하기·사투리·구어체·인식 오탈자도 의미를 기준으로 처리한다.
인용·부정·가정과 실행 요청을 구분하고 복합 작업이나 모호한 목적지의 후보를 해석하는 책임은 LLM에 있다.
서버는 활성 도구·인자 형식·만료·지도·대화 저장 및 변경·취소 소유권·중복 전송을 검사하며,
이 검사가 모델의 의미 해석 정확도를 보장하지는 않는다.

`거실로 가볼까?`처럼 목적지가 분명한 제안도 현재 이동 요청으로 해석한다.
바로 앞에서 목적지를 물은 뒤 사용자가 `거실`이라고 명시적으로 답하면 그 목적지로 요청할 수 있다.
기억이나 오래된 대화만으로 새 작업을 시작하지 않는다.

현재 선택된 지도에 등록된 목적지 이름을 `navigation_context.locations`로 LLM에 제공한다.
기존 이름 기반 도구는 등록 이름으로 이동을 제안하며,
서버가 원래 발화의 장소 이름과 후보를 비교한다. `기실로 가`를 `거실`로 해석한 경우처럼
이름이 비슷하고 다른 후보와 분명히 구분되면 추가 질문 없이 요청한다.
이름이 크게 다르거나 비슷한 후보가 여러 개라 구분하기 어려우면 먼저 목적지를 확인한다.
모델도 가능한 후보가 없거나 하나로 고를 수 없으면 등록된 이름으로 질문하고 이동을 제안하지 않는다.
실제로 `기실`이라는 이름이 등록되어 있으면 정확히 일치하는 그 장소를 우선한다.

서버의 확인 질문은 응답 토픽에 정상 발행된 뒤에만 효력이 생긴다. 확인 후보는
`navigation_context.confirmation_location`으로 다음 한 번의 발화에 전달하며 30초 뒤 만료한다.
현재 답변이 그 이동을 승인하는지, 거절하는지, 다른 장소를 요청하는지는 LLM이 의미로 판단한다.
`응`을 승인으로 판단해 같은 등록 목적지를 제안한 경우에만 대기 중인 확인을 사용할 수 있다.
거절이나 무관한 발화가 끼면 이전 확인을 이후의 `응`에 재사용하지 않는다.
현재 답변에서 `아니 주방으로 가`처럼 다른 등록 목적지를 명시하면 새 목적지 요청으로 검사한다.
지도·목적지 파일 또는 대화 세션이 바뀌거나 상황 확인 대화가 선점하면 대기 중 확인을 폐기한다.
질문의 발행은 실제 스피커 재생이나 사용자가 들었다는 증거와는 구분한다.

`와바라`, `이리 오너라`의 의미를 이해해도 현재는 발화자의 지도상 위치와 일회성 접근 기능이 없다.
`어느 장소로 갈까요?`처럼 등록된 목적지를 확인하며 좌표를 추측하거나 지속 따라오기로 대체하지 않는다.

## 발화별 이미지와 상태 (SWM25-222)

기존 `agent_communication` 노드가 RGB(depth=1, BEST_EFFORT), TF, Manager 상태와
카메라/클라우드 동의 snapshot을 읽는다. 새 ROS 노드·프로세스·비전 모델은 추가하지 않는다.
각 일반 대화의 주 LLM 요청에 최신 RGB 한 장을 최대 640×400 JPEG, `detail=low`로 첨부한다.
원본 버퍼는 한 장만 유지하고 발화 시점에 압축한다. 수신/촬영 시점 기준 2초를 넘긴 사진,
미래 시각, 변환 실패는 이미지 없이 대화를 계속한다. 부분 STT·호출 대상 분류에는 붙이지 않는다.

`scene_context_untrusted`에는 촬영 시각/age, 지도 ID/revision/해상도/원점/크기,
최대 32개 장소 이름과 대표 좌표, 최신 TF의 `x,y,yaw_deg`, 작업 상태를 넣는다.
TF가 오래되거나 없으면 `pose=null`이다. 실제 위치를 원점으로 대체하지 않는다.
작업 상태는 하트비트가 아닌 마지막 상태 전환 관측이므로 age와 출처를 함께 표시하고,
상태 publisher가 없으면 전달하지 않는다. 실행 중이라는 확정 근거로 사용하지 않는다.
지도 원본 OccupancyGrid, 깊이 영상, 파일 절대 경로는 LLM에 보내지 않는다.
모델은 사진의 방향 단서와 제공된 위치·장소 좌표를 조합해 적절한 이동 목표를 판단할 수 있다.
사소한 불확실성 때문에 매번 각도나 거리를 되묻지 않으며, 판단에 꼭 필요한 정보가 없을 때만 확인한다.
RGB 기반 거리·각도는 추정치이므로 무리한 이동은 피하고 후방 안전을 확인했다고 단정하지 않도록 짧게 주의한다.

사진 bytes/Base64는 요청 JSON·SQLite·입력 trace·대화 이력에 저장하지 않으며,
같은 요청의 재시도는 같은 사진만 재사용한다. 재전송 직전 freshness/동의를 재확인한다.
요약·기억 추출·기억 원문 검증 요청에는 사진과 장면 context를 붙이지 않는다.
관측으로 생성한 대화 답변 자체는 기존 대화 저장 정책을 그대로 따른다.

`HOMECAM_BACKEND_URL`이 있으면 `/malbut/falls/settings/snapshot`의 최근 30초 내
확인된 `camera_enabled && cloud_consent`가 있어야 사진을 전송한다. 낙상 enabled는
사진 동의와 무관하다. OFF·조회 실패·동의 만료 시 사진을 보내지 않는다.
클라우드 설정을 쓰지 않는 단독 실행에서는 이미지 전송이 기본 ON이며,
관측한 OFF 설정은 단독 실행에서도 존중한다. 끄려면 Bringup의
`speech_scene_images_enabled:=false`, 단독 `speech.launch.py`의
`scene_images_enabled:=false` 또는 노드 parameter `scene_images_enabled=false`를 사용한다.
RGB topic과 로봇 frame은 기존 Bringup 설정을 재사용한다.

## Nav2 툴 확장 경계

Agent의 툴 schema·인자 변환과 `malbut_interfaces/capabilities`의 등록 YAML만 확장한다.
Manager Python 코드는 변경하지 않는다. 모든 이동은 기존 `/malbut/mission/execute`를 사용한다.
기존 Bringup 준비 목록에 `spin/backup`은 motion, `follow_waypoints`는 motion+planning을 등록한다.
이미지를 Nav2로 보내거나 Agent가 속도 토픽을 발행하지 않는다.

좌표/경유점 요청의 `map_revision`은 발화에 제공된 값을 복사한다. 준비 전과 실제 전송 전에
지도·장소 revision을 재검사하고, 좌표는 지도 범위·free cell·0.22 m 여유를 확인한다.
이는 도착점 검증이지 경로 계획이 아니다. 실제 경로/장애물/이동 성공 여부는 Nav2가 판단한다.
카메라 한 장으로 정확한 깊이·화자 위치·후방 안전을 안다고 가정하지 않는다.

경유점은 1~16개이며 각 항목은
`{"location":"거실","x":null,"y":null,"yaw_deg":null}` 또는
`{"location":null,"x":1,"y":2,"yaw_deg":null}` 형식이다.
한 목록을 하나의 `FollowWaypoints` Goal로 보내므로 취소·선점도 기존 미션 하나로 처리한다.
`waypoint_follower.stop_on_failure=true`로 중간 실패 시 중단하며, 실패한 구간 뒤를 성공한 것으로
안내하지 않는다. 기존 낙상 중 이동 차단·세션/만료 검증·한 번만 전송하는 경계도 유지한다.

## 설정

실로봇 `bringup.launch.py`의 `speech_manager_commands` 기본값은 `true`다.
`speech.launch.py`에서는 `manager_commands`에 해당한다. 단독
`agent_communication`은 기존 동작을 유지하며 `--enable-manager-commands`를
명시해야 한다. `--check`는 실행 노드·모델·Manager를 호출하지 않는다.

장소 이동은 별도 목적지 파일이나 실행 인자가 필요하지 않다.
Agent는 `/malbut/localization/state`의 `LOCALIZATION` 상태에서 현재 선택된
지도 YAML을 확인하고, 해당 지도 옆의 `*.user-map.geojson`을 매 발화마다 읽는다.
웹에서 방을 설정하면 서버의 `robot_semantic_drafts`에 초안이 저장되고,
로봇이 `rooms_save`를 적용한 뒤 이 User Map에 반영된다. 서버의 미적용 초안은
음성 이동에 사용하지 않는다. 지도 전환과 적용된 방 이름·좌표 수정은
Agent 재시작 없이 다음 발화에 반영된다.

`role: room` Feature의 이름만 LLM에 제공하고, 모델이 선택한 방의
`properties.representative_point`를 서버에서 읽어 `map` 좌표계의 x, y로 변환한다.
방 문서에는 도착 방향이 없으므로 yaw는 0으로 요청한다. 모델은 좌표를 만들지 않는다.
방 이름은 공백 정리와 Unicode NFC 정규화 후 비교하며, 중복 이름은 거부한다.
저장된 대표 좌표가 없으면 도형의 중심이나 다른 기본 좌표로 대체하지 않는다.
User Map의 `map_revision`이 실제 지도 이미지·해상도·원점·점유 해석과 맞지 않거나,
활성 지도·적용된 방을 확인할 수 없으면 이동하지 않는다.

LLM 응답을 기다리는 동안, 목적지를 준비한 뒤, 확인 대화 중에도
지도 전환이나 User Map·지도 YAML·지도 이미지 변경을 재검증한다.
변경, 만료, 기억 변경, 대화 변경, 상황 확인 대화에 의한 선점이 발생하면
아직 보내지 않은 요청을 폐기한다. 대표 좌표 조회만으로 실제 위치 추정이나
경로의 주행 가능성을 보장하지 않는다. 기존 Manager·Nav2의 실행 검사와
로봇 안전 제어가 계속 적용된다.

## 결과·취소·중복

Manager 전송은 ROS 소유 스레드에서 수행하며 추적·순찰 종료를 기다리지 않는다.
그래서 실행 중에도 다음 발화로 취소를 요청할 수 있다. 접수·진행·종료는 기존
`MissionAnnouncer`가 Manager의 관측 결과로 안내한다. 모델의 완료 주장이나
취소 접수만으로 동작 완료·물리 정지를 선언하지 않는다.

취소 범위는 `/malbut/state`에서 관측한 전면 작업(대기·중단 포함)과 아직 접수 응답이
안 온 이 프로세스의 음성 Goal이다. 배경 작업은 유지하며, 각 Goal UUID를 기존
`/malbut/mission/execute/_action/cancel_goal`에 전달한다. 전체 Goal을 취소하는
wildcard나 별도 중지 인터페이스는 만들지 않는다. 취소 요청 이후 새로 들어온 작업까지
금지하는 전역 상태는 없으며, 물리 정지는 하위 Action의 실제 취소 완료에 따른다.

발화 ID는 기존 SQLite 수신 기록으로 중복을 막고, 전송 준비 결과는 한 번만
소비한다. 전송 오류·시간 초과·TTS 발행 실패에도 자동 재전송하지 않는다.
결과 불명 Goal은 같은 프로세스에서 늦은 결과 관측과 취소를 위해 유지한다.

## 낙상 확인 중 말과 이동 명령 (2026-10-12, 2026-10-13)

우선순위(2026-10-13 사용자): 직접 조작·복구·음성 "그만/멈춰" > 낙상 확인 > 그 밖의 모든 것(대화·음성 이동·
앱 이동·보호자 홈캠 말하기). Bringup 끄기·설정 바꾸기·처리 완료·개발자 화면은 사용자가 일부러 하는 조작이라
이 순서에 넣지 않는다. Manager 우선순위 표는 그대로다. 낙상 확인 이동·질문은 HIGH이고, 멈춰서 보는 동안에도
`fall_approach`의 `hold` 단계로 HIGH 미션을 쥐어 NORMAL 이하(목적지 이동·따라가기·순찰·지도 만들기·위치 확인)는
Manager가 거절한다. 대화·홈캠 말하기는 Manager 미션이 아니어서 Agent·STT·웹이 막는다.

- 낙상 우선 구간: `/malbut/falls/check/status`가 3초 안에 `active`이고 `phase`가 `approach`·`look`·`ask`·`wait`일
  때(의심부터 질문 끝까지). 돌아오기(`return`)는 확인이 끝난 뒤라 빠진다. `ask` 중 답은 지금처럼 낙상 질문이 듣는다.
- 구간에 들어가면 Agent가 대화를 멈추고(`DialogueWorker.suspend`), 말하던 대답을 끊고(`/malbut/speech/playback_control`
  STOP_ALL), 끊긴 뒤 "낙상이 의심돼서 먼저 확인할게요."라고 말한다. 대답을 기다리던 말이 있으면 그 `request_id`로
  말해 STT가 다시 듣게 한다. 구간이 끝나면(진행 중 상황 질문이 없으면) 대화를 다시 받는다.
- 구간 안에서 들은 말은 대화·모델·Manager로 보내지 않는다(`fall_check_watch.py`).
  - "그만·멈춰·정지·취소…"(`asks_to_stop`): `/malbut/falls/check/voice`에 `{"kind": "stop"}`을 보내고 "알겠어요. 확인을
    멈추고 보호자에게 알릴게요." 낙상 규칙이 직접 조작처럼 확인을 멈추고, 묻지 않고, 확인 필요 알림
    `check_stopped_by_user`를 보낸다. 사건은 열린 채로 둔다.
  - "도와줘·살려줘·아파·119…"(`asks_for_help`): `{"kind": "help"}`, "보호자에게 바로 알릴게요." 낙상 규칙이 도움
    필요로 끝내 긴급 알림을 보낸다.
  - 그 밖의 말: "지금 확인 중이에요. 잠시만요."
- 돌아오기 중이나 Manager에 낙상 미션만 남은 때는 대화를 받되 음성 이동은 받지 않는다.
  - 막는 것: `request_navigation`·`request_follow_person`·`request_patrol`·`request_mapping`·`request_relocalization`.
    직접 조작·복구는 그대로 받는다.
  - 낙상 확인 중: 상태의 `active`가 3초 안에 참이었거나, Manager `/malbut/state`에 `fall_confirmation`·`fall_approach`가
    실행·대기·중단 중일 때.
  - 대답: "네, …시작하겠습니다" 대신 "낙상 확인 중이라 잠시 이동할 수 없어요." 모델이 명령을 고른 직후(준비)와
    Manager에 보내기 직전에 두 번 확인한다. 확인을 못 하면 보내지 않는다.
- 음성으로 시킨 이동 중에 낙상이 의심되면 낙상 확인(HIGH)이 그 이동(NORMAL)을 끊는다. 직접 조작은 끊지 않고
  끝날 때까지 기다린다. 끊긴 이동은 다시 시작하지 않는다(이어서 하기는 추후 구현).

## 홈캠 정보 조회

홈캠 상태·감지·녹화 메타데이터·낙상 기록은 Agent 안의 API 클라이언트가 직접 조회한다.
기존 장치 토큰과 소유자의 음성 조회 위임을 사용하며 Manager나 하위 ROS Action에
연결하지 않는다. 조회값은 현재 답변에만 전달하고 추가 실행이나 설정 변경을 허용하지 않는다.
Bringup 자동 시작·지도 선택·위치 보정 재시도·상주 음성·관리자는 이 연결에 포함하지 않는다.

## 검증 범위

도구·인자 검사, 지도 바인딩·중복·만료·상황 선점·대화 저장·취소를 고정 Provider와
테스트용 Manager로 검증한다. ROS 통합 시험은 localhost의 격리된 도메인에서
실제 Manager 및 Agent와 시험용 하위 Action 서버를 사용한다.
목적지 이름만 모델에 전달되는지, 유일하게 비슷한 이름이 질문 없이 요청되는지,
크게 다른 이름의 확인 전에는 Goal이 없고 승인 후 한 번만 전달되는지,
거절·반복 승인·등록 정보 변경이 이전 확인을 재사용하지 않는지도 검사한다.
mock 표현 정규식은 `providers/mock_speech_intent.py`에 격리된 오프라인 시험용이며 실제 실행 정책이 아니다.
자동 시험 통과로 실제 LLM의 사투리·오탈자·인용·부정 등의 해석 정확도를 판정하지 않는다.
이는 실물 로봇 주행이나 실제 마이크·LLM·스피커 품질 검증과 구분한다.
