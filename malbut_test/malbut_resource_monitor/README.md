# 실기기 자원 측정 (SWM25-196)

`malbut_test` 전용, 수집기와 별도 **읽기 전용** 웹 뷰어. 응용 기능 내부에 측정 코드를 넣지 않는다.
목적지 이동·사람 추적·순찰·수동 이동·AutoSLAM·위치 보정·음성을 켠 상태의 자원 변화와
이미 존재하는 ROS 상태를 관측한다. Goal/취소, Service 호출, 속도 발행, 파라미터 변경은 하지 않는다.

## 실행

배포 복사본이 `~/ros2_ws/src/malbut`에 있는 기존 절차 그대로:

```bash
bash ~/ros2_ws/src/malbut/build.sh
source ~/ros2_ws/install/malbut_test/local_setup.zsh
# 기존 웹에서 Bringup을 켜거나 기존 robot.launch.py 명령을 그대로 실행
# 이제 수집기 준비 → 기존 Bringup 순으로 시작한다. 기능 시작은 기존 웹/CLI에서 한다.
```

추가 설치 라이브러리나 클라우드 연결은 필요 없다. Jetson의 기존 `tegrastats`가 PATH에 있어야
GPU·EMC·전력을 수집한다. 없어도 CPU/RAM 수집은 가능하며 미지원 항목을 0으로 꾸미지 않는다.

별도 터미널에서 로그 UI (신뢰하는 같은 Wi-Fi에서만 공개):

```bash
source ~/ros2_ws/install/malbut_test/local_setup.zsh
ros2 run malbut_resource_monitor resource_viewer --host 0.0.0.0 --port 8766
```

Mac 브라우저에서 `http://로봇IP:8766` → 실행 회차 → 전체/프로세스/토픽 → 지표 선택.
Action을 선택하면 상태 변화 시점이 세로선으로 표시된다. Goal 행을 누르면 해당 구간으로 좁힌다.
프로세스는 PID별 곡선이며, 경로와 ROS remap 이름은 아래 표에 나온다.
로그 자동 새로고침은 하지 않는다. 필요할 때 **다시 읽기**. 기본 바인딩은 localhost이며,
인증 없는 로그 뷰어이므로 인터넷 공개/AWS 배포용이 아니다.

로봇 없이 다른 PC에서 저장 로그를 보는 것도 가능하다 (Python 3.10+, ROS 불필요):

```bash
cd malbut_test/malbut_resource_monitor
python3 -m malbut_resource_monitor.viewer --root /가져온/resource_logs
```

수동 수집 / 순수 자원 측정 기준 실험:

```bash
ros2 run malbut_resource_monitor resource_recorder --parent-pid <로봇-launch-PID>
# ROS 구독 부하 없는 비교 측정:
ros2 run malbut_resource_monitor resource_recorder --no-ros --parent-pid <로봇-launch-PID>
```

Bringup 자동 수집 기본 `resource_monitor:=true`, 저장 위치 변경 `resource_log_root:=/경로`.
독립 수집기를 수동 실행할 때는 Bringup에 `resource_monitor:=false`를 줘 중복 수집을 피한다.
원하는 토픽 목록은 `--topics /경로/topics.json`으로 지정하는 절대 토픽명 JSON 배열이다.
Bringup 수집기는 패키지에 포함된 `topics.json`을 쓴다. 토픽 리맵이 있다면 이 목록도 맞춘다.

## 저장

기본 `~/.ros/malbut/resource_logs/<UTC시각>-<고유번호>/`.

```text
metadata.json          # 측정 기준, 호스트, 단위 설명, PID+시작 tick → 실행/스크립트 경로
system.jsonl           # 장치 전체 자원, 코어별 CPU/클럭, 수집 시간/지연
processes/*.jsonl      # 인식, 추적, 순찰, AutoSLAM, Nav2, 음성 등 기능 그룹별 PID 측정
topics/*.jsonl         # 토픽별 수신 Hz / CDR 바이트 수 / 마지막 수신 경과시간
actions/*.jsonl        # Action endpoint별 UUID / 상태 변화 / 수신 시각
missions.jsonl        # 관리자 mission ID ↔ capability 및 상태 관측 (결과 추정 금지)
phases/*.jsonl         # STT 입력/최종 transcript 도착, TTS 재생 상태 (발화 본문 저장 안 함)
tegrastats.jsonl       # 원본 NVIDIA 측정 줄 (해석 결과 검증용)
observer.jsonl        # 측정기 자체의 미지원/진단 알림 (기능 성능 지표 아님)
```

모든 행의 `t`는 회차 시작 이후 **monotonic 초**, `wall_ns`는 수신 시점 Unix 나노초.
ROS simulated time에 의존하지 않는다. Action의 `accepted_ros_ns`는 서버가 제공한 ROS 시각이며
`wall_ns`와 섞어서 지연시간을 계산하지 않는다. 기본 자원 샘플 주기는 1초.
실제 표본 간격 `sample_window_s`, 수집 소요 `collection_duration_ms`도 남긴다.
지연됐을 때 가짜 표본을 채우거나 밀린 샘플을 몰아서 기록하지 않는다.

로그는 회차별 새 폴더에 추가 기록하며 이전 로그를 지우지 않는다. 정상 종료 메타데이터가 없으면
수집 중/강제 종료를 구분할 수 없다고 표시한다. 디스크 여유가 256 MiB 미만이면 **수집만 중단**한다.
파일은 사용자 전용 권한으로 만들고 전체 argv/env는 저장하지 않는다 (토큰 노출 방지).

## 지표 해석 — 측정하지 못한 것을 추정하지 않는다

| 항목 | 출처 / 의미 |
|---|---|
| 전체·코어별 CPU % | `/proc/stat` 두 표본 차이. 전체는 장치=100%, guest 중복 합산 안 함 |
| 프로세스 CPU % | `/proc/PID/stat` 실행시간 차이 / 실제 경과시간. 코어 하나=100%, 멀티코어는 100% 초과 가능 |
| 전체 RAM·Swap | `/proc/meminfo`, MiB. RAM 사용 = Total − Available |
| 프로세스 RAM·Swap | `/proc/PID/status`, RSS 및 VmSwap, MiB. 공유 페이지 때문에 RSS 합산 금지 |
| CPU 클럭·온도 | sysfs cpufreq(MHz) / thermal(°C). 코어/센서가 안 보이면 값 없음 |
| GPU 사용률·클럭 | `tegrastats` GR3D_FREQ(%/MHz), **Jetson 전체**. 프로세스별 GPU %는 `null` |
| 메모리 대역폭 | `tegrastats` EMC_FREQ, 현재 EMC 클럭에 대한 사용률(%) 및 클럭(MHz). GB/s로 환산하지 않음 |
| 전력 | tegrastats 이름별 rail 순간/평균 mW. VDD_IN은 Jetson 입력, 모터 포함 로봇 전체 소비전력 아님. rail 합산 금지 |
| Topic Hz·바이트 | 수집기가 실제 받은 메시지 수 / 측정 구간. raw CDR 길이, 네트워크 전송량이나 발행률 자체가 아님 |

기본 토픽 구독은 `best_effort + volatile + keep_last(1)`. 영상/Depth 영상은 내용 저장 없이
수신 수와 직렬화 크기만 센다. **추가 구독은 공짜가 아니다**. 원본 대형 PointCloud2는 기본 목록에서
제외했다. `observer` 프로세스 그룹과 수집 소요시간을 함께 보고, 필요하면 `--no-ros` 회차와 비교한다.
토픽이 없는 경우 `null`, 구독은 되었지만 해당 측정 구간에 수신이 없으면 `0 Hz`이다.
이 값만으로 원래 publisher가 0 Hz였다고 단정하지 않는다.

기능 그룹은 **실행 파일/ROS 이름에 기반한 표시 분류**이다. 실제 수치는 프로세스별이며
Nav2의 planner/controller/costmap 등을 내부 노드별 사용량으로 나누지 않는다.
자원은 Bringup의 자손 프로세스와 별도로 실행된 Malbut 경로의 프로세스를 대상으로 한다.
제조사 센서 자손은 `other_robot`에 포함된다. PID 재사용은 `/proc` 시작 tick으로 구분한다.

Action은 `/_action/status`의 ACCEPTED/EXECUTING/종료를 구독한다. 관측 전에 이미 종료된
Goal은 “과거 종료 상태 첫 수신”으로 표시한다. **Goal 거부, 실제 요청 전송 시각, 모터 시작,
서비스 완료 시각, 전체 STT→Agent→TTS 지연은 이 방식만으로 측정할 수 없다.**
Action 수신 시각을 그 값들로 대신 표기하지 않는다. 통신 단절·수집 종료를 Goal 완료로 만들지 않는다.
관리자 상태에서 mission이 사라져도 성공/실패는 알 수 없어 `NO_LONGER_LISTED`로만 기록한다.

뷰어는 원본 표본을 연결해서 보여주며 보간·평활화·임의 0 채우기를 하지 않는다. 큰 로그는
선택 구간의 최근 50,000행까지만 표시하고 **잘림을 명시**한다. 시간 구간을 좁히거나 원본 전체를
다운로드할 수 있다. 이 도구는 기존 ROS 디버그 로그를 대체하거나 수정하지 않는다.

## 최소 검증

```bash
cd malbut_test/malbut_resource_monitor
python3 -m pytest -q test/test_measurement.py
# ROS Humble + malbut_interfaces를 source한 환경에서만:
python3 -m pytest -q test/test_ros_observer.py
```

Jetson GPU/EMC/전력은 실제 장치에서 원본 tegrastats와 대조해야 한다.
개발 PC/가짜 센서 테스트 통과를 실로봇 부하 검증으로 해석하지 않는다.

참고: [NVIDIA tegrastats](https://docs.nvidia.com/jetson/archives/r36.4.4/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html),
[Linux proc](https://docs.kernel.org/filesystems/proc.html),
[ROS 2 Action status](https://design.ros2.org/articles/actions.html).
