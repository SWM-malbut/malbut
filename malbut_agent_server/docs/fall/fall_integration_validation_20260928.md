# 최신 main과 로컬 낙상 기능 통합 검증

검증일: 2026-09-28. 기준 main: `084c39efe63b119daaba421237ac825641552a24`.

## 반영한 내용

- 로컬의 Cloud 단독 발견, 대상 미확인 장면 질문, 후속 Pose 연결 및 평가 도구를 유지했다.
- main에서 분리한 `malbut_fall_coordinator`가 설정 전달과 확인 질문을 담당한다.
  질문은 코디네이터 → Manager `ExecuteMission` → Agent `ConfirmSituation` 순서로 전달한다.
- 일반 Manager의 자원 조정과 충돌 미션 종료 확인을 거친다. 예전처럼 낙상 질문을
  일반 Manager 내부의 로컬 전용 코드에 덧붙이지 않았다.
- 평가 도구와 테스트의 옛 Manager import를 코디네이터로 옮겼다.
  ROS 워크스페이스를 설치하지 않는 CI에서도 형제 패키지를 찾도록 테스트 경로를 보완했다.
- 원본 패키지와 `malbut_test` 배포 복사본의 변경 파일 15쌍은 내용이 같다.

대상 미확인 Cloud 발견은 일반 확인 질문을 시작할 수 있다. 나중에 같은 사람임이
확인되면 개별 발견의 영상 근거를 사람별 사건에 연결한다. 장면 사건과 답변 전체를
사람 사건에 옮기거나, 남아 있는 장면 질문을 자동 정리하는 기능은 추가하지 않았다.

## PC 검증 결과

| 검사 | 결과 | 범위 |
|---|---|---|
| PC ROS 통합·런타임 | 55 통과, 제외 0 | 설정 적용, 중복 설정, 동의 철회, 카메라 OFF, 연결 중단·복구, 분석 실패, 재확인 제한, 주기 확인, 후속 대상 연결, 질문 전달 |
| 배포 복사본 PC ROS 재실행 | 같은 55개 모두 통과 | `malbut_test` 구현을 우선 import하여 별도 domain에서 재실행 |
| Agent·코디네이터 Python 전체 | 2,456 통과, 38 제외 | 실제 Cloud 호출 없는 코어·저장소·전송기·질문 회귀 검사 |
| CI 낙상 Python 스크립트 | 1,900 통과, 16 제외 | `.github/scripts/test-fall-python.sh` 그대로 실행 |
| 홈캠 오프라인 테스트 | 1,518 통과 | 모델 추론 없이 평가·검토·재생 도구 검사. `test_robot_launch.py` 제외 |
| Agent 단독 import 환경 | 45 통과 | ROS 설치 없이 코디네이터 질문 전달·후속 연결 테스트 수집/실행 |
| 변경 Python 문법 검사 | 88개 파일 통과 | `py_compile` |

검사 묶음에 같은 사례가 포함되므로 통과 수를 합산하지 않는다.
Python 전체 검사의 제외 항목은 ROS 미설치/실행 선택 조건과 선택 의존성
`pydantic` 관련 항목이다. 낙상 ROS 통합 항목은 별도 ROS 환경에서 실행했다.

첫 전체 검사에서는 평가용 Python 환경에 `tiktoken`이 없어 실패했다.
CI requirements를 설치한 별도 환경의 첫 실행에는 대화 요약 작업의 2초 종료 대기
검사 1개가 실패했고, 같은 전체 검사를 다시 실행해 모두 통과했다.
해당 대화 요약 소스나 시간 제한을 바꿔 통과시킨 것은 아니다.

## ROS 검사 방법과 확인한 동작

ROS 2 Humble에서 현재 `malbut_interfaces`를 새로 빌드하고,
`ROS_LOCALHOST_ONLY=1` 및 별도 domain으로 다른 로봇과 통신하지 않도록 했다.

검사 파일:

- `malbut_agent_server/test/test_fall_pc_flow.py`
- `malbut_agent_server/test/test_fall_deferred_ros_flow.py`
- `malbut_fall_coordinator/test/test_fall_confirmation_integration.py`
- `malbut_fall_coordinator/test/test_fall_settings_link.py`
- `malbut_agent_server/test/test_fall_runtime.py`

`MALBUT_RUN_FALL_ROS_TESTS=1`로 opt-in 검사를 활성화했다.
시간 초과 20초와 후보 없는 주기 확인 60초는 실제로 기다려 확인했다.
실제 DDS Service·Topic·Action을 이용하되, 카메라 이미지·Pose 관측·서버 설정·
Cloud 응답·Agent 답변·주행 Action 서버는 테스트 입력/서버로 대신했다.
런타임 단위 검사 중에는 노드 객체를 대신하는 검사도 포함된다.

- 같은 설정을 다시 적용해도 버퍼와 사건을 다시 시작하지 않는다.
- Cloud 동의 철회 시 진행 중 분석에 취소를 요청하고 새 전송을 막는다.
  카메라/감지 조건이 유효하면 Pose와 버퍼는 유지한다.
- 카메라 OFF 및 내부 연결 중단 시 영상 수집과 새 분석을 중단한다.
- Cloud 실패·시간 초과를 정상 행동으로 바꾸지 않는다.
- Cloud 단독 발견도 대상 미확인 장면 질문으로 전달하고 반복 발견의 중복 요청을 막는다.
- 후속 연결에 맞는 관측을 넣으면 발견 근거가 사람별 사건에 연결된다.
  장면 답변으로 다른 사람의 사건을 정상 종료하지 않는다.
- Manager의 충돌 미션 취소 완료 후 질문이 실행된다.
  이는 실제 바퀴가 정지했다는 검증은 아니다.

## 이번 검사로 확인하지 않은 것

- 실제 카메라의 연속 추적 결과를 운영 ROS 입력으로 연결하는 어댑터와 장시간 검증.
- 실제 다인·이동·가림 장면에서 같은 사람을 잘 연결하는지에 대한 전체 성능.
- 실제 Cloud 추론, API 인증/비용, 음성 질문·답변, 보호자 알림.
- Jetson에서 주행·음성과 함께 실행할 때의 부하 및 실제 정지 동작.
- 운영 서버 배포와 웹의 현재 실행 상태 표시.

이번 작업은 로컬 구현과 최신 구조의 통합·회귀 확인이다.
PC 테스트 통과를 사건 병합 정확도 개선이나 실물 로봇 검증 완료로 해석하지 않는다.
