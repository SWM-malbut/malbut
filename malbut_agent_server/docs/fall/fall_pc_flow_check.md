# 낙상 감지 전체 흐름 검증 — PC

최초 검사: 2026-09-25, 기준 코드 8b3ef5e.
2026-09-26에는 최신 main(70869fe)의 Manager 확인 결과 형식에 맞춰 검사를 갱신했다.

## 최신 main에서 다시 확인한 결과 — 2026-09-26

| 검사 | 결과 |
| --- | --- |
| PC의 실제 ROS Service·Topic 연결 | 11개 통과, 156.47초 |
| 기존 관련 검사 — Cloud·VLM·영상 평가·설정 전달·Pose 실행 조건 | 1,307개 통과, 15개 생략, 7.51초 |
| 위에서 ROS 환경이 없어 생략된 검사 | ROS 환경에서 별도 실행, 4개 통과, 0.45초 |
| 최신 ROS 자료형 빌드 | 성공 |
| 새 테스트 파일 flake8, 추가 파일 공백 검사 | 통과 |

일반 검사에서 생략한 15개는 별도로 실행한 PC 검사 11개와 ROS 환경이 필요한 4개다.
다른 검사 묶음과 범위가 겹칠 수 있으므로 저장소 전체 테스트 개수로 합산하지 않는다.

- 실제 Cloud API 호출, 영상 외부 전송, 로봇 명령, 웹 알림 발송은 하지 않았다.
- 운영 코드·설정은 바꾸지 않았다. 이번에 추가한 것은 검증 코드와 이 문서다.

위 시간은 테스트 묶음의 실행 시간이며 낙상 감지 지연시간이나 모델 성능을 뜻하지 않는다.
Cloud 대기 20초와 주기적 확인 60초를 줄이지 않고 실제로 기다렸다.

최초 2026-09-25 검사에서는 PC 검사 11개가 153.52초, 당시 기존 관련 검사 984개가
26.54초에 통과했다. 그 이후 질문은 영상 판단 뒤에 시작하고 Manager의 최종 확인
결과를 받도록 main이 변경됐다. 이번 검사는 이전 답변 입력 대신 현재
confirmation_result 형식을 사용하며, 도움 필요 결과도 첫 판단 뒤의 재분석 중에 보낸다.

## 무엇을 실제로 연결했나

| 구간 | 사용한 것 |
| --- | --- |
| 서버 설정 전달 | 테스트 노드가 만든 FallSettingsSnapshot. 실제 서버 조회는 하지 않음 |
| Manager → VLM | 실제 FallSettingsLink와 ApplyFallSettings Service. 전체 Manager의 이동·음성 기능은 켜지 않음 |
| 상태·연결 확인 | 실제 FallRuntimeStatus·FallControlHeartbeat Topic과 1초 타이머 |
| 카메라 입력 | 테스트 노드가 만든 640×400 RGB 이미지. Aurora 영상이나 평가용 합성 영상은 아님 |
| Pose 처리 | 입력 자세만 테스트 값으로 제공. FallPoseControl, PersonPoseTracker, FallCandidateDetector는 운영 코드 사용 |
| Pose → VLM | 실제 /homecam/person_poses·/homecam/fall_candidates Topic |
| VLM 실행 | 실제 create_fall_node와 spin_runtime |
| Cloud 요청·응답 | 실제 Ollama 요청 생성·응답 해석 코드. HTTP 전송 부분만 테스트 응답으로 대체 |
| 질문·답변 | 영상 판단 뒤의 질문 요청 이벤트와 Manager 최종 확인 결과 Topic. 확인 결과는 테스트 값이며, Agent Action·STT·TTS는 실행하지 않음 |
| 결과 저장 | 실제 SqliteFallJournal. DB를 다시 열어 낙상 기록이 남는지 확인 |
| 알림 | notification_requested 이벤트까지 확인. 웹 푸시 수신은 이번 범위가 아님 |

실제 카메라 장면이나 모델 판단의 정확도를 평가한 것이 아니다.
키 파일은 읽지 않았고, 모델 가중치도 불러오지 않았다.

## 확인한 11가지

| 검사 | 확인 내용 | 결과 |
| --- | --- | --- |
| 낙상 확인 + 도움 불필요 | 설정 전에는 수집하지 않음. 설정 회신 후 후보·Cloud 분석·질문 요청으로 이어짐. Manager의 confirmed_incident/false 결과로 처리를 끝내도 낙상 관측 기록은 유지 | 통과 |
| 상황 불명확 + 도움 필요 | Manager의 unknown/true 결과를 받으면 긴급 알림 이벤트 생성. 사건을 정상으로 끝내지 않음 | 통과 |
| 잘못된 Cloud 응답 | cloud_invalid_response 기록. 정상 행동으로 바꾸지 않음 | 통과 |
| Cloud 응답 없음 | 20초 뒤 cloud_timeout 기록. Local VLM을 실행하지 않음 | 통과 |
| 재분석 중 도움 필요 결과 | 첫 영상 판단 후 재분석의 응답을 기다리는 동안 Manager의 도움 필요 결과를 받으면 긴급 알림 이벤트와 도움 필요 상태 기록 | 통과 |
| 추가 확인 횟수 | 처음 1회 + 추가 2회 허용. 같은 사건 ID를 유지하고 분석 요청 ID는 각각 다름. 네 번째 요청은 차단 | 통과 |
| Cloud 동의 철회 | Manager를 통해 새 설정 적용. 진행 중 분석 취소, 새 전송 차단. 카메라·감지가 허용되어 있으면 Pose와 버퍼 유지 | 통과 |
| 카메라 OFF | 진행 중 분석 취소. Pose 처리 중단, 버퍼 비움 | 통과 |
| Manager 연결 중단 | 마지막 연결 확인 후 5초가 지나면 영상 수집·Pose·Cloud 요청 중단 | 통과 |
| 서버 설정 확인 중단 | Manager 연결이 살아 있어도 정상 서버 확인 후 15초가 지나면 Cloud 요청 중단. Pose와 버퍼는 유지 | 통과 |
| Pose 후보 없는 주기적 확인 | 후보가 없어도 60초 후 최근 5초의 RGB 12장을 한 요청으로 생성. 원본 640×400 유지. 위치가 없는 의심 결과도 대상 미확인 기록으로 저장 | 통과 |

같은 설정의 반복 전달이 Service 재적용으로 이어지지 않는지,
같은 자세가 계속 들어와도 동일 사건의 Cloud 요청이 반복되지 않는지도 확인했다.
SQLite 파일 권한은 0600이며 이미지나 모델 설명문을 사건 기록에 넣지 않는다.

## 다시 실행하는 방법

원본 저장소에서 실행한다. 이 테스트는 로봇 배포용 파일이 아니며,
일반 pytest 실행에서는 ROS 노드를 띄우지 않도록 건너뛴다.
실행하려면 명시적으로 MALBUT_RUN_FALL_ROS_TESTS=1을 설정해야 한다.

ROS Humble, /usr/bin/python3, pytest, cv_bridge, OpenCV, NumPy, Pillow가 필요하다.
아래 명령은 ROS를 새로 설치하지 않는다. aiohttp만 임시 경로에 설치한다.

~~~bash
cd /home/jisanggeun/malbut-vlm-eval
source /opt/ros/humble/setup.bash
FALL_CHECK_DIR=$(mktemp -d /tmp/malbut-fall-check.XXXXXX)

/usr/bin/python3 -m pip install \
  --target "$FALL_CHECK_DIR/python-deps" 'aiohttp>=3.9,<4'

PYTHONDONTWRITEBYTECODE=1 colcon --log-base "$FALL_CHECK_DIR/log" build \
  --base-paths malbut_interfaces \
  --build-base "$FALL_CHECK_DIR/build" \
  --install-base "$FALL_CHECK_DIR/install" \
  --packages-select malbut_interfaces \
  --cmake-args -DBUILD_TESTING=OFF \
  -DPython3_EXECUTABLE=/usr/bin/python3 -DPYTHON_EXECUTABLE=/usr/bin/python3

source "$FALL_CHECK_DIR/install/local_setup.bash"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$FALL_CHECK_DIR/python-deps:$PWD/homecam_agent/homecam_detector:$PWD/malbut_agent_server:$PWD/malbut_system_manager:$PYTHONPATH"

MALBUT_RUN_FALL_ROS_TESTS=1 ROS_LOCALHOST_ONLY=1 ROS_DOMAIN_ID=191 \
  /usr/bin/python3 -m pytest -v -p no:cacheprovider --tb=short \
  --junitxml="$FALL_CHECK_DIR/pc-flow.xml" \
  malbut_agent_server/test/test_fall_pc_flow.py
~~~

ROS_LOCALHOST_ONLY=1을 확인한 뒤에만 실행하고, 테스트마다 모든 낙상 Topic·Service를
새 UUID 경로로 바꾼다. 운영 중인 로봇의 설정 Service나 Topic을 사용하지 않는다.
다만 일반 ROS 통신의 인증·접근 권한을 구현하거나 검증했다는 의미는 아니다.

로컬 실행 결과 파일:

- /tmp/malbut-fall-pc-pr.etfgdk/pc-flow.xml (2026-09-26)
- /tmp/malbut-fall-pc-pr.etfgdk/regression.xml (2026-09-26)
- /tmp/malbut-fall-pc-pr.etfgdk/ros-regression.xml (2026-09-26)
- /tmp/malbut-spec-review.AkZ4vO/pc-flow-final.xml
- /tmp/malbut-spec-review.AkZ4vO/regression.xml

임시 경로의 실행 결과와 테스트 DB는 재부팅이나 임시 파일 정리로 없어질 수 있다.
Git에 남길 검증 코드가 다시 실행할 수 있는 기준이다.

## 이번에 확인하지 않은 것

- 실제 YOLO26s ONNX 추론과 낙상 미탐·오탐 비율.
- 실제 Cloud의 인증·요금·응답 속도·모델 판단 품질·HTTP 연결.
- 홈캠 C++ 서버 통신부에서 실제 서버 응답을 받아 Snapshot을 발행하는 전체 경로.
- 전체 Manager와 Agent·STT·TTS를 함께 실행한 질문·답변 처리. 이 검사는 Manager의 최종 확인 결과만 테스트 값으로 보낸다.
- 웹의 현재 실행 상태 표시와 보호자 웹 푸시 수신.
- Jetson·Aurora 입력, depth 정렬·거리 측정, 주행·음성과 동시 실행 부하.
- 실제 Bringup 프로세스 시작·종료. 기존 Bringup 자동 테스트와 별도다.

다음에는 테스트 응답을 실제 Cloud로 바꿔 별도로 확인한다.
사용할 키·모델과 영상 전송 동의·호출 비용을 확인한 뒤 진행하며,
이번 테스트 스위치를 켜는 것만으로 실제 Cloud가 호출되지는 않는다.

질문·답변 연결 자체는 최신 main의 SWM25-194에서 구현됐다.
그 구현과 별도 검사 범위는 [Agent–Manager 연결 문서](agent_fall_implementation.md)를 참고한다.
이 문서의 PC 테스트만으로 실제 마이크·스피커까지 검증했다고 해석하지 않는다.
