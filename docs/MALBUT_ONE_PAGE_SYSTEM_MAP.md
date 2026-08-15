# MALBUT 한 페이지 전체 구조 맵

> **한 문장:** Malbut은 로봇 설계·Gazebo/실물 센서가 ROS 2 데이터를 만들고,
> Nav2가 이동을 담당하며, Homecam이 영상을 웹으로 보내고, LLM Agent는 현재
> 안전한 대화와 **Tool 제안까지만** 담당하는 이동형 홈캠 로봇 시스템이다.

[브라우저용 한 장 포스터 열기](MALBUT_ONE_PAGE_SYSTEM_MAP.html)

> C4·DFD·Deployment·ROS Graph·ERD로 나눈 정식 표현은
> [MALBUT Architecture Atlas](architecture/README.md)에서 볼 수 있다.

## 1. 전체 층 구조

```mermaid
flowchart TB
  classDef user fill:#f4f0ff,stroke:#7657d6,color:#221b44
  classDef cloud fill:#fff1f5,stroke:#c64b79,color:#4a1728
  classDef app fill:#eef6ff,stroke:#397bc2,color:#102a43
  classDef ros fill:#ecfbf4,stroke:#2d9d68,color:#123d2b
  classDef robot fill:#fff7e8,stroke:#d48727,color:#4a2b08
  classDef gap fill:#fff0f0,stroke:#d64545,color:#5c1717,stroke-dasharray: 6 4
  classDef lab fill:#f3f4f6,stroke:#7a818b,color:#25282d,stroke-dasharray: 4 3

  subgraph L1["사람 · 브라우저 · 클라우드"]
    U["사용자"]:::user
    WEB["Homecam PWA / Next.js\n로그인·라이브·녹화·이벤트·PTT"]:::cloud
    BACK["Homecam Backend\nCognito · PostgreSQL · 장치 API"]:::cloud
    KVS["AWS KVS\nWebRTC · Storage · HLS"]:::cloud
  end

  subgraph L2["로봇 PC의 애플리케이션"]
    AGENT["malbut_agent_server\nHTTP · 대화 · 기억 · LLM · Safety"]:::app
    HOME["homecam_media_agent\nH.264/Opus · heartbeat · session"]:::app
    DET["homecam_detector\nmotion · person/dog/cat · event"]:::app
    NAV["SLAM Toolbox + Nav2\n위치추정 · 경로계획 · 제어"]:::app
    MAP["User Map / Room / Zone\nGeoJSON · Keepout mask"]:::app
  end

  subgraph L3["ROS 2 데이터 버스"]
    TOPICS["/camera/color · /camera/depth\n/scan · /imu/data · /odom · /tf\n/joint_states · /cmd_vel · /clock"]:::ros
  end

  subgraph L4["로봇 모델 · 시뮬레이터 · 실물 경계"]
    DESC["malbut_description\nvariant YAML → Xacro → URDF"]:::robot
    GZ["malbut_gazebo\nworld · physics · sensor plugins"]:::robot
    BRIDGE["ros_gz_bridge\nGazebo ↔ ROS 변환"]:::robot
    REAL["실물 base / Aurora / Jetson driver\n이 저장소 밖 또는 실물 검증 대기"]:::gap
  end

  U <--> WEB
  WEB <--> BACK
  WEB <--> KVS
  BACK <--> HOME
  KVS <--> HOME
  HOME --> DET
  DET --> BACK

  U --> AGENT
  AGENT -. "현재 Tool 제안만\n실제 ROS executor 없음" .-> NAV

  MAP --> NAV
  NAV <--> TOPICS
  HOME <--> TOPICS
  DET <--> TOPICS
  DESC --> GZ
  GZ <--> BRIDGE
  BRIDGE <--> TOPICS
  TOPICS -. "실물 adapter 필요" .-> REAL
```

### 이 그림에서 가장 중요한 사실

1. **로봇을 실제로 움직이는 주체는 LLM이 아니라 Nav2/주행 계층**이다.
2. **홈캠과 LLM Agent는 현재 서로 독립된 서브시스템**이다.
3. Agent의 `navigate`, `capture_photo` 등은 지금은 제안이며, ROS 실행기로
   이어지는 선은 아직 끊겨 있다.
4. `malbut_description`은 설계도, `malbut_gazebo`는 설계도에 물리·센서 기능을
   붙이는 디지털 트윈이다.

## 2. 네 개의 실제 흐름으로 읽기

| 노선 | 시작 → 끝 | 실제 흐름 | 현재 상태 |
|---|---|---|---|
| **① 보라색 영상 노선** | 카메라 → 사용자 | Gazebo/Aurora → ROS Image → Media Agent → KVS → PWA | 코드 구현. KVS 활성 빌드·실물 장시간 E2E는 조건부 |
| **② 초록색 이동 노선** | 목표 → 바퀴 | RViz/Nav2 Goal → Planner → MPPI Controller → `/cmd_vel` → Gazebo 바퀴 | 시뮬레이션 구현 |
| **③ 주황색 지도 노선** | LiDAR → 주행 규칙 | `/scan`+TF → SLAM → map → Room/Zone 편집 → mask → Nav2 | 구현 |
| **④ 파란색 대화 노선** | 발화 → 안전한 결정 | HTTP → Schema → Context → LLM → AgentDecision → Safety → 응답/Tool 제안 | 대화 구현, 물리 실행 미연결 |

```text
① VIDEO  Camera ─→ ROS Image ─→ H.264/Opus ─→ AWS KVS ─→ Web/PWA
                         └─────→ motion/YOLO ─→ Event API ─→ 알림

② MOVE   Nav2 Goal ─→ Planner ─→ Controller ─→ /cmd_vel ─→ Gazebo wheels
             ↑                 /scan · /odom · /tf feedback ───────┘

③ MAP    LiDAR+TF ─→ SLAM ─→ map.yaml/pgm ─→ Room/Zone ─→ cost mask ─→ Nav2

④ AGENT  Utterance ─→ Context ─→ LLM ─→ Decision ─→ Safety ─→ Proposal
                                                                  ╳ ROS
```

## 3. 패키지 소유권 지도

| 패키지/서비스 | 책임 | 가장 먼저 볼 파일 |
|---|---|---|
| `malbut_description` | 크기·질량·링크·조인트·센서 위치 | [`rosorin.xacro`](../malbut_description/urdf/rosorin.xacro), [`rosorin_model.xacro`](../malbut_description/urdf/rosorin_model.xacro) |
| `malbut_gazebo` | Gazebo, bridge, SLAM, Nav2, 지도/Zone | [`simulation.launch.py`](../malbut_gazebo/launch/simulation.launch.py), [`bridge.yaml`](../malbut_gazebo/config/bridge.yaml) |
| `homecam_media_agent` | 영상/음성 인코딩, 장치 health, KVS 세션 | [`media_agent_node.cpp`](../homecam_agent/homecam_media_agent/src/media_agent_node.cpp) |
| `homecam_detector` | motion/YOLO, 정지 확인, 이벤트 전송 | [`detector_node.py`](../homecam_agent/homecam_detector/homecam_detector/detector_node.py) |
| `malbut_agent_server` | HTTP 대화, 컨텍스트, Provider, Safety, Tool 제안 | [`orchestrator.py`](../malbut_agent_server/malbut_agent_server/orchestrator.py), [`continuous_voice.py`](../malbut_agent_server/malbut_agent_server/continuous_voice.py), [`room_mission.py`](../malbut_agent_server/malbut_agent_server/room_mission.py), [`README.md`](../malbut_agent_server/README.md) |
| 별도 `homecam_web` | PWA, 인증, DB, 장치 API, KVS broker | 별도 Node.js/AWS 저장소 |
| `malbut_vision` | OpenCV/LAB 학습 도구 | 로컬 실험 패키지이며 현재 제품 파이프라인 아님 |

## 4. Agent Server 내부 확대

```mermaid
flowchart LR
  REQ["POST /v1/agent/respond"] --> S["AgentRequest strict schema"]
  S --> O["AgentOrchestrator"]
  O --> C["Conversation SQLite\n최근 N턴 + rolling summary"]
  O --> M["Memory SQLite\n현재 사용자 검색 + revision"]
  O --> R["Capability registry\n서버 Tool ∩ 요청 Tool"]
  C --> P["Prompt builder\nuntrusted history/summary/memory"]
  M --> P
  R --> P
  P --> L["Mock 또는 OpenAI Responses"]
  L --> D["AgentDecision schema"]
  D --> SAFE["Local SafetyPolicy"]
  SAFE --> FINAL["최종 message / clarification / refusal / proposal"]
  FINAL --> DB["SQLite commit + idempotent HTTP response"]
  FINAL -.-> X["execution.authorized=false\n실제 ROS adapter 없음"]
  Q["별도 POST /v1/tools/query"] --> GW["ToolGateway"]
  GW --> SIM["read-only / Mock simulation"]
  GW -.-> X
```

- 등록 Tool: `navigate`, `monitor_room`, `detect_pet`, `capture_photo`,
  `send_notification`, `get_robot_status`
  ([`tools.py`](../malbut_agent_server/malbut_agent_server/tools.py)).
- WAV·짧은 push-to-talk용 one-shot 로컬 STT는 선택형 CLI로 구현됐다. 다만
  주입형 wake/STT/output을 연결하는 연속 상태 기계는 오프라인에서만
  구현됐고, 실제 wake detector·TTS·화자 인식·ROS 음성 bridge는 없다.
- `monitor_room`은 닫힌 문법으로 고수준 제안만 만든다. semantic
  Room의 명시적 goal·coverage viewpoint를 검증하는 process-local
  시뮬레이션과 verifier-issued confirmation 계약은 있지만 기본 monitorable
  Room은 비어 있다. durable authorization·Nav2·Homecam·KVS adapter는 아직
  GAP이다.
- 장기기억 변경과 감정표현 코어는 구현됐지만 공개 CRUD와 frontend/ROS
  renderer에는 아직 연결되지 않았다.
- `/respond`의 Tool 제안과 `/tools/query`는 자동으로 이어지지 않으며,
  production registry에는 실제 ROS adapter가 없다.
- Castform 같은 학습 플랫폼은 **LLM Provider 앞의 학습/평가 가지**에 붙고,
  SafetyPolicy와 ROS 실행 경계를 대체하지 않는다.

## 5. 공부 순서

```text
1 설계도      rosorin.xacro → rosorin_model.xacro → components/*
2 시뮬레이션  robot.gazebo.xacro → gazebo_plugins.xacro → bridge.yaml
3 이동        /scan,/odom,/tf → SLAM/Nav2 → /cmd_vel
4 지도        map → User Map → Room/Zone → Nav2 mask
5 홈캠        camera topic → media/detector → backend/KVS → PWA
6 LLM         request → context → provider → decision → safety → proposal
7 통합 경계   proposal → offline trusted confirmation/simulation (구현)
             → durable ledger/trusted ROS state/real adapter (미구현)
```

### 상태 범례

- **구현:** 현재 코드 경로와 테스트가 존재한다.
- **조건부:** SDK·모델·운영 환경이 있을 때만 동작하거나 실물 E2E가 남았다.
- **미연결:** 계약/코어는 있어도 다른 서브시스템으로 이어지는 실제 adapter가 없다.
- **외부:** AWS, 웹 서비스, 실물 driver처럼 ROS 저장소 바깥에서 소유한다.
