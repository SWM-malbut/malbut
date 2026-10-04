# 통합 대화·장기기억 체험

저장소 루트에서 실행한다. ROS나 실제 로봇 없이 대화, 이야기 기억, 모의 로봇과 낙상 확인을 체험한다.
코드는 Git에 포함되며 DB와 음성 모델은 `.runtime`에 별도로 둔다.

## 텍스트로 시작

Python 3.10 이상을 사용한다. 다음은 새 가상환경을 만드는 예다.

```bash
python3 -m venv .runtime/agent-console/venv
.runtime/agent-console/venv/bin/python -m pip install -r malbut_agent_server/requirements-openai.txt
./scripts/agent_console.sh --provider mock --no-tts
```

`mock --no-tts`는 외부 키, 음성 패키지, 모델 파일, 마이크, 스피커 없이 동작한다.
자동 이야기 요약은 실제 OpenAI 모드에서 제공한다. mock 모드에서 기억을 켜도 저장했다고 가장하지 않는다.

날씨는 로컬 지역 저장과 기상청 조회 실행기를 연결한다. 실제 조회에는 `.env` 또는 환경변수의
`KMA_SERVICE_KEY`가 필요하다. OpenAI 대화에서 “여기는 서울 강남구야” 다음 “오늘 날씨 어때?”라고
말하거나, `/tool set_weather_location {"location":"서울 강남구"}` 다음 `/tool get_weather`로 직접 확인한다.
지역은 체험 DB 옆의 `<DB 경로>.weather.sqlite3`에 유지하며, 지역이 모호하면 선택을 요청한다.
시작할 때는 외부 날씨 API를 호출하지 않는다. 로봇 도구는 계속 모의 실행이다.

다른 Python을 쓰려면 `MALBUT_AGENT_CONSOLE_PYTHON`에 해당 실행 파일 경로를 지정한다.
런처는 `.runtime/agent-console/venv`를 우선 사용하고, 기존 Mac 체험용
`.runtime/fall-voice-20260925/venv`가 있으면 재사용할 수 있다. 패키지를 자동 설치하거나 모델을 다운로드하지 않는다.

## 실제 대화와 이야기 기억

기존 저장소 루트 `.env` 또는 환경변수에 OpenAI 설정을 넣는다. 키는 Git에 넣지 않는다.
`malbut_agent_server/.env.example`의 설정 이름을 참고한다.

```bash
./scripts/agent_console.sh --provider openai --no-tts
```

첫 시작에서 별도 동의 내용을 읽고 `네`로 켠다. 파이프로 입력할 때는 자동 질문을 끼워 넣지 않으므로
`/stories on` 다음 `네`를 직접 입력한다. 기존 개인화 동의가 이야기 기억 동의를 대신하지 않는다.

1. “오늘 바다 전시를 봤는데 마음이 편해졌어. 다음에는 일요일에 다시 가고 싶어.”처럼 이야기한다.
2. `/stories sync`로 정리를 기다린다.
3. `/new`로 새 대화를 열고 “바다 전시 이야기 이어가자”라고 한다.
4. `/stories`에서 이야기 제목과 현재 상황을 확인한다.

| 명령 | 기능 |
| --- | --- |
| `/stories on`, `/stories off` | 별도 동의 후 켜기 / 새 저장·재사용 중지 |
| `/stories sync` | 진행 중 정리 기다리기, 기술적 실패 재시도 |
| `/stories sources ID` | 해당 이야기의 원문 근거 확인 |
| `/stories delete ID` | 해당 원문 부분과 연결된 기억 삭제 |
| `/stories history` | 표시된 과거 대화 범위를 확인한 뒤 별도 동의하여 포함 |
| `/new` | 대화만 새로 시작하고 장기기억 유지 |
| `/memory`, `/status` | 사실·이야기 기억과 처리 상태 확인 |
| `/help`, `/quit` | 전체 명령 / 종료 |

이야기 ID는 앞부분이나 제목으로도 선택할 수 있다. “바다 전시 이야기 잊어줘”처럼 말해도 된다.
대상이 여러 개면 선택을 요청한다. 정정은 일반 대화로 전달한다.
요약 전의 기록 때문에 삭제 범위를 확정할 수 없으면 먼저 정리 또는 해당 기록 처리에 대한 동의가 필요하다.
내용을 재사용한 말벗 답변은 삭제 과정에서 전체가 비워질 수 있다.

기본 DB는 `.runtime/agent-console/agent.sqlite3`다. `--database 경로`로 다른 DB를 쓸 수 있다.
기존 체험 DB를 쓰려면 그 경로를 명시한다. 기존 DB를 자동 복사하거나 삭제하지 않는다.
원문·요약과 정정·삭제 범위의 자세한 기준은 [장기기억 명세](../../docs/LONG_TERM_CONTEXT_MEMORY.md)를 따른다.
콘솔과 일반 서버는 같은 이야기 기억 엔진을 사용한다. 동의와 기억은 DB·사용자 ID별로 유지한다.
일반 서버의 인증·동의·기억 관리 API는 [명세 8.8절](../../docs/LONG_TERM_CONTEXT_MEMORY.md#88-일반-서버에서-쓰는-방법--구현한-범위)을 참고한다.

## 선택: 실제 음성

```bash
.runtime/agent-console/venv/bin/python -m pip install \
  -r malbut_stt/requirements-whisper-cpp.txt \
  -r malbut_tts/requirements-api.txt
./scripts/agent_console.sh --stt-model /path/to/ggml-small.bin \
  --stt-library /path/to/libmalbut_whisper.dylib
```

음성 인식은 로컬 Whisper 모델과 ABI 3 라이브러리가 필요하다.
[네이티브 빌드 안내](../../../malbut_stt/native/README.md)를 참고한다.
Linux 라이브러리는 `.so`, Mac 라이브러리는 `.dylib`다. 모델과 빌드 결과를 Git에 넣지 않는다.
`MALBUT_CONSOLE_STT_MODEL`, `MALBUT_CONSOLE_STT_LIBRARY` 환경변수로 경로를 지정할 수도 있다.
기존 Mac `.runtime` 자산은 파일이 있을 때만 기본값으로 재사용한다.

Enter 또는 `/voice`로 한 번 말하고, `/tts on|off`로 음성 출력을 바꾼다.
음성 합성은 OpenAI를 사용하므로 mock 대화 모드에서도 음성을 켜면 키와 외부 호출이 필요하다.
`--check`는 모델·라이브러리와 장치 준비를 검사하며 녹음·추론·API 호출은 하지 않는다.

## 검사

```bash
.runtime/agent-console/venv/bin/python -m pip install -r .github/requirements/agent-tests.txt
PYTHONPATH=malbut_agent_server .runtime/agent-console/venv/bin/python -m pytest -q \
  malbut_agent_server/test/test_agent_console_*.py
```

텍스트·기억·모의 로봇 검사는 기본 의존성만으로 실행한다. 선택 음성 의존성이 없으면 음성 검사는 건너뛴다.
음성 패키지까지 설치한 환경에서는 `/checks`로 낙상 음성 연결 검사도 함께 실행한다.
검사는 가상 대화와 임시 DB를 사용하며 외부 AI 호출이나 실제 로봇 동작을 하지 않는다.
