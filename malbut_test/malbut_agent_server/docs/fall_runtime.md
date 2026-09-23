# 낙상 Cloud·ROS 실행 연결

작성일: 2026-09-19. [전체 명세](fall_detection.md), [사건 저장](fall_storage_api.md).

## 이번에 연결한 부분

- 카메라 RGB → JPEG 순환 버퍼. YOLO 후보가 없어도 받는다.
- `/homecam/person_poses` → 사람 관측 여부와 Cloud 주기 조절.
- `/homecam/fall_candidates` → 대상별 사건 생성·같은 후보 중복 방지·근거 버전 갱신.
- 실행 코어 → Ollama 직접 Cloud API → 응답 검증 → 사건 상태·SQLite 기록.
- 질문 요청·분석 결과 → Manager 연결용 로컬 이벤트.
- Agent 답변, 재확인·종결 결정 → 실행 코어의 검증된 메서드.

**실물 실행·직접 Cloud API 인증 경로·푸시 수신은 아직 검증하지 않았다.**
2026-09-19에는 같은 입력 생성·응답 검증 코드로 Mac Ollama 인증을 거쳐 실제 Cloud에
합성 영상 84개를 호출했다. [평가 결과](../../homecam_agent/docs/FALL84_RUNTIME_CLOUD_12FRAMES_20260919.md):
12장 입력의 3분류 정확도 72/84(85.7%), 정상 오탐 6/34, 응답 중앙값 2.40초.
약 5초 합성 영상의 분류 평가이며, 실제 감지 지연까지 검증한 것은 아니다.
같은 조건의 [6장 비교](../../homecam_agent/docs/FALL84_RUNTIME_CLOUD_6_VS_12_20260919.md)도
완료했다: 70/84(83.3%), 정상 오탐 7/34, 응답 중앙값 1.73초. 예시 설정은 12장을 유지한다.
설정 Service·상태·연결 확인은 `malbut_interfaces` 자료형을 사용한다.
VLM·홈캠·Manager 연결, 서버 설정 응답과 웹 저장·회신 이력 표시를 구현했다.
현재 실행 등록·실행 상태의 웹 전달과 실물 검증은 남아 있다.
질문·답변·사건 이벤트는 임시 JSON 연결이므로 담당자와 맞춰야 한다.

## 영상 입력

카메라 입력은 640×400 그대로 받는다. 크기를 늘리거나 비율을 바꾸지 않는다.
Cloud에는 요청 구간에서 고른 JPEG들을 시간순으로 한 요청에 담고, 각 프레임의 상대 시각을 적는다.
원본 영상 파일을 보내는 방식과 같다고 보지 않는다. 소리·원본 depth는 보내지 않는다.
유효한 바닥 거리만 후보의 센서 요약에서 가져오며, 없는 속도를 0으로 만들지 않는다.
현재 ROS 연결부는 실제 선속도·각속도 요약을 아직 공급하지 않는다.

- 최근 **5초·최대 12장**을 사용한다(2026-09-20 사용자 결정).
  이전 예시 설정의 10초를 5초로 변경했다. 5초 전체에서 12장을 고르면 약 0.45초 간격이며,
  12장을 한 요청에 보낸다. Cloud를 12번 호출하지 않는다.
  기존 평가는 약 5.1초 합성 영상으로 수행했다. 변경 후 정확한 5초 절단 구간의
  Cloud 재평가나 실물 성능 검증까지 완료했다는 뜻은 아니다.
- `clip_window_s`, `max_images`, `input_fps`는 각각 전송 구간, 전송 장수 상한, 버퍼 입력 빈도다.
- `max_images`만 늘려도 버퍼에 원래 프레임이 부족하면 정보가 늘어나지 않는다.
- 현재는 구간 전체에서 균등하게 고른다. 후보 전후를 더 촘촘하게 고르는 기능은 아직 없다.
- JPEG 검증·메타데이터 제거 후 전송한다. 장치/사건/추적 ID와 원본 ROS 절대 시각은
  Cloud 프롬프트에 넣지 않는다. 실제 관측된 샘플 수·간격·누락 여부를 전달한다.
- 다른 크기의 카메라 입력을 조용히 변환하지 않고 거부한다.

## Cloud 연결

`OllamaCloudFallProvider`는 `https://ollama.com/api/chat`만 사용한다.
로컬 Ollama daemon이나 다른 공급자로 자동 전환하지 않는다.
직접 API의 모델 이름은 `gemma4:31b`이며, 기존 로컬 daemon 평가의
`gemma4:31b-cloud` 표기와 다르다. 실제 사용 가능 모델은 실행 전에 확인해야 한다.
공식 문서: [Cloud](https://docs.ollama.com/cloud),
[이미지 입력](https://docs.ollama.com/capabilities/vision),
[Chat API](https://docs.ollama.com/api/chat).

- 실제 비동기 HTTP를 사용한다. 취소 시 클라이언트 연결을 닫는다.
  이미 서버가 받은 데이터·원격 추론·과금까지 취소됐다고 주장하지 않는다.
- 요청은 한 번만 시도한다. 리디렉션·환경 프록시·자동 재시도를 사용하지 않는다.
- 응답 대기는 최대 20초, 요청 본문은 최대 16 MiB, 응답은 최대 64 KiB다.
  이는 현재 구현의 보호 상한이며 공급자의 공식 용량 상한이 아니다.
- 401/403, 402, 429는 인증·결제·사용량 문제로 구분하고 해당 공급자 인스턴스를 차단한다.
  복구 확인 후 재시작하기 전에는 추가 요청을 보내지 않는다.
- Cloud 구조화 출력 기능에 의존하지 않는다. 프롬프트에 출력 형식을 지정하고 클라이언트에서 검증한다.
  [구조화 출력 문서](https://docs.ollama.com/capabilities/structured-outputs).
- JSON 키 중복·추가 필드·알 수 없는 판정·잘린 응답·잘못된 자료형을 거부한다.
  전체 JSON을 감싸는 코드 블록 하나만 제거할 수 있다. 문장 속 JSON 추출이나 내용 보정은 하지 않는다.
- 사건 확인 출력은 `assessment`와 짧은 `explanation`이다. 주기적 확인에는 아래 대상 연결
  규격의 `findings`가 추가된다. 모델에 알림·주행·의학적 판단 권한을 주지 않는다.

이 런타임의 프롬프트는 예전 평가 프롬프트와 별개다. 과거 Gemma 정확도를 새 프롬프트의
성능으로 인용하면 안 된다. 공급자의 무료 사용량이나 요금제를 코드가 보장하지도 않는다.
유료 호출을 허용한 것은 아니며, 실제 실행 전 계정 상태를 따로 확인해야 한다.

## 실행과 제어

설정 적용 Service·실행 상태 Topic의 Manifest와 입력·출력 표는
[Manager–VLM 호출 명세](fall_manager_contract.md)에 정리했다.
실행기는 새 설정 Service·상태 발행·연결 확인 수신을 사용한다.
기존 JSON 설정 토픽과 `/homecam/monitoring_enabled` Bool로 감지를 켤 수 없다.

설정 양식: `config/fall_runtime.example.json`.
2026-09-23에 비어 있던 수치를 **로봇 테스트용 시작값**으로 채웠다.
운영 기준으로 확정하거나 Jetson에서 성능을 검증한 값은 아니다.
각 값과 실물 확인 순서는 [로봇 실행 준비](fall_robot_preparation.md)에 정리했다.
설정 검사만 할 때는 예시를 그대로 사용할 수 있다. 실제 실행 전에는 등록된
`device_id`로 바꾸고 보호된 키 파일·저장 경로를 준비해야 한다.
예시 ID가 남아 있으면 `--execute`는 ROS·키·DB를 열기 전에 거부한다.
Cloud 의존성은 패키지의 `fall-cloud` extra로 설치하거나 ROS 의존성으로 설치한다.

```bash
malbut-fall-monitor --config /absolute/path/fall_runtime.json
```

기본은 설정 확인만 한다. ROS 시작·토큰 읽기·DB 생성·Cloud 요청을 하지 않는다.
실제 실행은 `--execute`를 추가한다. 실행해도 다음 조건 전에는 감지용 영상을 받지 않는다.

1. 시작 시 읽기 전용 ROS 파라미터 `manager_runtime_id`로 현재 Manager 실행 ID를 지정.
2. 이번 VLM 실행 ID에 맞는 설정을 적용. 낙상 감지·카메라 허용이 모두 ON.
3. 지정한 Manager의 새 연결 확인 메시지를 수신. 시작·재연결 때는 새 서버 확인과 설정 적용도 필요.

VLM의 입력 허용은 KVS 저장 설정과 분리했다. Cloud 전송에는 별도 동의가 필요하다.
Bringup이 실행 ID를 각 노드에 전달하고 Manager가 서버에서 확인한 설정을 적용한다.
상위 카메라·YOLO 노드의 발행 조건 분리는 별도 확인이 필요하다.
서버가 아직 `fallSettings`를 보내지 않으면 기본 Bringup 실행은 대기한다.
ID를 아는 것만으로 호출자를 인증하지 않으며, ROS 접근 권한은 별도 설정해야 한다.

### Bringup에서 함께 시작

`robot.launch.py`는 **`navigation` 모드에서만** 실행기를 시작한다.
`fall_monitor:=auto`가 기본이며,
`/etc/malbut/fall_runtime.json`이 있으면 로봇 준비 후 Manager와 함께 한 번 시작하고,
없으면 이유를 표시하고 건너뛴다. 설정 경로는 `MALBUT_FALL_CONFIG` 또는
`fall_config` 인자로 지정한다. `fall_monitor:=true`는 설정 누락도 오류로 처리하고,
`fall_monitor:=false`는 노드를 시작하지 않는다. 파일이 있는데 설정이 잘못된 경우는
`auto`에서도 시작을 거부한다.

`mapping`이나 현재 남아 있는 `sensors` 모드에서는 `fall_monitor:=true`여도
VLM 노드를 시작하지 않으며, VLM 설정 파일을 확인하지 않는다.
`sensors` 제거·기본 실행 모드 변경은 Bringup 담당자가 진행하고,
여기서는 VLM 자동 시작 범위만 제한한다. 수동 단독 실행기의 모드 검사를 추가한 것은 아니다.

설정의 `image_topic`은 Bringup의 `rgb_topic`으로 remap하므로 같은 카메라를 사용한다.
새 카메라나 별도 VLM 서버를 띄우지 않으며, 기존 직접 Cloud API 실행기를 사용한다.
Manager 설정·카메라 허용·Cloud 동의를 확인한다.
Manager는 홈캠의 유효한 Snapshot을 받아 처음 시작·설정 변경·연결 복구 때 Service를 호출한다.
같은 설정을 새로 조회한 경우에는 확인 시각만 전달하고 매초 재적용하지 않는다.
웹에서 감지·전송 동의를 따로 저장하고 재시작 없이 적용하는 기준은
[전체 명세의 웹 설정 항목](fall_detection.md#웹에서-설정하고-저장-후-적용)에 정리했다.
로봇 설정 전달·적용 회신과 서버·웹 저장·회신 이력 표시를 구현했다.
현재 실행 등록·실행 상태의 웹 전달은 남아 있어, 회신 이력을 ‘현재 감지 중’으로 표시하지 않는다.
합의한 적용·회신 목표는 온라인 로봇 기준 저장 성공 후 3초 이내이며,
6초 동안 회신이 없으면 웹에 ‘회신 없음’을 표시한다. 이 6초를 아래
`control_lease_s`나 Cloud 분석 응답 대기 시간으로 사용하지 않는다.
2026-09-23에는 Manager–VLM 양방향 확인을 1초마다 하고, 5초간 새 메시지가 없으면
끊김으로 처리하기로 정했다. Manager는 웹에 ‘낙상 감지 상태 확인 불가’를 전달하고,
VLM은 낙상 분석용 수집·새 전송을 중단하고 대기 요청을 취소한다.
저장된 설정은 유지하며 복구 후 최신 설정을 다시 확인한 뒤 재개한다.
서버에서 정상 설정 응답을 받지 못한 지 15초가 되면 새 Cloud 전송도 중단하기로 정했다.
이때 내부 연결·카메라·감지 허용이 유지되면 YOLO-Pose와 최근 영상 버퍼는 유지한다.
Cloud 대기 요청은 취소하고 진행 중 요청도 취소를 시도한다. 복구 후 최신 설정과 동의를 다시 확인한다.
VLM의 5초·15초 중단과 연결 확인 수신은 구현했다. 상태 보고 자체로 Cloud를 호출하지 않는다.
서버 확인 시각을 전달하는 Manager 발행부와 서버 응답의 `fallSettings` 확장을 구현했다.
[호출 명세 3.5~3.6절](fall_manager_contract.md)을 따르며, 실제 사용 전 `0010_fall_settings` DB 적용과 서버 배포가 필요하다.
홈캠 → Manager 설정 전달(`FallSettingsSnapshot`)과 Manager → 홈캠 적용 회신(`FallSettingsReport`),
기존 heartbeat 요청으로 서버에 결과를 보내는 형식은 같은 명세 3.7~3.8절에 작성했다.
새 ROS 타입 5종은 `malbut_interfaces`와 실기기 적용본에 만들었다.
홈캠·Manager는 해당 타입으로 설정과 실제 Service 회신을 전달한다.
기존 서버가 낙상 설정을 보내지 않으면 기존 홈캠 동작은 유지하고 낙상 감지는 켜지 않는다.
`control_lease_s`는 합의한 5초로 고정 검증한다. 기존 다른 값을 쓰는 설정 파일은 5로 바꿔야 한다.
노드 자동 실행과 실제 감지·Cloud 전송·질문 연동 완료를 구분해야 한다.
키 누락·실행 의존성 오류·실행 중 종료는 기존 Bringup 오류 종료 정책을 따른다.
단독 실행과 Bringup 실행을 동시에 사용하지 않는다.

### Manager 연결용 토픽

설정·상태 연결은 다음과 같다. Manager 발행·수신도 구현했다.

| 연결 | 방식·타입 | VLM 동작 |
|---|---|---|
| `/malbut/falls/settings/apply` | Service, ApplyFallSettings | 설정 수신·적용 결과 회신 |
| `/malbut/falls/control/heartbeat` | Topic, FallControlHeartbeat | 현재 Manager의 연결·서버 확인 시각 수신 |
| `/malbut/falls/status` | Topic, FallRuntimeStatus | 1초마다 설정·실행·실제 분석 요청 상태 발행 |

- 같은 설정 번호·내용은 `already_applied`로 회신하지만 연결 시간을 갱신하지 않는다.
- 이전 실행·낮은 설정 번호·같은 번호의 다른 내용은 거부한다.
- 카메라 OFF·감지 OFF·Manager 5초 끊김은 수집 중단과 버퍼 비우기로 처리한다.
- Cloud 동의만 철회하거나 서버 확인만 15초 만료되면 로컬 입력은 유지하고 Cloud를 막는다.
- 차단 중 요청을 모아 두지 않는다. 복구 후 자동으로 몰아서 보내지 않으며 재확인은 별도 요청한다.
- 진행 중 취소는 원격 처리·이미 보낸 영상의 회수를 보장하지 않는다. 늦게 온 결과는 정상 판단에 쓰지 않는다.
- 이 연결은 신뢰된 로컬 ROS graph 전제다. 인증 API나 SROS2 접근 제어를 대신하지 않는다.

아래 사건·Agent 연결은 기존 `std_msgs/String` JSON을 유지한다.

| 토픽 | 방향 | 용도 |
|---|---|---|
| `/malbut/falls/runtime/events` | 발행 | 사건·질문·영상 판정·실패·알림 요청 메타데이터 |
| `/malbut/falls/runtime/agent_reply` | 수신 | 해석된 Agent 답변. 실제 TTS/STT를 대신하지 않음 |
| `/malbut/falls/runtime/subject_observation` | 수신 | 외부 판단부용 관측 입력. Pose의 관측은 별도로 내부에서 생성·검증 |
| `/malbut/falls/runtime/decision` | 수신 | 담당 판단부가 내린 재확인·종결 결정 |

Agent 답변은 전체 명세의 `AgentCheckReply`와 동일하다. 실제 질문이 재생되지 않았는데
`no_response`라고 보내면 거부한다. Agent가 연결되지 않았다고 무응답을 만들어내지 않는다.

판단 메시지는 `incident_id`, `evidence_revision`, `action`을 받는다.
`action`은 `recheck`, `ask_again`, `resolve`, `unresolved` 중 하나다.
`resolve`는 `reason`, `unresolved`는 `suspicion_persists` 필드가 추가로 필요하다.
그 외 필드는 거부한다. 오래된 버전이나 근거 없는 정상 종결도 코어에서 거부한다.
승인된 [정상 종결 규칙](fall_decision_policy.md)을
적용했다. 최초 정상 결과와 유효한 답변이 모이면 같은 사건의 새 영상 확인을 예약한다.
새 영상도 정상이고 같은 대상의 새 관측이 유효할 때만 자동 종결한다.
명시적 `resolve(normal_verified)`도 이 검사를 통과해야 한다.

대상 관측의 필드는 다음과 같다. 아래 시각·ID는 형식 예시이지 운영값이 아니다.

```json
{"incident_id":"사건 ID","subject_key":"대상 ID","evidence_revision":1,"request_id":"최근 분석 요청 ID","observed_at":103.0,"state":"clear","association_verified":true}
```

- `state`: `clear`(유효한 관측에서 의심 없음), `suspected`(의심 있음), `unknown`(관측 불가/불확실).
- `association_verified`: 분석 영상과 현재 관측이 같은 대상인지 연결부가 확인했는지.
  추적 ID가 있다는 이유만으로 참을 넣지 않는다. Cloud가 생성한 ID도 쓰지 않는다.
- `observed_at`: 현재 실행과 같은 monotonic 시간 기준의 실제 관측 시각.
  Unix 시각·변환하지 않은 ROS 시각·메시지 수신 시각으로 대신하지 않는다.
- `analysis_completed` 메타데이터에 `request_id`, `sample_times`를 제공한다.
  최근 요청 ID·사건·대상·버전이 맞지 않으면 관측을 거부한다.
- 정상 관측은 최신 영상 이후의 실제 관측이어야 하며, 기존 필수 설정
  `max_person_observation_age_s` 안에서만 사용한다. 별도의 임의 자세 임계값을 만들지 않았다.
- 기존 `/homecam/person_poses`와 빈 후보 목록만으로 `clear`를 생성하지 않는다.
  새 감지기의 자세 관측·정확한 촬영 시각·연속된 대상 박스를 함께 검증한다.
  [관측 생성 기준](fall_subject_observation.md). Agent 답변은 만들어내지 않는다.
- 추가 확인 2회, Cloud 대기 20초와 기존 호출 간격 설정을 유지한다.
  실패 시도도 횟수에 포함하던 기존 동작은 그대로이며, 최종 제품 규칙 합의는 별도다.

정상 종결의 확인 근거는 SQLite `incident_events.closure_evidence`에 별도로 남긴다.
기존 DB에는 이 선택적 열을 추가하고, 웹 업로드 JSON에는 포함하지 않는다.

SQLite → 웹 업로드는 기존 `malbut-fall-upload` 워커를 별도로 실행한다.
ROS 처리 루프에서 업로드를 기다리지 않는다. 이 실행 명령에 systemd 자동 시작을 추가하지 않았다.

## 남은 제한과 검증

- Cloud 주기적 확인에서 사람별 위치를 받아 촬영 시각별 Pose 관측과 연결한다.
  명확히 연결되면 사건에 합치고, 연결 불가는 `cloud_discovery` 이벤트와
  SQLite `cloud_discoveries`에 대상 미확인으로 남긴다.
  미확인 발견의 웹 업로드·알림은 아직 없다. [규격과 제한](fall_subject_observation.md).
  현재 구현만으로 보조 경로의 실제 질문·알림까지 완성된 것은 아니다.
- 사건 영상에 유효한 대상 박스가 모두 연결되면 해당 사람을 확인하도록 요청한다.
  연결할 수 없는 다인 장면은 임의로 한 사람을 고르지 말고 `unobservable`로 답하도록 했다.
  모델 준수·박스 정확도는 실영상 검증이 필요하다.
- Manager 측 이벤트 수신·질문 재생·답변 연결·재확인 판단은 담당자와 연결해야 한다.
  ROS 발행 성공은 Agent 처리 완료가 아니다.
- 현재 테스트는 HTTP 응답/취소를 흉내 낸 전송기, 실제 코어, ROS 메시지/콜백을 쓴다.
  ROS 노드 생성은 시험용 객체로 대체하며 DDS graph·실물 카메라·Cloud 추론은 실행하지 않는다.
- 6장/12장 합성 영상 Cloud 비교는 위 기록을 참고한다. 직접 API 인증 경로와
  Jetson 동시 부하·시간 측정, 실제 Agent 답변·대상 관측을 합친 E2E는 남아 있다.

검증 기록(2026-09-19): Agent 패키지 1026개 통과·12개 건너뜀. 별도 ROS 통신 테스트 두 파일은
기존과 같이 제외했다. 전체 테스트의 로컬 HTTP 서버는 소켓 제한 해제 후 재검증했다.
변경 Python 파일 lint, 패키지 메타데이터 검사, `git diff --check`도 통과했다.

같은 날 정상 자동 종결 규칙 추가 후 재검증: **1068개 통과·12개 건너뜀**.
동일한 ROS 통신 테스트 두 파일은 제외했고 변경 파일 lint·`git diff --check`도 통과했다.
정상 확인 두 건·유효한 대상 관측·답변의 도착 순서와 실패 조건,
SQLite 기존 DB 변경·종결 근거 보존을 포함한다. 실제 Cloud 호출이나 알림 발송은 하지 않았다.
