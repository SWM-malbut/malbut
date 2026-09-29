# 실기기 자원 측정 (SWM25-196)

`malbut_test` 전용, 수집기와 별도 웹 뷰어. **측정 로그는 읽기 전용이며 회차 표시 이름만 편집**한다.
응용 기능 내부에 측정 코드를 넣지 않는다.
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
기능별 프로세스 화면에서는 기능 그룹을 체크해 함께 비교하고, 범례 체크로 개별 곡선을 숨긴다.
체크 상태는 **그래프 표시 여부**이지 로봇 기능의 실행/정지 상태가 아니다.
범례의 주 이름은 저장된 ROS 노드명 또는 실행 파일명에서 가져온다. PID는 보조 정보로 표시하며,
같은 프로세스의 색·선 모양은 체크 전환·새로고침에도 유지한다. 경로는 범례 툴팁과 아래 표에 나온다.
프로세스 이름이 없거나 `python3`처럼 식별이 불가능한 기록은 임의로 추정하지 않는다.
그 프로세스가 아직 실행 중이면 로봇에서 `ps -p <PID> -o pid=,comm=,args=`로 확인할 수 있다.
명령 인자에 키가 있다면 공유 전에 가린다. 이미 종료된 프로세스는 현재 `ps`로 복원할 수 없으며,
PID가 재사용될 수도 있으므로 현재 프로세스의 이름을 과거 로그에 그대로 대입하면 안 된다.

실행 회차의 **회차 이름 → 이름 저장**으로 `거실 · 사람 추적 1차`처럼 이름을 붙일 수 있다.
이름은 회차 폴더의 `viewer.json`에 별도 저장하므로 수집 중에도 원본 표본·메타데이터·폴더명은
바뀌지 않는다. 다른 브라우저에서도 같은 이름이 보이며, 빈 이름을 저장하면 기본 날짜 표시로 돌아간다.
2초마다 로그 변경 여부를 확인하고, 변경된 그래프·Action 상태·목록만 갱신한다.
변경 없는 파일은 재전송하거나 그래프를 다시 그리지 않는다. 숨긴 탭에서는 자동 조회를 쉬며,
연결 오류가 나면 기존 표시를 유지하고 재시도한다. **다시 읽기**로 즉시 조회할 수도 있다.
선택한 회차·로그·기능/곡선 체크·지표·시간 구간·Action, 펼친 설명과 화면 스크롤 위치는 같은 브라우저에
저장되어 새로고침 후 복원된다. 새 회차가 생겨도 보고 있던 회차를 임의로 바꾸지 않는다.
별도 웹 빌드나 AWS 배포 없이 위 로봇 빌드와 뷰어 실행만 하면 된다. 기본 바인딩은 localhost이며,
인증 없는 로그 뷰어이므로 인터넷 공개/AWS 배포용이 아니다.

### 음성 대화 확인

**로그 종류 → 음성 대화 · STT / TTS**에서 사용자의 최종 STT 인식 문장,
로봇이 TTS에 요청한 답변 문장, TTS 재생 상태를 수신 시각순으로 확인한다.
최신 이벤트가 위에 나오며 기존 2초 자동 갱신·회차/시간 구간 선택을 그대로 사용한다.
화면에는 구간 내 최신 300개 이벤트를 표시하며 전체는 JSONL로 저장할 수 있다.

`/malbut/speech/transcript`, `/malbut/speech/response`,
`/malbut/speech/playback_status`를 수신만 한다. 음성 처리·Agent 코드는 변경하지 않는다.
요청 문장이 실제로 재생되었다고 단정하지 않으며, 재생 상태는 별도 이벤트로 표시한다.
선택 구간에 같은 `playback_id`의 요청이 있으면 재생 상태 행에도 그 요청 문장을 표시한다.
요청이 구간 밖에 있거나 누락된 경우, 요청 ID가 비어 있거나 같은 ID의 문장이 서로 다르면
상태만 표시한다. TTS가 생성한 ID를 추정하거나 STT와 답변을 시간순으로 임의 짝짓지 않는다.
텍스트/상태는 best-effort, volatile, keep-last(50)으로 관측하므로 수신 누락은 가능하다.
과거 회차에는 발화 본문이 없어서 복원할 수 없다. **업데이트한 수집기로 새 회차를 시작해야 한다.**
원본 오디오는 녹음하지 않지만 **인식·답변 텍스트는 `speech.jsonl`에 저장**하므로
대화 개인정보가 포함될 수 있다. 신뢰하는 LAN에서만 열고 로그 공유 시 확인한다.

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
viewer.json            # 사용자가 저장한 회차 표시 이름 (원본 측정값과 분리)
system.jsonl           # 장치 전체 자원, 코어별 CPU/클럭, 수집 시간/지연
processes/*.jsonl      # 인식, 추적, 순찰, AutoSLAM, Nav2, 음성 등 기능 그룹별 PID 측정
topics/*.jsonl         # 토픽별 수신 Hz / CDR 바이트 수 / 마지막 수신 경과시간
actions/*.jsonl        # Action endpoint별 UUID / 상태 변화 / 수신 시각
missions.jsonl        # 관리자 mission ID ↔ capability 및 상태 관측 (결과 추정 금지)
phases/*.jsonl         # STT 입력/최종 transcript 도착, TTS 재생 상태 (발화 본문 저장 안 함)
speech.jsonl           # STT 원문, TTS 요청 문장, 재생 상태 및 ID (오디오 녹음 없음)
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
