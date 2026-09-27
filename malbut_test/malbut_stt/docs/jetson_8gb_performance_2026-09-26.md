# Jetson 8GB 음성 성능 개선 기록 — 2026-09-26

실제 사용자처럼 입력을 넣어 일반 대화, 낙상 판단, 한국어 STT를 시험했다. **현재 배포 기본값은 multilingual Whisper small F16을 유지하고, Jetson에서 가장 먼저 비교할 후보는 Q8_0으로 정했다.** 로봇 접속이 불가능하여 아래 실측은 Mac M4 Pro Metal 및 실제 클라우드 API 결과다. Orin Nano 8GB/NX 8GB의 정확한 보드와 JetPack·전력 모드는 확인 전이다.

## 확인한 실패와 수정

1. 실제 상황 판단 API가 `도와주세요`를 받았는데도 `unknown / help_needed=null`과 첫 질문을 다시 반환했다. 공통 `SituationDialogue.answer`에서 단독 `도와줘`, `도와주세요`는 명확한 도움 요청으로 완료하게 했다. 기존 사건 판단을 보존하고 답변 판단 API를 생략한다. 부분 문자열·부정문·인용문은 이 규칙으로 처리하지 않는다. 기존 `좋아져` 별칭은 낙상에서만 적용한다. 공유 ROS 메시지 규격은 변경하지 않았다.
2. `좋아져`가 문장 종료 힌트에 없어 2초 fallback을 기다렸다. 완결 표현 목록에 추가하되 `좋아져서`, `좋아져도`, `좋아져라고`는 제외했다. 기존 1초 무음 확정 경계와 재발화 시 후보 무효화는 유지한다. 의미 판단에서 `상태가 좋아져`까지 도움 요청으로 확장한 것은 아니다.
3. 녹음 replay가 EOF 후 입력을 멈춰 느린 추론을 마이크 고장으로 오인할 수 있었다. EOF 이후에도 실제 마이크처럼 32ms 간격의 무음을 공급하고, 입력 완료 시각은 처음 한 번만 기록한다. replay가 생성한 native 모델의 worker가 늦게 끝나면 종료 후 해제하며 실행 중인 context를 먼저 해제하지 않는다.
4. 기존 replay CLI에 `whisper_cpp`와 `--library-path`를 연결했다. Jetson의 0.8초 predecode / 2초 fallback을 그대로 사용하고 native 메타데이터·원문·추론별 시간·입력 해시를 남긴다. 모델 정밀도는 파일에서 결정한다. 별도 대화 엔진이나 스트리밍 프레임워크를 만들지 않았다.

`좋아져` 수정 전후에 같은 F16 모델·같은 PCM을 실제 속도로 재생했다. 마지막 VAD 음성 프레임 처리부터 최종 전사까지 수정 전 **2.015초(1회)**, 수정 후 **1.037 / 1.018 / 1.024초(3회)**였다. 세 번 모두 같은 `좋아져`를 반환했다. 이는 특정 재현 사례의 대기 감소이며 물리 마이크의 발화 종료부터 로봇 답변까지의 지연은 아니다.

## 같은 한국어 녹음으로 정밀도 비교

whisper.cpp SHA `da54572229bcf64ba367d96c7ef15770376c4280`, ABI 3, 한국어 고정, greedy/beam 1, temperature 0, no_context, Flash Attention, CPU helper 6개를 고정했다. 원본 F16에서 같은 upstream 도구로 Q8_0/Q5_0을 생성했다. 모델마다 별도 프로세스에서 한 번 로드하고 평가에 포함하지 않는 개발 음성으로 예열했다. Metal 장치 선택은 각 native 로그에서 확인했다.

- 기존 FLEURS `ko_kr` test의 사람 낭독 16개, 기존 합성 종료·정정·부정·조건 문장 4개, 낙상 답변 합성 음성 5개: **25개, 232.51초**.
- 각 정밀도에서 동일한 25개를 3회 전사: **총 225회**. 반복은 새 화자나 새 음성 표본을 뜻하지 않는다. 이전 실험에 사용했던 자료여서 새 holdout이 아니다.
- CER는 NFC 후 공백·문장부호를 제거하는 기존 평가 함수를 재사용했다. 숫자 표기 차이는 오류에 포함한다. 원문과 실패를 그대로 보존했다.
- FLEURS는 CC-BY-4.0, revision `70bb2e84b976b7e960aa89f1c648e09c59f894dd`다. 합성 음성은 실제 사용자·원거리 마이크·로봇 소음을 대표하지 않는다. [데이터 원본](https://huggingface.co/datasets/google/fleurs/blob/70bb2e84b976b7e960aa89f1c648e09c59f894dd/README.md)

| Mac 관측값 | F16 | Q8_0 | Q5_0 |
|---|---:|---:|---:|
| 모델 파일, MB(10진수) | 487.60 | 264.46 | 175.21 |
| 프로세스 최대 RSS, GB(10진수) | 1.289 | 0.806 | 0.614 |
| 사람 낭독 CER, 16개×3회 | 6.88% | 6.00% | 7.17% |
| 사람 낭독 전사 중앙값 | 224ms | 217ms | 216ms |
| 사람 낭독 전사 p95, nearest rank | 283ms | 273ms | 272ms |
| 낙상 답변 전체 WAV 정확 일치 | 15/15 | 15/15 | 15/15 |
| 합성 종료·정정·부정·조건 문장 CER, 4개×3회 | 37.10% | 37.10% | 40.32% |

Q8은 이 표본에서 파일 약 46%, 프로세스 최대 RSS 약 37%를 줄였다. 속도 차이는 작으며 실행 순서·데스크톱 부하를 통제한 Jetson 가속 실험이 아니다. Q5는 더 작지만 사람 낭독과 합성 문장의 CER가 모두 높아 우선 후보에서 제외했다. Q8의 낮은 CER가 일반적인 정확도 향상을 증명하지 않는다. RSS는 전체 통합 메모리·CUDA peak가 아니며 Jetson 8GB 수용량으로 옮겨 계산하지 않는다.

추가로 F16/Q8에서 합성 9개씩 실제 시간 간격의 `DialoguePipeline` replay를 실행했다. 두 정밀도 모두 낙상 5개 중 4개가 정확 일치했고 `도와줘`는 `좋아져`로 인식했다. 기존 도움 별칭은 의도 처리만 보완하며 ASR 정확 일치 점수에는 오류로 남겼다. 2.3초 중간 쉼이 있는 조건 문장은 현재 2초 fallback 때문에 두 전사로 나뉘었다. 이 한계를 숨기거나 성공으로 집계하지 않는다. 합성 음색 자체의 발음 품질과 실제 사용자 조건은 별도 확인이 필요하다.

F16 replay 이후 `좋아져` endpoint를 수정하고 Q8 replay를 실행했으므로, 두 replay의 종료 지연을 정밀도 차이에 따른 속도 비교로 사용하지 않는다. 같은 F16을 사용한 별도 수정 전후 측정이 endpoint 개선의 근거다. 위 전체 WAV 정밀도 비교에는 endpoint 정책이 개입하지 않는다.

## 사용자 역할로 실제 API 사용

일반 대화는 현재 설정 `gpt-5.6-luna / low`로 10턴·10요청을 실행했다. 정정된 장소와 나머지 조건 유지, 말하지 않은 정보를 모른다고 답하기, 상세정보 거절 유지, 직전 계획 이어가기, 대화 종료가 선택한 기준을 충족했다. 기존 production smoke 9턴과 실제 `console.main()` 1턴을 구분해 기록했다. 재시도·요청 오류는 없었다.

응답 중앙값은 **2.998초**, 범위는 **1.506–4.760초**였다. 총 30.752초 중 API transport가 30.648초였다. 네트워크·서버 대기·생성을 따로 분리한 수치는 아니다. 이번 표본에서는 로컬 일반 대화 리팩터링보다 API 응답 대기가 큰 병목이었다. 기억·동의 동작을 없애거나 timeout을 늘리는 것을 속도 개선으로 처리하지 않았다.

낙상은 독립된 5개 사례, 실제 API 9회로 시험했고 초기 결과는 **4개 통과, 1개 실패**였다. 실패한 `도와주세요` 재질문을 위 공통 경로 수정의 회귀로 남겼다. `좋아져` 직접 별칭, 문장 속 `상태가 좋아져`의 비별칭 처리, 스트레칭 상황 해소, 넘어짐 확인 뒤 도움 거절은 초기 실험에서 기준을 충족했다. 문장 속 좋아져 사례는 추가 질문 뒤 테스트가 명시적으로 무응답을 입력해 종료했으므로 사용자의 도움 의사를 즉시 판정한 성공으로 해석하지 않는다.

수정 뒤 같은 실패 사례의 첫 질문을 추가 API 1회로 생성하고 `도와주세요`를 입력해 `unknown / true`, 질문 1회로 완료되는 것을 확인했다. 답변 판단 API는 0회였다. 기존 실패 원문은 보존했다. 관련 대화·session·음성 결합 검사 **183개**, endpoint·streaming 검사 **120개**, native replay 검사 **40개**가 각각 통과했다. 합계 **343개**이며 실제 ROS·마이크·Jetson 실행 검사 수는 아니다.

## 논문과 실제 구현에서 채택한 판단

| 1차 자료 | 확인한 내용 | 적용 판단 |
|---|---|---|
| [Whisper-Streaming, IJCNLP-AACL 2023](https://aclanthology.org/2023.ijcnlp-demo.3.pdf) | LocalAgreement-2, large-v2 FP16, A40 48GB, 영어 ESIC에서 평균 단어 출력 지연 3.3초; 다국어 회의 서비스 사용 | 기존 incremental 구현에 이미 가설 일치 정책이 있다. 3.3초를 한국어 Jetson의 발화 종료 지연으로 쓰지 않는다. 중복 프레임워크 도입 없이 실제 입력으로 측정한다. |
| [Simul-Whisper, Interspeech 2024](https://arxiv.org/abs/2406.10052) | cross-attention의 시간 정렬과 잘린 단어 감지로 Whisper를 streaming에 적용 | 다른 디코딩 경로를 필요로 하므로 당장 bridge를 바꾸지 않는다. 현재 작은 개선 이후에도 지연·부분 인식 병목이 남을 때 비교한다. |
| [한국어 Whisper 평가, 2023](https://www.eksss.org/archive/view_article?pid=pss-15-3-75) | 모델 크기·자발화/방송 등 도메인에 따라 오류가 달랐다. small은 평가 대상에 없다. | 영어 속도표를 근거로 base/tiny로 즉시 축소하지 않는다. 한국어 도움 요청·부정·정정을 별도로 평가한다. |
| [NVIDIA whisper_trt 구현](https://github.com/NVIDIA-AI-IOT/whisper_trt/blob/268eff10a1e38118a2734745b9db14f7419a08a5/whisper_trt/model.py) | 등록 모델은 tiny.en/base.en/small.en, tokenizer도 영어 전용 | 현재 한국어 STT의 즉시 교체 후보에서 제외한다. 공개 Nano 처리량과 프로세스 RSS 차이는 전체 서비스 지연·메모리가 아니다. |
| [whisper.cpp 양자화](https://github.com/ggml-org/whisper.cpp/blob/da54572229bcf64ba367d96c7ef15770376c4280/README.md#quantization) | 양자화는 메모리/파일을 줄이고, 속도 효과는 하드웨어에 의존한다. | 같은 small 구조·원본 모델·디코딩을 유지하는 Q8을 먼저 시험한다. CUDA에서도 빠르다고 미리 확정하지 않는다. |
| [Qwen3-ASR, 2026](https://arxiv.org/html/2601.21337v1#S2.SS4), [공식 구현](https://github.com/QwenLM/Qwen3-ASR/tree/7c6daf77a2421100f5fb066495372c00129d39ff) | 0.6B 한국어·streaming 지원. 보고한 92ms TTFT는 vLLM/BF16/CUDA Graph 조건이며 GPU 모델 미공개. 현재 streaming API는 vLLM 경로 | 차기 비교 후보로 남기되 Jetson 8GB 성공 실측으로 취급하지 않는다. 기존 JetPack/ROS 환경에 바로 설치하지 않고 호환성·peak RAM부터 별도 비교한다. |

Nano/NX의 총 TOPS를 STT 속도 비율로 환산하지 않는다. Super 사양에서 NX 8GB의 총 TOPS에는 DLA가 포함된다. 8GB는 CPU·GPU·OS·ROS·카메라·STT가 공유하는 DRAM이다. 모델 파일만으로 배치 가능성을 판단하지 않는다. [NVIDIA 사양과 전력 조건](https://developer.nvidia.com/blog/nvidia-jetpack-6-2-brings-super-mode-to-nvidia-jetson-orin-nano-and-jetson-orin-nx-modules/), [Tegra 메모리 구조](https://docs.nvidia.com/cuda/cuda-for-tegra-appnote/index.html)

## 다음 채택 기준과 재현

1. 정확한 보드·JetPack·전력/온도를 기록하고 동일 PCM으로 F16/Q8을 **STT 단독 → YOLO 동시 실행** 순서로 비교한다. 전체 RAM/SWAP, p50/p95, 오류·중단, YOLO 지연을 함께 기록한다. 현재 6개 CPU helper도 필요할 때 2/4/6으로 비교한다.
2. Q8은 도움 요청 누락·부정 반전·정정 손실이 추가되지 않고 메모리 여유 또는 지연 개선이 실제로 관측될 때 배포 후보로 채택한다. 평균값이 좋아도 꼬리 지연·오류가 늘면 보류한다.
3. 마지막으로 실제 마이크·스피커에서 최초 사용자 3명×10분을 실행한다. 호출→입력→답변 재생→후속 대화→종료와 발화 끝→첫 답변 재생 시간을 확인한다. 이 단계와 Jetson 실측은 미실시다.

실행 명령은 [native replay 안내](../native/README.md#same-audio-performance-replay)에 있다. 입력 WAV와 모델의 해시를 유지하고 한 모델을 순차 재사용해야 cold load와 warm decode를 분리할 수 있다. `tegrastats`와 보드 조회 명령·후보 버전은 `.runtime/jetson-performance-20260926/nvidia-research.md`에 보존했다.

로컬 증거는 저장소 루트 기준 다음 폴더에 있다. `.runtime`은 Git 추적 대상이 아니므로 재현할 장비로 해당 자료를 별도 복사해야 한다.

- `.runtime/jetson-performance-20260926/`: frozen manifest, 비교 실행기, F16/Q8/Q5 전체 원문·해시·native 로그, 18개 paced replay, 같은 F16 도움 요청 수정 전후, 회귀 RED/GREEN.
- `.runtime/conversation-user-eval-20260926/`: 실제 일반 대화 10턴, API 사용량·지연, CLI 기록, 별도 테스트 DB와 source SHA.
- `.runtime/fall-user-eval-20260926/`: 낙상 초기 4/5 결과와 실패한 실제 provider JSON. 이후 수정 검증은 원본 실패를 덮어쓰지 않고 별도 기록한다.
- `.runtime/fall-user-eval-20260926-fix/`: 같은 실패 사례 수정 전 회귀와 수정 후 실제 설정 검증, 관련 183개 검사.

paced replay의 전체 wall time에는 녹음 길이와 EOF tail 대기가 포함된다. 비교 실행기의 해당 `elapsed_s`/`aggregate_rtf`를 순수 추론 처리량으로 사용하지 않는다. 파일별 `inference[].elapsed_s`와 `transcripts[].vad_last_speech_to_text_s`가 각각 추론과 종료 후 지연 근거다.

## 후속 사용 실험: 긴 쉼을 무조건 기다리면 해결되는가

2.3초 중간 쉼이 있는 같은 합성 PCM을 F16/Metal로 다시 재생했다. 기존 정책은
`물이 다쳐있다면`과 `그 앞에서 기다려주세요`를 각각 전사했다. 실험 스크립트에서만
`…다면` 뒤 무음 허용을 3초로 늘리자 전사는 하나가 됐지만, 결과는
`그 앞에서 기다려주세요.`여서 앞의 조건이 사라졌다. 각 정책 1회 관측이며 일반화할
표본은 아니다. **이 대기 연장은 제품에 반영하지 않았다.** 이전 가설을 강제로
이어 붙이면 인식 정정을 덮어쓸 수 있으므로 그런 보정도 적용하지 않았다.
원문·native 로그·소스 해시는 `.runtime/first-user-trial-20260926/pause-experiment-v2/`,
실험 실행기는 같은 상위 폴더의 `pause_check.py`에 보존했다.

두 조각을 실제 Agent에 순차 입력한 경우와 정확한 대조문 `문이 닫혀 있다면`을
입력한 경우를 별도 DB에서 시험했다. 실제 API 4회에서 첫 미완결 문장은 모두
확인 질문을 유발했고, 정확한 대조문의 후속 답변은 앞서 말한 문을 참조했다.
실행 도구가 없는 환경이므로 조건부 로봇 행동의 검증은 아니다.
`.runtime/midpause-agent-eval-20260926/`에 요청·응답과 시간을 기록했다.

AEC 없는 입력에서는 첫 답변 재생이 뒤 발화 도중 시작하면 그 발화가 폐기되는
기존 상태 전이를 대역 입력으로 재현했다. 일반 API 처리 중이라는 이유만으로
발화를 막는 것은 아니며, 재생 시작과 겹치는 것이 원인이다. 실제 마이크·스피커와
클라우드를 결합해 이 충돌 빈도를 측정하지는 않았다. 재생 중 말하기와 긴 중간 쉼은
해결된 항목으로 집계하지 않고 다음 현장 시험에서 관측한다.

종료·재개를 추가로 실제 API 6턴에서 확인했다. “이제 그만하고 쉴게”와 최종 종료
표현에 추가 질문 없이 답했고, 같은 프로세스에서 `ConsoleCore`와 DB 연결을 닫았다가
새로 만든 뒤에도 정정된 목요일과 시간·장소·상대를 회상하고 초안에 반영했다.
응답 중앙값은 2.209초(범위 1.971–3.446초)였다. 실제 호출어 재호출, OS 프로세스
재시작, 한 시간 TTL 만료를 실행한 것은 아니다. 이 평가에서는 제품 수정이 필요한
실패가 재현되지 않았다. 원문·사전 기준·연결 수명 기록은
`.runtime/conversation-ending-eval-20260926/`에 보존했다.

현재 발화 상태만 보고 Agent의 답변 발행을 보류하는 변경도 검토했다. 입력 큐
overflow 때 일반 발화의 종료 이벤트가 없고, 이미 TTS에 보낸 요청의 합성 중에
새 발화가 시작할 수도 있다. 두 반례와 소스 검토는
`.runtime/speech-turn-handoff-20260926/`에 기록했다. 단순 발행 보류를 충돌 해결로
채택하지 않았으며, 현재 noAEC 배포 정책을 유지한다. 실제 로봇과 최초 사용자에게
필요한 네 완료 기준의 미검증 근거는
`.runtime/first-user-trial-20260926/completion-audit.md`에 별도로 정리했다.
