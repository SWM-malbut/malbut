# 공통 ROS 인터페이스

[응용 기능 책임 및 인터페이스 규격](../APPLICATION_INTERFACE_RULES.md)을 따른다.
필드·자료형·상수의 정본은 이 패키지의 `.action`, `.srv`, `.msg` 파일이다.
아래 표와 모듈별 명세는 통신 경로와 동작 의미를 설명하며 별도 타입 정의를 만들지 않는다.
타입은 `CMakeLists.txt`의 `rosidl_generate_interfaces`에 등록한다.

## 음성·낙상 확인 연결

| 방향 | 방식·이름 | 타입 정본 | 역할 |
|---|---|---|---|
| FallCoordinator → Manager → FallCoordinator | Action `/malbut/mission/execute` | [ExecuteMission](action/ExecuteMission.action) | `fall_confirmation` capability로 확인을 요청하고 하위 Action의 최종 결과를 받음 |
| Manager → Agent → Manager | Action `/malbut/agent/confirm_situation` | [ConfirmSituation](action/ConfirmSituation.action) | 등록된 capability를 실행해 상황 요약으로 확인 대화를 요청하고 최종 상황 판단·도움 필요 여부를 받음 |
| Agent → STT → Agent | Service `/malbut/speech/session_control` | [ControlSpeechSession](srv/ControlSpeechSession.srv) | 호출어 없는 청취 세션의 시작·종료·생존 조회 |
| 홈캠 미디어 → STT → 홈캠 미디어 | Service `/malbut/speech/web_talk_control` | [ControlWebTalk](srv/ControlWebTalk.srv) | 웹 말하기 중 입력 차단·갱신·종료. 입력 및 이전 인식 결과 차단 후 접수 응답 |
| STT → Agent | Topic `/malbut/speech/input_status` | [SpeechInputStatus](msg/SpeechInputStatus.msg) | 일반 대화·확인 세션의 발화 시작·청취 또는 인식 실패 |
| STT → Agent | Topic `/malbut/speech/transcript` | [SpeechTranscript](msg/SpeechTranscript.msg) | 최종 인식 문장과 발화·세션 ID |
| STT → Agent → STT | Service `/malbut/speech/classify_addressee` | [ClassifySpeechAddressee](srv/ClassifySpeechAddressee.srv) | 일반 대화의 수신 대상 판정. 확인 세션에서는 생략 |
| Agent → TTS | Topic `/malbut/speech/response` | [SpeechRequest](msg/SpeechRequest.msg) | 대화·알림·확인 발화와 재생 ID, 요청 ID, 중간 안내 여부 |
| Agent·STT → TTS → 호출자 | Service `/malbut/speech/playback_control` | [ControlSpeechPlayback](srv/ControlSpeechPlayback.srv) | 개별 재생 제어·전체 중단과 접수 여부 |
| TTS → Agent·STT | Topic `/malbut/speech/playback_status` | [SpeechPlaybackStatus](msg/SpeechPlaybackStatus.msg) | 실제 재생 상태. 요청 접수와 재생 완료는 별개 |

`SpeechInputStatus`의 빈 `session_id`는 일반 대화이며, `STARTED`·`FAILED` 모두
비어 있지 않은 `utterance_id`를 사용한다. 빈 최종 인식 결과는 전사나 `FAILED` 없이
호출어 대기로 돌아간다. 최종 인식 중 예외가 발생하면 같은 발화 ID의 `FAILED`를
전달하고 실패 안내를 기다리는 동안 새 입력을 차단한다. Agent는 최신 `STARTED`와
일치하는 `FAILED`에만 한 번, 추가 대화 판단 LLM 호출이나 대화 메모리 기록 없이
“잘 알아듣지 못했어요. 다시 제이크라고 불러 주세요.”를 기존 `SpeechRequest`로 발행한다.
이 안내는 원래 `utterance_id`를 `request_id`로 사용하고 항상 `interim=false`다.
같은 요청의 최종 `finished`·`failed`·`stopped`가 도착하면 `playing` 이전의 종료라도
호출어 대기로 돌아간다. 실패 안내 대기 시작 후 45초 동안 같은 요청의 최종 종료가
없으면 이 안내의 입력 차단만 해제하며, 실제 재생 중 입력 차단과 잔향 차단은 유지한다.
안내 뒤에는 호출어 없는 후속 발화를 받지 않는다. 오래된 발화·중복 실패·확인 세션
상태는 이 일반 대화 안내에서 제외한다. 기존 필드와 타입은 그대로 사용한다.

호출어 감지 뒤 알림음의 잔향 차단이 끝난 시점부터 `start_timeout_s`(기본 5초) 안에
유효한 발화 시작이 없으면 전사나 안내 음성 없이 호출어 대기로 돌아간다.
비어 있지 않은 `session_id`는 Agent 주도 확인 세션이 소유하며, 세션 전체의
`FAILED`에는 빈 `utterance_id`를 허용한다. 확인 세션의 빈 최종 결과와 인식 예외는
기존처럼 `FAILED`로 알리고 Agent가 종료를 결정한다. AEC가 있는 일반 끼어들기로
기존 답변을 일시정지한 경우에는 `STARTED`를 전달하되 최종 인식 실패의 `FAILED`를
보내지 않아 안내가 일시정지된 TTS 뒤에 쌓이지 않도록 하며, 기존 일시정지·수신 대상
판정·다음 발화 처리를 유지한다.

`SpeechRequest.interim`과 `SpeechPlaybackStatus.interim`은 기본값이 `false`인 `bool`이다.
Agent는 최종 답변을 준비하는 중의 지연·재시도 안내에만 `true`를 지정하고, TTS는 해당
요청의 모든 재생 상태에 같은 값을 전달한다. 일반 호출어 대화는 전사 하나를 전달한
시점부터 새 입력을 차단한다. 같은 요청의 `interim=false`인 재생이 완료·실패·중단되면
호출어 대기로 돌아간다. 중간 안내와 다른 요청의 종료는 이 차단을 해제하지 않는다.

`SpeechRequest.request_id`는 같은 사용자 요청의 진행 안내와 최종 답변을 묶는다.
빈 값이면 기존 독립 재생을 유지하며, 지정할 때는 공백뿐인 값 없이 최대 256자를
사용한다. Agent는 원래 발화 ID를 전달한다. 최종 답변 접수 시 TTS는 같은 요청의
아직 재생하지 않은 진행 안내를 `stopped`로 취소하고, 이미 재생·일시정지 중인
음성은 유지한다. 각 발화의 `playback_id`는 계속 고유하며 상태 연결에 사용한다.
`SpeechPlaybackStatus.request_id`에도 이 값을 전달하므로 STT는 재생 전 실패를 포함해
자신이 전달한 발화의 최종 종료를 확인할 수 있다. `CONFIRMATION`은 기존 재생 ID 기반
제어를 유지하며 상태의 `request_id`는 빈 값이다.

[FallCoordinator](../malbut_fall_coordinator/README.md)는 VLM 사건을 해석하고
`ExecuteMission`으로 `fall_confirmation`을 요청한다. Manager는
[등록된 Manifest](capabilities/fall_confirmation.yaml)의 `FOREGROUND`, `URGENT`,
`[BASE, SPEAKER]` 규칙에 따라 충돌 미션의 종료를 확인한 뒤 Agent의
`ConfirmSituation` Action을 실행한다. Coordinator는 Manager가 돌려준 최종 결과를
검증하여 VLM 사건에 적용한다. 취소된 기존 주행은 자동 재개하지 않는다.
STT·TTS 제어 서비스는 내부 보조 호출로 유지한다. Agent는 확인 대화 시작 전에
TTS의 `STOP_ALL`을 요청하고, 개별 질문은 재생 ID로 제어한다.

## 동작 명세

- [낙상 확인 계약](../malbut_agent_server/docs/fall/agent_fall_implementation.md): 요청 검증, 결과·실패·취소, 중복·오래된 결과, 음성 세션과 시간 제한.
- [확인 대화 정책](../malbut_agent_server/docs/fall/agent_fall_interaction.md): 상황 판단·도움 확인·무응답·재질문 정책.
- [STT 명세](../malbut_stt/docs/stt_agent.md) · [TTS 명세](../malbut_tts/docs/tts_agent.md): 각 노드의 입출력과 음성 처리.
- [낙상 설정·상태 계약](../malbut_agent_server/docs/fall/fall_manager_contract.md): 설정 적용·연결 확인·실행 상태·홈캠 전달용 타입.
- [VLM 런타임–FallCoordinator JSON 계약](../malbut_agent_server/docs/fall/fall_runtime.md): 기존 `/malbut/falls/runtime/events`·`decision`은 `std_msgs/msg/String`을 유지한다. JSON 필드와 검증 규칙은 이 문서를 따르며, 확인 대화 Agent는 이 토픽을 직접 사용하지 않는다.

계약 변경 시 영향받는 발행·수신 노드와 동작 명세, `malbut_test`의 대응 파일을 함께
갱신한다. 필드·상수는 ROS 정의에서 바꾸고 문서에 독립된 규격을 추가하지 않는다.
`interim`과 `request_id` 추가는 ROS 메시지의 wire 타입 변경이다. `malbut_interfaces`와
영향받는 모든 발행·수신 패키지(Agent·STT·TTS·모니터)를 같은 정의로 함께 재빌드하고
실행 중인 노드를 모두 재시작해야 한다. 기본값을 사용해도 이전 타입과 혼용하지 않는다. 이전 빌드·검증
snapshot을 변경된 타입의 검증 결과로 간주하지 않으며, 실제 로봇 동작은 별도로 확인한다.
