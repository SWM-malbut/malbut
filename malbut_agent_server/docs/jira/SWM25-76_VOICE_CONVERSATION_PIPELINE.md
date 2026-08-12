# SWM25-76 음성 대화 파이프라인

## 문서 정보

| 항목 | 값 |
| --- | --- |
| Jira 스토리 | SWM25-76 음성 대화 파이프라인 |
| 대상 패키지 | `malbut_agent_server` |
| 구현 기준일 | 2026-08-13 |
| 현재 범위 | 순수 Python 오프라인 계약과 결정론적 Mock 시험 |
| 현재 상태 | 텍스트·메타데이터 경계 구현, 실제 STT·TTS·ROS adapter 미연결 |
| 안전 원칙 | 최종 transcript만 추론, 원시 오디오 비수용·비저장, 최종 Safety 응답만 TTS |

이 문서는 SWM25-34가 소유하는 음성 입출력과 SWM25-69 대화 서버 사이에서
SWM25-76이 구현한 오프라인 경계를 설명한다. 현재 구현은 실제 음성을
인식하거나 재생하지 않는다. 마이크·스피커, 외부 STT·TTS API, ROS Topic과
Action을 호출하지 않고 합성 이벤트로 계약만 검증한다.

관련 상위 책임 경계는
[`SWM25-69 대화·에이전트 계약`](SWM25-69_CONVERSATION_AGENT_CONTRACT.md)의
다음 원칙을 그대로 따른다.

- Agent Server는 STT·TTS·VAD 신호 처리를 소유하지 않는다.
- 최종 transcript와 신뢰된 사용자·세션 결속만 대화 입력으로 사용한다.
- 부분 transcript로 Tool을 제안하거나 실행하지 않는다.
- TTS는 취소 가능해야 하며 로봇 자신의 발화를 사용자 입력으로 재인식하지
  않아야 한다.
- 실제 ROS Action 서버가 재생과 취소의 최종 실행 권한을 소유한다.

## 1. 목표와 비목표

### 1.1 이번 오프라인 MVP가 구현하는 것

- 엄격한 `SpeechTranscriptEvent` 입력 계약
- 서버가 소유하는 `TrustedSpeechBinding`
- 원시 음성 없이 길이·sample rate·channel만 담는 `AudioMetadata`
- 최종 안전 응답을 전달하는 text-only `TTSRequest`
- 끼어들기와 세션 종료를 위한 idempotent `TTSCancelRequest`
- 최종 transcript와 기존 `AgentOrchestrator` 턴의 안정적인 상관 ID
- 부분·낮은 신뢰도·결속 불일치·self-echo·오래된 이벤트의 추론 전 차단
- 중복 최종 transcript의 provider 재호출 방지와 변조된 중복 거절
- TTS 재생 구간과 다음 수음 구간을 분리하는 `capture_epoch`
- 음성 세션 종료 시 활성 TTS 취소와 대화 세션 종료
- transcript 원문과 speaker 식별자를 제외한 명시적 audit projection

### 1.2 이번 범위에서 하지 않는 것

- PCM, WAV, Opus 등 음성 데이터 수신·디코딩·저장
- 파일 경로, URI, object storage key를 통한 음성 참조
- VAD, wake word, speaker recognition, echo cancellation 구현
- STT·TTS 모델 또는 유료 API 호출
- 마이크 입력과 스피커 출력
- `rclpy` node, ROS Topic, Service, Action 정의·호출
- 실제 음성 latency·WER·한국어 인식 품질 측정
- 실제 TTS 재생 성공·실패·취소 확인

따라서 이번 결과를 “음성 기능 실기 완료”로 판정하면 안 된다. 현재 증거는
음성 adapter가 지켜야 하는 텍스트 경계와 상태 전이가 오프라인에서
fail-closed한다는 것까지만 증명한다.

## 2. 구현 위치

| 파일 | 역할 |
| --- | --- |
| [`speech.py`](../../malbut_agent_server/speech.py) | typed 계약, 세션 상태와 coordinator |
| [`test_speech_pipeline.py`](../../test/test_speech_pipeline.py) | 합성 이벤트 기반 오프라인 회귀 시험 |
| 이 문서 | 책임 경계, 정책, 상태 전이와 남은 운영 결정 |

기존 대화 서버 변경 없이 `SpeechConversationCoordinator`가
`AgentOrchestrator` 앞에 놓인다.

```text
신뢰된 로컬 speech adapter
        |
        | SpeechTranscriptEvent
        v
SpeechConversationCoordinator
  - schema와 binding 검사
  - final/confidence/order/echo 검사
  - stable request_id·turn_id 생성
        |
        | AgentRequest(확정 transcript text만)
        v
AgentOrchestrator
  -> conversation store
  -> provider
  -> SafetyPolicy
        |
        | 최종 decision.message만
        v
TTSRequest(interruptible=true)
        |
        | 후속 ROS/TTS adapter 범위 — 현재 미구현
        v
실제 음성 재생
```

## 3. 입력 계약

### 3.1 `TrustedSpeechBinding`

binding은 일반 transcript payload에서 받지 않고 인증된 로컬 adapter 또는
서버 설정이 만든다.

| 필드 | 의미 |
| --- | --- |
| `user_id` | 기존 Agent Server의 사용자 격리 키 |
| `speaker_id` | 신뢰된 인식 계층이 공급한 화자 ID |
| `speech_session_id` | 한 번의 음성 capture·playback 생명주기 |
| `conversation_id` | 기존 SQLite 대화 세션 ID |
| `source` | 신뢰된 로컬 adapter 이름 |

`SpeechTranscriptEvent`에는 `user_id`가 없다. 클라이언트가 transcript에
`user_id`를 추가하면 strict unknown-field 검증에서 거절한다. coordinator는
binding의 `user_id`만 `AgentRequest`에 주입한다.

같은 `speech_session_id`를 다른 사용자, 화자, 대화 또는 source에 다시
결속하면 거절한다. 음성 세션을 열 때 기존 대화 저장소의 세션을
idempotent하게 생성한다.

### 3.2 `SpeechTranscriptEvent`

현재 schema version은 `1`이며 다음 필드만 허용한다.

| 필드 | 제한과 용도 |
| --- | --- |
| `schema_version` | 정확히 `1` |
| `utterance_id` | 발화 중복 제거 ID, 최대 128자·제어문자 금지 |
| `speech_session_id` | binding과 정확히 일치 |
| `conversation_id` | binding과 정확히 일치 |
| `speaker_id` | binding과 정확히 일치 |
| `source` | binding과 정확히 일치, 최대 64자 |
| `sequence` | 세션 내 증가하는 양의 정수 |
| `capture_epoch` | TTS와 다음 수음 사이의 fence |
| `source_timestamp_ns` | source가 부여한 0 이상의 timestamp |
| `text` | trim 후 1~2,000자 UTF-8 text |
| `confidence` | finite `0.0~1.0` |
| `is_final` | boolean |
| `capture_origin` | `microphone`, `self_echo`, `unknown` 중 하나 |
| `audio_metadata` | 원문 없는 제한된 세 필드 |

unknown field는 모두 거절한다. 특히 다음 필드는 top-level과
`audio_metadata` 양쪽에서 허용하지 않는다.

```text
audio, bytes, pcm, waveform, path, uri
```

### 3.3 `AudioMetadata`

오디오 내용 대신 입력 경계와 latency 측정에 필요한 최소 metadata만 받는다.

| 필드 | 현재 임시 상한 |
| --- | ---: |
| `duration_ms` | 1~30,000 |
| `sample_rate_hz` | 8,000~48,000 |
| `channel_count` | 1~2 |

이 값들은 대화 DB에 저장하지 않는다. 실제 장치와 STT provider가 확정되기
전의 보수적인 오프라인 한계이며 운영 SLA 승인이 아니다.

## 4. 처리 정책

coordinator는 다음 순서로 검사한다. provider보다 앞에서 실패한 이벤트는
provider 호출 수가 0이어야 한다.

1. 등록된 `speech_session_id`인지 확인한다.
2. conversation, speaker와 source가 binding과 같은지 확인한다.
3. 동일 `utterance_id`의 fingerprint를 검사한다.
4. 세션이 열려 있는지 확인한다.
5. 부분 transcript는 `ignored/partial_transcript`로 끝낸다.
6. `self_echo` 또는 알 수 없는 capture origin을 거절한다.
7. 현재 `capture_epoch`인지 확인한다.
8. TTS 재생이 활성 상태라면 사용자 발화로 처리하지 않는다.
9. 완료된 final sequence보다 오래된 이벤트를 거절한다.
10. confidence가 기본 `0.75` 미만이면 거절한다.
11. 안정적인 `request_id`와 `turn_id`를 생성한다.
12. binding의 사용자와 대화 ID로 `AgentRequest`를 만든다.
13. 기존 `AgentOrchestrator`를 호출한다.
14. `raw_decision`이 아니라 SafetyPolicy 이후의 `decision.message`로
    `TTSRequest`를 만든다.

부분 이벤트는 같은 `utterance_id`의 final 이벤트가 뒤에 올 수 있도록
완료 cache에 넣지 않는다. 반대로 final 이벤트의 성공·거절 결과는 bounded
cache에 남긴다.

`source_timestamp_ns`는 현재 타입과 감사 상관관계까지만 검증하며, 수치의
단조 증가나 wall-clock freshness는 판정하지 않는다. source clock의 epoch와
동기화 오차, 허용 지연이 아직 합의되지 않았기 때문이다. 현재 stale 차단
증거는 `sequence`와 `capture_epoch`에 기반한다. 실제 ROS bridge는 신뢰된
clock 기준과 최대 age를 확정한 뒤 timestamp freshness gate를 추가해야 한다.

## 5. ID 상관관계와 중복 방지

`request_id`, `turn_id`와 TTS `request_id`는 다음 입력을 SHA-256에 넣어 만든
결정론적 식별자다.

```text
user_id + speech_session_id + conversation_id + utterance_id
```

- 동일 `utterance_id`와 동일 전체 transcript 재전송은 같은 coordinator 결과,
  같은 Agent ID와 같은 TTS ID를 반환한다.
- coordinator cache에 있는 동안 동일 ID의 text·confidence·metadata 변조는
  `utterance_conflict`로 거절한다.
- bounded cache에서 제거된 뒤에도 기존 Conversation Store의
  `request_id` fingerprint가 변조된 재사용을 내구성 있게 거절한다.
- cache 기본 크기는 세션당 transcript 256개와 activity 256개다.

`utterance_id`는 신뢰된 STT bridge가 세션 내에서 유일하게 만들어야 한다.
LLM은 이 값을 만들거나 바꾸지 않는다.

## 6. TTS, self-echo와 끼어들기

### 6.1 `TTSRequest`

현재 TTS 요청은 text-only다.

| 필드 | 정책 |
| --- | --- |
| `request_id` | Agent 요청에서 결정론적으로 파생 |
| `speech_session_id` | 입력 음성 세션과 동일 |
| `conversation_id` | 입력 대화와 동일 |
| `turn_id` | Agent 턴과 동일 |
| `source_utterance_id` | 원 final transcript ID |
| `text` | 최종 Safety 응답, 최대 2,000자 |
| `voice` | 현재 `default`만 허용 |
| `style` | 현재 `neutral`만 허용 |
| `interruptible` | 반드시 `true` |

TTS 요청은 오디오 blob, 출력 파일 경로 또는 재생 장치 이름을 포함하지
않는다.

### 6.2 self-echo 차단

세션에는 `capture_epoch`가 있다.

- TTS가 활성 상태인 동안 새 final transcript는 `tts_playback_active`로
  거절한다.
- `capture_origin=self_echo`는 항상 provider 앞에서 거절한다.
- TTS가 terminal 상태가 되면 epoch를 증가시켜 이전 수음 구간의 늦은
  transcript를 거절한다.
- 실제 음향 echo 판정과 AEC는 이 coordinator가 아닌 신뢰된 speech adapter가
  소유한다.

따라서 adapter가 실제 로봇 자신의 음성을 `microphone`으로 잘못 분류하면 이
코드만으로 음향적 진위를 판단할 수 없다. 실제 통합 전 echo 분류·AEC와
loopback 시험이 필요한 이유다.

### 6.3 barge-in

신뢰된 VAD bridge는 사용자의 새 발화 시작을 `SpeechActivityEvent`로 보낸다.
binding의 session, speaker와 source가 모두 일치할 때만 처리한다.
이 event도 현재 `capture_epoch`를 포함해야 한다. bounded replay cache에서 오래된
event ID가 제거되더라도 과거 epoch의 재전송이 새 TTS를 취소하지 못한다.

```text
사용자 발화 시작
  -> 활성 TTS를 분리
  -> TTSCancelRequest(reason=barge_in)
  -> capture_epoch 증가
  -> 새 epoch의 final transcript만 허용
```

동일 `event_id` 재전송은 동일 취소 요청을 반환한다. 같은 ID를 다른 payload로
재사용하면 `activity_conflict`다. 실제 재생 취소 deadline과 성공 여부는
후속 TTS Action adapter가 검증해야 한다.

한 프로세스에서는 동일한 `(user_id, conversation_id)`에 두 live speech
session을 결속하지 않는다. 다만 이 lease는 아직 DB에 영속되지 않으므로
multi-process deployment에는 별도 durable lease가 필요하다.

## 7. 세션 종료

`close_session(speech_session_id, control_id)`은 다음 작업을 한 번 수행한다.

1. 연결된 기존 Conversation Store 세션을 `closed`로 전환한다.
2. 활성 TTS가 있으면 `TTSCancelRequest(reason=session_closed)`를 만든다.
3. capture epoch를 증가시킨다.
4. 음성 세션을 terminal 상태로 바꾼다.
5. 이후 transcript와 barge-in을 거절한다.

같은 `control_id`의 재시도는 저장한 동일 결과를 반환한다. 다른 control ID로
이미 닫힌 세션을 다시 닫으려 하면 `close_conflict`다.

## 8. 개인정보와 저장 정책

현재 구현의 데이터 흐름은 다음과 같다.

| 데이터 | 대화 DB 저장 | 명시적 audit projection |
| --- | --- | --- |
| 확정 transcript text | 예, 기존 사용자 발화로 저장 | 아니요, 길이만 |
| 최종 assistant text | 예, 기존 안전 응답으로 저장 | coordinator가 별도 기록하지 않음 |
| 원시 audio·PCM·waveform | 입력 자체가 불가능 | 불가능 |
| audio 파일 path·URI | 입력 자체가 불가능 | 불가능 |
| audio metadata | 아니요 | duration/rate/channel만 |
| speaker ID | 대화 DB에 저장하지 않음 | 제외 |
| speech·conversation·utterance ID | speech cache의 상관관계 | 포함 |
| confidence | 대화 DB에 저장하지 않음 | 포함 |

`SpeechTranscriptEvent.to_dict()`는 transport용이라 transcript text를 포함한다.
그 값을 운영 로그에 직접 남기면 안 된다. 내용 없는 감사에는 반드시
`to_audit_dict()`를 사용한다. 현재 cache는 메모리 안의 bounded 구조이며
프로세스가 끝나면 사라진다.

대화 DB에는 확정 transcript 원문이 남기 때문에 실제 배포 전 보존 기간,
사용자 삭제 요청, 백업 삭제와 접근 권한 정책을 별도로 승인해야 한다.

## 9. 오프라인 검증

전용 시험은 실제 장치와 외부 서비스를 호출하지 않는다.

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server

PYTHONPATH=. python3 -m pytest -q test/test_speech_pipeline.py

python3 -m py_compile \
  malbut_agent_server/speech.py \
  test/test_speech_pipeline.py

python3 -m flake8 \
  malbut_agent_server/speech.py \
  test/test_speech_pipeline.py

pydocstyle \
  malbut_agent_server/speech.py \
  test/test_speech_pipeline.py
```

2026-08-13 최종 결과: **28 passed**

검증 범위:

- raw audio·PCM·path·URI·waveform과 untrusted `user_id` 거절
- audio metadata unknown field와 범위 오류 거절
- NaN·Infinity confidence와 잘못된 sequence·epoch 거절
- partial 뒤 같은 ID의 final 허용
- partial·낮은 confidence·binding mismatch·self-echo·stale에서 provider 0회
- 활성 TTS 중 추가 final transcript 차단
- SafetyPolicy가 Tool 제안을 거절했을 때 최종 refusal만 TTS에 전달
- TTS와 대화 DB에 audio metadata가 저장되지 않음
- final 중복의 provider 재호출 0회와 변조 충돌
- barge-in 취소 1회와 새 capture epoch
- TTS terminal·늦은 feedback·self-echo fence
- 음성·대화 세션 종료, 활성 TTS 취소와 늦은 transcript 차단
- audit projection에서 transcript와 speaker 원문 제외

이번 시험의 stale transcript는 `sequence`와 `capture_epoch`만 대상으로 한다.
`source_timestamp_ns` freshness는 아직 시험·승인하지 않았다.

전체 패키지 시험은 다음으로 회귀를 확인한다.

```bash
PYTHONPATH=. python3 -m pytest -q test
```

2026-08-13 최종 통합 워크트리 결과: **237 passed in 8.25s**

## 10. 실제 연결 전 미결정 사항

다음 항목은 오프라인 코드에서 임의로 운영 확정하지 않았다.

1. **speaker→user 신뢰 adapter**

   얼굴·화자 인식 결과 중 어느 계층이 `speaker_id`와 `user_id` 결속을
   승인할지, 낮은 인식 신뢰도에서 익명 대화만 허용할지 결정해야 한다.

2. **confidence와 발화 길이**

   기본 confidence `0.75`, 최대 30초와 2,000자는 임시 제한이다. 실제 STT의
   confidence calibration과 한국어 데이터로 threshold를 정해야 한다.

3. **ROS interface package와 QoS**

   `malbut_interfaces`의 Transcript message, Speak Action, VAD event 타입,
   reliability·durability·deadline QoS를 공동 소유자와 확정해야 한다.

   source timestamp의 clock domain, clock skew, 최대 age와 미래 timestamp
   허용 오차도 이때 확정해야 한다. 현재 코드는 timestamp의 타입·범위만
   검증하며 freshness 판단에는 사용하지 않는다.

4. **TTS lifecycle**

   accepted, playback-started, completed, failed, cancelled, timed-out 상태와
   늦은 결과 무시, 취소 응답 deadline을 Action adapter에서 구현해야 한다.

5. **실제 self-echo·barge-in**

   AEC, TTS reference stream, VAD source 신뢰, 취소 후 speaker 출력 정지와
   다음 수음 epoch 개시 시점을 실장치에서 검증해야 한다.

6. **보존과 삭제**

   확정 transcript와 assistant text의 보존 기간, 사용자 삭제 SLA, 백업 삭제,
   운영 trace 접근 권한을 승인해야 한다.

7. **latency와 품질**

   STT finalization, Agent, TTS goal, first audio와 cancel latency를 분리 측정하고
   한국어 WER·의도 정확도·false barge-in을 실제 데이터로 평가해야 한다.

8. **동시성·재시작 상태**

   현재 coordinator lock은 provider 호출 동안 유지된다. 따라서 느린 provider
   추론 중 들어온 barge-in·close는 추론이 반환될 때까지 기다린다. 실제 음성
   통합 전에는 per-session in-flight token과 두 단계 commit으로 provider 호출
   중 lock을 놓고, 돌아왔을 때 epoch/closed 상태가 바뀌었으면 TTS를 만들지 않는
   구조가 필요하다.

   transcript/activity/TTS terminal cache도 process-local이다. 재시작 뒤 같은
   speech session ID를 재사용하면 TTS lifecycle exactly-once가 보장되지 않는다.
   운영에서는 새 session ID를 강제하거나 durable speech/TTS ledger를 구현해야
   한다. 외부에서 conversation이 먼저 close·expire·delete된 경우도 typed speech
   rejection으로 변환하는 service integration이 필요하다.

## 11. 완료 판정

| 조건 | 현재 판정 |
| --- | --- |
| strict text·metadata schema | 구현 |
| trusted identity/session binding | 구현 |
| final-only·confidence·ordering gate | 구현 |
| raw audio/path/URI 비수용·비저장 | 구현·오프라인 검증 |
| Agent correlation과 중복 방지 | 구현·오프라인 검증 |
| 최종 Safety 응답의 text-only TTS | 구현·오프라인 검증 |
| self-echo fence·barge-in cancel 계약 | 구현·오프라인 검증 |
| session close와 늦은 입력 차단 | 구현·오프라인 검증 |
| 실제 STT·TTS·ROS bridge | 미구현 |
| 실장치 echo·latency·음성 품질 시험 | 미실시 |

따라서 SWM25-76의 현재 정확한 명칭은 다음과 같다.

> **실제 음성 I/O가 없는 안전한 오프라인 음성 대화 계약 MVP.**

실제 음성 파이프라인 완료 처리는 위 미결정 사항과 ROS adapter, 실장치 시험을
통과한 뒤에만 가능하다.
