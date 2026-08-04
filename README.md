# Malbut Simulation

ROS 2 Humble과 Gazebo Fortress에서 Malbut 로봇 모델과 시뮬레이션 환경을 실행하기 위한 패키지입니다.

- 저장소: [SWM-malbut/malbut](https://github.com/SWM-malbut/malbut)
- 로봇 모델 패키지: `malbut_description`
- 시뮬레이션 패키지: `malbut_gazebo`
- 홈캠 패키지: `homecam_media_agent`, `homecam_detector`
- 대화·에이전트 계약 패키지: `malbut_agent_server`

## 1. 기준 환경

Ubuntu 설치, GPU 드라이버, 네트워크와 GitHub 계정 설정은 완료되어 있다고 가정합니다.

- Ubuntu 22.04
- ROS 2 Humble
- Gazebo Fortress 및 `ros_gz`
- Nav2, SLAM Toolbox, `colcon`, `rosdep`

## 2. ROS/Gazebo 설치

ROS와 Gazebo가 없다면 팀 기반환경 설치기를 사용합니다.

```bash
git clone https://github.com/hyenje/Foundation.git ~/Foundation
cd ~/Foundation

chmod +x install_ros2_gazebo.sh
./install_ros2_gazebo.sh install
./install_ros2_gazebo.sh check --smoke-test
```

이미 ROS 2 Humble과 Gazebo Fortress가 설치되어 있다면 이 단계는 생략합니다.

## 3. 프로젝트 최초 설정

### 저장소 받기

```bash
mkdir -p ~/ros2_ws/src
cd ~/ros2_ws/src
git clone https://github.com/SWM-malbut/malbut.git
```

이미 저장소가 있다면 이 단계는 생략합니다.

### 의존성 설치 및 빌드

```bash
cd ~/ros2_ws
source /opt/ros/humble/setup.bash

rosdep update
rosdep install --from-paths src --ignore-src -y --rosdistro humble

colcon build --symlink-install
source ~/ros2_ws/install/local_setup.bash
```

`--symlink-install`의 하이픈은 문서용 긴 대시(`—`)가 아닌 일반 하이픈 두 개(`--`)를 사용해야 합니다.

### 빌드 확인

```bash
colcon list
ros2 pkg prefix malbut_description
ros2 pkg prefix malbut_gazebo
```

두 패키지의 설치 경로가 출력되면 정상입니다.

## 4. 셸 환경과 약어 설정

매번 ROS 환경을 직접 불러오는 대신 `~/.typerc`에 아래 내용을 저장할 수 있습니다.

```bash
source /opt/ros/humble/setup.bash

if [ -f "$HOME/ros2_ws/install/local_setup.bash" ]; then
  source "$HOME/ros2_ws/install/local_setup.bash"
fi

if [ -f /usr/share/colcon_argcomplete/hook/colcon-argcomplete.bash ]; then
  source /usr/share/colcon_argcomplete/hook/colcon-argcomplete.bash
fi

if [ -f /usr/share/vcstool-completion/vcs.bash ]; then
  source /usr/share/vcstool-completion/vcs.bash
fi

if [ -f /usr/share/colcon_cd/function/colcon_cd.sh ]; then
  source /usr/share/colcon_cd/function/colcon_cd.sh
fi

export _colcon_cd_root="$HOME/ros2_ws"
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RCUTILS_CONSOLE_OUTPUT_FORMAT='[{severity}]: {message}'
export RCUTILS_COLORIZED_OUTPUT=1
export RCUTILS_LOGGING_USE_STDOUT=0
export RCUTILS_LOGGING_BUFFERED_STREAM=1

if command -v register-python-argcomplete3 >/dev/null 2>&1; then
  eval "$(register-python-argcomplete3 ros2)"
  eval "$(register-python-argcomplete3 colcon)"
fi

alias cw='cd ~/ros2_ws'
alias cs='cd ~/ros2_ws/src'
alias ccd='colcon_cd'

alias cb='cd ~/ros2_ws && colcon build --symlink-install'
alias cbs='colcon build --symlink-install'
alias cbp='cd ~/ros2_ws && colcon build --symlink-install --packages-select'
alias cbu='cd ~/ros2_ws && colcon build --symlink-install --packages-up-to'

alias ct='cd ~/ros2_ws && colcon test'
alias ctp='cd ~/ros2_ws && colcon test --packages-select'
alias ctr='cd ~/ros2_ws && colcon test-result --verbose'

alias tl='ros2 topic list'
alias te='ros2 topic echo'
alias nl='ros2 node list'

alias di='cd ~/ros2_ws && rosdep install --from-paths src --ignore-src -y --rosdistro humble'
```

`~/.bashrc` 마지막에 다음 한 줄을 추가한 뒤 새 터미널을 열거나 `source ~/.bashrc`를 실행합니다.

```bash
source ~/.typerc
```

자주 사용하는 약어:

| 약어 | 기능 |
| --- | --- |
| `cw` | `~/ros2_ws`로 이동 |
| `cs` | `~/ros2_ws/src`로 이동 |
| `cb` | 워크스페이스 전체 빌드 |
| `cbp <패키지>` | 선택한 패키지만 빌드 |
| `ct` | 전체 테스트 |
| `ctr` | 테스트 결과 확인 |
| `tl` | 토픽 목록 확인 |
| `te <토픽>` | 토픽 메시지 확인 |
| `nl` | 노드 목록 확인 |
| `di` | 워크스페이스 의존성 설치 |

Gazebo Fortress 프로세스는 실행한 터미널에서 `Ctrl+C`로 종료합니다. 기존 `killgazebo` 약어는 Gazebo Classic용이므로 사용하지 않습니다.

## 5. 실행 방법

새 터미널에서는 먼저 환경을 불러옵니다. `~/.typerc`를 등록했다면 생략할 수 있습니다.

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/local_setup.bash
```

### 빈 월드

```bash
ros2 launch malbut_gazebo worlds.launch.py world_name:=empty
```

### 실내 테스트 월드

```bash
ros2 launch malbut_gazebo worlds.launch.py world_name:=small_house
```

### 로봇 모델만 확인

```bash
ros2 launch malbut_description display.launch.py
```

### 키보드 조작

시뮬레이션을 실행한 상태에서 새 터미널을 열고 실행합니다.

```bash
ros2 run malbut_gazebo teleop_key_control
```

조작 키는 `w`/`s`(전진/후진), `a`/`d`(좌우 횡이동),
`q`/`e`(좌우 회전), `Space`(정지)이며 종료는 `Ctrl+C`입니다.

### 저장된 지도 기반 내비게이션

시뮬레이션과 브리지가 실행 중인 상태에서 새 터미널을 열고 Nav2를
실행합니다.

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/local_setup.bash
ros2 launch malbut_gazebo navigation.launch.py
```

기본 지도는 패키지의 `maps/map_01.yaml`입니다. 다른 지도를 사용하려면
절대 경로를 전달합니다.

```bash
ros2 launch malbut_gazebo navigation.launch.py \
  map:=/absolute/path/to/map.yaml
```

기본값은 개별 Nav2 프로세스를 실행합니다. composition 경로를 검증하거나
사용하려면 `use_composition:=True`를 전달합니다. RViz가 열린 뒤
`2D Pose Estimate`로 초기 위치를 지정하고 `Nav2 Goal`로 목표를 보냅니다.

## 6. 사용자 지도와 공간 영역 만들기

사용자 지도는 로봇이 집을 탐색한 뒤 SLAM Toolbox로 저장한 지도
`.yaml + .pgm`을 공간 좌표의 기준으로 사용합니다. 흑백 OccupancyGrid를
그대로 노출하지 않고 노이즈 제거, 벽 방향 정렬, 폴리곤 단순화를 거쳐
사용자용 벡터 지도로 변환합니다. Nav2 지도와 User Map은 같은 `map`
좌표계를 유지하므로 사용자가 지정한 영역을 로봇 동작에 다시 적용할 수
있습니다. 현재 변환기는 ROS map YAML의 기본 `trinary` 모드만 지원하며,
점유도 의미가 다른 `scale`·`raw` 모드는 잘못 해석하지 않도록 명시적으로
거부합니다. `negate`와 점유 임계값을 생략하면 map_server 기본값을 사용합니다.

SLAM 탐색과 지도 저장을 마친 뒤 저장된 YAML을 변환합니다.
`preview.png`는 결과 확인용이고, 영역 편집의 기준 데이터는 GeoJSON입니다.

```bash
mkdir -p ~/malbut_maps/my_home
ros2 run malbut_gazebo build_user_map \
  ~/malbut_maps/my_home/map.yaml \
  -o ~/malbut_maps/my_home/user_map.geojson \
  --preview ~/malbut_maps/my_home/preview.png \
  --map-id my-home
```

공간 편집기를 실행하고 출력된 주소를 브라우저에서 엽니다.

```bash
ros2 run malbut_gazebo user_map_editor \
  --map ~/malbut_maps/my_home/user_map.geojson
# http://127.0.0.1:8765/?map=user-map.geojson
```

편집기에서 자동 생성된 방 후보를 선택하고 `방 나누기`를 누른 뒤
방 안의 두 지점을 지정하면, 두 점을 지나는 직선으로 방을 나눌 수 있습니다.
너무 작은 방이 생기거나 정확히 두 공간으로 나뉘지 않는 선은 적용하지
않습니다. 나뉜 방을 다시 합치려면 첫 번째 방을 선택하고 `방 합치기`를
누른 다음 지도나 방 목록에서 맞닿은 두 번째 방을 선택합니다. 서로 떨어진
방은 하나로 합칠 수 없습니다. 편집 결과는 브라우저에 자동 저장되며
`User Map 내보내기`로 수정된 방 경계가 포함된 GeoJSON을 내려받습니다.

Room을 선택하면 이름과 유형(거실·침실·주방·복도 등)을 지정할 수 있습니다.
분할된 Room은 변하지 않는 기본 이름을 공유합니다. 다시 합치면 목록에는
`거실 (거실 A + 거실 B)`처럼 표시하지만 내부 기본 이름은 `거실`로 유지해,
분할과 병합을 반복해도 이름이 계속 이어 붙지 않습니다. 같은 분할에서 나온
두 Room을 합칠 때는 분할 전 경계를 그대로 복원하므로 반복 편집으로 Room
색칠 영역이 줄어들지 않습니다.

그다음 거실·침실·진입 금지 구역처럼 원하는 Zone 폴리곤을 직접 그릴 수
있습니다. Zone은 현재 지도의 `map_id`와 함께 별도 GeoJSON으로 내보내므로,
다른 집의 지도에 잘못 적용되지 않습니다.

Zone은 하나의 Room 안에만 만들 수 있으며 자기 교차 경계와 `0.1m²` 미만
영역은 거부합니다. 각 Zone에는 소속 Room, 면적, 중심점, 공간 유형, 로봇
동작(`진입 허용`·`가급적 회피`·`진입 금지`)이 저장됩니다. Room을 나눈 뒤
Zone이 새 경계를 가로지르면 `소속 방 확인 필요`로 표시되고, 경계를 고치거나
Zone을 삭제하기 전에는 영역 파일을 내보낼 수 없습니다.

| 단계 | 입력 | 출력 |
| --- | --- | --- |
| SLAM 탐색·저장 | `/scan`, TF | `map.yaml`, `map.pgm` |
| 사용자 지도 생성 | 저장된 SLAM 지도 | `user_map.geojson` |
| 방 경계 편집 | User Map + 사용자 분할선 | `*-user-map.geojson` |
| Zone 편집 | User Map + 사용자 입력 | `*-zones.geojson` |

현재 버전은 OccupancyGrid의 기하 구조와 출입구 폭을 이용해 방 후보를
분할합니다. 이후 RGB-D 문 검출을 같은 User Map에 추가하면 개방형 공간의
경계 근거를 보강할 수 있습니다.

User Map 생성기는 좁은 출입구를 기준으로 방 후보도 자동 생성합니다. 기본
출입구 폭은 `0.8m`, 최소 방 면적은 `1.5m²`입니다. 집 구조에 맞게 조정할 수
있지만, 넓게 연결된 오픈형 공간은 근거 없이 나누지 않고 하나의 방 후보로
유지합니다.

```bash
ros2 run malbut_gazebo build_user_map ~/malbut_maps/my_home/map.yaml \
  -o ~/malbut_maps/my_home/user_map.geojson \
  --preview ~/malbut_maps/my_home/rooms.png \
  --doorway-width 0.8 \
  --minimum-room-area 1.5
```

## 7. 센서 모델

현재 제공되는 로봇 프로필은 `Aurora930 Pro` RGB-D 카메라를 사용합니다.
센서 형상과 시뮬레이션 파라미터는
`malbut_description/config/ultimate_orin_nx_super_mecanum.yaml`에서 관리합니다.
다른 카메라 프로필은 아직 제공하지 않습니다.

## 8. 홈캠 영상 스트리밍

기존 ROS 2/Gazebo 기반 환경을 설치한 팀원은 기반 환경을 다시 설치하지
않습니다. 저장소를 업데이트한 뒤 홈캠 전용 의존성을 설치하고 현재
시뮬레이션·홈캠 패키지를 빌드합니다.

```bash
cd ~/ros2_ws/src/malbut
git pull

./homecam_agent/scripts/setup_portable_sim.sh
./homecam_agent/scripts/run_gazebo_homecam.sh --check-only
```

`--check-only`는 장치 자격 증명 없이 `small_house`를 실행하고 RGB,
CameraInfo 및 실제 카메라 프레임 수신을 확인한 뒤 종료합니다.

원격 스트리밍을 처음 실행할 때만 관리자가 PC별로 발급한 장치 ID와 token을
등록합니다. token은 명령행이나 Git에 저장하지 않고 숨김 입력으로
`~/.config/homecam`에 권한 `600`으로 보관합니다.

```bash
./homecam_agent/scripts/configure_sim_device.sh \
  --device-id REGISTERED_DEVICE_ID \
  --backend-url https://YOUR_BACKEND

./homecam_agent/scripts/run_gazebo_homecam.sh
```

이미 다른 터미널에서 Gazebo가 실행 중이면 해당 카메라 토픽을 그대로
재사용합니다.

```bash
./homecam_agent/scripts/run_gazebo_homecam.sh --reuse-gazebo
```

자세한 설정과 장애 대응은
[`homecam_agent/README.md`](homecam_agent/README.md)를 확인합니다.

## 9. 대화·에이전트 안전 계약

`malbut_agent_server`는 LLM과 로봇 실행 계층 사이의 요청·응답 스키마,
고수준 Tool allowlist와 결정론적 안전 게이트를 정의합니다. LLM은
`/cmd_vel`, 모터 PWM, 비상 정지 해제 같은 저수준 제어를 직접 수행하지
않습니다.

상세 계약과 미승인 연관 인터페이스는
[`SWM25-69_CONVERSATION_AGENT_CONTRACT.md`](malbut_agent_server/docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md)를
확인하십시오.

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server
PYTHONPATH=. python3 -m pytest -q test
```

SWM25-70 브랜치부터는 외부 API를 호출하지 않는 Mock으로 세션 생성·조회·
초기화·종료·삭제, 최근 10턴, 재전송 중복 방지와 단일 프로세스 동시 요청
순서를 검증할 수 있습니다.

SWM25-71에서는 최근 N턴 원문, 그 이전 대화의 rolling summary, 사용자별
장기 기억을 서로 분리해 제한된 모델 입력으로 구성합니다. 저장된 문맥은
신뢰되지 않은 JSON 데이터로만 전달되며, 응답에는 원문이 아닌 영역별 크기
메트릭만 노출합니다. 상세 설계와 검증 근거는
[`SWM25-71_USER_CONTEXT_INTEGRATION.md`](malbut_agent_server/docs/jira/SWM25-71_USER_CONTEXT_INTEGRATION.md)를
확인하십시오.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --provider mock \
  --database /tmp/malbut-agent-demo.sqlite3 \
  --check
```

## 10. 수정 후 다시 빌드

launch나 config 파일이 삭제된 변경을 받은 뒤에는 이전 `--symlink-install`
링크가 `build`와 `install`에 남을 수 있습니다. 이 경우 Malbut 패키지의
생성물만 정리한 뒤 다시 빌드합니다.

```bash
cd ~/ros2_ws
rm -rf \
  build/malbut_description build/malbut_gazebo \
  install/malbut_description install/malbut_gazebo
colcon build --symlink-install \
  --packages-select malbut_description malbut_gazebo
source ~/ros2_ws/install/local_setup.bash
```

일반적인 소스 수정에는 생성물을 지울 필요가 없습니다.

```bash
cd ~/ros2_ws
colcon build --symlink-install
source ~/ros2_ws/install/local_setup.bash
```

특정 패키지만 수정했다면 다음처럼 빌드할 수 있습니다.

```bash
cbp malbut_description
cbp malbut_gazebo
```

## 11. 기본 점검

```bash
ros2 topic list
ros2 node list
ros2 topic echo /odom
ros2 topic echo /scan
ros2 topic echo /imu
```

문제가 발생하면 먼저 아래 순서로 확인합니다.

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/local_setup.bash
rosdep check --from-paths ~/ros2_ws/src --ignore-src --rosdistro humble
colcon build --symlink-install
```

## 12. 라이선스

Malbut Contributors가 작성한 프로젝트 코드는 Apache License 2.0으로
배포합니다. 이 라이선스는 저장소에 포함된 모든 제3자 자료에 일괄
적용되지 않습니다.

- AWS RoboMaker Small House 에셋에는 번들된 MIT 형식의 라이선스가
  적용됩니다.
- Hiwonder ROSOrin에서 유래하거나 이를 바탕으로 수정된 로봇 자료는
  `LicenseRef-Hiwonder-ROSOrin`으로 구분하며, Apache-2.0 적용 대상에서
  제외합니다.
- Intel 및 Open Source Robotics Foundation의 기존 Apache-2.0 고지는
  해당 파일에 유지합니다.

전체 범위와 출처는 [LICENSE](LICENSE) 및
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 확인하십시오.
