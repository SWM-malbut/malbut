# 음성 명령 터미널 실험실

이동·따라오기·순찰·취소를 직접 입력하거나 정해진 시나리오로 실행한다.
경로는 **터미널 발화 → 실제 Agent → 실제 Manager → 시험용 Action 서버**다.
직접 입력은 기본 mock 또는 `--chat`의 실제 OpenAI Provider를 선택할 수 있다.
자유 대화 모드에서는 일상 대화·질문·후속 질문과 이동 명령을 같은 대화에서 처리한다.
STT는 텍스트 입력으로 대신하고 응답은 터미널에 표시한다. 실물 로봇·마이크·스피커는 사용하지 않는다.
자동 시나리오는 항상 mock으로 실행하며, 물리 주행은 이 실험실의 검증 범위에 포함되지 않는다.
mock의 정규식은 `providers/mock_speech_intent.py`에서 고정 시험 응답을 만드는 용도로만 사용한다.
실제 음성 경로는 정규식·키워드 허용 목록 대신 LLM이 의미를 해석한다.

## 시작

자유 대화와 작업 요청을 함께 시험하려면 저장소 루트에서 실행한다.

```bash
./scripts/voice_lab.sh --terminal --chat
```

`--chat`은 기존 `~/.config/malbut/agent.env`가 있으면 읽고 OpenAI Provider를 선택한다.
프로세스 환경 변수는 파일 값보다 우선하며, `--model`은 대화 모델을 명시적으로 바꾼다.
다른 기존 설정 파일은 `--env-file /절대/경로/agent.env`로 지정한다. 키는 출력하거나 새로 저장하지 않는다.
자유 대화의 입력·문맥은 설정된 OpenAI API로 전달되며, 로봇 기능은 계속 시험용 Action 서버에서 실행한다.

```bash
./scripts/voice_lab.sh --terminal --chat --env-file ~/.config/malbut/agent.env
./scripts/voice_lab.sh --terminal --provider mock
```

새 터미널 창이 열린다. 현재 터미널에서 실행하려면 `--terminal`을 생략한다.
실제 모델 연결에 실패하면 오류를 알리며 mock 응답으로 바꾸지 않는다.
첫 실행은 `malbut_interfaces`만 사용자 캐시에 따로 빌드한다. 원래 작업 공간의 설치 결과를 덮어쓰지 않는다.
ROS 2 Humble, colcon, Python의 PyYAML·tiktoken·pytest가 필요하다.
Python 의존성이 부족하면 사용자 캐시 설치를 시도한다. 기본 Python은 `/usr/bin/python3`이고
다른 인터프리터는 `MALBUT_VOICE_LAB_PYTHON`으로 지정할 수 있다.

ROS는 localhost와 실행마다 새로운 namespace를 사용한다. 기본 domain은 197이다.
Action의 Goal·Result·취소 서비스와 Feedback·Status Topic까지 시험용 이름으로 연결한다.
지도·등록 목적지·Manifest·대화 DB는 각 실험용 임시 파일이다.

## 자유 대화와 직접 실행

자유 대화 모드에서는 별도 접두사 없이 자연스럽게 입력한다. 이전 질문과 답변이 같은 대화 문맥에 유지된다.

```text
주말 여행 후보로 제주도와 부산을 비교하고 있어. 각각의 장점을 말해줘.
내가 방금 말한 두 곳 중 바다 산책하기 좋은 곳은 어디야?
따라와
아까 이야기하던 여행 계획을 조금 더 짜보자.
멈춰
```

`/new`는 새 대화 문맥을 시작한다. `/reset`은 시험 작업까지 정리하고 DB·그래프를 새로 만든다.
모델 응답 대기시간은 Provider의 전체 요청 제한시간에 여유를 더해 적용한다.

일반 문장을 입력하면 최종 발화로 전달한다. 기본 시험 목적지는 `거실`, `주방`, `현관`이다.

```text
거실로 가줘
거실로 가볼까?
따라와
/status
멈춰
꼼꼼히 순찰해
```

기본 따라오기는 계속 실행하며, 이동·순찰은 짧은 시험 실행 후 성공한다.
`[하위 Goal]`에서 실제 Manager가 보낸 인자, `[Manager]`에서 접수·진행·종료,
`[응답]`에서 Agent가 음성 출력 Topic으로 발행한 문장을 확인한다.
실제 주행 완료를 재현하는 대신 공개 ROS 계약과 실행 경로를 확인한다.
실제 LLM은 `거실로 가볼까?` 같은 현재 이동 제안과 표현 변형·사투리·인식 오탈자를 의미로 해석한다.
`거실로 갈 수 있어?` 같은 기능 질문, `거실로 가보지 마` 같은 부정문,
`"거실로 가볼까?"라는 문장을 설명해` 같은 인용문을 실제 실행 요청과 구분하는지도 `--chat`에서 확인한다.
이 의미 구분은 LLM 책임이며 결정적 서버 검사로 보장되는 동작은 아니다.
서버는 제공 도구·인자·유효 시간·지도·대화 저장 및 변경·취소 소유권·중복 전송 검사를 유지한다.

바로 앞의 목적지 확인 질문에 `거실`이라고 명시적으로 답하면 LLM이 현재 이동 요청과 연결할 수 있다.
과거 대화나 기억만으로 오래된 작업을 다시 시작해서는 안 된다.
`와바라`, `이리 오너라`는 의미를 이해해도 발화자 위치와 일회성 접근 기능이 없어 등록된 목적지를 묻는다.
이때 임의 좌표를 만들거나 계속 실행되는 따라오기를 시작해서는 안 된다.

| 명령 | 기능 |
| --- | --- |
| `/scenarios` | 실행 가능한 시나리오 ID와 설명 |
| `/run ID` | 해당 시나리오 하나 실행. 목록 번호도 허용 |
| `/all` | 전체 독립 시나리오 실행 |
| `/regression` | 전송 직전 만료·대화/기억 변경·상황 선점·TTS 실패 등의 집중 회귀 |
| `/status` | 현재 미션 관측과 하위 Goal 목록 |
| `/behavior 기능 결과 [초] [취소지연초]` | 다음 하위 Goal에 적용할 시험 동작 설정 |
| `/map home\|other\|none\|mapping\|switching\|error` | 시험용 지도·위치 추정 상태 변경 |
| `/repeat` | 마지막 발화를 같은 ID로 다시 전달하여 중복 방지 관찰 |
| `/cancel` | `멈춰` 발화 전달 |
| `/new` | 새 대화 문맥 시작 |
| `/reset` | 현재 시험 작업 정리 후 새 DB·그래프로 초기화 |
| `/help`, `/quit` | 도움말, 작업 정리 후 종료 |

`/run`, `/all`, `/regression`은 현재 수동 시험을 정리하고 실행한다.
자동 검증에는 항상 mock을 사용하고 완료되면 선택한 Provider의 수동 시험 환경을 새로 시작한다.
이때 이전 대화 문맥도 초기화한다. 각각의 자동 시나리오 역시 독립된 그래프를 사용한다.

### 실제 모델 의미 해석 확인

2026-09-28에 OpenAI `gpt-5.6-luna`와 격리된 실제 Agent·Manager, 시험용 Action 서버로
아래 16개 발화를 확인했다. 정답은 mock 파서와 독립적으로 정했고, 모델 결정·인자와
실제 하위 Goal을 대조했다. API 오류나 무응답을 비실행 성공으로 세지 않았다.
각 사례를 한 번 확인한 결과이며 모든 표현이나 반복 실행의 정확도를 보장하지 않는다.

| 입력 | 관측 결과 |
| --- | --- |
| 우리 거실로 가볼까 | 거실 이동 Goal |
| 거실로 와바라 | 거실 이동 Goal |
| 주방으로 오너라 | 주방 이동 Goal |
| 어, 그… 거실로 좀 가주이소 | 거실 이동 Goal |
| 거실로, 아니 주방으로 가줘 | 최종 정정된 주방 이동 Goal |
| 제 뒤를 따라오실래요 → 이제 그만 따라와 | 추적 Goal 이후 같은 작업의 취소 종료 |
| 집안을 좀 꼼꼼히 둘러봐 줘 | 꼼꼼한 순찰 Goal |
| 와바라 → 거실 | 장소 확인 질문 이후 거실 이동 Goal |
| 이리 오너라 | 장소 확인 질문, 이동·추적 Goal 없음 |
| 거실로 갈 수 있어? | 대화 응답, Goal 없음 |
| 거실로 가보지 마 | 대화 응답, Goal 없음 |
| “거실로 가볼까?”라는 문장을 설명해 | 문장 설명, Goal 없음 |
| 내일 주방으로 가줘 | 확인 질문, 즉시 실행 Goal 없음 |
| 거실로 가고 순찰해 | 작업 확인 질문, Goal 없음 |

## 실패와 취소를 직접 재현

기능 이름은 `nav`, `follow`, `patrol`이며 결과는 `success`, `hold`, `abort`, `reject`다.
설정은 이후 받은 Goal에 적용한다. 현재 실행 중인 Goal을 즉시 바꾸지는 않는다.

```text
/behavior nav hold
거실로 가줘
/status
멈춰

/behavior patrol abort
순찰해

/behavior follow reject
따라와

/behavior patrol hold 0.3 2
순찰해
멈춰
```

마지막 예시는 취소 종료를 2초 지연시켜 접수와 종료가 다르다는 점을 보여준다.
`follow success`는 시험 서버가 의도적으로 성공 결과를 내보내는 장애 주입용 설정이다.
실제 따라오기 기능은 지속 작업이며, 일반 `follow` 시나리오는 자동 성공 종료를 기대하지 않는다.

```text
/map other
거실로 가줘
/map home
거실로 가줘
/repeat
```

이 예시는 목적지 설정과 지도 불일치, 정상 지도 복귀, 같은 발화 재수신을 확인한다.
지도 상태 조작은 Agent의 장소 이동 전송 조건을 검증한다.
시험 Manager는 SLAM·AMCL을 시작하지 않으므로 실제 위치 추정과 전체 지도 전환 운영을 검증하지 않는다.

## 자동 시나리오

목록의 정상·실패 조건은 정해진 발화와 시험용 Action 결과로 재현한다.
자동 통과는 서버 검사와 ROS 연결 경로를 확인하며, 실제 LLM의 자연어 정확도를 측정하지 않는다.
특히 mock의 인용·부정·복합 요청 사례는 고정 응답에 대한 연결 시험이다.
표현 변형이나 대화 문맥을 포함한 실제 모델의 의미 구분은 `--chat`에서 별도로 평가한다.
전체 자연어 표현이나 모든 현실 장애를 포괄한다는 의미는 아니다.

| ID | 시나리오 |
| --- | --- |
| `navigation` | 목적지 이동 |
| `navigation-suggestion` | 제안형 목적지 이동 |
| `follow` | 사람 따라가기 |
| `cancel-follow` | 따라가기 취소 |
| `patrol-light` | 가벼운 순찰 |
| `patrol-normal` | 기본 순찰 |
| `patrol-thorough` | 꼼꼼한 순찰 |
| `cancel-navigation` | 이동 취소 |
| `cancel-patrol` | 순찰 취소 |
| `cancel-delayed` | 취소 접수와 종료 분리 |
| `abort-navigation` | 이동 실행 실패 |
| `abort-follow` | 추적 실행 실패 |
| `abort-patrol` | 순찰 실행 실패 |
| `reject-navigation` | 이동 Goal 거절 |
| `reject-follow` | 추적 Goal 거절 |
| `reject-patrol` | 순찰 Goal 거절 |
| `preempt-base` | BASE 작업 선점 |
| `unknown-place` | 등록되지 않은 목적지 |
| `map-not-selected` | 저장 지도 미선택 |
| `map-mismatch` | 목적지와 지도 불일치 |
| `map-switching` | 지도 전환 중 |
| `map-error` | 지도 상태 오류 |
| `capability-question` | 이동 기능 질문 |
| `negation` | 부정 명령 |
| `quotation` | 인용문 |
| `multiple-tasks` | 복합 요청 |
| `relative-destination` | 불명확한 목적지 |
| `duplicate` | 중복 발화 |
| `cancel-empty` | 취소할 작업 없음 |
| `manager-unavailable` | Manager 연결 없음 |
| `navigation-disabled` | 목적지 설정 없음 |

## 터미널 없이 일괄 실행

```bash
./scripts/voice_lab.sh --list
./scripts/voice_lab.sh --scenario cancel-delayed
./scripts/voice_lab.sh --all --report /tmp/voice-lab/results.json
./scripts/voice_lab.sh --regression --report /tmp/voice-lab-regression/results.json
```

자동 실행은 전부 통과하면 종료 코드 0, 실패한 시나리오가 있으면 1을 반환한다.
준비 오류는 2, 사용자의 Ctrl+C 중단은 130이다. 각 시나리오는 실제 관측과 기대값을 대조한다.
종료 시 자동 정리 과정에서 발생한 취소를 사용자 취소의 성공 증거로 세지 않는다.

결과 파일에는 사례별 판정·검사 내용·Goal·Manager 이벤트·응답을 저장한다.
관측 로그는 결과 파일 이름 뒤에 `.events.jsonl`을 붙여 기록한다.
기본 저장 위치는 `~/.local/state/malbut/voice-lab/<실행시각>/results.json`이다.
완료된 사례는 즉시 저장하므로 이후 시나리오에서 중단해도 앞선 결과가 유지된다.
회귀 실행은 같은 디렉터리에 `regression.log`, `regression.xml`도 남긴다.

실행 계약은 [기능 명세](MANAGER_VOICE_SPEC.md)와 [인터페이스 정의](MANAGER_VOICE_INTERFACE.md)를 참조한다.
