# 음성 요청의 수명, 입력 정책과 복구

이 문서는 최신 소스의 동작 계약이다. 실제 배포 버전이나 마이크의 AEC 품질을 증명하는
자료가 아니다. Python 설정과 모델 진단만으로 사용자가 관찰한 실제 무음을 부정할 수 없다.

## 입력과 응답

- 호출어는 `제이크`, `제이크야`다. 선두 호출어 뒤 공백·구두점으로 구분된 명령도 한
  발화에서 처리한다. 예를 들어 `제이크야, 지금 몇 시야?`는 명령 부분을 한 번 전달한다.
  이름의 중간 언급·조사 결합·전체 명령 인용은 호출로 받아들이지 않는다. 의미 전체를
  이해하는 호출어 판별기는 아니며 따옴표 없는 간접화법까지 판별한다고 보장하지 않는다.
- 호출어 캡처 한도는 6초다. 초과한 음성을 잘라 명령으로 실행하지 않는다. 긴 명령은
  호출어를 말한 뒤 실제 입력 준비가 된 다음 말한다.
- VAD는 20ms 프레임 4개의 음성 증거(기본 80ms)를 요구한다. 자격을 확인하는 동안
  비음성 한 프레임은 보존하지만 두 번째 비음성 프레임이나 busy 경계에서는 초기화한다.
  첫 음성과 pre-roll은 유지한다. 한두 프레임짜리 클릭을 바로 명령으로 받지 않는다.
  따라서 매우 짧은 실제 음성도 아직 제외될 수 있으며 실음성·잡음을 함께 평가해야 한다.
- 호출음 재생 중 PCM은 버린다. AEC 선언 입력은 호출음 drain 뒤 별도 300ms guard를
  생략하고, 기존 read를 넘겨 다음 fresh capture read가 시작됐을 때 `input_ready`를
  기록한다. 비AEC 입력은 300ms 잔향 guard를 유지한다. 이 경계는 캡처 루프의 read epoch이며
  물리 ADC 시각이나 장치 내부 버퍼를 측정한 결과가 아니다.
- 실제 `input_ready` 뒤 일반 발화 시작은 기본 5초 안에 확인되어야 한다. 시작하지
  않거나 최종 인식이 비거나 실패하면 로컬 삐빅 한 번 뒤 새 호출어를 기다린다. 사용자
  오류 문장을 Agent로 보내지 않는다. 실패음·종료 수신음의 기존 잔향 guard는 유지한다.

일반 최종 전사를 보내면 해당 요청의 응답 대기 gate를 닫는다. 기본 5초 동안
`SpeechRequestStatus.accepted` 등의 접수 증거를 기다리고, 전사 발행 시점부터 전체
120초 동안 최종 TTS 상태를 기다린다. 접수·재생 시작·중간 안내가 전체 시간을 연장하지
않는다. 같은 request의 non-interim finished/failed/stopped는 재생 시작 전이라도 대기를
종료한다. 다른 request나 중간 안내의 종료는 현재 요청을 해제하지 않는다.

접수·전체 응답 timeout 또는 거절·실패·취소 시 해당 request ID만 Agent와 TTS 양쪽에
취소한다. 아직 도착하지 않은 전사·TTS 요청도 ID로 취소 예약한다. Agent는 대기 중·
진행 중·이미 drain한 응답의 뒤늦은 발행을 막고, TTS는 같은 request의 interim/final을
막는다. 취소 접수 전에 이미 실행된 외부 작업을 되돌리는 기능은 아니다. 재전송하거나
임의의 로봇 행동을 수행하지 않는다. 유효한 최종 사용자 승인 정책은 변경하지 않는다.

취소 이력은 최근 256개이며 실행 중인 취소 요청은 완료 전 보호한다. STT의 종료 요청
격리는 최대 256개·5분이다. 새 요청에는 항상 새 UUID를 사용해야 한다. 늦은 상태가 새
request/session을 덮어쓰지 않으며 실제 오래된 재생이 관측되면 그 playback ID만 정지한다.
실제 재생 중임이 알려진 비AEC 입력은 정지 접수만으로 열지 않는다. 매칭 종료 상태 또는
TTS 취소 응답의 `quiescent=true`로 해당 요청의 정리 완료를 확인하고 잔향 guard를 거친다.
종료 토픽이 유실돼도 TTS 서비스가 정리를 확인하면 복구할 수 있다. 다른 요청의 재생은
해제하지 않으며, 서비스·장치 복구 실패는 추가적인 운영 장애로 남을 수 있다.

## 설정별 정책

| 상태 | 정책 |
| --- | --- |
| 로봇 통합 bringup/cloud | AEC 선언 기본 true. standalone speech/node/YAML 기본 false. 명시 false override 가능 |
| 일반 요청 답변 대기 | AEC 여부와 관계없이 추가 요청 차단. deadline과 매칭 terminal로 복구 |
| 일반 reply gate가 없는 재생과 입력 | 기존 AEC 기반 pause·대상 판정·stop/resume 조건 사용 |
| 확인 질문 재생 중 | AEC=true라도 확인 청취 세션을 열지 않음 |
| 확인 질문 최종 finished 후 | 새 세션을 열고 ACK 뒤 답변 시작 10초 대기. 질문 failed/stopped는 무응답으로 세지 않음 |
| 웹 말하기 | STT 입력 gate와 TTS quiet lease를 모두 적용. 실제 작업 정리 확인 후에만 시작 승인 |
| 실패음·종료 수신음 | 기존 재생 중 차단과 종료 후 300ms guard 유지 |

`input_has_aec`는 하드웨어·OS가 이미 처리한 입력이라는 선언이다. 에코 제거 기능을
설치하거나 켜지 않으며, 현재 하드웨어가 이 선언을 만족하는지는 별도 검증이 필요하다.
일반 답변 전체를 언제든 끊는 정책은 이 변경에 포함하지 않는다.

웹 quiet 서비스는 lease를 먼저 설치해 새 TTS 요청을 막고 현재·대기 재생을 중단한다.
active 작업·출력 정리·중단 작업이 모두 끝난 뒤에만 ACK한다. STOP/STOP_ALL의 접수와
quiet ACK를 구분한다. 장치 stop/close가 끝나지 않거나 오류가 나면 quiet 성공을 만들지
않는다. TTL과 서비스 제한시간, lease 교체·오래된 응답을 검사하고 이미 버린 말은 재생하지
않는다. 종료·만료 뒤 STT는 300ms guard를 유지한다. 실제 장치가 정지를 무시하는 경우의
물리적 무음까지 소프트웨어 테스트가 증명하지 않는다.

## 제한시간과 감시

| 항목 | 기본 | 의미 |
| --- | --- | --- |
| `start_timeout_s` | 5초 | 호출 뒤 실제 청취 개방부터 발화 시작까지 |
| `request_receipt_timeout_s` | 5초 | 일반 전사 전달부터 접수 증거까지 |
| `reply_timeout_s` | 120초 | 일반 전사 전달부터 최종 재생 종료까지, 접수 뒤 연장 없음 |
| `stt_decode_timeout_s` | 30초 | cpp native 호출별 협력적 취소 |
| `inference_timeout_s` | decode timeout + 5초 | 각 ASR 작업의 독립 실제 시간 감시; 기본 35초 |
| supervisor heartbeat | 1초 송신 / 10초 유실 | READY 이후 owner poll 생존 감시 |
| supervisor 시작 제한 | 120초 | 초기화·CUDA OOM 재시도까지 포함한 총 시작 한도 |
| STT respawn | 종료 후 5초 | STT만 재시작, Agent/TTS 동료 유지 |

모든 사용자 제한시간은 유한한 양수로 검증한다. decode가 취소를 무시해도 owner가
실제 시간 한도로 중단을 요청한다. owner나 cleanup까지 멈추면 별도 supervisor가
STT의 프로세스 그룹만 TERM 후 1초 대기, 필요하면 KILL하고 회수한다. 정상 종료 시
재시작하지 않도록 기존 launch 종료 조건을 유지한다. 독립 Agent/TTS나 로봇 미션을
이 감시자가 함께 종료하지 않는다. 이 보호는 supervisor를 켠 통합 speech launch 경로에
해당하며 단독 노드 실행만으로 외부 프로세스 감시가 생기지 않는다.

## 무음 전사 방어와 한계

기본 0 PCM 차단과 VAD에 더해, 모델이 **진짜** `no_speech_prob`와 `avg_logprob`를
제공하면 `no_speech_prob > 0.6 AND avg_logprob < -1.0`인 구간을 제외한다. 하나만
나쁘거나 정보가 없고 잘못된 경우에는 무음이라고 단정하지 않는다. 특정 문구
blacklist나 임의 음량 기준으로 `구독과 좋아요`만 지우지 않는다.

의심 구간 사이의 정상 텍스트와 timestamp는 보존한다. 제외한 tail의 유효한 시간 범위는
실제 누락과 구분하되 PCM 해제 기준으로 쓰지 않는다. 불연속·잘못된 시간·아직 설명되지
않은 음성 tail은 기존 누락 검사를 유지한다. 모든 구간이 제외되면 이전 committed prefix를
성공한 최종 결과로 되살리지 않는다.

CT2/MLX에서 정확한 점수가 있으면 추가 필터가 적용된다. upstream decoder가 이미 같은
필터를 적용한 경우 결과가 달라지지 않을 수 있다. cpp ABI3에는 선택적인 실제 no-speech
getter를 추가했고 구 브리지와 호환한다. 공개 API에는 decoder 평균 logprob가 없으므로
token 평균을 같은 값이라고 속여 쓰지 않는다. 따라서 주력 cpp의 새로운 외부 confidence
차단 효과는 보장하지 않으며 기존 Whisper 내부 필터를 유지한다. 로봇의 실제 무음
환각률·약한 발화 누락률은 녹음 자료와 모델 평가 없이 해결 완료로 분류할 수 없다.

## 원문 없는 기본 진단

- `stt_runtime_config`: 설치된 Python 소스 fingerprint, backend, 유효 설정·제한시간.
  `MALBUT_SOURCE_SHA`는 유효한 40자리일 때만 **검증되지 않은 빌드 라벨**로 표시한다.
  git HEAD를 실행 바이너리 버전이라고 추정하지 않는다.
- `speech_lifecycle`: 발화 started/terminal과 request published/accepted/terminal,
  gate·busy·취소·overflow·빈 결과·오류 등의 사유와 ID. 허용되지 않은 gate 아래 PCM을
  실제 사용자 발화라고 꾸며내지 않는다. Agent 오류 안내용 input status와 별개다.
- `input_ready`: 효과음 guard와 fresh capture 경계를 통과한 실제 소프트웨어 입력 개방.
- confidence 진단: 거부 사유·숫자 점수·metadata 제공 여부·집계. 원문을 포함하지 않는다.

기본 진단은 실제 대화·PCM·키·모델의 사적인 절대경로를 기록하지 않는다. 기존
`MALBUT_STT_DIAGNOSTIC_DIR`를 명시하면 원음·전사 재현 파일을 저장하는 별도 opt-in
기능이므로 구분해서 사용한다. 이번 검증에서는 실제 마이크·스피커·유료 API·로봇을
사용하지 않았다.

## 오프라인 검증과 배포 준비

새 회귀는 `test_reply_lifecycle.py`, `test_wake_command.py`, `test_onset_dropout.py`,
`test_confidence.py`, `test_runtime_identity.py`, `test_speech_profile_contract.py`,
TTS `test_request_quiet.py`·`test_quiet_node.py` 및 기존 node/Agent/프로세스 테스트에 있다.
실패 사례부터 재현한 뒤 수정했으며 fake 장치·모델·ROS와 로컬 child process로 검증한다.
테스트 통과는 실제 AEC·CUDA hang·ROS DDS·음성 품질의 실기 통과와 다르다.

새 `SpeechRequestStatus`와 `CancelSpeechRequest` 정의를 포함해 `malbut_interfaces`,
STT·Agent·TTS를 같은 소스로 함께 빌드해야 한다. 원본과 `malbut_test` runtime 복사본의
정합성을 테스트한다. 이 작업에서는 빌드 결과를 로봇에 배포하거나 재시작하지 않는다.
