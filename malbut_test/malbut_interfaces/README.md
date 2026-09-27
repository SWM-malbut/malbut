# 공통 ROS 인터페이스

[응용 기능 책임 및 인터페이스 규격](../../APPLICATION_INTERFACE_RULES.md)을 따른다.
필드·자료형·상수의 정본은 이 패키지의 `.action`, `.srv`, `.msg` 파일이다.
아래 표와 모듈별 명세는 통신 경로와 동작 의미를 설명하며 별도 타입 정의를 만들지 않는다.
타입은 `CMakeLists.txt`의 `rosidl_generate_interfaces`에 등록한다.

## 음성·낙상 확인 연결

| 방향 | 방식·이름 | 타입 정본 | 역할 |
|---|---|---|---|
| FallCoordinator → Manager → FallCoordinator | Action `/malbut/mission/execute` | [ExecuteMission](action/ExecuteMission.action) | `fall_confirmation` capability로 확인을 요청하고 하위 Action의 최종 결과를 받음 |
| Manager → Agent → Manager | Action `/malbut/agent/confirm_situation` | [ConfirmSituation](action/ConfirmSituation.action) | 등록된 capability를 실행해 상황 요약으로 확인 대화를 요청하고 최종 상황 판단·도움 필요 여부를 받음 |
| Agent → STT → Agent | Service `/malbut/speech/session_control` | [ControlSpeechSession](srv/ControlSpeechSession.srv) | 호출어 없는 청취 세션의 시작·종료·생존 조회 |
| STT → Agent | Topic `/malbut/speech/input_status` | [SpeechInputStatus](msg/SpeechInputStatus.msg) | 일반 대화·확인 세션의 발화 시작·청취 또는 인식 실패 |
| STT → Agent | Topic `/malbut/speech/transcript` | [SpeechTranscript](msg/SpeechTranscript.msg) | 최종 인식 문장과 발화·세션 ID |
| STT → Agent → STT | Service `/malbut/speech/classify_addressee` | [ClassifySpeechAddressee](srv/ClassifySpeechAddressee.srv) | 일반 대화의 수신 대상 판정. 확인 세션에서는 생략 |
| Agent → TTS | Topic `/malbut/speech/response` | [SpeechRequest](msg/SpeechRequest.msg) | 대화·알림·확인 발화와 재생 ID, 중간 안내 여부 |
| Agent·STT → TTS → 호출자 | Service `/malbut/speech/playback_control` | [ControlSpeechPlayback](srv/ControlSpeechPlayback.srv) | 개별 재생 제어·전체 중단과 접수 여부 |
| TTS → Agent·STT | Topic `/malbut/speech/playback_status` | [SpeechPlaybackStatus](msg/SpeechPlaybackStatus.msg) | 실제 재생 상태. 요청 접수와 재생 완료는 별개 |

`SpeechInputStatus`의 빈 `session_id`는 일반 대화이며, `STARTED`·`FAILED` 모두
비어 있지 않은 `utterance_id`를 사용한다. Agent는 최신 `STARTED`와 일치하는
`FAILED`에만 한 번, 추가 대화 판단 LLM 호출이나 대화 메모리 기록 없이
“잘 알아듣지 못했어요. 다시 말씀해 주세요.”를 기존 `SpeechRequest`로 발행한다.
앞서 접수한 답변이 아직 송출되지 않았다면 `interim=true`, 그 외에는 `false`를 사용한다.
오래된 발화·중복 실패·확인 세션 상태는 이 일반 대화 안내에서 제외한다.
비어 있지 않은 `session_id`는 기존 Agent 주도 확인 세션이 소유하며, 세션 전체의
`FAILED`에는 빈 `utterance_id`를 허용한다. `interim=false`인 안내의 정상 재생 완료 뒤에는
기존 5초 후속 발화 대기를 적용하며, `interim=true`인 안내로는 이 대기를 시작하지 않는다.
이 확장은 기존 필드와 타입을 그대로 사용한다. 이 안내는 TTS가 정상 동작할 때 음성으로 전달되며, TTS 자체 고장 대응은 이번 범위에 포함하지 않는다.
단, AEC가 있는 일반 끼어들기로 기존 답변을 일시정지한 경우에는 `STARTED`를 전달하되 최종 인식 실패의 `FAILED`를 보내지 않아 재시도 안내가 일시정지된 TTS 뒤에 쌓이지 않도록 하며, 기존 일시정지·수신 대상 판정·다음 발화 처리와 확인 세션의 `FAILED` 발행은 유지한다.

`SpeechRequest.interim`과 `SpeechPlaybackStatus.interim`은 기본값이 `false`인 `bool`이다.
Agent는 최종 답변을 준비하는 중의 지연·재시도 안내에만 `true`를 지정하고, TTS는 해당
요청의 모든 재생 상태에 같은 값을 전달한다. STT는 `PLAYING`에서 재생 ID와 함께 이
값을 기억하며, 중간 안내의 `FINISHED`로 일반 대화의 5초 종료 대기를 시작하지 않는다.
최종 답변의 `interim=false` 재생이 정상 완료된 뒤 기존 5초 대기를 적용한다.

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
`interim` 추가는 ROS 메시지의 wire 타입 변경이다. `malbut_interfaces`와 영향받는 모든
발행·수신 패키지(Agent·STT·TTS)를 같은 정의로 함께 재빌드하고 실행 중인 노드를 모두
재시작해야 한다. 기본값이 `false`여도 이전 타입과 혼용하지 않는다. 이전 빌드·검증
snapshot을 변경된 타입의 검증 결과로 간주하지 않으며, 실제 로봇 동작은 별도로 확인한다.
