# 실제 응용 기능을 통한 관리자 실행 실험

현재 등록된 `patrol`, `navigate_to_pose`, `follow_person`을 **실제 Gazebo·Nav2·응용 Action 서버**에 요청한다. 명령은 모두 `/malbut/mission/execute`로 전달하며, 응용 코드와 관리자 내부 코드를 직접 호출하거나 모의 서버로 대체하지 않는다.

## 실행

저장소 루트에서:

```bash
bash malbut_system_manager/experiments/run_mission_sequence.sh
```

기본 실행은 GUI 없이 실제 시뮬레이터·센서·인식·Nav2를 구동한다. Gazebo,
RViz, Image View까지 보려면 다음과 같이 실행한다.

```bash
MALBUT_EXPERIMENT_GUI=true bash malbut_system_manager/experiments/run_mission_sequence.sh
```

설치된 현재 작업 코드가 사용되므로 변경 패키지는 먼저 빌드해야 한다.
기존 실행과 통신이 섞이지 않도록 별도 ROS domain(기본 86)과 Gazebo
partition을 사용한다. `MALBUT_EXPERIMENT_DOMAIN`으로 domain을 변경할 수 있다.
결과는 workspace의 `log/manager_experiment/run-*/`에 실행별로 보관하고,
스크립트가 해당 경로를 출력한다. 사람은 이 실험에서 실행 15초 후 등장한다.

시뮬레이터와 응용 서버를 직접 실행해 둔 경우에는 동일한 ROS domain과 설치 환경에서 측정 프로그램만 실행할 수도 있다.

```bash
source /opt/ros/humble/setup.bash
source /home/jong/ros2_ws/install/local_setup.bash
python3 malbut_system_manager/experiments/mission_sequence.py \
  --output-dir /tmp/malbut-manager-sequence \
  --gap-seconds 12 \
  --goal-x -3.665503 --goal-y -0.4874 --goal-yaw 0
```

기본 목적지는 small_house의 로봇 초기 생성 위치다. 다른 환경에서는 해당 지도에서 이동 가능한 목적지를 인자로 지정한다. `--startup-timeout`(기본 120초)과 `--settle-timeout`(기본 30초)은 **실험 대기 기한**이며 응용 기능의 동작 제한을 수정하지 않는다. 출력 경로는 실행마다 새로운 경로를 사용한다.

## 명령 시퀀스

1. 실제 Action 서버, 진행하는 시뮬레이션 시계, 지도, Global Costmap, RGB·CameraInfo·LiDAR·오도메트리 및 TF를 기다린다. 시작 시 관리자에 기존 미션이 없어야 한다.
2. 최초 사람 추적 요청 → 20초 관찰하며 실제 `target_visible: true` 피드백으로 대상 획득 확인 → 취소 및 정지·IDLE 확인.
3. LIGHT 순찰 요청 → 실제 `/patrol` 실행 확인 → 12초 관찰.
4. 목적지 이동 요청 → 같은 NORMAL 우선순위인 기존 순찰이 취소·보류되고 새 요청이 실행되는지 확인 → 12초 관찰.
5. 사람 추적 요청 → 실제 `/follow_person` 실행 확인 → 12초 관찰. 앞선 이동이 이미 끝났다면 재개된 순찰을 선점한다.
6. 사람 추적 취소 → 실제 하위 실행 종료 및 보류 미션 재개 확인 → 12초 관찰.
7. 남은 이동·순찰 요청 취소 → 관리자 `IDLE`, 활성·대기·보류 목록 비움, 하위 Action 종료, 오도메트리 기준 3초 정지 확인. 실제 응용 미션이 ABORTED로 끝난 경우에도 실패한다.

관찰 간격은 `--gap-seconds`, 최초 대상 획득 관찰 시간은 `--initial-follow-seconds`로 변경한다. 최초 추적을 0초로 명시하면 해당 단계를 생략한다. 이 경우 대상 획득 검증은 없으므로 기본 실행과 구분한다. 실패 시에도 이 프로그램이 요청한 Goal만 취소한다. 임의의 다른 클라이언트 Goal에 전체 취소를 보내지 않는다.

## 저장하는 결과

- `events.jsonl`: 실행 요청·취소, 관리자 상태/피드백/결과, 실제 하위 Action Goal UUID와 상태, 순찰 coverage, 사람 검출 개수, 오도메트리. 모든 행에 monotonic 기준 경과 시간과 별도의 시뮬레이션 시간을 기록한다. 이미지 자체는 저장하지 않는다.
- `summary.json`: 성공 여부, 통과한 관리자 검증 항목, 실패 이유, 단계별 오도메트리 이동 거리, 검출 및 추적 대상 가시 피드백 횟수, 하위 Goal 개수와 종료 상태.
- 실행 스크립트가 저장하는 서버 로그는 시뮬레이터·Nav2·응용 서버 원인 분석에 사용한다.

**관리자 통과와 응용 성능을 구분한다.** 이 실험은 실제 기능의 실행·선점·취소·재개 연결을 확인하는 smoke test다. 순찰 전체 완료, 카메라의 실제 공간 관측률, 사람 추적 정확도를 합격시켰다는 의미가 아니다. `target_visible` 피드백과 검출 토픽 수신도 따로 기록하므로 추적 서버 실행만으로 사람을 따라갔다고 판단하지 않는다. 이동 거리는 오도메트리 기반이며 Gazebo ground truth가 아니다.

모든 현재 등록 미션은 `[BASE]`이므로 실험 중 활성 BASE 미션이 둘 이상이면 실패한다. 자원이 서로 다른 미션의 병행은 등록된 실물 기능이 아직 없어 이 시퀀스의 검증 대상이 아니다. 서로 다른 ROS Topic의 수신 시각만으로 미세한 선후 관계를 단정하지 않고, 관리자 전이와 하위 Action 종료 상태를 함께 확인한다.
