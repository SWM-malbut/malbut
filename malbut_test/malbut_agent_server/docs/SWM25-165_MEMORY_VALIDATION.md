# SWM25-165 장기기억 구현 검증 기록

기준은 [기존 Agent 명세](malbut_agent.md)의 대화 처리·장기기억 규칙이다.
이 문서는 구현 결과와 시험 재현 방법이며 별도 기억 저장소 명세가 아니다.

## SWM25-165 마무리 — Luna와 A 방식

현재 Jira 작업 번호는 SWM25-165다. 초기 작업 공간·시험 경로의 SWM25-163은
이전 식별자로 보존하며 아래 초기 검증 기록과 최신 검증을 구분한다.

OpenAI 기본 모델과 `.env.example`을 `gpt-5.6-luna`로 맞췄다. HTTP와 음성
대화 모두 같은 설정·factory·`AgentOrchestrator`를 사용한다. reasoning은
`none`, 출력 상한은 500토큰이며 재시도·자동 fallback은 기본 비활성이다.
기존의 명시적인 모델 설정은 우선하고, Provider 기본값은 오프라인 `mock`을
유지한다. 실제 OpenAI 실행에는 `MALBUT_AGENT_PROVIDER=openai`를 지정한다.

선택한 A 방식은 **답변·기억 후보를 한 번에 생성 → 검증·기억과 대화 기록 확정
→ 응답 반환**이다. 기억 변경과 최종 답변은 같은 트랜잭션에서 확정하며,
B/C 방식은 비교 도구에만 남긴다. 기존 모델 비교 도구의 Luna/Terra 비교 쌍은
운영 기본값과 분리해 유지한다.

마무리 검토에서 부정한 이름을 사실로 저장할 수 있는 원문 검증 오류를
수정했다. `내 이름은 현재가 아니야`의 ‘현재’와 `두부가 아니라 초코야`의
‘두부’는 저장을 거절하고, 후자의 새 이름 ‘초코’는 정정할 수 있다.

### 최신 자동 시험

- macOS·Python 3.12: **889 passed, 2 skipped**. ROS 미설치에 따른 skip이다.
- Ubuntu 22.04·ROS 2 Humble·aarch64·Python 3.10: **902 passed, 0 skipped**.
  실제 ROS Topic·Action 시험 **13개를 포함**한다. 관련 4개 패키지 빌드도
  성공했다. 별도 도메인 194를 사용하고 기존 Manager/domain 159를 유지했다.
- HTTP/음성 factory에서 기본 Luna·reasoning none·500토큰·단일 호출과
  응답 전 실제 기억 확정을 확인하는 회귀 시험을 추가했다.
- 부정된 이름·호칭·반려동물 속성 및 정정의 잘못된 후보를 거절하는 회귀
  시험을 추가했다. Python 변경 파일 35개 flake8과 `git diff --check`를 통과했다.

Ubuntu 전체 시험 후 테스트 함수명만 짧게 변경했으며 실행 코드는 동일하다.
변경 파일의 Provider 시험 39개를 다시 통과시켰다. 최종 Python 128개 파일은
현재 작업 공간과 일치하며 코드 묶음의 SHA-256은
`10ee91171416ff0053533aebab73ce4fb1e2a9aa75b80722f9f60e5a411fdc25`다.

ROS 환경을 source한 다음 `PYTHONPATH=.:"${PYTHONPATH}" python3 -m pytest -q test`로
실행한다. `PYTHONPATH=.`만 지정하면 ROS 메시지 모듈 경로를 잃을 수 있다.
[최신 Ubuntu 검증 요약](validation/SWM25-165_LUNA_2026-09-09/ros-validation-summary.json)과
[소스 일치 검사](validation/SWM25-165_LUNA_2026-09-09/worktree-code-check.json)를 남긴다.
전체 로그·JUnit·빌드 스냅샷은 개발 환경의
`/var/folders/5s/gq4btl_j0cl6870s1__ggygh0000gn/T/malbut-swm25-165-release-ros-wvc3vf55/`에 있다.

### 실제 Luna 연결

별도 임시 SQLite와 가상 사용자로 서버 기본 설정을 사용했다. 제한 시간도
기존 기본값인 시도당 5초·전체 11초이며, 모델 전환이나 자동 재시도는 없다.

| 경로 | 확인한 결과 |
| --- | --- |
| 인증된 loopback HTTP | 동의 질문·동의 → 반려견 이름 저장 → 새 대화 조회 → 정정 → 삭제 → 개인화 중단 |
| 실제 `DialogueWorker` | 재동의 → 반려견 이름 저장 → 응답 준비 → worker 재시작 후 새 대화에서 조회 |
| 저장 시점 | HTTP 응답과 음성 응답 준비 시 SQLite 변경이 이미 확정됨 |
| 모델 | 실제 응답의 model이 모두 `gpt-5.6-luna`, status가 모두 `completed` |

최종 사례는 11개 발화·6회 API 호출로 통과했다. 동의·중단 등 명확한 내부
제어는 모델을 호출하지 않는다. 최초 시험 3회까지 포함하면 총 **9회 API
호출**, 입력 16,934토큰·출력 1,010토큰·캐시 입력 12,109토큰이다.

최초 시험에서는 `콩이야`를 모델이 이름 ‘콩이’로 추출해 시험에서 기대한
‘콩’과 달랐다. 이 결과를 성공으로 집계하지 않았다. 별도 DB에서
`두부가 아니라 초코야`라는 명확한 사례로 전체 흐름을 다시 확인했다.
이는 모든 한국어 이름의 형태소·소유 관계 해석을 검증한 결과가 아니다.
일반적인 한국어 해석 성능과 실제 마이크·스피커 사용 검증은 별도다.

- [최종 실제 응답·저장 결과](validation/SWM25-165_LUNA_2026-09-09/results.json)
- [최초 이름 해석 불일치 기록](validation/SWM25-165_LUNA_2026-09-09/ambiguous-results.json)

실제 모델 시험은 HTTP와 음성 **대화 처리 worker**까지다. `publish_reply`의
시험 콜백을 사용했으며 TTS Topic 발행·합성·재생을 실제로 시험한 것으로
표현하지 않는다. ROS 전송은 고정 Provider를 사용한 별도 시험으로 확인한다.

## 초기 구현 검증 결과 — 2026-09-09

| 실행 환경·검사 | 결과 |
| --- | --- |
| macOS·Python 3.12 Agent 전체 | **856 passed, 2 skipped** — ROS 미설치에 따른 모듈 skip |
| Ubuntu 22.04·ROS 2 Humble·ARM64 패키지 빌드 | **4개 패키지 성공** — interfaces, Agent, TTS, System Manager |
| Ubuntu 실제 ROS Topic·Action 회귀 | **13 passed, 0 skipped** — 기존 통신 11개 + 기억 통신 2개 |
| 같은 Ubuntu 환경 Agent 전체 | **869 passed, 0 skipped** — 위 ROS 13개 포함 |
| 변경·추가 Python 29개 파일 flake8 | 통과 |

Ubuntu 시험은 Python 3.10.12·Pydantic 2.13.5에서 수행했다. 별도 도메인
193·localhost 설정을 사용했고, 기존 Manager(PID 1096, domain 159)는
유지했다. 종료 후 시험 도메인의 프로세스가 남지 않았음을 확인했다.

시험 스냅샷은 기준 커밋 `8747abdea846fe6ea6bb873fad4148cf89e6d6a0`에 작업
공간의 변경·미추적 파일을 포함한 184개 파일이다. 스냅샷 전체 파일의 SHA-256이
일치하고, 시험 종료 후 Python 126개 파일이 현재 작업 공간과도 일치함을
확인했다. 코드 묶음의 SHA-256은
`248bdcb477f64442f9530525bc60e2bce46460b6864231f16281e328fa20c767`이다.

시험 로그·JUnit·출처 목록은 개발 환경의 다음 디렉터리에 남겼다.

- 호스트: `/var/folders/5s/gq4btl_j0cl6870s1__ggygh0000gn/T/malbut-swm25-163-final-ros-025a9kp8/`
- Docker: `/tmp/malbut-swm25-163-final-ros-025a9kp8/`
- 파일: `validation-summary.json`, `source-provenance.json`,
  `worktree-code-check.json`, `build.log`, `ros-communication-tests.log`,
  `ros-communication-tests.xml`, `agent-all-tests.log`, `agent-all-tests.xml`

## 구현 경로

| 구성 | 책임 |
| --- | --- |
| `ProviderResult.memory_proposal` | 현재 발화에서 답변과 기억 처리 후보를 같은 추론으로 제안 |
| `PersonalMemory` | 동의·현재 원문·사용자 범위·대상·질문 유효성을 검증하고 결과 문장을 작성 |
| `SQLiteMemoryStore` | 기억·동의·출처·사용자별 변경 번호의 영속 저장 |
| `SQLiteConversationStore.complete_turn` | 기억 효과와 최종 대화 결과를 같은 트랜잭션으로 확정 |
| `TextTurnService` | 기억 동의 답변과 로봇 실행 확인을 구분, 승인 직전 상태 재검사 |
| `DialogueWorker` / ROS 응답 발행 | 대기열과 발행 직전 낡은 답변을 차단 |

일반 대화 문맥과 장기기억은 구분한다. 개인화가 꺼져도 현재 대화는 사용할 수
있으며, 명시적인 기억 조회·정정·삭제는 허용한다. 처음 동의할 때 앞으로의
자동 저장과 활용 범위를 설명한다. 사용자 신원은 모델이 결정하지 않는다.

## 달성 조건과 시험 연결

| 조건 | 구현·시험 근거 |
| --- | --- |
| 사용자 정보의 장기 저장 | `test_memory_policy.py`, `test_personal_memory_flow.py`, `test_memory_fact_evidence.py`: 이름·호칭·반려동물·취향, 같은 사실 중복 방지, 다른 반려동물·선호 분리, 재시작 |
| 관련 기억을 대화에 제공 | `test_personal_memory_flow.py`, `test_memory_providers.py`: 동의 전 무활용, 새 대화의 관련 검색, 무관한 기억 제외, 일반 대화 입력과 결합 |
| 요청에 따른 정정·삭제 | `test_personal_memory_flow.py`, `test_personal_memory_authority.py`: 명시적 정정, 모호한 대상 질문, 원문·요약·옛 응답의 재사용 차단, 새 명시적 재저장 |
| 사용자별 관리 | `test_memory_policy.py`, `test_personal_memory_flow.py`: 사용자 A/B 검색·변경 격리, 다른 사용자의 변경이 응답을 무효화하지 않음 |
| 동의·중단 | `test_personal_memory_flow.py`, `test_text_memory_boundary.py`: 보류한 저장, 유효한 동의 답변, 거절·재동의·초기화·만료, 로봇 확인과 구분 |
| 늦은 처리·재전송 | `test_personal_memory_flow.py`, `test_speech_memory.py`: 추론 중 삭제·철회, commit 직전 변경, 전체 rollback, 완료 객체·재전송·음성 대기열의 유효성 |
| Provider 경계 | `test_memory_providers.py`, `test_rai_memory.py`: OpenAI 구조화 출력 한 번, 잘린 출력·잘못된 근거, RAI v1/v2 및 실제 Pydantic 출력 모델 |
| 원문·대상·의미 | `test_memory_fact_evidence.py`: 강아지/고양이 바꿔치기, 이름/품종 속성 오해석, 좋아함/싫어함 반전, 일반 대화에 무관한 기억 검색 차단 |
| 실행 승인 경계 | `test_text_memory_boundary.py`: 기억 질문 중 “네”, 중첩 질문, DB 승인 직전 동의 변경·새 기억 질문의 경합에서 로봇 작업 0건 |
| 실제 ROS 통신 | `test_ros_memory_communication.py`: 실제 Topic 입력, Agent DB 처리, TTS 수신, 발행 직전 별도 DB 삭제 |

기존 기억을 가진 평가 사례에는 동의 상태를 명시한 fixture를 사용한다.
평가 원문과 기대 결과는 유지하며, 운영 사용자의 기본 비활성 정책을 바꾸지
않는다. 구형 Provider의 함수 형식은 호출 전에 확인하므로 호환 오류를 이유로
같은 추론을 다시 호출하지 않는다.

## 자동 시험 재현

2026-09-09 macOS Python 3.12 시험 결과는 **856 passed, 2 skipped**다.
두 skip은 `rclpy`가 없는 환경에서 수집을 건너뛴 ROS 시험 모듈이며 아래
Ubuntu 환경에서 별도로 검증한다. JUnit 결과는
`/tmp/malbut-swm25-163-agent-final.xml`에 남겼다.

변경·추가된 Python 파일 29개의 flake8은 통과했다. pydocstyle에서는 기존
`DialogueWorker.__init__`의 D107 한 건만 남았으며, 기반 커밋에도 같은
docstring 누락이 있음을 확인했다. 이번 작업의 새 문서 검사 오류는 없다.

Agent 패키지 디렉터리에서 실행한다.

```bash
PYTHONPATH=. python3 -m pytest -q test
```

ROS가 없는 환경에서는 ROS 전용 시험 모듈을 건너뛴다. 그 항목은 아래
Ubuntu ROS 환경에서 별도로 실행한다. 로컬 시험에는 실제 API 키·모델 호출이
필요하지 않다.

## ROS 시험 재현

Ubuntu 22.04·ROS 2 Humble의 별도 작업 공간에서 변경·미추적 파일까지 포함한
현재 소스를 사용한다. 기존 실행 도메인과 겹치지 않는 도메인을 선택한다.

```bash
source /opt/ros/humble/setup.bash
colcon build --packages-select malbut_interfaces malbut_agent_server malbut_tts
source install/setup.bash
export ROS_DOMAIN_ID=193
export ROS_LOCALHOST_ONLY=1
cd src/malbut_agent_server
PYTHONPATH=.:$PYTHONPATH python3 -m pytest -q \
  test/test_ros_memory_communication.py
```

기존 `test_node_communication_ros.py` 회귀 시험은 `malbut_system_manager`
패키지가 함께 설치된 환경에서 실행한다. ROS Python 경로가 사라지지 않도록
`PYTHONPATH=.:$PYTHONPATH`를 사용한다.

최종 스냅샷에서 전체 시험을 다시 실행하려면 다음을 사용한다.

```bash
cd /tmp/malbut-swm25-163-final-ros-025a9kp8
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=193
export ROS_LOCALHOST_ONLY=1
cd src/malbut_agent_server
PYTHONPATH=.:$PYTHONPATH python3 -m pytest -q test
```

## 저장·이행 동작

- 기존 `memories`, 대화·확인·작업 기록은 보존한다. `memory_policy_state`,
  `memory_state_counter`, `memory_fact_slots`, `memory_tombstones`,
  `memory_turn_state`, `memory_questions`를 추가한다.
- 기존 사용자는 동의 근거가 없으면 개인화를 꺼진 상태로 시작한다.
- 별도 연결의 쓰기도 사용자별 영속 변경 번호에 반영한다. DB 트랜잭션 밖에서
  추론하고, 결과 적용 때 같은 트랜잭션에서 재검사한다.
- 정정·삭제된 사실의 과거 원문과 답변은 기록으로 남지만 모델 입력에서 제외한다.
  관련 요약은 유효한 문맥에서 다시 만든다. 출처가 없는 기존 자료는 과거 문맥을
  넓게 제외할 수 있다.
- 오래된 요청 ID는 새로운 추론·기억 변경에 재사용하지 않는다. 새 요청으로 다시
  말해야 한다. 동의 철회·세션 초기화 뒤 늦게 끝난 결과도 출력하지 않는다.
- HTTP 대화 DB와 음성 대화 DB는 각 실행 설정을 따른다. 같은 사용자 기억 공유가
  필요하면 같은 DB 파일과 사용자 범위를 사용한다.

## 검증의 한계

테스트의 Provider 출력은 고정 대역이며 OpenAI·RAI의 실서비스를 호출하지
않았다. 실제 한국어 모델의 해석 정확도와 마이크·스피커 사용은 별도 검증이다.
ROS 시험은 실제 메시지 전달과 Agent의 기억 처리를 확인하며 음성 재생이나
물리 로봇 동작을 검증하지 않는다. 이미 TTS Topic으로 발행한 텍스트를 회수하는
보장은 현재 인터페이스에 없다.

자동 저장은 현재 원문의 한국어 단서와 허용 속성으로 대상을 검증할 수 있는
경우에 한한다. 복합절에서 대상이나 긍정·부정 의미를 확정하기 어렵거나
지원하지 않는 속성이 제안되면 저장하지 않고 질문한다. 임의의 한국어 표현을
모두 해석한다는 보장은 없으며, 실제 모델 평가에서 표현 범위를 별도로 확인한다.

새 ROS Node, `.msg`·`.action`, Capability Manifest, 별도 기억 저장소 명세는
추가하지 않았으며 기존 Agent 명세 본문은 유지했다.
