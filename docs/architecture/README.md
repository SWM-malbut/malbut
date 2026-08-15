# MALBUT Architecture Atlas

이 디렉터리는 Malbut 전체를 하나의 거대한 그림으로 뭉개지 않고, 같은 시스템을
서로 다른 질문으로 바라보는 **architecture view set**이다.

[브라우저 Atlas 열기](index.html)

로컬에서 바로 열려면 다음 명령을 실행한다.

```bash
cd /path/to/malbut
xdg-open docs/architecture/index.html
```

## 왜 그림이 여러 개인가

| 표현 기법 | 답하는 질문 | Malbut에서 보는 대상 |
|---|---|---|
| **C4 System Context** | 누가 이 시스템을 쓰고 어떤 외부 시스템과 만나는가? | 사용자, Malbut, AWS, OpenAI, 실물 장치 |
| **C4 Container** | 프로그램과 실행 단위가 어떻게 나뉘는가? | PWA, Backend, ROS/Nav2, Homecam, Agent, DB |
| **DFD + Trust Boundary** | 어떤 데이터가 어디로 이동하고 어디서 신뢰가 바뀌는가? | 영상, event, token, LLM 요청, DB |
| **Deployment View** | 어느 컴퓨터·클라우드·장치에서 실행되는가? | 브라우저, AWS, Robot PC/Jetson, 외부 API |
| **ROS Runtime Graph** | 어떤 ROS node가 어떤 topic을 발행·구독하는가? | Gazebo, bridge, Nav2, SLAM, Homecam |
| **ERD** | 저장 데이터와 키 관계가 어떻게 생겼는가? | Agent SQLite, Homecam PostgreSQL |
| **UML-style Dynamic / Sequence** | 한 사용자 시나리오가 시간 순서대로 어떤 경계를 통과하는가? | wake/STT, `monitor_room`, 확인, Room plan, simulation과 실제 E2E GAP |

ERD는 데이터베이스에는 강하지만 프로세스·배포·ROS topic을 보여주지 못한다.
반대로 C4는 시스템 경계를 잘 보여주지만 DB row 관계와 메시지 순서는 생략한다.
그래서 전체 시스템에는 여러 관점을 함께 쓰는 편이 정확하다.

## 다이어그램

| 번호 | 바로 보기 | 편집 원본 |
|---|---|---|
| 01 | [C4 System Context](01-c4-system-context.svg) | [`01-c4-system-context.dot`](01-c4-system-context.dot) |
| 02 | [C4 Container](02-c4-container.svg) | [`02-c4-container.dot`](02-c4-container.dot) |
| 03 | [DFD + Trust Boundary](03-dfd-trust-boundaries.svg) | [`03-dfd-trust-boundaries.dot`](03-dfd-trust-boundaries.dot) |
| 04 | [Deployment View](04-deployment.svg) | [`04-deployment.dot`](04-deployment.dot) |
| 05 | [ROS Runtime Graph](05-ros-runtime.svg) | [`05-ros-runtime.dot`](05-ros-runtime.dot) |
| 06 | [Conceptual ERD](06-conceptual-erd.svg) | [`06-conceptual-erd.dot`](06-conceptual-erd.dot) |
| 07 | [Room Live Dynamic / Sequence](07-room-live-dynamic.svg) | [`07-room-live-dynamic.dot`](07-room-live-dynamic.dot) |

## 상태 표기

- **RUN**: 현재 실행 코드와 검증 경로가 있다.
- **COND**: SDK·모델·계정·실물 환경이 있을 때 동작한다.
- **OFFLINE**: 안전 계약과 코어는 있지만 실제 adapter가 없다.
- **GAP**: 두 서브시스템 사이 연결 자체가 아직 없다.
- **EXT**: 외부 서비스이거나 이 저장소가 소유하지 않는다.

특히 `malbut_agent_server → Nav2/Homecam` 관계는 빨간 점선 GAP이다. 현재
Agent 응답의 `execution.authorized`는 항상 `false`이고 production Tool
registry에 실제 ROS adapter가 없다.

07 Dynamic View의 파란 경로는 실행 준비가 끝났다는 뜻이 아니다.
`ContinuousVoiceSession`, 닫힌 발화 문법, 명시적 확인 binding,
`SemanticRoomResolver`와 `SimulationRoomMissionAdapter`를 이어 **부작용 없이**
계약을 시험하는 경로다. `simulation_succeeded`, `simulated=true`,
`viewer_live=false`는 실제 Nav2 이동,
Homecam/KVS 송출 또는 인증된 브라우저의 영상 표시를 증명하지 않는다.

## 해석할 때의 한계

- C4, DFD, Deployment는 코드와 문서에서 재구성한 **source-derived model**이다.
  `RUN`은 해당 코드·검증 경로가 있다는 뜻이며 현재 revision의 production 배포를
  자동으로 증명하지 않는다.
- ROS Runtime Graph는 launch·bridge 설정으로 만든 예상 topology다. 실제 실행
  순간의 node/topic 증거는 `rqt_graph`, `ros2 node list`, `ros2 topic list`로
  별도 캡처해야 한다.
- ERD는 주요 entity와 관계를 설명하는 conceptual view다. 모든 column, index,
  migration을 대체하지 않는다.
- Homecam Web과 LLM Agent 사이, Agent와 ROS 실행기 사이에는 현재 연결이 없다.
  그림의 빨간 GAP을 향후 구현된 선으로 읽으면 안 된다.
- Agent 패키지의 `malbut-stt`는 WAV·짧은 마이크 입력 한 건을 처리하는 선택형
  로컬 CLI다. `ContinuousVoiceSession`은 주입형 wake/STT/TTS adapter로 반복
  수명주기를 검증하는 offline core이며, 실제 wake detector나 ROS/Web→Agent
  연결로 읽으면 안 된다.
- Room mission의 1회 실행 장부는 프로세스 메모리 안에만 있다. 재시작에도
  안전한 durable ledger, 실제 Nav2 adapter, Homecam 상태/KVS viewer callback,
  trusted Tool result의 same-conversation feedback은 모두 GAP이다.

## 다시 렌더링

Graphviz `dot`만 있으면 네트워크 없이 SVG를 다시 만들 수 있다.

```bash
cd docs/architecture
./render.sh
```

생성된 SVG에는 관련 코드로 이동하는 링크가 포함된다. DOT가 사람이 편집하는
원본이고 SVG는 생성 결과다.

## 방법론 근거

이 구성은 하나의 모델만 정답으로 삼지 않고 관심사별 view를 분리하는
architecture viewpoint 방식이다. 각 view에서 같은 이름과 상태 범례를 재사용해
서로 다른 그림이 같은 시스템을 설명하도록 했다.

- [C4 Model 공식 사이트](https://c4model.com/): Context, Container,
  Component, Code의 단계적 확대와 보조 Dynamic/Deployment view
- [C4 diagram 안내](https://c4model.com/diagrams): 대부분의 팀은 Context와
  Container만으로도 충분하다고 설명
- [OMG UML](https://www.omg.org/spec/UML): Component와 Deployment를 포함한
  표준 소프트웨어 모델링 언어
- [OMG SysML](https://www.omg.org/sysml/sysmlv1/): software·hardware를 block,
  part, port, connector로 함께 표현하는 시스템 엔지니어링 모델
- [OWASP DFD 설명](https://owasp.org/www-community/Threat_Modeling_Process):
  external entity, process, data store, data flow, trust boundary
- [ROS 2 topic/rqt_graph 공식 튜토리얼](https://docs.ros.org/en/galactic/Tutorials/Topics/Understanding-ROS2-Topics.html)
- [ER Model 원전](https://doi.org/10.1145/320434.320440): 데이터베이스 설계를
  위한 entity-relationship 모델
- [ISO/IEC/IEEE 42010](https://standards.ieee.org/ieee/42010/6846/): 관심사별
  architecture view와 viewpoint로 구조 설명을 나누는 국제 표준
