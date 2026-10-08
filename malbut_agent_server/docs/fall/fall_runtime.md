# 낙상 Cloud·ROS 실행 연결

작성일: 2026-09-19. 실행 구조·계약 갱신: 2026-09-28.

## 2026-09-28 실행부 후속 연결

검사 결과와 미완료 범위는 [후속 검증 기록](fall_runtime_followup_validation_20260928.md)에 정리했다.

Cloud 주기 확인은 평가한 `native_boxes_v4` 지시문을 사용한다.
Cloud의 `box_2d=[top,left,bottom,right]`(0~1000 정수)를 검사한 뒤,
내부 `CloudPersonRegion.box=(left,top,right,bottom)`(0~1)으로 바꾼다.
내부 ROS 이벤트·대상 박스·DB 계약은 바꾸지 않는다. 사건 확인 요청의 지시문도 그대로다.
단위 추측, 잘못된 좌표 잘라내기, 순서 뒤집기, JSON 일부 추출은 하지 않는다.
위치만 잘못된 의심 응답은 기존처럼 `localization_failed`로 남기고 사람을 지정하지 않는다.
분류를 정상으로 바꾸거나 질문 시작을 막는 근거로 사용하지 않는다.
평가용 기본 지시문과 이전 결과 재생용 함수의 기본값은 유지했다.

2026-10-07 실기 오탐(쪼그려 앉기를 낙상, 바닥에 앉은 사람을 23분 동안 반복 의심, 가방을 쓰러진 사람)
후, 운영 provider만 모든 요청(주기 확인·사건 확인)의 지시문 끝에 `LIVE_RULES`를 덧붙인다.
조절하며 앉거나 눕기, 이미 바로 앉아서 스스로 움직이는 모습은 정상으로 보고, 갑자기 털썩·다리 풀림·
뒤나 옆으로 넘어짐은 앉은 자세로 끝나도 낙상으로 본다. 가방·옷·이불 등은 사람이 아니라고 적는다.
녹화 장면과 합성 영상으로 Cloud 11회만 확인했다(쪼그려 앉기 → 정상, 카메라 바로 앞의 앉은 사람 →
여전히 의심, 합성 낙상 2개 → 낙상 유지, 가방 오탐 장면 7개 중 6개 → 정상·15:10 1개 의심 유지).
평가 프로필과 `build_payload` 기본값에는 들어가지 않는다.
같은 시험에서 "AI 박스를 확대해 사람인지 다시 묻기"도 확인했으나, 물건 4곳 중 1곳만 걸렀고 이불에 가려
쓰러진 합성 사람 1명을 물건으로 걸러 운영에 넣지 않았다.

사건 영상의 사람 표시는 Pose 트랙의 `confidenceLevel`(detector 박스 점수 0.45 기준)을 받아,
이 사건의 대상이 아닌 약한 박스를 빼고 그린다. 옷걸이·침대에 그려지던 약한 박스를 없애기 위한 표시 규칙이며,
주기 확인 간격·사람 연결·낙상 판단에는 쓰지 않는다. 이전 detector처럼 값이 없으면 그대로 그린다.

낙상 깊이는 런치 인자 `fall_depth_aligned_to_rgb` 하나로 켠다(기본 `false`). 로봇의 `depth0`가 RGB에
정렬됐는지 아직 실측하지 않았기 때문이다(`launch_support.py` 주석, `depth_costmap/README.md`). 켜면 함께 바뀐다.
- `malbut_fall_pose`: 깊이·카메라 정보·`camera_height_m`(`fall_camera_height_m`, 기본 0.12 m: URDF 0.0919 + base 0.028)·
  `camera_pitch_rad`를 받아 몸통이 바닥에서 0.30 m 넘게 떨어진 누운 자세(침대·소파)를 낮은 자세로 보지 않는다.
  빠르게 털썩 눕는 동작(급격한 자세 변화)은 깊이와 무관하게 후보가 될 수 있다.
- 낙상 런타임: `depth_topic`, `camera_info_topic`(`fall_depth_camera_info_topic`)을 받아 Cloud 박스를 지도 좌표로 바꾼다.
  꺼져 있으면 대상 미확인 사건은 화면 위치로 같은 자리를 판단한다.
실기 확인 전에는 켜지 않는다: 정렬, 카메라 높이, 같은 물건을 두 위치에서 찍은 지도 좌표 차이를 먼저 확인한다.

2026-10-08 다가가 확인하기(런치 `fall_approach`, 기본 꺼짐, 깊이 스위치 필요):
- 질문할 때 사람이 확실하지 않으면(대상 미확인 사건이거나, 대상 Pose가 약하거나 관절로 잴 수 없음)
  지도 좌표를 `approach_target`으로 붙이고 `approach_started`(reason `patrol_stopped`/`follow_stopped`/null)를 남긴다.
  확실한 Pose 사람이거나 지도 좌표가 없으면 지금처럼 바로 묻는다.
- 코디네이터의 `approach_result`를 `approach_completed`(reason = outcome)로 남긴다. `arrived`면 3초 동안
  새 Pose에서 확실한 사람(0.45 이상 + 관절)이 목표 1 m 안(지도 좌표가 없으면 화면 가운데 절반)에 보이는지 본다.
  없으면 Cloud에 가까이서 찍은 RGB 3장으로 "사람 몸인가"만 한 번 묻는다(`check_person`, 전용 지시문).
- `person_check_completed`(reason `person`/`not_a_person`, Cloud 설명은 analysis purpose `person_check`).
  사람·애매·실패·Cloud 차단은 `person`(묻기). `not_a_person`이면 사건을 `incident_resolved`(reason `not_a_person`)로
  닫는다. 그 사건의 분석이 진행 중이면 닫지 않고 묻는다.
- `return_result`는 `approach_returned`(reason `returned`/`return_failed`)로 남긴다.
- 웹은 이 이벤트들과 `not_a_person` 종료를 받아야 하므로 웹을 먼저 배포한다.

실시간 영상 추적은 **선택 기능이며 기본 OFF**다. `tracking: null` 또는 필드 생략이면
기존 동작을 유지한다. GPU·모델을 자동 설치하거나 CPU로 대신 실행하지 않는다.
켜려면 실제 런타임 JSON의 `tracking`을 다음 객체로 바꾼다. 아래 경로는 예시다.

```json
{
  "python_executable": "/opt/malbut-sam/bin/python",
  "source_path": "/opt/malbut-sam/sam2",
  "checkpoint_path": "/opt/malbut-sam/sam2.1_hiera_tiny.pt",
  "python_paths": []
}
```

- Python 환경에는 CUDA/BF16을 지원하는 PyTorch, SAM2 실행 의존성이 필요하다.
  `python_paths`는 필요한 경우에만 지정하는 신뢰된 로컬 의존성 경로다.
- SAM2 소스 revision: `2b90b9f5ceec907a1c18123530e92e794ad901a4`.
  해당 소스의 `sam2` 변경 여부를 검사한다. 사설 영상 상태 구조를 쓰므로 버전 변경 시 재검증한다.
- SAM2.1 tiny 체크포인트 SHA-256:
  `7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69`.
- 추적 모델은 Cloud 발견의 시드 RGB가 버퍼에 남아 있을 때 별도 프로세스에서 시작한다.
  모델 시작 대기 최대 30초, 프레임 응답 대기 최대 2초다. 설정 확인·질문·ROS 처리는 기다리지 않는다.
- 한 번에 발견 하나, 세션당 최대 64프레임이다. 이미 받은 RGB를 순서대로 한 장씩 처리한다.
  시드/중간 영상이 사라지면 건너뛰어 이어 붙이지 않는다. 0.5초 초과 촬영 간격,
  마스크 소실, 오래된 입력, 설정 변경 시 해당 세션을 중단한다.
- 추적 과정의 JPEG·텐서는 메모리에만 둔다. 임시 이미지 파일을 만들지 않으며 자식 프로세스에
  Cloud 키·사람 ID·질문 답변을 전달하지 않는다. 종료 시 프로세스를 종료하고 회수한다.
- 16 MiB는 기존 JPEG 버퍼 상한이다. 모델/텐서/추론 상태 RAM·VRAM까지 16 MiB라는 뜻이 아니다.
  1024 입력의 64장 float32 텐서만 약 768 MiB이며 모델과 추론 상태 메모리가 추가된다.
- 추적 실패·혼잡은 `discovery_tracking_...` 로그로 표시한다. 연결하지 못해도 원래 Cloud
  발견·장면 질문은 유지한다. 이 상태가 `FallRuntimeStatus`의 새 필드로 전달되는 것은 아니다.
- [사건 연결 조건](fall_deferred_association.md)은 그대로다. 박스가 비슷하다는 이유만으로
  사람을 확정하거나, 장면 답변을 사람 사건에 복사하거나 정상 종결하지 않는다.

### 실제 실행 준비

모델은 두 분석 역할 모두 `gemma4:31b`를 유지한다. 역할별 다른 공급자 선택은 이번 변경에 없다.
Ollama 직접 Cloud API 키는 로봇의 `/etc/malbut/ollama-cloud.key`에 한 줄로 저장한다.
Git·Jira·설정 JSON에는 키 값을 넣지 않고 JSON의 `cloud_key_file`에는 파일 경로만 쓴다.
권장 권한은 실행 계정 소유 `0600`이다. 키를 명령 인자나 셸 기록에 넣지 않는다.
Google Gemini 키로 대체하지 않는다. 키가 없으면 실제 실행이 실패하며,
`--config`만 실행하는 구성 검사는 키를 읽지 않는다.

#### 앱에서 등록한 키 받기 (`key_sync`, 선택)

소유자가 앱에서 등록한 로봇별 키를 낙상 노드가 서버에서 직접 받는다. 설정 JSON에 추가한다.

```json
"key_sync": {"base_url": "https://malbut.hyenje29.click", "allow_hosts": ["malbut.hyenje29.click"],
             "token_file": "/etc/malbut-homecam.token", "interval_s": 60}
```

- 주기(`interval_s`, 30~3600초, 기본 60)마다 `POST /api/device/v1/fall-cloud-key`로 가진 키 버전과
  모델 이름(`model`)을 보낸다. 서버 AI 검토는 이 모델을 쓴다.
- 서버에 키가 한 번도 등록되지 않았으면 로봇의 `cloud_key_file`을 그대로 쓴다.
- 새 키가 오면 `cloud_key_file`을 같은 디렉터리의 임시 파일로 쓴 뒤 바꿔 넣고(0600), 버전을
  `cloud_key_file.version`에 남긴 다음 재시작 없이 새 키로 바꾼다. 앱에서 지우면 키 파일도 지우고 Cloud 확인을 멈춘다.
- 서버 연결이 실패하면 마지막 키를 계속 쓴다. 키 파일 디렉터리는 실행 계정이 쓸 수 있어야 한다
  (예: `/var/lib/malbut-falls/ollama-cloud.key`). `/etc/malbut`처럼 root만 쓸 수 있으면 받은 키를 저장하지 못한다.
- `key_sync`가 있으면 시작할 때 키 파일이 없어도 실행된다. 첫 동기화 전까지 Cloud 확인은 `cloud_auth_required`로 멈춘다.
- 키 값은 로그·상태·저널에 남기지 않는다. 기기 토큰은 업로드 워커와 같은 파일을 쓴다.

이번 PC의 기본 런타임 설정/키/Pose 경로가 준비됐다는 뜻은 아니다.
운영 서버의 설정 전달, Jetson의 모델 실행 환경, 동시 주행·음성 부하, 웹 상태/알림은
실기기 단계에서 확인해야 한다. 이번 변경은 실제 영상 업로드나 추가 과금을 실행하지 않았다.

[전체 명세](fall_detection.md), [사건 저장](fall_storage_api.md).

> **SWM25-164:** 낙상 설정 전달·heartbeat·확인 결과 검증·Agent 연결 책임은
> 시스템 관리자에서 `malbut_fall_coordinator`로 이동했다. ROS/웹 인터페이스는 유지한다.
> VLM 파라미터·메시지의 `manager_runtime_id`와 홈캠의 `fall_manager_runtime_id`는
> 이름을 유지하지만 낙상 코디네이터의 실행 ID를 담는다. 확인 대화만 일반 관리자의
> `fall_confirmation` 미션으로 실행한다([현재 실행 구조](../../../malbut_fall_coordinator/README.md)).

## 이번에 연결한 부분

- 카메라 RGB → JPEG 순환 버퍼. YOLO 후보가 없어도 받는다.
- `/homecam/person_poses` → 사람 관측 여부와 Cloud 주기 조절.
- `/homecam/fall_candidates` → 대상별 사건 생성·같은 후보 중복 방지·근거 버전 갱신.
- 실행 코어 → Ollama 직접 Cloud API → 응답 검증 → 사건 상태·SQLite 기록.
- 질문 요청·분석 결과 → 낙상 코디네이터 연결용 로컬 이벤트.
- Agent 답변, 재확인·종결 결정 → 실행 코어의 검증된 메서드.

**실물 실행·직접 Cloud API 인증 경로·푸시 수신은 아직 검증하지 않았다.**
2026-09-19에는 같은 입력 생성·응답 검증 코드로 Mac Ollama 인증을 거쳐 실제 Cloud에
합성 영상 84개를 호출했다. [평가 결과](../../../homecam_agent/docs/FALL84_RUNTIME_CLOUD_12FRAMES_20260919.md):
12장 입력의 3분류 정확도 72/84(85.7%), 정상 오탐 6/34, 응답 중앙값 2.40초.
약 5초 합성 영상의 분류 평가이며, 실제 감지 지연까지 검증한 것은 아니다.
같은 조건의 [6장 비교](../../../homecam_agent/docs/FALL84_RUNTIME_CLOUD_6_VS_12_20260919.md)도
완료했다: 70/84(83.3%), 정상 오탐 7/34, 응답 중앙값 1.73초. 예시 설정은 12장을 유지한다.
설정 Service·상태·연결 확인은 `malbut_interfaces` 자료형을 사용한다.
VLM·홈캠·낙상 코디네이터 연결, 서버 설정 응답과 웹 저장·회신 이력 표시를 구현했다.
현재 실행 등록·실행 상태의 웹 전달과 실물 검증은 남아 있다.
사건 이벤트는 JSON 연결을 유지한다. 2026-09-25에는 Manager가 이벤트를 받아
Agent의 확인 Action을 호출하고 최종 판단을 돌려주는 경로를 추가했다.
현재는 낙상 코디네이터가 이벤트를 받아 Manager에 확인 미션을 요청하는 구조다.
[이상 상황 확인 구현](agent_fall_implementation.md)에 계약과 검증 범위를 정리했다.

현재 설정 전달: `홈캠 → 낙상 코디네이터 → VLM 설정 Service → 코디네이터 → 홈캠 회신`.
현재 질문 전달: `VLM 이벤트 → 낙상 코디네이터 → Manager ExecuteMission → Agent ConfirmSituation`.
최종 질문 결과는 Manager·코디네이터를 거쳐 원래 VLM 사건에 반영한다.
영상 분석·사건 병합·저장은 VLM, 설정 전달·질문 ID/버전 검증은 코디네이터,
미션 우선순위·자원 조정은 Manager가 담당한다.

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
[낙상 설정·VLM 호출 명세](fall_manager_contract.md)에 정리했다.
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

1. 시작 시 읽기 전용 ROS 파라미터 `manager_runtime_id`로 현재 낙상 코디네이터 실행 ID를 지정.
2. 이번 VLM 실행 ID에 맞는 설정을 적용. 낙상 감지·카메라 허용이 모두 ON.
3. 지정한 코디네이터의 새 연결 확인 메시지를 수신. 시작·재연결 때는 새 서버 확인과 설정 적용도 필요.

VLM의 입력 허용은 KVS 저장 설정과 분리했다. Cloud 전송에는 별도 동의가 필요하다.
Bringup이 실행 ID를 각 노드에 전달하고 코디네이터가 서버에서 확인한 설정을 VLM에 요청한다.
상위 카메라·YOLO 노드의 발행 조건 분리는 별도 확인이 필요하다.
서버가 아직 `fallSettings`를 보내지 않으면 기본 Bringup 실행은 대기한다.
ID를 아는 것만으로 호출자를 인증하지 않으며, ROS 접근 권한은 별도 설정해야 한다.

### Bringup에서 함께 시작

`robot.launch.py`의 통합 navigation 실행에서 VLM을 시작한다.
`fall_monitor:=auto`가 기본이며,
`/etc/malbut/fall_runtime.json`이 있으면 로봇 준비 후 코디네이터·VLM·낙상용 Pose를 각각 한 번 시작하고,
없으면 이유를 표시하고 건너뛴다. 설정 경로는 `MALBUT_FALL_CONFIG` 또는
`fall_config` 인자로 지정한다. `fall_monitor:=true`는 설정 누락도 오류로 처리하고,
`fall_monitor:=false`는 노드를 시작하지 않는다. 파일이 있는데 설정이 잘못된 경우는
`auto`에서도 시작을 거부한다.

최신 Bringup은 별도 `mode` 인자를 없앴다. Manager는 위치 추정을 위해 먼저 시작하고,
낙상 코디네이터·VLM·Pose는 준비 완료 뒤 시작한다. Manager를 두 번 띄우지 않는다.
별도 `mapping_backend.launch.py`는 VLM을 시작하지 않는다.
실제 수집·전송에는 여전히 최신 설정·연결 확인·동의가 필요하다.

설정의 `image_topic`은 Bringup의 `rgb_topic`으로 remap하므로 같은 카메라를 사용한다.
새 카메라나 별도 VLM 서버를 띄우지 않으며, 기존 직접 Cloud API 실행기를 사용한다.
코디네이터를 통해 받은 낙상 설정·카메라 허용·Cloud 동의를 확인한다.
코디네이터는 홈캠의 유효한 Snapshot을 받아 처음 시작·설정 변경·연결 복구 때 Service를 호출한다.
같은 설정을 새로 조회한 경우에는 확인 시각만 전달하고 매초 재적용하지 않는다.
웹에서 감지·전송 동의를 따로 저장하고 재시작 없이 적용하는 기준은
[전체 명세의 웹 설정 항목](fall_detection.md#웹에서-설정하고-저장-후-적용)에 정리했다.
로봇 설정 전달·적용 회신과 서버·웹 저장·회신 이력 표시를 구현했다.
현재 실행 등록·실행 상태의 웹 전달은 남아 있어, 회신 이력을 ‘현재 감지 중’으로 표시하지 않는다.
합의한 적용·회신 목표는 온라인 로봇 기준 저장 성공 후 3초 이내이며,
6초 동안 회신이 없으면 웹에 ‘회신 없음’을 표시한다. 이 6초를 아래
`control_lease_s`나 Cloud 분석 응답 대기 시간으로 사용하지 않는다.
2026-09-23에 정한 양방향 1초 확인·5초 연결 만료 기준은 현재 낙상 코디네이터–VLM에 적용한다.
코디네이터는 VLM의 새 상태를 5초간 받지 못하면 연결 확인 불가로 취급한다.
VLM은 코디네이터의 새 heartbeat를 5초간 받지 못하면 낙상 분석용 수집·새 전송을
중단하고 대기 요청을 취소한다.
웹에 현재 연결 상태를 표시하는 연동은 아직 남아 있다.
저장된 설정은 유지하며 복구 후 최신 설정을 다시 확인한 뒤 재개한다.
서버에서 정상 설정 응답을 받지 못한 지 15초가 되면 새 Cloud 전송도 중단하기로 정했다.
이때 내부 연결·카메라·감지 허용이 유지되면 YOLO-Pose와 최근 영상 버퍼는 유지한다.
Cloud 대기 요청은 취소하고 진행 중 요청도 취소를 시도한다. 복구 후 최신 설정과 동의를 다시 확인한다.
VLM의 5초·15초 중단과 연결 확인 수신은 구현했다. 상태 보고 자체로 Cloud를 호출하지 않는다.
서버 확인 시각을 전달하는 코디네이터 발행부와 서버 응답의 `fallSettings` 확장을 구현했다.
[호출 명세 3.5~3.6절](fall_manager_contract.md)을 따르며, 실제 사용 전 `0011_fall_settings` DB 적용과 서버 배포가 필요하다.
홈캠 → 코디네이터 설정 전달(`FallSettingsSnapshot`)과 코디네이터 → 홈캠 적용 회신(`FallSettingsReport`),
기존 heartbeat 요청으로 서버에 결과를 보내는 형식은 같은 명세 3.7~3.8절에 작성했다.
새 ROS 타입 5종은 `malbut_interfaces`와 실기기 적용본에 만들었다.
홈캠·코디네이터는 해당 타입으로 설정과 실제 Service 회신을 전달한다.
기존 서버가 낙상 설정을 보내지 않으면 기존 홈캠 동작은 유지하고 낙상 감지는 켜지 않는다.
`control_lease_s`는 합의한 5초로 고정 검증한다. 기존 다른 값을 쓰는 설정 파일은 5로 바꿔야 한다.
노드 자동 실행과 실제 감지·Cloud 전송·질문 연동 완료를 구분해야 한다.
키 누락·실행 의존성 오류·실행 중 종료는 기존 Bringup 오류 종료 정책을 따른다.
단독 실행과 Bringup 실행을 동시에 사용하지 않는다.

### 낙상 코디네이터 연결용 토픽

설정·상태 연결은 다음과 같다. 코디네이터 발행·수신도 구현했다.
일반 Manager가 아래 낙상 전용 토픽·Service를 직접 처리하는 구조는 아니다.

| 연결 | 방식·타입 | VLM 동작 |
|---|---|---|
| `/malbut/falls/settings/apply` | Service, ApplyFallSettings | 설정 수신·적용 결과 회신 |
| `/malbut/falls/control/heartbeat` | Topic, FallControlHeartbeat | 현재 코디네이터의 연결·서버 확인 시각 수신 |
| `/malbut/falls/status` | Topic, FallRuntimeStatus | 1초마다 설정·실행·실제 분석 요청 상태 발행 |

- 같은 설정 번호·내용은 `already_applied`로 회신하지만 연결 시간을 갱신하지 않는다.
- 이전 실행·낮은 설정 번호·같은 번호의 다른 내용은 거부한다.
- 카메라 OFF·감지 OFF·코디네이터 heartbeat 5초 끊김은 수집 중단과 버퍼 비우기로 처리한다.
- Cloud 동의만 철회하거나 서버 확인만 15초 만료되면 로컬 입력은 유지하고 Cloud를 막는다.
- 차단 중 요청을 모아 두지 않는다. 복구 후 자동으로 몰아서 보내지 않으며 재확인은 별도 요청한다.
- 진행 중 취소는 원격 처리·이미 보낸 영상의 회수를 보장하지 않는다. 늦게 온 결과는 정상 판단에 쓰지 않는다.
- 이 연결은 신뢰된 로컬 ROS graph 전제다. 인증 API나 SROS2 접근 제어를 대신하지 않는다.

아래 런타임 토픽은 기존 `std_msgs/String` JSON을 유지한다.
새 대화 Agent는 이 토픽을 직접 쓰지 않는다. 코디네이터가 Manager의
`/malbut/mission/execute`에 `fall_confirmation`을 요청하면,
Manager가 Agent의 `/malbut/agent/confirm_situation` Action을 실행한다.
Manager는 `URGENT`, `[BASE, SPEAKER]` 자원 규칙에 따라 충돌 미션 종료를 확인한 뒤
질문 미션을 실행한다. 코디네이터가 Agent에 직접 질문 Goal을 보내지는 않는다.

| 토픽 | 방향 | 용도 |
|---|---|---|
| `/malbut/falls/runtime/events` | 발행 | 사건·질문·영상 판정·실패·알림 요청 메타데이터 |
| `/malbut/falls/runtime/agent_reply` | 수신 | 기존 개별 답변 계약용 입력. 새 확인 Action 경로에서는 사용하지 않음 |
| `/malbut/falls/runtime/subject_observation` | 수신 | 외부 판단부용 관측 입력. Pose의 관측은 별도로 내부에서 생성·검증 |
| `/malbut/falls/runtime/decision` | 수신 | 코디네이터가 전달하는 최종 확인 결과·정상 종결 요청 및 기존 호출자의 재확인·종결 결정 |

확인 대화의 ROS Action·음성 타입은 [공통 인터페이스 색인](../../../malbut_interfaces/README.md)을 따른다.
아래 JSON은 기존 런타임 연결의 payload 계약이며 새 `.msg` 정의가 아니다.
발행자는 VLM 런타임의 `event_metadata`와 낙상 코디네이터의 `FallConfirmationCoordinator`,
수신 검증은 각각 `FallConfirmationCoordinator.receive`와 `apply_decision`이 맡는다.

`events`의 `question_requested`에서 낙상 코디네이터가 확인하는 필드는 다음과 같다.
사건·분석·종결 이벤트도 같은 토픽으로 받아 최신 버전과 종료 상태를 갱신한다.

| 필드 | JSON 타입 | 의미·검증 |
|---|---|---|
| `kind` | string | 확인 요청은 `question_requested` |
| `boot_id`, `incident_id`, `question_id` | string | 공백이 아닌 최대 200자의 ID. 이전 런타임 부팅·종결 사건은 다시 실행하지 않음. 진행 중인 질문은 발급 당시 근거 버전으로 재전송 가능 |
| `confirmation_scope` | string | `subject` 또는 `scene`. 생략하면 기존 사람별 계약인 `subject`로 처리 |
| `subject_key` | string 또는 null | `subject`이면 공백이 아닌 최대 200자의 ID. `scene`이면 필드를 생략하지 않고 명시적으로 null 전달 |
| `runtime_id` | string | 코디네이터에 대상 VLM 실행 ID가 설정돼 있으면 일치해야 함 |
| `evidence_revision` | integer | 1 이상. boolean은 허용하지 않음 |
| `video_assessment` | string | 사람별 질문은 `observed_fall`, `suspected_fall`, `unobservable`, `normal_activity`. 장면 질문은 앞의 낙상·의심 두 값만 허용 |
| `reason` | string | `normal_activity`의 확인 요청은 `prior_fall_observed`여야 함 |

이벤트는 메타데이터를 더 포함할 수 있지만 코디네이터가 영상·자유 형식 모델 설명을
Agent 요청에 복사하지 않는다. 사건·대상·버전의 연결 정보는 코디네이터가 유지하고,
구조화된 영상 판정을 짧은 상황 요약으로 바꾼다. Manager 미션에는
`request_id`, `situation_type`, `summary`를 전달하고, Manager가 이를
`ConfirmSituation` 요청으로 실행한다.

새 확인 경로에서 사용하는 `decision`의 공통 필수 필드는 `action: string`, `boot_id: string`,
`incident_id: string`, `evidence_revision: integer`다. 다음 표의 필드만 추가로 허용하며
누락·추가 필드, 현재 부팅 ID 불일치, 1 미만 또는 boolean인 버전을 거절한다.

| `action` | 추가 필수 필드 | 의미 |
|---|---|---|
| `confirmation_result` | `question_id: string`, `subject_key: string 또는 null`, `situation_assessment: string`, `help_needed: boolean` | Action 성공 결과. 대상은 원래 질문의 ID 또는 null을 유지. 상황 값은 `ConfirmSituation.Result`의 상수와 같고 도움 여부는 독립된 값 |
| `confirmation_failed` | `question_id: string` | 음성·모델·통신 실패 또는 취소. 사용자 무응답이나 도움 필요로 바꾸지 않음 |
| `dismiss_normal` | 없음 | 대상이 확인된 사건에서 이전 낙상 관측이나 확인 대기가 없는 최신 정상 영상의 종결. 대상 미확인 장면 사건에는 적용하지 않음 |

사건·질문·대상·근거 버전은 발급한 질문의 고정된 정보와 다시 대조한다. 진행 중
새 근거가 들어와도 원래 질문의 결과를 수락하고 당시 근거에 기록한다. 그 결과로
최신 근거를 해결 처리하지 않으며, 종료 후 최신 분석이 준비되면 필요한 질문 하나를
발급한다. 중복 결과는 다시 적용하지 않고 다음 질문의 결과로도 사용하지 않는다.
결과 메시지에는 `confirmation_scope`를 추가하지 않는다. 원래 사건·질문과
`subject_key`의 일치 여부로 장면/사람별 결과를 다시 검증한다.

2026-09-27부터 대상 미연결 Cloud 의심도 확인을 시작한다. 사건의 `subject_key=null`,
이벤트의 `confirmation_scope=scene`으로 구분하고, 코디네이터는 특정인을 지목하지 않는
일반 확인 질문을 Manager를 통해 전달한다. 기존 사람별 질문은 `confirmation_scope=subject`이다.
대상 미확인 질문의 `help_needed=false`는 답변을 기록하되 사건을 `recheck_required`로
남긴다. 대답한 사람이 Cloud가 본 사람인지 모르므로 정상 종결에 쓰지 않는다.
도움 필요 결과는 해당 장면 사건에만 적용한다. 다른 사람의 사건은 변경하지 않는다.
장면 정상 판정도 미확인 사건을 닫지 않는다. 대상 없는 `recheck` 요청은 거부하고
주기적 장면 확인은 유지한다. [동작·검증·제한](fall_unidentified_verification.md).

Agent가 일시적으로 요청을 거절하면 Manager 경로로 재시도한다. 이미 수락한 대화의
실패·취소를 사용자 무응답이나 도움 요청으로 바꾸지는 않는다. VLM 근거가 있는
질문은 발급부터 결과 또는 실패까지 ID·근거 버전·영상 판정을 유지한다. 새 근거는
별도로 갱신하며 진행 중인 대화를 취소하거나 다시 시작하지 않는다. 다른 사건의
질문은 순서대로 대기한다. 같은 근거의 실패를 자동 반복 질문으로 바꾸지 않는다.
코디네이터가 재시작했을 때 Manager에
기존 확인 미션이 남아 있으면 종료 상태를 확인한 뒤 요청한다.

이하 개별 답변·재확인 계약은 기존 호출자용으로 유지한다.

Agent 답변은 전체 명세의 `AgentCheckReply`와 동일하다. 실제 질문이 재생되지 않았는데
`no_response`라고 보내면 거부한다. Agent가 연결되지 않았다고 무응답을 만들어내지 않는다.

기존 호출자의 재확인·종결 결정 메시지는 `incident_id`, `evidence_revision`, `action`을 받는다.
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

SQLite → 웹 업로드는 별도 프로세스인 `malbut-fall-upload`가 처리한다.
Bringup의 낙상 모듈은 웹 주소·기기 토큰 설정이 있으면 같은 저널·기기 ID로 워커를 자동 시작한다.
ROS 처리 루프에서 업로드를 기다리지 않는다. 직접 `malbut-fall-monitor`만 실행할 때는
워커도 별도로 실행해야 한다. 설정 및 밀린 알림 주의 사항은 Bringup README의
「사건 기록·클립 자동 업로드」를 따른다.

## 남은 제한과 검증

- Cloud 주기적 확인에서 사람별 위치를 받아 촬영 시각별 Pose 관측과 연결한다.
  명확히 연결되면 사건에 합친다. 연결 불가 발견은 별도로 기록하면서 대상 미확인
  장면 사건을 열고 코디네이터 → Manager → Agent 일반 확인으로 전달한다. 반복 발견은 열린 장면
  확인 하나에 보관하지만 같은 사람으로 병합했다는 뜻은 아니다.
  이후 연속 영상 추적과 같은 시각의 Pose 관측으로 연결이 확인되면, 개별 발견의 영상 근거를
  기존 또는 새 사람별 사건에 반영하는 코어를 구현했다. 장면 사건 자체나 장면에서 받은
  답변을 사람 사건으로 통째로 옮기지는 않는다. 기존 장면 질문과 사람별 질문이 함께
  남을 수 있으며 자동 정리는 구현하지 않았다.
  ROS 카메라의 연속 추적 어댑터 연결과 실환경 검증은 남아 있다.
  [후속 연결 구현·제한](fall_deferred_association.md), [관측 규격](fall_subject_observation.md).
- 사건 영상에 유효한 대상 박스가 모두 연결되면 해당 사람을 확인하도록 요청한다.
  연결할 수 없는 다인 장면은 임의로 한 사람을 고르지 말고 `unobservable`로 답하도록 했다.
  모델 준수·박스 정확도는 실영상 검증이 필요하다.
- 코디네이터 → Manager → Agent 확인 Action 연결을 구현했다.
  2026-09-28 병합 검증에서는 실제 DDS와 테스트용 Agent Action 서버로 왕복 확인했다.
  실제 음성·모델·카메라를 합친 확인은 남아 있다.
- 아래 2026-09-19 검증은 HTTP 응답/취소를 흉내 낸 전송기, 실제 코어, ROS 메시지/콜백을 쓴다.
  ROS 노드 생성은 시험용 객체로 대체하며 DDS graph·실물 카메라·Cloud 추론은 실행하지 않는다.
- 6장/12장 합성 영상 Cloud 비교는 위 기록을 참고한다. 직접 API 인증 경로와
  Jetson 동시 부하·시간 측정, 실제 Agent 답변·대상 관측을 합친 E2E는 남아 있다.

2026-09-28 최신 main과 로컬 개선의 병합 작업본 검증:

- 질문 통합 ROS 검사 14개를 두 번 실행해 모두 통과했다(서로 다른 28개 사례가 아님).
  VLM 단독 발견, 장면 답변 6조합, 중복 방지, 이전 질문 취소, Manager/Agent 연결 상실,
  일시 거절 후 재시도와 충돌 미션 종료 후 질문 실행을 포함한다.
- 코어·코디네이터·평가 도구 회귀 검사 618개 통과, 5개 제외.
  제외한 항목은 별도 Python 환경의 rclpy 의존 검사 1개와 opt-in 후속 연결 ROS 검사 4개다.
- ROS 검사는 localhost 전용 별도 domain에서 현재 인터페이스를 새로 빌드해 수행했다.
  Cloud 응답·Agent 답변·주행 Action은 테스트 값/서버다. 실제 Cloud 호출·카메라·음성·주행은 없다.
  주행 검사는 미션 취소 완료 순서를 확인한 것이며 실제 바퀴 정지를 검증한 것은 아니다.
- 설정 전달·연결 중단 등을 포함한 PC ROS 통합·런타임 검사 55개를 추가 실행해 모두 통과했다.
  Agent·코디네이터 Python 전체 2,456개와 CI 낙상 검사 1,900개도 통과했다.
  검사 간 중복이 있어 통과 수를 합산하지 않는다.
  이번 병합으로 기존 사건 병합 정확도가 새로 개선됐다는 뜻은 아니다.

검증 기록(2026-09-19): Agent 패키지 1026개 통과·12개 건너뜀. 별도 ROS 통신 테스트 두 파일은
기존과 같이 제외했다. 전체 테스트의 로컬 HTTP 서버는 소켓 제한 해제 후 재검증했다.
변경 Python 파일 lint, 패키지 메타데이터 검사, `git diff --check`도 통과했다.

같은 날 정상 자동 종결 규칙 추가 후 재검증: **1068개 통과·12개 건너뜀**.
동일한 ROS 통신 테스트 두 파일은 제외했고 변경 파일 lint·`git diff --check`도 통과했다.
정상 확인 두 건·유효한 대상 관측·답변의 도착 순서와 실패 조건,
SQLite 기존 DB 변경·종결 근거 보존을 포함한다. 실제 Cloud 호출이나 알림 발송은 하지 않았다.
