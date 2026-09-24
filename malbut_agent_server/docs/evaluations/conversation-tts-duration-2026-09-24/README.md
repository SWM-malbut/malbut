# 긴 설명 TTS 길이 측정

2026-09-24 KST. 기존 [합성 대화 평가](../conversation-spec-final-followup-2026-09-24.json)의
`long_explanation` 응답 두 개를 현재 생산용 `OpenAISynthesizer`로 합성했다.
이번 실행은 새 LLM 대화 생성이나 물리 음성 왕복 검사가 아니다.

| 표본 | 문자 수 | PCM frame 수 | 전체 음원 길이 | 첫 버퍼 반환 대기 | 합성 완료까지 |
|---|---:|---:|---:|---:|---:|
| [explanation-1.wav](explanation-1.wav) | 163 | 613,200 | **25.55초** | 2.456초 | 5.851초 |
| [explanation-2.wav](explanation-2.wav) | 197 | 669,600 | **27.90초** | 1.962초 | 5.865초 |

두 파일은 **AI 생성 음성**이며 `gpt-4o-mini-tts`·`marin`, 24 kHz·mono·signed 16 bit
PCM이다. 전체 frame 수 / 24,000으로 길이를 구했다. ROS·마이크·스피커·로봇
장치는 사용하지 않았다. API 요청은 정확히 2회, SDK 재시도는 0회다.

## 실행 근거

[measure.py](measure.py)는 실제 실행한 `/tmp/malbut-tts-duration.py`의 보관본이다.
저장소 루트에서 실행한 명령은 다음과 같다.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=malbut_tts \
  /tmp/malbut-context-venv/bin/python /tmp/malbut-tts-duration.py --live
```

실행기는 `--live`, 새 출력 디렉터리, 공식 speech endpoint, 최대 2회 요청,
SDK `max_retries=0`을 검사한다. 기존 산출물이 있으면 중단하므로 이 명령을
다시 실행해 저장 결과를 덮어쓰거나 추가 과금하지 않는다. 실행기 원문을
보존했으며 경로를 바꿔 추가 평가하는 범용 도구는 아니다.

`completed=true`는 generator가 정상 종료하고 PCM이 존재하며 취소되지 않았을
때에만 기록한다. 두 표본 모두 true이고 `error_type`이 없다. 실행 기록이 정상
스트림 종료의 근거이며 WAV 파일만으로 종료 상태를 역으로 판정하지 않는다.
[results.json](results.json)에 원본 텍스트와 API 요청 본문, 원본·실행기·TTS
소스·WAV의 SHA-256, 환경 버전, 측정값을 보관했다. 실행기 해시, 오프라인
검사 결과와 가격 참고 항목은 실행 뒤 기록에 추가했다. 키·헤더·원시 오류
응답은 보관하지 않았다.

환경은 Python 3.12.13·OpenAI SDK 2.54.0·NumPy 1.26.4다. 기존 임시 venv에
TTS 요구사항의 SDK와 NumPy만 설치했으며 `sounddevice`는 설치하지 않았다.
실행 전 아래 네트워크 없는 검사는 **39 passed, 4.31초**였다.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=malbut_tts \
  /tmp/malbut-context-venv/bin/python -m pytest -q \
  malbut_tts/test/test_api_synthesis.py malbut_tts/test/test_api_synthesis_sdk.py
```

## 해석 범위

- 길이는 확인 질문과 무음을 포함한 전체 파형이다. 질문 시작 시각, 설명만의
  길이와 음성 내용의 청취 일치는 검사하지 않았다.
- 첫 버퍼 반환 대기는 초기 400ms 분량의 PCM이 준비된 뒤 처음 반환된 시간이다.
  원시 첫 바이트 지연이나 스피커 재생 시작 시간이 아니다.
- 두 저장 응답의 약 30초 분량만 확인했다. 모든 응답의 30초 상한, 현재 전체
  LLM→TTS 경로, 물리 재생 `finished`나 Jetson 지연의 검증으로 확대하지 않는다.
- [공식 모델 가격](https://developers.openai.com/api/docs/models/gpt-4o-mini-tts)은
  2026-09-24 확인 기준 텍스트 입력 $0.60/백만 토큰·음성 출력 $12/백만 토큰이다.
  이 PCM 응답에서는 토큰 사용량을 얻지 못했다. 음원 길이로 사용 비용이나
  실제 청구액을 역산하지 않았다.
