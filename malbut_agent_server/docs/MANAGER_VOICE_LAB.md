# 음성 명령 터미널 실험실

이동·따라오기·순찰·지도 작성·위치 보정·취소를 입력하거나 정해진 시나리오로 실행한다.
경로는 **터미널 발화 → 실제 Agent → 실제 Manager → 시험용 Action 서버**다.
직접 입력은 기본 mock 또는 `--chat`의 실제 OpenAI Provider를 선택할 수 있다.
자유 대화 모드에서는 일상 대화·질문·후속 질문과 이동 명령을 같은 대화에서 처리한다.
STT는 텍스트 입력으로 대신하고 응답은 터미널에 표시한다. 실물 로봇·마이크·스피커는 사용하지 않는다.
`voice_lab` 내장 자동 시나리오는 항상 mock으로 실행한다. 아래의 별도 실모델 평가기는
실제 OpenAI를 호출한다. 물리 주행은 두 실험실 모두의 검증 범위에 포함되지 않는다.
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
새집 지도를 만들어 줘
위치 다시 찾아줘
지도 전체에서 위치 다시 찾아줘
```

기본 따라오기는 계속 실행하며, 나머지 기능은 짧은 시험 실행 후 성공한다.
지도 작성은 말한 이름을 `map_name`으로 전달하고, 위치 보정은 일반 요청을 `method=0`,
명시적인 전체 지도 탐색을 `method=2`로 전달한다. 시험 서버는 지도 파일을 실제로
만들거나 AMCL을 구동하지 않으며 Result도 시험 데이터다.
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

2026-10-19 지도 이름 대기 상태 추가 후 최종 시험은 216개 시나리오 전체 1회,
이름 14종 각 20회, 상태 경계·기존 오류·Manager 미연결 13종 각 10회다.
총 626회 시나리오·1,145개 발화가 모두 통과했다. 기존 설정의 실제
`gpt-5.6-luna`(`reasoning=low`)와 실제 Agent/Manager, 시험용 Action 서버를 사용했다.
이름 답변 280회 중 180회는 상태에 묶인 필드 입력으로 도구·이름을 제한했고,
100회는 `tool_choice=auto`에서 LLM이 선택했다. 전체를 자유로운 분류 정확도로 계산하지 않는다.
관련 단위·ROS·배포 복사본 검사는 중복을 제외하고 520개 통과했다.
집계는 [지도 이름 상태 평가 결과](../tools/map_voice_eval/result-20261019-state.json)에 있다.
원문·CSV·시험 소스는 작업 공간의 `/home/shin/ros2_ws/voice_map_state_20261019/`에 보존했다.
첫 수정본의 `주방으로` 이동 오분류 1회와 중단한 반복 기록도 보존했으며,
필드 입력 보강 후 이름 반복과 전체 시나리오를 처음부터 재실행했다.

지도 작성·순찰·위치 보정의 실모델 일괄 평가는
`tools/map_voice_eval/catalog.py`에 정답을 먼저 선언하고 `run_sweep.py`로 실행한다.
각 시나리오는 독립 대화·임시 지도·실제 Agent/Manager·시험용 Action 서버를 사용한다.
이름 후속 답변·정정·중단·주제 전환, 도구와 옵션, 인용·부정·예약·복합 요청,
잘못된 이름, 취소, 하위 거절·실패, Manager 부재와 작업 교체를 포함한다.

`malbut_interfaces`가 빌드된 ROS 환경에서 저장소 루트를 기준으로 실행한다.
이 명령은 `--chat`과 같은 기존 API 설정을 사용하여 실제 모델을 호출한다.

```bash
PYTHONPATH="$PWD/malbut_agent_server:$PWD/malbut_system_manager:$PYTHONPATH" \
  /usr/bin/python3 malbut_agent_server/tools/map_voice_eval/run_sweep.py \
  --output /tmp/map-voice-live.json --domain 194
```

`--only map-exact-1`은 “지도 만들어줘 → 우리집”만 시험한다.
`--repeat 20`을 더하면 같은 대화를 매번 새 세션에서 20번 실행한다.
결과의 `source_id`와 `repeat`로 원본 시나리오와 반복 번호를 구분한다.
독립 프로세스로 분할하려면 서로 다른 `--domain`을 사용하고
`--shards 2 --shard 0`, `--shards 2 --shard 1`을 각각 지정한다.
결과에는 실행 전 기대값, 모델명·응답 ID, 정책 적용 전 Provider 응답,
정책 적용 후 결정, 실제 하위 Goal·Manager 결과·출력 문장을 기록한다.
API 오류와 무응답은 비실행 성공으로 세지 않는다. `raw_decision`은 기존 Orchestrator의
필드명으로 기억 정책 적용 후 바뀔 수 있으므로 모델 원문 판단에는
`provider_before_policy`를 확인한다.
이름 대기 상태 회귀는 `--only state-name-workshop-quoted state-name-workshop-particle`
등 `state-*` 시나리오로 실행한다. Provider 입력의 `mapping_name_requested`, 이름 질문의
`clarification_for`, 실제 API payload의 `mapping_context`·`tool_choice`·도구 스키마를
함께 기록한다. 대기 중 유효한 인용 이름만 전달한 경우와 등록된 장소명에 조사만 붙인
답변은 명시적인 필드 입력으로 도구와 이름이 제한된다. 미등록 이름·다른 문장은
`tool_choice=auto`에서 LLM이 추출하므로 두 경우를 나눠 평가한다.
payload 기록에는 API 헤더·키·환경 설정을 넣지 않는다.
도구·인자 정답은 이 Provider 원문에도 적용하며, Manager 부재 사례는
불가 안내가 정확히 한 번 발행되는지도 검사한다. 음성 횟수는 SpeechRequest
발행 횟수이며 실제 스피커 재생 횟수를 측정한 것은 아니다.
Manager의 작업 교체는 `FAILED`와 `preempted` 사유이며, 사용자의 취소와 구분한다.
평가기는 하위 Action 취소가 끝난 뒤 새 Goal이 도착하는지도 확인한다.

2026-10-18에는 기존 설정의 `gpt-5.6-luna`, reasoning `low`로 초기·진단·재시험을 합해
445개 발화를 실제 모델에 전달했다. 수정 후 전체 시험은 179개 독립 시나리오,
214개 발화이며 211개 통과·3개 불일치다. 서버/ROS/기억 정책 등 별도 회귀 시험은
368개 통과했다. 원문 로그와 기대값을 바꾸지 않고 실패 사례도 보존했다.
집계는 [평가 결과](../tools/map_voice_eval/result-20261018.json)에 있다.

“지도 만들어줘”는 이름을 묻고 “우리집”이라는 답을 `autoslam(map_name="우리집")`으로
전달했다. 이름 없는 지도 저장과 명령 정정을 개인 기억 관리로 가로채던 서버 코드를
수정했고, 잘못된 파일 이름은 준비 단계의 일반 오류 대신 정책 단계에서 이름 조건을
안내한다. 예약·지연 실행과 지원하지 않는 순찰 조건도 도구 지침에 명시했다.

| 2026-10-18 시험에서 남은 불일치 | 기대 | 당시 관측 |
| --- | --- | --- |
| 이름 질문 뒤 `"새집으로"` | 따옴표 안 이름 전체 보존 | `map_name="새집"`으로 요청 |
| 지도 이름 질문 뒤 `지도는 됐고 대충 순찰해` | `light` | `normal`로 요청 |
| `지도 만들기는 그만하고 순찰 기능이 뭔지 설명해줘` | 설명 응답 | 취소 도구 선택 후 진행 중인 작업이 없다는 응답 |

앞의 두 오류는 형식상 유효한 다른 인자이므로 서버의 형식 검사를 통과했다.
단일 문장이나 한 차례 성공으로 같은 표현의 반복 정확도를 보장할 수 없다.
당시 추가 관찰로 Manager 부재 시 세 기능 모두 불가 안내가 두 번 발행됐다.
이 중복 안내는 별도 경고로 기록했으며 도구·전송 검사의 통과 여부와 구분한다.
마이크 인식·실물 주행·실제 지도 생성·AMCL 위치 추정은 이 평가의 범위 밖이다.

2026-10-19에는 세 의미 해석 오류의 도구 설명과 시스템 지침을 보완했다.
명시적으로 인용한 이름 전체를 보존하고, 주제 전환 뒤 새 명령의 순찰 강도를 유지하도록 지시한다.
필수 정보를 묻는 중인 미접수 요청의 철회는 현재 질문으로 넘어가는 것으로 해석하며,
실제로 접수된 작업을 멈추라는 명령은 계속 취소 도구로 처리한다.
의미 판단은 이 지침과 대화 문맥을 받은 LLM이 하며 문장별 고정 분기표를 추가하지 않았다.
확대 회귀에서 발견한 “응/네” 후속 답변도 이름을 제공한 것으로 처리하지 않고,
지도 작성 문맥을 유지한 채 이름을 다시 묻게 보완했다.
장소 이름처럼 보이는 지도 이름 답변이 이동 안내로 바뀌는 사례도 있어,
등록 목적지 규칙은 실제 이동 의도를 확인한 뒤에만 적용하도록 범위를 명시했다.
이 규칙의 회귀에는 실제 이동 명령, 이동 질문의 후속 목적지, 지도 이름으로 쓴 장소명,
지도 작성에서 이동으로 바꾼 요청을 함께 포함한다.

Manager 미연결 시에는 `submit()` 내부의 즉시 실패 이벤트와 대화 응답이 같은 안내를
각각 발행하고 있었다. 이제 같은 Manager 요청 ID의 즉시 이벤트를 대화 응답 발행까지
보류하고, 같은 안내가 발행된 경우에만 이미 안내한 것으로 기록한다.
대화 응답이 발행되지 않으면 이벤트 안내를 유지하며, 다른 요청과 이후의 실제 완료
알림은 별도로 처리한다. 합성 음성과 녹음 안내 경로 모두 이 규칙을 사용한다.
실험 그래프의 낙상 확인 토픽도 자체 네임스페이스로 분리하여, 같은 ROS 도메인의
다른 시험에서 보낸 낙상 이벤트가 평가 중인 대화를 중단하지 않게 했다.

최종 코드는 `gpt-5.6-luna`, reasoning `low`로 195개 전체 회귀 시나리오(248개 발화)와
8개 핵심 대화의 각 20회 반복(160개 시나리오, 300개 발화)을 평가했다.
합계 548개 발화 중 545개 통과·3개 불일치였으며 API/시험 실행 오류는 없었다.
기존 세 재현 대화는 각각 20/20 통과했다. Manager 미연결은 세 기능 각각 20/20이며
불가 안내가 요청당 정확히 한 번 발행됐다. “응”을 거친 이름 확인도 20/20 통과했다.
서로 다른 응답 ID를 확인했으며 도구 선택·인자는 정책 적용 전 LLM 응답까지 검증했다.
별도 단위·ROS·배포 일치 검사는 중복을 제외한 313개가 통과했다.

잔여 불일치는 모두 지도 이름 질문 뒤 `"작업실로"`라는 추가 표현을 이동 요청으로
오해해 목적지를 되묻는 경우다. 20회 반복에서 2회, 전체 회귀에서 1회 관측했으며,
잘못된 Action Goal을 전송한 것은 아니다. 이 표현 전체의 의미 해석은 아직 안정적이지 않다.
도구 설명과 대화 지침은 LLM의 의미 판단을 유도하며, 형식 검증만으로 이러한 오분류를
모두 잡을 수는 없다. 실패를 통과로 바꾸거나 제외하지 않고
[2026-10-19 집계](../tools/map_voice_eval/result-20261019.json)에 함께 기록했다.
중간 진단과 중단한 시험을 포함해 기록된 실모델 응답은 총 1,825개다.

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

기능 이름은 `nav`, `follow`, `patrol`, `autoslam`, `relocalize`이며
결과는 `success`, `hold`, `abort`, `reject`다.
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

/behavior autoslam hold
새집 지도를 만들어 줘
지도 만들기 취소해

/behavior relocalize abort
위치 다시 찾아줘
```

`patrol hold 0.3 2` 예시는 취소 종료를 2초 지연시켜 접수와 종료가 다르다는 점을 보여준다.
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
| `mapping` | 발화의 지도 이름으로 자동 지도 작성 요청 |
| `relocalize-auto` | AUTO 위치 보정 |
| `relocalize-global` | GLOBAL_SEARCH 위치 보정 |
| `cancel-mapping`, `cancel-relocalize` | 지도 작성·위치 보정 취소 |
| `abort-mapping`, `abort-relocalize` | 지도 작성·위치 보정 실패 |
| `reject-mapping`, `reject-relocalize` | 지도 작성·위치 보정 Goal 거절 |
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
