# Malbut STT → Agent

ROS STT 노드는 로컬 Whisper로 **“제이크” 또는 “제이크야”**를 확인한 뒤
일반 발화 하나를 수집합니다. Agent에 전달한 뒤에는 최종 답변 종료까지 입력을
차단하고, 다음 요청에는 다시 호출어가 필요합니다. 호출어와 문장 전사는 모두
로컬에서 실행하며 STT 노드에는 `OPENAI_API_KEY`가 필요하지 않습니다.
끼어든 발화의 대상 판단은 별도 Agent가 최근 대화 문맥으로 수행합니다.
Agent를 OpenAI로 실행하면 대상 판단과 답변 생성에는 모델 API를 사용합니다.

```mermaid
flowchart LR
    M["연속 마이크 입력"] --> W["로컬 호출어 인식"]
    W --> S["대화 모드·로컬 문장 전사"]
    S -->|"일반 발화: SpeechTranscript"| A["Agent 대화 처리"]
    S -->|"ClassifySpeechAddressee 요청"| C["Agent 발화 대상 판단"]
    C -->|"판정 응답"| S
    S -->|"ControlSpeechPlayback 요청"| G["Manager"]
    G --> T["TTS"]
    T -->|"접수 응답 · 실제 재생 상태"| G
    G -->|"접수 응답 · SpeechPlaybackStatus"| S
```

## 통신과 동작

| 방향 | 방식 | 이름 | `malbut_interfaces` 타입 |
|---|---|---|---|
| STT → Agent | Topic | `/malbut/speech/transcript` | `msg/SpeechTranscript` |
| STT → Agent 요청·응답 | Service | `/malbut/speech/classify_addressee` | `srv/ClassifySpeechAddressee` |
| STT → Manager 요청·응답 | Service | `/malbut/speech/playback_control` | `srv/ControlSpeechPlayback` |
| Manager → STT | Topic | `/malbut/speech/playback_status` | `msg/SpeechPlaybackStatus` |

- 필드·상수 원본: [ROS 메시지](../malbut_interfaces/msg), [ROS 서비스](../malbut_interfaces/srv)
- 사용 명세: [STT 명세](docs/stt_agent.md)
- Topic QoS: `RELIABLE`, `VOLATILE`, `KEEP_LAST`, depth `10`. Service는 ROS 기본 Service QoS를 사용합니다.

STT는 발화마다 UUID를 새로 생성합니다. 같은 문장을 다시 말해도 새 ID를
사용하며, 전사 결과의 앞뒤 공백을 제거하되 요약이나 명령 변환은 하지 않습니다.
중간 인식 결과·오류 문장·빈 원문은 발행하지 않습니다.

호출어와 명령 수집은 기본적으로 VAD가 20ms 프레임 4개(80ms)를 연속해서
음성으로 판정해야 발화로 시작합니다. 짧은 클릭·잡음 후보는 인식기에 보내지
않으며 발화 ID, 시작·실패 알림, 수신음을 만들지 않습니다. 중간에 비음성
프레임이 끼면 연속 판정을 처음부터 셉니다. 시작을 확인하는 동안 첫 음성과
그 이전 pre-roll은 보존합니다. 정상적으로 다시 말한 같은 문장은 각각 전달합니다.
이 조건을 통과하는 지속적인 잡음이나 모델 오인식까지 제거하는 보장은 없으며,
실제 마이크에서 짧은 응답과 잡음을 함께 확인해야 합니다.

`waiting_for_wake`에서 호출어만 부르고 쉽니다. 종료 무음 0.4초·최대 발화 6초로
수집하며, 공백·구두점을 제외한 전체 전사가 `제이크` 또는 `제이크야`이면
통과합니다. 호출어와 명령을 한 문장으로 이어 말하는 방식은 지원하지 않습니다.
`wake_detected` 뒤에는 호출어 없이 말합니다. ROS 노드는 기본 0.8초 무음에서
후보 전사를 시작하고, 명확한 한국어 종결 표현이면 1초 무음 이후 발화를 확정합니다.
따라서 실제 조기 확정은 1초 무음과 후보 추론 완료 중 늦은 시점입니다.
판단이 불확실하면 기본 2초 무음을 기다리며, 그동안 새 음성이 없으면 같은 전사를 재사용합니다.
후보 추론 중 다시 말하면 같은 발화에 이어 붙이고 이전 후보로 종료하지 않습니다.
마이크는 계속 열어 두고 별도 작업자에서 로컬 추론을 수행합니다. 원본 음성을
파일에 자동 저장하지 않습니다. 기본 모델 실행은 CPU int8입니다. 호출어와 문장의
모델 디렉터리가 같으면 심볼릭 링크 별칭까지 확인하여 모델 하나를 공유합니다.
ROS의 `compute_type` 파라미터로 `float32`를 선택할 수 있습니다.

CPU/CUDA 로컬 Whisper의 연속 대화에서는 말하는 동안에도 약 2초마다 중간 전사를
시도합니다. 인식이 진행 중이면 최신 음성만 다음 후보로 유지하며, 중간 결과는
Agent Topic이나 발화 대상 판정 Service에 전달하지 않습니다. 노트북 `--dialogue`
출력의 `partial_transcript`에서 수정 가능한 중간 문장을 확인할 수 있습니다.
연속된 결과에서 일치하는 앞부분은 텍스트로 확정하고, 확정된 경계 이전의
오래된 PCM은 해제합니다. 마지막 확정 구간의 음성은 문맥으로 남겨 후속 인식과
겹쳐 확인합니다. 연결이 불확실하면 현재 남아 있는 음성 구간만 다시 인식하고,
이미 해제한 음성에 대응하는 확정 텍스트는 유지합니다. 말하는 중의 결과만으로
종료하지 않고, 무음 뒤 실제 끝부분 음성을
반영한 결과를 1초/2초 기준에 따라 한 번 전달합니다. 중간 인식 실패는
현재 발화를 버리지 않으며, 최종 인식 실패만 기존 실패 처리로 이어집니다.
MLX 선택 경로와 한 문장 실행기는 기존 전사 방식을 사용합니다.

일반 전사를 Agent에 전달하면 답변 준비와 TTS 재생 중 새 발화·호출어를 모두
버립니다. 같은 `request_id`의 최종 답변(`interim=false`)이 `finished`, `failed`,
`stopped`에 도달하면 호출어 대기로 돌아갑니다. 재생 전 실패도 포함하며,
중간 안내나 다른 요청의 종료는 입력 차단을 해제하지 않습니다. 재생 후 호출어
없는 5초 후속 발화 대기는 열지 않습니다. Agent 주도 확인 세션의 정책은 유지합니다.

호출어 인정 뒤에는 알림음과 잔향 차단이 끝난 시점부터 `start_timeout_s`(기본 5초)
안에 발화 시작이 확인되어야 합니다. 무발화·최종 빈 전사·인식 오류는 Agent에
전사·`FAILED`를 보내지 않고 로컬 이중 비프(90ms 두 번, 중간 무음 60ms) 한 번 후
호출어 대기로 돌아갑니다. 비프 재생 중과 종료 후 300ms 동안 AEC 여부와 관계없이
입력과 대기 오디오를 버립니다. 중복·늦은 결과는 비프나 전사를 반복하지 않습니다.
정상 전사의 답변 대기, 확인 세션, 웹 말하기 정책은 유지합니다. 기존 VAD를 통과한
비어 있지 않은 Whisper 환각의 별도 품질 판정은 없어 이 변경만으로 차단하지는 못합니다.

### STT 재현 기록 (기본 OFF)

필요한 재현 실행에만 `MALBUT_STT_DIAGNOSTIC_DIR`를 로컬 폴더로 지정합니다.
예를 들어 `export MALBUT_STT_DIAGNOSTIC_DIR=/tmp/malbut-stt-repro` 뒤 같은 환경에서
음성 노드를 시작합니다. 이미 실행 중인 노드에는 적용되지 않습니다. 다음 실행에서
끄려면 `unset MALBUT_STT_DIAGNOSTIC_DIR`를 사용합니다.

이 모드는 로컬 모델에 **실제 전달한 입력 구간**을 `decode-*.wav`로 저장하고,
같은 이름의 JSON에 입력 해시, 원시 segments, 모델 인자, 발화·세션 ID와 추론 세대,
부분/종료 추론 구분을 기록합니다. incremental 추론이 앞부분을 잘랐다면 잘린 뒤의
실제 입력이 저장됩니다. `events.jsonl`에는 호출어·입력 허용 경계, 발화 시작,
추론 결과와 후처리 오류 상세가 들어갑니다. `speech_end_s`는 수집기의 마지막
음성 판정 시점이며 원음이나 사람의 발화 여부를 대신하는 증거는 아닙니다.

파일은 로컬에만 저장하며 음성과 전사 원문을 포함합니다. 한 STT 프로세스 기준
기존 진단 파일까지 합쳐 64MiB에 도달하면 추가 기록을 중단하고 기존 파일을 보존합니다.
저장 실패도 STT 결과를 바꾸지 않으며 `diagnostics_stopped`로 알립니다. 녹음·파일 쓰기
부하가 생기므로 재현 구간에만 켭니다. 이 옵션 자체는 오인식 필터가 아닙니다.

### 문제 문구 감지 시 직전 과정 자동 저장

`stt`를 실행하면 별도 명령이나 환경변수 없이 실행 기록 수집이 시작됩니다.
최종 STT 발행 또는 짧은 음성 품질 검사 거절 시 조사 대상 문구가 일치하면
**직전 30초와 이후 15초**의 관측 기록을 자원 로그 폴더 아래
`~/.ros/malbut/resource_logs/stt_incidents`에 저장합니다. 기본값은 음성 파일을 저장하지
않는 로그 전용 모드입니다. 기존 8766 뷰어의 **STT 실행 추적**을 열면 바로 조회할 수
있으며, 뷰어를 나중에 열어도 이미 완료된 기록이 표시됩니다. STT 자체는 실행 중이어야
수집되며, 뷰어가 마이크나 별도 STT 프로세스를 시작하지는 않습니다.
핵심 산출물은 발화 한 건의 실행 경로인 `transactions.json`입니다. 작업 큐 입출력,
모델 호출과 원시 결과, 결과 큐 입출력, 채택·폐기·종료 후보 재사용 이유, 최종 발행,
Agent 수신·답변 로그와 TTS 상태를 ID로 연결합니다. `--no-audio`로 실행하면
PCM을 버퍼에 보관하거나 WAV로 저장하지 않고 실행 이벤트와 모델 메타데이터만 남깁니다.
STT 문장, VAD 판정, 호출어 정책, Agent 전달 여부를 변경하지 않습니다.
정상적으로 말한 문구도 일치할 수 있으므로 저장 사건을 오인식 확정으로 해석하지 않습니다.

기본 트리거는 `구독`, `시청해주셔서`, `자막제공`을 포함하는 문장과,
전체 문장이 `감사합니다`, `고신을주셔서감사합니다`, `다음편에참아`인 경우입니다.
비교할 때만 공백·문장부호를 제거하고 Unicode NFC를 적용하며, 저장되는 원문은 보존합니다.
일반 부분 인식, 호출어 인식 결과와 TTS 문장 자체는 저장 트리거가 아닙니다.
품질 검사에 걸린 문구는 Agent에 보내지 않아도 저장하며, `triggers[].event`가
`recognition_rejected`로 표시됩니다. 발행 여부는 별도의 관측 상태로 확인합니다.

ROS나 마이크 없이 저장 경로부터 확인하려면 저장소 루트에서 실행합니다.

```bash
PYTHONPATH=malbut_stt python3 -m malbut_stt.incident_cli \
  --demo --no-audio --output /tmp/malbut-stt-incident-demo
```

이 데모의 파형, 모델 결과, 큐·Agent 이벤트는 모두 합성이며 실제 재현 실험이 아닙니다.
`incident.json`에 `metadata.synthetic=true`와 조기 종료 상태가 기록됩니다.

설치한 패키지로 단독 STT 테스트를 실행할 때는 기존 `stt` 대신 아래 진입점을 사용합니다.
`--` 뒤의 인자는 기존 STT/ROS에 그대로 전달됩니다. 필요한 ROS 환경과 모델·라이브러리는
기존 STT 실행과 동일하게 준비해야 하며, 이미 실행 중인 STT와 중복 실행하지 않습니다.

```bash
ros2 run malbut_stt stt_incident \
  --no-audio \
  --output "$HOME/.ros/malbut/resource_logs/stt_incidents" \
  --pre-seconds 30 --post-seconds 15 \
  -- --ros-args -p backend:=whisper_cpp \
  -p stt_model_path:="$HOME/.cache/malbut_speech/models/ggml-small.bin" \
  -p stt_library_path:="$HOME/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so"
```

일반 Bringup 실행에는 추가 설정이 필요 없습니다. 저장 위치나 관측 시간을 바꿀 때만
시작 전에 아래 환경변수를 지정합니다. 현재 실행 중인 노드에는 적용되지 않습니다.
연속 저장 모드인 `MALBUT_STT_DIAGNOSTIC_DIR`를 지정하면 자동 사건 수집을 대신합니다.
사건 설정과 연속 저장을 둘 다 명시적으로 켜면 시작을 거부합니다.

```bash
export MALBUT_STT_INCIDENT_CONFIG='{"directory":"/tmp/malbut-stt-incidents","pre_seconds":30,"post_seconds":15,"save_audio":false}'
# 이후 평소 사용하는 bringup 명령으로 테스트 실행
```

다음 실행에서 수집을 끄려면
`export MALBUT_STT_INCIDENT_CONFIG='{"enabled":false}'`를 사용합니다.
`unset MALBUT_STT_INCIDENT_CONFIG`는 기본 자동 수집으로 되돌립니다.
CLI의 `--contains`, `--exact`는 해당 기본 목록을 대체하고 여러 번 지정할 수 있습니다.
모든 기본 목록을 끄려면 `--no-default-triggers --exact '조사할 문장'`을 사용합니다.
원음도 비교하려면 `--no-audio`를 생략하거나 환경변수 JSON에 `"save_audio":true`를 지정합니다.
로그 전용 모드도 대화 원문은 포함합니다.

| 저장 파일 | 내용 |
| --- | --- |
| `incident.json` | 트리거 원문·발화 ID, 실제 관측 시간 범위, 모델·입력 설정, 누락·종료 정보 |
| `transactions.json` | 발화 ID별 실행 단계·관측 시각·트리거 기준 경과 시간·큐/폐기 이유와 원본 이벤트 줄 번호 |
| `rosout.jsonl` | STT·Agent·TTS 노드의 ROS 실행 로그: 원문, 로거, 레벨, 원본 시각, 파일·함수·줄 번호 |
| `microphone.wav` | 음성 저장 모드만: 게이트로 버린 입력까지 포함한 마이크 PCM16/16kHz |
| `microphone_chunks.jsonl` | 각 청크의 수신 시각, 원본 샘플 위치, WAV 위치, 차단·busy·overflow 상태 |
| `events.jsonl` | 호출어·VAD·큐·추론·결과 처리·STT 발행·ROS 로그·TTS 요청과 재생 상태의 전체 관측 이벤트 |
| `decodes/decode-*.wav` | 음성 저장 모드만: 호출어/부분/종료 추론 각각에서 모델에 실제 전달한 오디오 |
| `decodes/decode-*.json` | 입력 해시·인자·원시 세그먼트·오류, 발화 ID·generation, 입력/결과 확보 여부 |

`utterance_id`와 TTS의 `request_id`를 연결하고, `playback_id`로 재생 상태를 추적합니다.
Agent 수신은 `malbut_agent_communication`의 `speech_transcript/status=received` 로그를,
답변 발행은 `dialogue_response_published` 로그를 실제로 관측했을 때만 표시합니다.
`observed=false`는 보관된 구간에서 보지 못했다는 뜻이며, 실행 실패나 미실행을 뜻하지 않습니다.
ROS 발행 자체를 Agent 수신 성공으로 취급하지 않습니다. `/rosout`은 테스트 시작 이후
`malbut_stt`, `malbut_agent_communication`, `malbut_tts` 로거를 수집합니다. ID 없는 경고는
전체 로그에 남기고 특정 발화에 임의로 붙이지 않습니다. 이름을 바꾼 노드나 ROS 로그 발행이
꺼진 노드의 로그, 네이티브 stderr, SQL BEGIN/COMMIT, LLM 내부 프롬프트·실행은 수집하지 않습니다.
로그의 수신 순서는 원격 실행 순서와 다를 수 있으므로 원본 시각도 함께 남깁니다.
로그 한 건은 UTF-8 16KiB로 제한하며 넘으면 `truncated=true`로 표시합니다.
whisper.cpp ABI 4 연결부는 `decode_result.native_decode.windows`에 실제 디코더의
`no_speech_prob`, `avg_logprobs`, 적용 기준, 무음 제외 여부, 다음 디코딩 위치를
기록합니다. 문장이 제외된 창도 포함하며 추론당 최대 64개 창을 보관합니다.
이는 세그먼트 토큰으로 재계산한 점수가 아니라 무음 판정에 사용한 원래 값입니다.
`save_audio=false`에서도 기록하며 뷰어의 **STT 실행 추적 → Whisper 원시 결과**에서
확인할 수 있습니다. ABI 3 라이브러리는 전사를 계속 수행하되 점수는
`unavailable/bridge_abi_3`로 표시합니다. 새 관측 기능에는 [네이티브 재빌드](native/README.md)가
필요합니다. 기존 사건의 미수집 점수는 복원하지 않습니다.

선택적으로 `MALBUT_STT_SILERO_GATE_MODEL=/absolute/path/to/silero-v6.2.3.onnx`를
설정하면 실제 Whisper 호출 직전에 Silero 검사를 수행합니다. 점수 0.5 이상의 음성이
연속 80ms 미만이면 일반 발화의 중간·최종 인식 입력을 Whisper에 보내지 않습니다.
호출어 인식은 Silero 차단에서 제외하고 기존 WebRTC·Whisper 경로를 유지합니다.
통과한 오디오는 원본 그대로 전달하며, `MALBUT_STT_SHADOW_VAD_MODEL` 관찰 모드와는
별개입니다. 짧거나 작은 목소리가 거절될 수 있으므로 실제 마이크 시험이 필요합니다.
설치·활성화·로그·오류 동작은 [Silero 적용 안내](tools/README_VAD.md)를 확인하세요.

저장한 점수의 무음 제외 조건을 일괄 확인하거나 동일 WAV에 WebRTC/Silero VAD를
비교하려면 [오프라인 비교 도구](tools/README_VAD.md)를 사용합니다. 원음 없는 사건은
판정 로그만 분석할 수 있습니다. VAD 비교는 별도 환경에서 실행하며 운영 설정을 바꾸지 않습니다.

같은 안내의 **Observe the existing microphone** 절은 실제 마이크 입력의 관찰 모드입니다.
`MALBUT_STT_SHADOW_VAD_MODEL`로 켜면 Silero와 관찰용 WebRTC 점수를 기존 사건 로그에
추가하고 뷰어의 **STT 실행 추적 → VAD**에 표시합니다. 실제 인식·입력 차단 판정은
기존 WebRTC가 계속 담당하며, 관찰 모드 자체는 WAV 저장을 켜지 않습니다.

평소에는 RAM에만 보관하고 파일 쓰기는 전용 스레드에서 수행합니다. 기본 버퍼는
페이로드 32MiB/12,000개 항목으로 제한하며, 저장 중/저장 대기 스냅샷이 각각 한 개씩
추가 메모리를 보유할 수 있습니다. Python 객체와 직렬화 작업 메모리는 별도입니다.
디스크는 기존 파일과 미완성 파일까지 포함해 기본 512MiB로 제한하며 자동 삭제하지 않습니다.
CLI에서 `--buffer-mib`, `--disk-mib`로 조절할 수 있습니다.

같은 발화 ID의 반복 트리거는 합치고, 첫 트리거의 15초 창 안에 발생한 다른 발화도 같은
사건에 기록합니다. 저장 대기가 가득 차면 새 트리거 누락을 `skipped_triggers_total`로,
버퍼 용량으로 밀려난 기록은 `buffer_evictions_total`로 표시합니다. 시작 직후나 버퍼 초과 시
30초 전체가 없을 수 있습니다. 마이크 청크 사이의 누락은 샘플 위치로 확인해야 하며,
연결된 WAV를 시간상 연속 녹음으로 가정하면 안 됩니다.

정상 종료 시 진행 중 사건도 저장하고 `post_window_complete=false`로 표시합니다.
강제 종료 시 아직 RAM에만 있던 기록은 남지 않을 수 있습니다. 완료된 폴더만 `incident-*`로
보이며, 쓰기 실패 중 남은 `.partial-*`는 미완성 증거입니다. 저장 실패는 원래 인식 결과와
ROS 발행을 유지하고 `stt_incident_stopped:<원인>`으로 알립니다. 저장 성공 시 `stt_incident_saved:<경로>`를
출력합니다. 기본 자동 수집에는 대화 원문과 처리 로그가 포함되며, 음성은
`save_audio=true`를 명시한 경우에만 추가 저장합니다.

일반 요청의 답변 대기 중에는 끼어들기를 받지 않습니다. 요청 전달 전에 다른
재생과 겹친 발화는 검증된 AEC 입력에서 기존 끼어들기 처리를 사용합니다.
현재 재생에 `pause`를 요청한 뒤 `utterance_id`, `playback_id`, `text`를
Agent Service에 보냅니다. Agent는 판정 발화를 일반 대화 기록에 추가하지 않고
`addressed`, `not_addressed`, `unknown` 중 하나를 응답합니다. ROS가 응답을 요청과
연결하고, STT는 요청에 보낸 두 ID로 현재 대기 중인 판정인지 확인합니다.
`addressed`는 기존 재생에 `stop`을 요청하고 같은 발화 ID로 일반 전사를 전달합니다.
`not_addressed`는 `paused` 확인 후 같은 재생에 `resume`를 요청합니다.
`unknown`이나 45초 판정 대기 초과는 새 전사와 자동 재개 없이 대화 모드를 종료합니다.
판정은 최근 생성한 답변을 참고하며 실제로 어디까지 들었는지는 알지 못합니다.

재생 제어의 `accepted` 응답은 접수 여부입니다. 실제 재생 상태는
`SpeechPlaybackStatus`로만 반영합니다. STT는 Service 응답을 비동기로 기다리며
마이크 처리와 상태 수신을 계속합니다. 응답 대기를 끝내더라도 원격 Service 실행이
취소되는 것은 아니며, 자동 재요청하지 않습니다.

ROS 노드는 호출 성공음과 발화 종료 수신음을 같은 로컬 출력 장치로 재생합니다.
호출음은 180ms 상승 2음, 수신음은 더 작은 150ms 단음입니다. 수신음은 발화 종료를
확정했을 때 한 번만 재생하며, 중간 인식·종료 미확정 상태의 쉼·폐기된 입력에는 재생하지 않습니다.
이는 음성 수집 완료 표시이며 내용 이해나 명령 실행의 성공을 뜻하지 않습니다.
재생 중과 종료 후 `chime_tail_s`(기본 0.3초)의 마이크 입력은 잔향 유입을 막기 위해 제외합니다. TTS가
재생·일시정지 상태이면 수신음을 생략하고, 수신음 출력 실패도 전사 전달을 막지 않습니다.
수신음 재생은 동기식이므로 최종 전사 전달에는 PCM 재생 150ms와 장치 처리 시간이 추가됩니다.
TTS 재생·중단·재개는 Manager가 중계합니다. 위 공개 Topic·Service는 Manager가 제공합니다.
이 `malbut_test` 배포본의 ROS STT 노드는 끼어들기를 임시로 꺼 둡니다.
`input_has_aec=true`로 실행해도 파이프라인에는 `false`를 적용합니다.
재생 종료 뒤 0.3초 동안 잔여 입력을 비운 후 다음 발화를 듣습니다.

일반 구현은 `input_has_aec=false`가 기본이며, 이때는 TTS 재생 중 마이크 입력을 버리고
`barge_in_requires_aec`를 기록하므로 끼어들기가 비활성입니다. `true`는 선택한
마이크가 이미 에코 제거된 입력을 제공한다는 설정이며, AEC를 구현하거나 켜는
옵션이 아닙니다. 최종 전사·대상 판정 처리 중 추가 발화는 종료 무음까지 버립니다.
무음 뒤 후보 전사 중에는 이어지는 음성을 계속 수집합니다. 일반 전사가
실패하거나 비어 있으면 Agent에 보내지 않고 위 실패 비프 후 새 호출어를 기다립니다.
이때 TTS의 대상을 추정하여 자동으로 재개하거나 중지하지 않습니다.

Agent의 `received` 로그와 SQLite 기록으로 실제 일반 전사 접수를 확인합니다. 늦게 시작한 Agent에
과거 발화를 재생하거나, 중단 중 유실된 발화를 자동 복구하는 기능은 없습니다.
이 Topic에는 STT→Agent의 별도 접수 응답이나 애플리케이션 재전송 기능이 없습니다.

## 노트북에서 먼저 시험하기 (ROS 불필요)

STT 노트북 실행기 `smoke`는 최종 원문을 터미널 JSON에 출력합니다.
`--dialogue --local`은 ROS 노드와 같은 `DialoguePipeline`으로 연속 수집과
1초/2초 발화 종료를 시험합니다. 이 CLI는 `endpoint_predecode_s`를 지정하지 않아
후보 전사를 1초 무음에서 시작합니다. `--dialogue`가 없으면 기존 한 문장용
`SpeechPipeline`을 사용합니다. 기본 명령 인식은 `OpenAITranscriber`이므로
**API 없이 시험하려면 `--local`을 명시합니다.** `--wake-only`도 API를 호출하지 않습니다.
이 실행기에는 ROS Topic 발행·Agent·TTS·로봇 동작이 연결되어 있지 않습니다.
`--dialogue`는 전사를 출력한 뒤 로컬 완료 처리하여 다음 호출을 받습니다.
`--manual`은 STT 진단용으로 다음 발화를 바로 수집하며, 실제 재생 완료를 검증하지 않습니다.

저장소 루트에서 전용 환경을 준비합니다. `.runtime`은 Git에서 제외됩니다.
macOS에서는 설치된 Python 3.12를 사용하고, 다른 환경에서는 해당 Python 명령으로
바꿉니다. OpenAI STT 시험에는 기존 `OPENAI_API_KEY`가 실행 터미널 환경에 있어야
하며, 아래 로컬 호출어 시험에는 필요하지 않습니다.

```bash
python3.12 -m venv .runtime/stt-laptop
.runtime/stt-laptop/bin/python -m pip install -r malbut_stt/requirements.txt
PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python -m malbut_stt.smoke --list-devices
```

### 로컬 연속 대화 시험: MLX 또는 CPU

**기본 선택은 CPU int8입니다.** MLX는 속도를 비교하기 위한 선택 옵션이며,
짧은 명령의 독립 평가에서 다른 언어 출력과 반복 오류가 확인되어 기본값으로
채택하지 않았습니다. 설정 선택 근거와 한계는
[로컬 Whisper 평가 보고서](docs/local_whisper_evaluation_2026-09-13.md)를 확인합니다.

현재 Apple Silicon Mac에서 선택할 수 있는 MLX 경로입니다. 기존 CPU 환경과
분리된 `.runtime/stt-mlx-laptop`을 사용하며, ROS 노드의 기본 backend는 바꾸지
않습니다. 환경이 아직 없다면 저장소 루트에서 한 번 준비합니다.

```bash
python3.12 -m venv .runtime/stt-mlx-laptop
.runtime/stt-mlx-laptop/bin/python -m pip install -r malbut_stt/requirements-mlx-laptop.txt
```

현재 내려받아 둔 MLX Whisper small 모델은
`.runtime/stt-autotune-20260913-2215/mlx-model`에 있습니다.
`config.json`과 `weights.npz`가 필요하며 실행 중 자동 다운로드하지 않습니다.
이 경로는 로컬 실험 자료이므로 새 체크아웃에는 포함되지 않습니다.
MLX는 Apple Silicon macOS에서만 선택하며 fp16으로 실행합니다.
`--backend mlx`와 `--compute-type`을 함께 지정하면 오류로 종료합니다.

먼저 장치 목록을 확인합니다. 현재 Mac 내장 마이크 번호는 `1`이며 장치 연결
상태에 따라 바뀔 수 있으므로 아래 실행 명령의 번호를 실제 목록에 맞춥니다.

```bash
env -u OPENAI_API_KEY -u PICOVOICE_ACCESS_KEY HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  PYTHONPATH=malbut_stt .runtime/stt-mlx-laptop/bin/python -m malbut_stt.smoke --list-devices
```

Enter를 한 번 누르고 연속 발화를 시험합니다.

```bash
env -u OPENAI_API_KEY -u PICOVOICE_ACCESS_KEY HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  PYTHONPATH=malbut_stt .runtime/stt-mlx-laptop/bin/python -m malbut_stt.smoke \
  --local --dialogue --manual --backend mlx \
  --model-path .runtime/stt-autotune-20260913-2215/mlx-model --device-index 1
```

`ready`에서 `mode: dialogue`, `backend: local`, `local_backend: mlx`를 확인합니다.
Enter 뒤 `listening`이 표시되면 말합니다. 다음 발화부터는 Enter나 호출어 없이
이어 말하며, 종료는 `Ctrl+C`입니다. 실제 마이크는 실행 동안 한 번 열어 둡니다.
1초 무음에서 후보를 인식하고 명확한 종결 표현이면 추론 완료 후 확정합니다.
불확실한 구절은 2초 무음을 기다립니다. 후보 처리 중 다시 말하면 이전 후보를
확정하지 않고 이어지는 음성을 수집합니다. 출력의 `transcript`에는 발화 ID와
원문만 표시하며, 종료 대기는 `checking_endpoint`, `endpoint_checked`,
`endpoint_finalized` 로그로 확인합니다. 이 모드는 기존 한 문장 실행기의
`transcription_s` 또는 전역 VAD 지연 값을 출력하지 않습니다.

호출어에서 시작하려면 `--manual`을 뺍니다.

```bash
env -u OPENAI_API_KEY -u PICOVOICE_ACCESS_KEY HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  PYTHONPATH=malbut_stt .runtime/stt-mlx-laptop/bin/python -m malbut_stt.smoke \
  --local --dialogue --backend mlx \
  --model-path .runtime/stt-autotune-20260913-2215/mlx-model --device-index 1
```

`waiting_for_wake`에서 “제이크야”만 말하고, `wake_detected` 뒤에 명령을 따로
말합니다. 전사가 출력된 뒤 다음 문장을 시험하려면 호출어를 다시 부릅니다. 호출어와 일반
발화는 모델 하나를 공유하되 이름 힌트는 호출어에만 적용합니다.

MLX를 사용하지 않을 때는 기존 CPU int8 환경으로 같은 연속 대화 파이프라인을
시험할 수 있습니다.

```bash
env -u OPENAI_API_KEY -u PICOVOICE_ACCESS_KEY HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python -m malbut_stt.smoke \
  --local --dialogue --manual --backend faster-whisper --compute-type int8 \
  --model-path .runtime/stt-laptop/models/whisper-small --device-index 1
```

CPU의 `float32`를 따로 비교하려면 마지막 명령의 `--compute-type int8`만
`--compute-type float32`로 바꿉니다. MLX 전용 모델과 faster-whisper 모델은
파일 형식이 다르므로 각각 지정한 디렉터리를 사용합니다.

마이크 없이 준비된 파일로 호출어와 한 요청의 전사·추가 입력 차단을 확인할 수도 있습니다.
`replay_local.py`는 **비압축 mono 16kHz PCM16 WAV**만 받으며, 파일을 실제 시간에
맞춰 파이프라인에 공급합니다. 마이크를 열거나 스피커로 파일을 재생하지 않습니다.
다음 명령은 현재 준비된 개발용 합성 시퀀스를 CPU int8로 처리합니다.
결과를 보존하려면 실행할 때마다 새로운 `--output` 파일명을 사용합니다.

```bash
env -u OPENAI_API_KEY -u PICOVOICE_ACCESS_KEY HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python malbut_stt/tools/replay_local.py \
  --backend faster-whisper --compute-type int8 --wake \
  --model-path .runtime/stt-laptop/models/whisper-small \
  --wav .runtime/stt-autotune-20260913-2215/wake_sequence/development_wake_two_followups.wav \
  --output .runtime/stt-replay/user-wake-check-01.json
```

`status: completed`는 파일 관측이 끝났다는 뜻이며 인식 성공을 보장하지 않습니다.
결과 JSON에서 `clean_shutdown`, `events`의 `wake_detected`, `transcript_count`,
`transcripts`의 원문을 확인합니다. 이 파일의 기대 결과는 호출어 검출 1회와
“창문을 열어 줘.”라는 전사 1개입니다. 이 재생 도구에는 답변 완료가 연결되어
있지 않아 이후 “저녁은 언제 먹을까?”는 차단되며, 호출어 자체도 전송하지 않습니다.
기존 개발 음성을 연결한 점검이므로 독립 평가나 실제
사용자 마이크 성능으로 해석하지 않습니다.

`--dialogue`에는 `--local`이 필수이며 `--once`, `--wake-only`를 함께 사용할 수
없습니다. 결과는 터미널에만 출력하고 원본 녹음을 자동 저장하지 않습니다.
이 CLI는 전사 출력으로만 로컬 완료 처리하므로 **Agent 답변 준비와 실제 TTS
종료까지 입력이 차단되는지는 ROS 연결 시험이 필요합니다.** `ready`의
`tts_completion_events`는 `false`입니다. 끼어들기와 실제 TTS 제어 역시 별도
연결 시험이 필요합니다.

### 1. Enter로 시작하는 한 문장 시험

호출어 모델 없이 마이크·VAD·STT를 먼저 확인합니다. `--device-index`는 위 목록에서
원하는 마이크 번호로 바꿉니다. 번호는 장치 연결 상태에 따라 달라질 수 있습니다.
생략하면 기본 입력 장치를 사용합니다.

```bash
PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python -m malbut_stt.smoke \
  --manual --device-index 1 --once
```

Enter를 누르고 `listening`이 표시되면 “오늘 날씨가 어때?”처럼 짧게 말합니다.
1초간 조용해지면 `transcribing`으로 전환되고, `event: transcript`에 원문·새 발화 ID·
`transcription_s`가 표시됩니다. 이 시간은 음성 인식 요청 처리 시간이며, 말하기 시간과
종료 무음 1초는 포함하지 않습니다. `--once`를 빼면 다음 Enter를 기다립니다.
`Ctrl+C`로 종료합니다. API 처리 중 마이크는 닫혀 있습니다.

`--once`는 무음·길이 초과·API 오류도 한 번의 시도로 보고 종료합니다.
5초간 말하지 않으면 `no_speech`로 끝나며 API를 호출하지 않습니다.
API 오류는 `transcription_failed:<오류 종류>`로만 표시하고 원문 오류는 출력하지 않습니다.
macOS 마이크 권한 요청이 뜨면 실행한 앱(Codex 또는 터미널)에 허용합니다.
입력이 안 되면 시스템 설정 → 개인정보 보호 및 보안 → 마이크에서 해당 앱을 확인합니다.

#### API 없이 Whisper small로 전체 문장 시험하기

`--local`은 명령 STT를 로컬 Whisper로 바꾸는 노트북 시험 옵션입니다.
아래 명령은 기존에 내려받은 모델을 사용하며, 모델이 없다면 아래 2절의 다운로드를
먼저 완료합니다. 마이크 번호는 `--list-devices`로 확인합니다.

```bash
env -u OPENAI_API_KEY HF_HUB_OFFLINE=1 PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python -m malbut_stt.smoke --manual --local --model-path .runtime/stt-laptop/models/whisper-small --device-index 1
```

`ready`에 `backend: local`, `model: small`이 표시됩니다. Enter 후 `listening`이
나오면 일반 문장을 말하고 1초간 조용히 기다립니다. `transcript.text`가 로컬 인식
결과이며, `transcription_s`는 모델 로딩·녹음·종료 무음 대기를 제외한 로컬 추론
시간입니다. Enter로 반복하고 Ctrl+C로 종료합니다. 한 번만 시도하려면 `--once`를
추가합니다. OpenAI SDK를 초기화하거나 API를 호출하지 않습니다.

호출어부터 명령까지 모두 로컬에서 시험하려면 `--manual`을 뺍니다.

```bash
env -u OPENAI_API_KEY HF_HUB_OFFLINE=1 PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python -m malbut_stt.smoke --local --model-path .runtime/stt-laptop/models/whisper-small --device-index 1
```

`waiting_for_wake`에서 “제이크야”만 말하고, `wake_detected` 다음 `listening`이
표시되면 명령을 따로 말합니다. 호출어와 명령은 같은 모델 인스턴스를 재사용하며,
기본 설정은 CPU int8·6 threads·한국어입니다. “로봇 이름은 제이크입니다.” 힌트는
호출어에만 적용하며 일반 문장은 이름 힌트 없이 전사합니다.
녹음 종료 조건은 기존과 같고, 원본 음성은 파일에 저장하지 않습니다.
이 옵션은 노트북 실행기에 적용됩니다. ROS 노드는 별도로 로컬 문장 전사를 사용합니다.

#### 실제 목소리 20문장 순서대로 기록하기

조용한 곳에서 평소 목소리로 [고정 문장 20개](test/fixtures/stt_manual_ko.json)를 읽습니다.
문장을 확인하고 Enter를 누른 뒤 `listening`이 나오면 말합니다. 각 문장마다
Enter를 다시 기다리므로 결과를 확인한 뒤 다음 문장을 시작할 수 있습니다.
아래 마이크 번호는 실행 직전 `--list-devices`로 확인한 번호로 바꿉니다.

```bash
PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python malbut_stt/tools/manual_stt.py \
  --device-index 1 --output-dir .runtime/stt-manual/my-run
```

`events.jsonl`에 기준 문장, 인식 원문, 발화 ID와 이벤트를 기록합니다.
원본 음성은 저장하지 않습니다. `transcription_s`는 API 처리 시간이고,
`vad_last_speech_to_text_s`는 VAD가 마지막 음성 프레임을 판정한 뒤 결과가 나올
때까지의 시간입니다. 후자는 종료 무음 대기도 포함하는 진단값이며,
사람이 실제로 말을 끝낸 시각을 정밀하게 측정한 값은 아닙니다.

첫 단어와 부정·정지·취소 표현이 보존됐는지 문장별로 확인합니다.
이 시험의 인식 결과는 화면과 로그에만 출력되며 로봇 이동 요청으로 전달되지 않습니다.
중단했다면 `--start-case 7`처럼 다시 읽을 문장 번호를 지정할 수 있습니다.
거리·소음·호출어 연결은 이 기본 발화 시험 이후 별도 조건으로 시험합니다.

### 2. 로컬 호출어와 명령 STT 연결하기

ROS 노드와 `smoke`가 같은 로컬 호출어 인식기를 사용합니다. `faster-whisper 1.2.1`은
위 requirements에 포함되어 있습니다. 최초에 모델을 명시적으로 다운로드하고,
실행할 때에는 `tokenizer.json`을 포함해 다운로드가 완료된 디렉터리를 지정합니다.
실행기는 `local_files_only=True`로 모델을 열어 자동 다운로드하지 않습니다.
모델·키·녹음은 Git에 포함하지 않습니다.
Whisper에는 고유명사 인식을 돕는 “로봇 이름은 제이크입니다.” 힌트를 사용합니다.

```bash
.runtime/stt-laptop/bin/python -c \
  'from faster_whisper.utils import download_model; download_model("small", output_dir=".runtime/stt-laptop/models/whisper-small")'
HF_HUB_OFFLINE=1 PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python -m malbut_stt.smoke \
  --model-path .runtime/stt-laptop/models/whisper-small --device-index 1 --wake-only --once
```

`--wake-only`에는 계정과 OpenAI 키가 필요 없습니다. Enter 없이 마이크를 열므로
`waiting_for_wake`가 표시된 뒤 “제이크야”만 부릅니다. `recognizing_wake` 동안
마이크를 닫고, 결과에 따라 `wake_detected` 또는 `not_wake`를 표시합니다.
`--once`는 무음(`wake_no_speech`)·길이 초과(`wake_too_long`)·다른 말도 한 번의
호출어 수집 시도로 보고 종료합니다. 빼면 계속 기다리며 `Ctrl+C`로 종료합니다.

호출어를 확인한 뒤 `--wake-only`를 빼고 기존 `OPENAI_API_KEY`가 설정된 터미널에서
명령 STT까지 시험합니다.

```bash
HF_HUB_OFFLINE=1 PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python -m malbut_stt.smoke \
  --model-path .runtime/stt-laptop/models/whisper-small --device-index 1 --once
```

“제이크야”를 부른 뒤 **`listening`을 기다렸다가** 명령을 말합니다. 이 모드의
`--once`는 호출어가 통과한 뒤 첫 명령 수집·STT 시도에서 종료합니다. 호출어가
거부되거나 무음이면 계속 호출어를 기다립니다. `--once`를 빼면 명령 처리 뒤 다시
호출어를 기다립니다. 발화 단위 ASR 검사이므로 상시 대기 비용·주변 대화 오탐률·
실제 로봇에서의 지연은 별도로 측정해야 합니다.

| 시험 | 확인할 결과 |
|---|---|
| 짧은 한국어 문장 3개 | 첫 단어 누락 없이 원문 출력, 각 요청 시간 기록 |
| 같은 문장 2회 | 원문이 같아도 발화 ID는 서로 다름 |
| `listening` 후 5초간 무음 | `no_speech`, API 전송 없음 |
| 명령을 20초 넘게 계속 말하기 | `too_long`, 잘린 문장을 전송하지 않음 |
| 호출어와 명령을 한 번에 이어 말하기 | 전체 전사가 `제이크야`와 달라 `not_wake`, API 전송 없음 |

준비된 파일만 확인하려면 `malbut_stt.local_wake --model-path <모델 디렉터리>
--wav /absolute/path/to/wake.wav`를 사용할 수 있습니다. 이 보조 실행기의 파일 모드는
VAD 수집 없이 파일 전체를 인식하며, 마이크·명령 STT 연결 시험과 구분합니다.

### 3. 동일한 합성 음성으로 로컬과 OpenAI 비교하기

고정된 한국어 20개(호출어 3·유사 발음 5·로봇 요청 6·일상 5·무음 1)를
macOS `say`의 Yuna 목소리로 생성합니다. 두 엔진에 같은 PCM16 mono 16kHz 음성을
넣으며, 앞 0.3초·뒤 0.4초 무음을 공통으로 붙입니다. 이 비교에서는 VAD·마이크·ROS를
거치지 않고 완성된 음성 전체를 인식합니다.

`local_base`(한국어 설정만)와 `openai`(한국어 설정만)가 기본 비교이며,
현재 호출어 실행기의 이름 힌트를 넣은 `local_hint`는 별도로 기록합니다.
실험 도중 설정·문장·생성 음성을 바꾸지 않습니다.

```bash
# 먼저 음성과 기준 문장을 확정합니다. 아래 출력 경로는 새 실험마다 바꿉니다.
PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python malbut_stt/tools/compare_stt.py \
  prepare --output .runtime/stt-benchmark/my-run

# 내려받은 모델만 사용하며 키가 필요하지 않습니다.
HF_HUB_OFFLINE=1 PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python \
  malbut_stt/tools/compare_stt.py local --output .runtime/stt-benchmark/my-run \
  --model-path .runtime/stt-laptop/models/whisper-small

# 기존 OPENAI_API_KEY로 실제 API를 호출합니다: 파일당 1회, 총 20회, 자동 재시도 없음.
PYTHONPATH=malbut_stt .runtime/stt-laptop/bin/python malbut_stt/tools/compare_stt.py \
  openai --output .runtime/stt-benchmark/my-run

# 기존 결과만 읽어 집계합니다. API를 호출하지 않습니다.
.runtime/stt-laptop/bin/python malbut_stt/tools/summarize_stt_comparison.py \
  .runtime/stt-benchmark/my-run
```

출력 폴더에는 WAV, 입력 SHA-256·설정, 시도 기록, 원문·지연을 보존합니다.
이미 시도한 파일·엔진 조합은 재실행해도 다시 호출하지 않으며, 실패하거나 중단된
시도도 자동 반복하지 않습니다. 새 측정은 새 출력 폴더로 명시적으로 시작합니다.
로컬은 모델을 한 번 준비하고 무음으로 예열한 뒤 측정하며, API 시간에는 네트워크가
포함됩니다. 모델 로딩·음성 생성·사용자의 말하기 시간은 처리 시간에 포함하지 않습니다.

[2026-09-12 실제 비교 결과](docs/stt_comparison_2026-09-12.md)에 전체 전사와 집계를
기록했습니다. 숫자 표기 차이(`십오`/`15`)는 정규화하지 않아 글자 오류율에 포함됩니다.
합성 한 화자의 단회 시험은 실제 사용자 정확도·시간당 호출어 오탐률·로봇 성능을
입증하지 않습니다. 이름 힌트를 준 로컬 모델에서 무음 전사도 발생했으므로, 호출어
판정과 무음 환각을 별도로 확인합니다.

### 2026-09-11 노트북 준비 검증 (교체 전 기록)

아래는 SWM25-172의 ROS 호출어 교체 **이전** 기록입니다. 당시 환경은 macOS ARM64 /
Python 3.12, Porcupine `4.0.3`, PvRecorder `1.2.7`, WebRTC VAD wheels `2.0.14`,
OpenAI SDK `3.13.0`이며, 현재 구현의 회귀 시험 결과와 구분합니다.

| 대상 | 결과 |
|---|---|
| STT 자동 시험 (로컬·Porcupine 호출어 단독 모드 포함) | 89 passed, 1 skipped: ROS 미설치 |
| MacBook Pro 내장 마이크 | 실제 16kHz 입력 3.01초 수집·장치 해제 성공 |
| 합성 한국어 WAV → 실제 OpenAI API | “오늘 날씨가 어때?” 원문 일치, 1.211초 음성의 요청 처리 3.575초 |
| 합성 음성 → 실제 VAD → 로컬 Whisper small | 호출어 1개 감지, 다른 발화 7개 거부, 무음 1개 ASR 생략. 처리 0.643~0.728초 (말하기·종료 무음 시간 제외) |
| 사용자 실제 “제이크야” 감지 | 로컬 모델·실행기 준비 완료, 사람의 목소리 감지는 미검증 |
| Porcupine 호출어 감지 | 한국어 언어 모델 준비 완료, 키·Mac용 호출어 모델 준비 전, 미검증 |

마이크 입력 확인과 합성 음성 API 확인은 별개 시험입니다. 두 결과를 실제 사용자
발화의 전체 경로 성공이나 로봇에서의 성능으로 해석하지 않습니다.
로컬 시험은 `faster-whisper 1.2.1`, CPU int8, 6 threads로 수행했고 두 API 키를 제거한
`HF_HUB_OFFLINE=1` 환경에서 내려받은 모델만 사용했습니다. 같은 합성 음성으로 설정을
조정한 개발 중 점검이므로 독립 평가 데이터의 정확도나 실제 환경 오탐률은 아닙니다.
이름 힌트 없이 VAD 결과를 읽었을 때에는 “제이크야”가 “스테이크야”로 오인식되었습니다.

실제 목소리 20문장 시험은 첫 시도가 `empty_transcript`로 끝나 **완료 0/20**입니다.
사용자 음성의 전체 경로 성공, 실제 호출어 감지와 로봇 검증은 아직 확인하지 못했습니다.

### 2026-09-12 로컬 호출어 교체 검증

| 대상 | 결과 |
|---|---|
| macOS / Python 3.12, STT 자동 시험 | 118 passed, 1 skipped: ROS 미설치 |
| Ubuntu / ROS 2 Humble 컨테이너, STT·Agent 통신 시험 | 관련 5개 패키지 빌드 성공, 131 passed, 3 skipped: 선택적 VAD·OpenAI SDK 미설치 |
| 합성 호출어 → 실제 VAD·로컬 Whisper → 별도 명령 녹음 → 실제 OpenAI | “제이크야” 감지 후 “오늘 날씨가 어때?” 원문과 UUID 1개를 로컬 콜백으로 전달, API 요청 1회 |

연결 시험은 파일을 마이크 대역으로 입력했고 두 녹음의 종료를 확인했습니다.
결과는 `.runtime/swm25-172-validation/connected-result.json`에 보존했습니다.
이 합성 API 연결 시험은 실제 마이크·ROS·Agent 수신을 사용하지 않았으며, 기록된 총 실행 시간에는
모델 로딩과 가상 녹음도 포함되어 사용자 체감 지연으로 해석하지 않습니다.

별도 Ubuntu 통합 시험은 호출어·명령 녹음 및 모델 응답을 대역으로 제공하고,
실제 ROS Topic에서 STT 파이프라인 → Agent 원문 수신·대화 → TTS 텍스트 수신,
중복 접수 방지를 확인했습니다. 실제 마이크·외부 모델 API·스피커·로봇 동작은
포함하지 않습니다. 로그는 `.runtime/swm25-172-validation/local-ros-test.log`에 보존했습니다.

## Ubuntu에서 준비

아래는 기본 `faster_whisper` CPU 경로의 준비 절차입니다. ROS 노드에서
`whisper_cpp`를 선택하는 Jetson 경로는 [네이티브 빌드·실행 안내](native/README.md)와
[Jetson 설정](config/jetson.yaml)을 따릅니다. 기존 `smoke` CLI의 backend 선택에는
`whisper_cpp`가 포함되어 있지 않습니다.

기존 CPU 경로의 기준 환경은 **Ubuntu 22.04 / x86_64 / Python 3.10 / ROS 2 Humble**입니다.
포팅 대상은 **Jetson Orin NX 8GB / aarch64**이며, 실제 JetPack·ROS 버전과 패키지
설치 가능 여부, CUDA 추론 지연·마이크 입력은 장비에서 따로 확인해야 합니다.
저장소 루트에서 다음을 실행합니다.

```bash
uname -m
source /opt/ros/humble/setup.bash
python3 -m pip install --user -r malbut_stt/requirements.txt
python3 -c 'from faster_whisper.utils import download_model; download_model("small", output_dir=".runtime/stt-robot/models/whisper-small")'
colcon build --symlink-install --packages-select malbut_interfaces malbut_agent_server malbut_stt
source install/setup.bash
python3 -m sounddevice
```

Whisper·오디오 SDK는 ROS STT의 실제 음성 실행에 필요합니다. OpenAI SDK는 기존
노트북 API 비교 시험에 사용합니다. ROS 빌드와
대역을 사용하는 Python 시험은 키·마이크·이 SDK들이 없어도 수행할 수 있습니다.
WebRTC VAD는 동일한 `webrtcvad` Python API를 제공하는 `webrtcvad-wheels`로 설치합니다.

실행 환경에 다음을 준비합니다. 키를 코드·ROS parameter·명령 인자·Git에 넣지 않습니다.

| 준비물 | 용도 |
|---|---|
| `tokenizer.json`을 포함한 Whisper small 디렉터리 | 로컬 호출어·문장 전사 |
| 사용 가능한 마이크 | 기본 장치 또는 장치 번호로 선택 |
| 에코 제거된 마이크 입력 | 실제 TTS 중 끼어들기 사용 시 필요 |

모델 디렉터리가 존재해도 필요한 파일이 불완전하면 초기화에 실패합니다.
실행 중 다운로드하지 않으므로 최초 다운로드를 완료한 뒤 절대 경로를 지정합니다.

## 두 Node 실행

먼저 Agent 실행 모드를 선택합니다. `speech_receiver`는 **수신 확인만** 하며
LLM·Manager·TTS를 호출하지 않습니다. 아래 예시는 같은 PC의 시험 도메인 `191`을
사용하며 두 터미널의 도메인을 맞춥니다.

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=191
export ROS_LOCALHOST_ONLY=1
ros2 run malbut_agent_server speech_receiver \
  --db-path ~/.local/state/malbut/speech-receipts.sqlite3
```

대화까지 연결하려면 위 수신기 대신 `agent_communication`을 실행합니다. Provider
환경 설정도 없으면 기본은 `mock`입니다. 실제 OpenAI 대화는 기존 키 환경에서
`--provider openai`로 선택합니다. 대화와 발화 대상 판단에는 모델 API를 사용하며,
음성 전사는 계속 로컬입니다. `mock`과 `rai-sidecar`의 대상 판단은 현재 `unknown`입니다.

```bash
ros2 run malbut_agent_server agent_communication --provider mock \
  --db-path ~/.local/state/malbut/speech-receipts.sqlite3 \
  --conversation-db ~/.local/state/malbut/speech-dialogue.sqlite3
```

두 Agent 모드를 동시에 실행하지 않습니다. `agent_communication`은 원문을 기존
대화 처리에 전달하고 `/malbut/speech/response`에 답변 텍스트를 발행합니다.
대화 발화·응답은 별도 대화 DB에 저장하며, `speech_receiver`의 ID·해시 수신 기록과
다릅니다. `.env`는 자동 로드하지 않으므로 필요하면 `--env-file`을 지정합니다.
TTS 수신 확인과 Manager 연결의 자세한 절차는 [Agent 연결 안내](../malbut_agent_server/README.md#stt--agent-대화--manager--tts-연결)를
따릅니다. 답변 텍스트 발행은 스피커 재생이나 로봇 동작 성공을 뜻하지 않습니다.

두 번째 터미널에는 ROS 환경과 내려받은 로컬 모델이 필요합니다. 다음 모델 경로는
다운로드를 마친 실제 디렉터리의 절대 경로로 바꿉니다.

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=191
export ROS_LOCALHOST_ONLY=1
ros2 run malbut_stt stt --ros-args \
  -p wake_model_path:=/absolute/path/to/whisper-small \
  -p device_index:=0
```

`waiting_for_wake`가 나오면 호출어만 부르고, `wake_detected` 뒤 호출 성공음이 끝나면 문장을 말합니다.
호출음은 선택한 출력 장치에서 로컬 PCM으로 재생하며, 재생 중과 종료 후 `chime_tail_s`(기본 0.3초)의 입력은 버립니다.
호출음 직후 바로 말하면 앞부분이 제외될 수 있으므로 차단 구간 뒤에 말합니다.
`transcribing`은 로컬 음성 인식 중입니다. Agent에 같은 ID와 원문을 포함한
`status: received`가 나오는지 확인합니다.
종료는 각 터미널에서 `Ctrl+C`로 합니다.

### 시작 시 적용하는 ROS parameter

| 이름 | 기본값 | 의미 |
|---|---|---|
| `backend` | `faster_whisper` | 로컬 엔진; `whisper_cpp` 선택 방법은 [네이티브 안내](native/README.md) 참고 |
| `wake_model_path` | 빈 문자열 | `faster_whisper`에서는 모델 디렉터리 필수; `whisper_cpp`에서는 비우거나 STT와 같은 모델 파일 지정 |
| `stt_model_path` | 빈 문자열 | 문장 전사 모델 디렉터리 또는 `whisper_cpp` 모델 파일; 비어 있으면 `wake_model_path` 사용 |
| `input_has_aec` | `false` | 선택한 마이크가 이미 AEC 처리된 입력을 제공하는지 여부 |
| `playback_control_timeout_s` | `5.0` | 재생 제어 Service의 접수 응답을 기다리는 시간; 실제 재생 완료 후 대화 대기와 별개 |
| `device_index` | `0` | sounddevice 목록의 장치 번호; 현재 로봇의 XFM 입력. `-1`을 명시하면 시스템 기본 입력 |
| `wake_chime_device_index` | `-1` | 호출 성공음 출력 장치; 통합 speech launch에서는 TTS와 같은 출력 번호 사용 |
| `chime_tail_s` | `0.3` | 기존 호출·수신·실패음 종료 후 입력 차단 시간 유지(0~3초); TTS 차단 시간과 별개 |
| `short_speech_guard` | `false` | 실험용 짧은 음성 대비 긴 한글 전사 검사; 실음성 검증 전까지 기본 비활성 |
| `stt_decode_timeout_s` | `30.0` | `whisper_cpp`의 호출어·부분·최종 전사 각각에 적용하는 협력적 취소 제한시간 |
| `vad_mode` | `2` | WebRTC VAD의 음성 판단 모드, `0`~`3` |
| `start_timeout_s` | `5.0` | 무음 수집 창; 발화가 없으면 새 창으로 이어서 대기 |
| `silence_timeout_s` | `2.0` | 문장 종료 판단이 불확실할 때 기다리는 무음 시간 |
| `endpoint_predecode_s` | `0.8` | 후보 전사를 시작하는 무음 시간; 완성 문장도 1초 무음 전에 확정하지 않음 |
| `max_utterance_s` | `0.0` | 기본은 전체 발화 시간 제한 없음; 양수로 명시한 경우에만 해당 시간 제한 적용 |
| `max_buffer_s` | `60.0` | 미처리·겹침 확인용 PCM 보관 상한; 확정한 앞부분은 비우므로 발화 전체 길이와 무관 |
| `pre_roll_s` | `0.3` | 발화 시작 직전 보존할 소리 |
| `min_speech_s` | `0.08` | 발화 시작에 필요한 연속 VAD 음성 시간; 20ms 단위로 올림, 양수이며 `pre_roll_s` 이하 |

위 수집 parameter는 명령에 적용합니다. 호출어는 시작 대기 5초·종료 무음 0.4초·
최대 발화 6초·시작 직전 소리 0.3초·최소 연속 음성 0.08초로 고정합니다. WebRTC VAD 입력은 20ms PCM16
mono이며 마이크가 16kHz가 아니면 실행을 중단합니다. 문장 전사는 지정한 로컬
Whisper 모델과 한국어 설정을 사용하며 호출어용 이름 힌트를 넣지 않습니다.
parameter는 시작 시 읽습니다. 값을 바꾸려면 새 `-p` 인자로 재실행합니다.

### 실험용 짧은 음성 결과 검사

기본값은 `false`입니다. 아래 기준은 실제 녹음으로 유효성을 검증하지 않았으므로
환각 방지의 검증된 해결책으로 사용하지 않습니다. 호출음 뒤 차단 시간을 늘리는 방법도
효과가 검증되지 않아 기존 0.3초를 유지합니다.

`short_speech_guard=true`이면 후보·부분·최종 추론 결과 모두 실제 VAD 양성 프레임의
누적 길이와 비교합니다. 무음, pre-roll 및 추론용 무음 패딩은 이 길이에 포함하지 않습니다.
VAD 양성 시간이 0.6초 이하일 때 한글 음절 수가 `max(6, ceil(20 * voiced_s))`를
넘으면 `InsufficientSpeechEvidence`로 거절합니다. 예를 들어 0.28초 음성 판정에서
12음절이 나오면 거절하지만, 짧은 “네”, “아니요”, “감사합니다”는 이 검사로 막지 않습니다.
문구별 차단 목록은 사용하지 않습니다. 긴 실제 발화에는 이 검사를 적용하지 않습니다.

이는 신뢰도 점수가 아닌 실험용 휴리스틱입니다. 조용하거나 빠른 실제 발화에서 VAD가
많이 누락되면 정상 문장도 거절할 수 있고, 짧은 오인식이나 더 긴 잡음은 통과할 수 있습니다.
중간 거절 이후 음성이 추가되면 새 후보를 검사하고, 최종 추론도 거절되면 기존 실패음으로
턴을 종료합니다. 자동으로 실패 문장을 Agent에 보내지 않습니다.
음성 파일이 없는 기존 사건은 VAD 판정·길이와 고정 모델 출력으로 회귀 검증할 수 있지만,
실제 모델·마이크 재현 검증을 대신하지 않습니다.

### 오류와 중복 처리

- `wake_too_long`: 호출어 녹음을 버리고 로컬 인식 없이 다시 대기합니다.
- `wake_chime_failed:<오류 종류>`: 호출음 출력 실패 시 대화를 시작하지 않고 호출어 대기로 돌아갑니다.
- `not_wake`: 전체 전사가 호출어와 달라 대화 모드를 시작하지 않습니다.
- `utterance_discarded:too_long`: `max_utterance_s`를 양수로 명시했을 때만 적용하는 발화 시간 제한입니다. 기본 설정에서는 발생하지 않습니다.
- `utterance_discarded:buffer_overflow`: 인식이 따라가지 못하거나 안정된 경계를 확정하지 못해 미처리 PCM이 보관 상한을 초과한 상태입니다. 일부 텍스트만 명령으로 보내지 않으며, 종료 침묵 이후 호출어 대기로 복귀합니다.
- `speech_discarded:busy`: 처리 중 추가 발화를 끝까지 버립니다.
- `addressee_unknown:*`: 판정 실패·시간 초과·재생 변경으로 새 발화를 전달하지 않습니다.
- `playback_control_*`: 제어 서비스의 미가동·실패·거절·응답 대기 초과를 기록합니다.
  응답이 없거나 거절되었다고 실제 TTS가 일시정지·종료된 것으로 처리하지 않습니다.
- `empty_transcript` / `transcription_failed:<오류 종류>`: 발행 없이 다시 대기합니다.
  예외 원문 대신 오류 종류만 기록합니다.
- 모델이 없거나 장치 초기화·마이크 읽기에 실패하면 오류 종류를
  기록하고 종료합니다.
  예: `STT stopped during opening_microphone: RuntimeError`.
- Agent는 같은 ID·같은 원문을 `duplicate`, 같은 ID·다른 원문을 `conflict`로 구분합니다.
  다른 ID·같은 원문은 새 발화입니다. 빈 ID·공백뿐인 원문은 접수하지 않습니다.
- 수신 기록 SQLite에는 ID·원문 SHA-256·수신 시각만 저장합니다. 같은 DB로 재시작하면
  중복 방지가 유지됩니다. DB 삭제·교체 시 이 기록도 없어집니다.
- `received`는 DB commit 이후에만 출력합니다. 저장 실패는 접수 완료로 표시하지
  않습니다. commit 직후 종료하면 화면 로그가 누락될 수 있으므로 로그 출력까지
  정확히 한 번 보장한다고 표현하지 않습니다.

## 시험과 검증 범위

패키지 디렉터리에서 순수 Python 시험을 실행합니다.

```bash
cd malbut_stt
PYTHONPATH=. python3 -m pytest -q test
```

ROS 통신 시험은 생성 메시지와 설치된 Agent 실행 파일이 있는 환경에서 실행합니다.
미준비 환경에서는 해당 시험만 skip합니다. 시험은 임시 DB·별도 프로세스를 사용하며
마이크나 실제 API를 사용하지 않습니다. 운영 로봇과 분리된 ROS 도메인에서 실행합니다.

```bash
source install/setup.bash
ROS_DOMAIN_ID=191 ROS_LOCALHOST_ONLY=1 \
  colcon test --packages-select malbut_interfaces malbut_agent_server malbut_stt \
  --event-handlers console_direct+ --return-code-on-test-failure
colcon test-result --verbose
```

`test_audio.py`는 무음·발화 끝·길이 제한·프레임 변환을, `test_pipeline.py`는
호출어 전 무전송·오류 복귀·새 ID·종료 중 늦은 결과 차단을 검사합니다.
`test_transcription.py`는 WAV와 한국어 힌트, 설치된 SDK의 실제 multipart 직렬화를
검사합니다. SDK 시험의 HTTP 응답은 로컬 대역이며 외부 API를 호출하지 않습니다.
`test_ros_transcript_delivery.py`는 실제 DDS와 설치된 Agent 수신기를 사용합니다.

실제 음성 시험에서는 호출어 감지, 한국어 원문 도착, 같은 문장의 새로운 ID,
무음 후 복귀, 네트워크 실패 후 복귀를 확인합니다. 자동 시험 통과와 실제 음성·장치
검증 결과는 별도로 기록합니다.

### 2026-09-08 구현 검증 기록 (교체 전)

아래는 이전 Porcupine 구현 시점의 통신·자동 시험 기록이며, 현재 로컬 호출어 구현이나
실제 로봇의 음성 경로를 검증한 결과가 아닙니다.

| 환경·대상 | 결과 |
|---|---|
| macOS / Python 3.12, Agent 전체 시험 | 591 passed |
| macOS / Python 3.12, STT 시험 | 34 passed, 1 skipped: ROS 미설치 |
| Ubuntu 22.04.5 ARM64 / Humble, 세 패키지 빌드 | 모두 성공 |
| 같은 Ubuntu 환경, Agent 전체 시험 | 591 passed |
| 같은 Ubuntu 환경, STT 시험 | 32 passed, 3 skipped: 선택적 VAD·OpenAI SDK 시험 |
| 실제 DDS·설치된 Agent 수신기 | 원문 보존·QoS·중복·충돌·재시작·SIGINT 통과 |
| Ubuntu x86_64 실제 마이크·“말벗아”·외부 OpenAI API | 미검증 |

Ubuntu 시험은 기존 컨테이너 안의 별도 임시 workspace와 domain `191`에서
수행했습니다. 컨테이너에는 마이크 장치가 없습니다. Ubuntu에서 생략한 vendor
시험 3건은 macOS에서 실제 설치 라이브러리와 로컬 HTTP 대역으로 통과했습니다.
확인한 라이브러리는 OpenAI SDK `3.8.0`, `webrtcvad-wheels` `2.0.14`입니다.
실제 OpenAI API 요청·실제 호출어 모델 활성화·음성 수집은 수행하지 않았습니다.
CI 선택 스크립트 시험, 신규 Python 코드 lint, 문서 링크와 변경 공백 검사도 통과했습니다.

Manager의 `accepted` 상태는 발화 접수만 뜻하므로 STT는 이를 재생 시작·완료로
처리하지 않는다. 실제 재생 상태와 기존 request_id·interim 기반 입력 차단 규칙을 사용한다.
