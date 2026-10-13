# 테스트 실행

## 테스트 작업과 담당 범위

Python 실행 목록은 `scripts/run-python-tests.py` 한곳에서 관리한다.

| 작업 | 담당 범위 |
|---|---|
| Agent Python | Agent의 대화·기억·도구·정책 검사. 낙상 작업도 실행되면 겹치는 낙상·VLM 파일은 그 작업에 맡긴다. |
| Fall Python | 낙상·VLM 처리와 홈캠 평가 도구의 오프라인 검사. ROS launch 검사는 제외한다. |
| ROS | 변경한 패키지의 테스트와 기존 Agent ROS 연결 검사. 빌드 의존성이라는 이유만으로 다른 패키지의 테스트까지 실행하지 않는다. |
| Homecam | ROS launch, SDK를 켠 C++ 빌드·검사, portable runtime 검사. 낙상 작업이 없을 때는 홈캠 오프라인 검사도 실행한다. |
| Web / Web infrastructure | 기존 웹 lint·빌드·테스트와 인프라 빌드·테스트·synth를 유지한다. |

전체 CI에서는 Agent·낙상·홈캠 사이의 오프라인 테스트 파일을 한 번씩 실행한다.
일부 작업만 선택되면 다른 작업에 검사를 맡기지 않고 원래 범위를 유지한다.
ROS 환경에서 다시 검사하는 연결 경계와 실기기 복사본 검사도 유지한다.

실행하지 않고 목록만 확인하려면:

```bash
python3 .github/scripts/run-python-tests.py agent --list
python3 .github/scripts/run-python-tests.py agent --fall-job true --list
python3 .github/scripts/run-python-tests.py fall --list
python3 .github/scripts/run-python-tests.py agent-ros --list
```

`--fall-job true`는 같은 CI에서 낙상 작업을 실행할 때만 사용한다.
로컬에서 Agent만 검사할 때는 생략한다.

## 로봇과 Cloud 키 없이 낙상 코드 확인

Python 3.10 가상환경에서 저장소 루트를 기준으로 실행한다.

```bash
python -m pip install -r .github/requirements/fall-tests.txt
bash .github/scripts/test-fall-python.sh
```

영상 입력 구성, 사건 병합, Cloud 응답 형식, 재확인, 저장 및 평가 도구를 검사한다.
시험용 응답을 사용하므로 실제 VLM 인식률을 측정하는 명령은 아니다.
ROS 설치, 모델 다운로드, Cloud 키, 실제 영상 전송과 알림 발송은 필요하지 않다.
ROS 콜백 테스트는 ROS 작업에서, 로봇 launch 테스트는 홈캠 작업에서 실행한다.

Agent만 검사하려면 같은 가상환경에서 다음을 실행한다.

```bash
python .github/scripts/run-python-tests.py agent
```

## CI에서 실행하는 범위

| 바뀐 파일 | 검사 |
|---|---|
| 목록에 등록된 낙상·VLM 분석 코드, 평가 도구·데이터 | Python 낙상 테스트 |
| ROS 연결부, 공통 타입, 실행 설정, 패키지 정보 | 관련 ROS 빌드·연결 테스트 |
| 홈캠 미디어·감지기 코드 | 홈캠 두 패키지의 빌드·테스트 |
| 시뮬레이션 연결·실행 스크립트 | 홈캠 검사와 Gazebo 검사 |
| 문서만 변경 | 기존 문서 제외 규칙 적용. 무거운 검사 생략 |
| CI 규칙·테스트 의존성 변경 또는 수동 전체 실행 | 전체 검사 |

ROS를 생략할 수 있는 파일은 `scripts/select-ci.py`에 명시한다.
새 파일이나 이름이 바뀐 파일을 자동으로 가벼운 검사에 넣지 않는다.
공통 메시지의 변경은 그 메시지를 쓰는 패키지도 검사한다.
PR은 마지막 커밋만이 아니라 main 대비 전체 변경을 기준으로 선택한다.
따라서 코드가 포함된 PR에 문서 커밋을 추가해도 코드 검사를 생략하지 않는다.

Python 다운로드 캐시를 사용하되, 매 실행에 의존성을 설치·확인한다.
ROS apt 설치는 여전히 ROS 작업에서 수행한다. ROS가 미리 설치된 공용 이미지는
현재 사용하지 않으며, 홈캠은 저장소 전체 대신 홈캠 패키지의 의존성만 설치한다.

## 선택 규칙 확인

```bash
bash .github/scripts/test-detect-ci-modules.sh
```

삭제·이름 변경·여러 모듈의 동시 변경, 알 수 없는 새 파일, 시뮬레이션 연결을 검사한다.
홈캠 빌드 명령은 시험용 실행 도구로 검사하며 실제 ROS 빌드를 대신하지 않는다.
셸 진입점은 `scripts/test-*.py`의 표준 라이브러리 검사들을 실행한다.
모듈 선택 표·Git diff 검사는 `test-select-ci.py`, 실행 명령은 `test-ci-workflow.py`,
테스트 목록의 누락·중복·부분 실행은 `test-python-suites.py`에서 확인한다.
