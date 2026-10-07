# Agent 로봇·홈캠 통합 검증

> **기록 범위:** 아래 표와 실기 기록은 이전 통합 커밋 `43b2b5d`에 대한 역사적 증거다.
> PR은 최신 main `723c901` 위로 이식했으며, 이식본의 검증은 `pr-validation.md`에 따로 기록한다.
> 사용자 요청으로 2026-10-08 로봇의 통합 검증 프로세스·overlay·전용 작업공간을 제거했다.
> 변경 코드는 Mac에 보존했고, 이후 PR 준비 중 로봇에 접속하거나 배포하지 않았다.
> 이전 실기 결과를 현재 PR 코드의 실기 통과 또는 현재 로봇 실행 상태로 해석하지 않는다.

기존 변경을 보존한 snapshot `b60d0b5` 위의 `codex/agent-robot-integration` 작업이다.
원래 `/Users/sinhyeonjae/Documents/ChatGPT/malbut`의 dirty `main`은 변경하지 않았다.
소스·자동 시험·실제 ROS 통신·로봇 빌드·실제 주행/음향은 각각 다른 증거로 취급한다.

## 구현 범위

- `cloud.launch.py` 상주 음성 프로세스와 자식 로봇 실행 그룹 분리, 공유 Pulse 입력,
  독립 준비 상태, Manager 없는 대화·날씨·재시작 요청.
- Manager 전체 이동 중지, 정확한 충돌 ID 확인, 수동 입력 중립 재무장,
  지연 접수 Goal/지도 준비 거부, 종료 미확인 시 이동 차단.
- 상태·관측·지도·기존 구역, 따라오기·순찰·자동 지도 작성·위치 보정·수동 모드·복구,
  고정 준비 단계와 SQLite 전송 기록. 실제 지도에 묶인 목적지 설정이 없으면 장소 이동 미노출.
- 기본 비활성 소유자 음성 위임, 장치 범위 홈캠 API, 기록·감사·인증된 화면 결과,
  설정 저장과 heartbeat 적용 보고 구분. 가족·credential·Cloud 분석 동의 변경은 미노출.

## 코드 및 자동 시험

| 범위 | 결과 | 증거의 한계 |
|---|---|---|
| Bringup 전체 Python/ROS/launch/배포 계약/lint | **535 passed**, ROS CLI 인자 수정 후 **69/69**, 마지막 준비 만료 정리·ROS·lint **35/35**, 2 dependency deprecation warnings | 가상 ROS와 모의 장치; 실제 주행·음향 제외 |
| 인터페이스·Manager 생성/빌드 | 2개 패키지 성공 | Ubuntu ARM64 ROS 2 Humble 컨테이너 |
| Manager 전체 + 마지막 정지 회귀 | **205/205**, 이후 **17/17**; 서로 다른 테스트 206개 | 실제 ROS Action/Service, 하위 동작은 테스트 서버 |
| Agent 전체 offline | **3021 passed, 31 skipped, 10 failed, 24 subtests passed** | 아래 기존 실패 설명 참조 |
| Agent 마지막 workflow/client 회귀 | **97/97**, 마지막 상태·홈캠 회신 안내 **62/62** | 준비 epoch 전송, 실제 음성 준비 여부, 저장값과 적용 회신을 구분하는 안내 포함 |
| Agent 실제 ROS 계약·음성 메시지 경로 | **38/38**, 마지막 알림 ID 회귀·TTS **68/68** | 마이크 대신 SpeechTranscript, 실제 스피커 대신 관측된 TTS 요청 |
| 웹 권한·설정·결과 계약 | **38/38**, TypeScript·변경 파일 ESLint 통과 | 프로덕션 웹 배포를 뜻하지 않음 |
| 실제 PostgreSQL 16 | 전체 migration/재실행, 동시 같은-ID 요청, 설정 적용 receipt, 위임 해제 후 재시도 통과 | 임시 DB 사용 |
| 미디어 C++ | ROS **55/55**, native Clang **16/16** | GStreamer/CURL 활성, KVS 비활성 컨테이너 빌드 |
| 실제 LLM 고정 문장 평가 | 최초 **29/30**, 마지막 조회 지침 수정 후 기존 사례 **32/32**와 이전 조회가 있는 대화 맥락 **6/6** | 합성 한국어·실행 adapter 없음; 임의 발화의 정확도 추정 아님 |

Agent 전체 수집의 10개 실패는 원래 변경 전 checkout에서도 동일하게 재현했다.
기존 `StoryMemoryProvider` wrapper와 구형 Routed/ReliableProvider 직접 접근 테스트의
불일치다. 이 작업에서 기억 구현이나 동의 상태를 바꿔 실패를 우회하지 않았다.
상세 실패명과 LLM 사례별 결과는
`malbut_agent_server/docs/validation/agent_robot_integration/`에 보존했다.
LLM 모델은 `gpt-5.6-luna`, reasoning `low`; 두 번째 실행은 입력 194743,
출력 1615 tokens이며 API가 청구 금액을 반환하지 않았다.
마지막 조회 지침 회귀는 기존 32개에 입력 203735/출력 1592 tokens,
대화 맥락 6개에 입력 37741/출력 249 tokens를 사용했다. 최종 지침은 현재 상태·관측·목록·최근
기록 질문마다 실제 제공된 조회 도구를 새로 선택하게 하며, 과거 결과 자체를 회상하는
요청·인용·기능 설명은 구분한다. 마지막 프롬프트·provider·문맥 회귀 58개도 통과했다.

배포 대응 파일 62쌍을 확인했다. `robot.launch.py`의 기존 테스트 전용
리소스 모니터 hook을 제외한 소스는 동일하다. 이 예외도 전체 배포 계약 시험에서 검증했다.

## 장치 검증과 배포

SSH 접속 후 기존 로봇 checkout은 clean `main`의 `723c901`이며,
기존 로봇 실행 프로세스가 없는 것을 확인했다. 기존 checkout과 설치본을 보존하기 위해
검증용 소스·빌드·설치는 `/home/ubuntu/agent-integration-ws`에 둔다.
기존 `/home/ubuntu/ros2_ws`는 하드웨어 underlay와 이미 준비된 SDK를 제공한다.
기존 XFM Pulse 입력, speech venv와 native ABI 3, 모델 파일, 장치 토큰의 존재를 확인했다.
비밀번호·API 키·장치 토큰은 보고서나 소스에 저장하지 않는다.

격리된 실제 로봇 빌드는 홈캠 3/3 패키지(KVS/GStreamer/CURL 활성),
본체 17/17 패키지 모두 성공했다. 기존 speech native ABI 3과 SDK를 재사용했다.
18개 고유 패키지 prefix가 새 overlay로 해석되고, 새 인터페이스 6종의 native
rclpy type support와 미디어 동적 라이브러리 연결을 확인했다. 새 작업공간은 약 186 MB,
남은 디스크는 약 4.5 GB다. 첫 staging에서 빠진 공개 `.env.example` 4개를 복원한 후
재빌드했으며 실제 credential 파일은 복사하지 않았다. 첫 실기 실행에서 선택적 목적지 설정이 빈 CLI 인자로 전달되어 음성 자식이
시작되지 않는 문제를 확인했다. 빈 인자를 생략하도록 수정하고 실제 ROS launch 인자
parser 회귀 시험을 추가했다. 클라우드 제어는 음성 실패 중에도 유지됐다.
수정 후 상주 음성 실행에 성공했다. 실제 Orin CUDA Whisper small 모델과
XFM Pulse 마이크가 로드되어 호출어 대기 상태를 관측했다. Manager가 없는 상태에서
합성 SpeechTranscript 상태 질문을 실제 Agent에 한 번 전달했고, 상태 DeviceOperation
성공 및 실제 TTS의 playing → finished를 최초 답변·workflow 알림 각각의 ID로 확인했다.
로봇 runtime=STOPPED, voice.ready=true/standby, 저장 지도 7개, Manager 상태 없음이
실제 조회 결과다. 이는 마이크로 발화한 사용자의 인식 정확도나 사람이 들은 음향 품질,
실제 바퀴 정지를 증명하지 않는다. 최초 probe의 즉시 node count는 DDS discovery
완료 전 값이므로 중복 여부는 4초 관측 후의 graph-probe.json으로 판단한다.
상주 bridge·STT·Agent·TTS는 각각 1개이며 Manager·미디어 자식은 0개다.
첫 이동 없는 실행 그룹 시작·종료 시험은 검증용 비대화형 SSH 환경에
벤더 `DEPTH_CAMERA_TYPE` 설정이 빠져 시작이 실패했다. 종료 후 음성 준비 상태는 유지됐다.
기존 `/home/ubuntu/ros2_ws/.typerc`의 하드웨어 설정을 읽도록 검증 런처를 수정했다.
실제 값은 aurora/MS200/ROSOrin_Mecanum이며 ROS domain은 0이다.
기존 설정의 Cyclone DDS 파일은 없지만 실제 선택된 RMW는 Fast DDS여서 초기화에 성공했다.
상주 실행 전체를 재시작할 때 바깥 LaunchService의 기본 5초 종료 제한이 내부 음성
LaunchService의 종료 제한보다 먼저 만료되어 Agent가 남는 사례를 관측했다.
바깥 음성 자식의 종료 유예를 20초로 늘려 내부 INT/TERM/KILL 정리가 먼저 끝나게 했다.
수정한 launch 계약과 lint 69개를 통과했다.
수정 후 실기에서 `runtime_start(mapping)`이 하드웨어부터 홈캠·낙상까지 7/7 준비를
완료했고 `runtime_stop`도 성공했다. 요청 ID는
`lifecycle-05a0def7b995427da5c7e96eabebe5fe`다. 실행 중 bridge·STT·Agent·TTS·Manager·미디어는
각각 1개였고, 종료 후 Manager·미디어만 0개가 됐다. 준비 완료 이후부터 종료까지 음성은
ready를 유지했다. 전송한 이동 Goal은 0개, 관측된 nonzero `/cmd_vel`은 0개다.
이는 지도 작성 모드의 센서/실행 그룹 시작·종료 시험이며 AutoSLAM 주행 시험이 아니다.
이후 상주 그룹 전체 종료도 1.62초에 끝났고 해당 process group의 살아 있는 잔여 PID는
0개였다. 재시작 후 음성 준비도 다시 확인했다. 같은 대화에서 반복한 상태 질문에
이전 상태를 직접 답하고 새 조회를 생략한 사례는 `standby-probe.stale-context.json`에
보존했다. 이 사례의 TTS 재생 완료는 새 상태 조회의 성공으로 간주하지 않는다.
최종 조회 지침 수정 후 같은 대화 DB를 유지한 재질문은 새 조회를 실행했다.
발화 `integration-status-445e2925510e45b5a6973343a491539a`와 workflow
`speech-request-c4ac7ab2557a37e163b7a3f76ce0f27895348d5d79eeb79fc52ab6cf8e774e5b`로
"로봇 기능은 꺼져 있고, 음성 대화는 대기 중"이라는 실제 상태 결과와 TTS의
playing → finished를 확인했다. `standby-probe.json`은 이 최종 결과다.
마지막 상주 전체 재시작 때에는 종료에 20.31초가 걸렸고 살아 있는 잔여 PID가 0개인 것을
확인한 다음 새 프로세스를 시작했다. 당시 검증 종료 시점의 상주 launch PID는 19435였으며 음성 준비 상태였다.
이 프로세스와 검증용 overlay는 이후 사용자 요청으로 모두 제거했다.
현재 지도 선택과 현장 관찰 확인 전에는 물리적 이동 시험을 진행하지 않았다.
리소스 뷰어는 기존 설치본에서
별도로 실행했으며 `http://192.168.0.86:8766/`의 HTTP 200을 확인했다.

프로덕션 홈캠 웹 배포는 아직 수행하지 않았다. 운영 배포에는
`malbut_web/db/migrations/0024_voice_agent.sql`과 웹 API/UI,
생성 ROS 인터페이스·Manager·Bringup·Agent·미디어 agent의 호환 버전 적용이 필요하다.
단순히 기존 `main`을 pull하는 것으로 이 로컬 통합 브랜치가 설치되지는 않는다.
