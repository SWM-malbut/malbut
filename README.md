# Malbut Simulation

ROS 2 Humble과 Gazebo Fortress에서 Malbut 로봇 모델과 시뮬레이션 환경을 실행하기 위한 패키지입니다.

- 저장소: [SWM-malbut/malbut](https://github.com/SWM-malbut/malbut)
- 로봇 모델 패키지: `malbut_description`
- 시뮬레이션 패키지: `malbut_gazebo`
- RGB-D 사람 인식 패키지: `malbut_perception`
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
ros2 pkg prefix malbut_perception
```

세 패키지의 설치 경로가 출력되면 정상입니다.

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

### RGB-D 사람 인식

최초 한 번 호환 YOLO와 OSNet 모델을 준비한 뒤 휴머노이드와 센서 기반
사람 인식·재식별을 함께 실행합니다.

```bash
cd ~/ros2_ws/src/malbut
./malbut_perception/scripts/prepare_yolov5_model.sh
./malbut_perception/scripts/prepare_osnet_model.sh

cd ~/ros2_ws
source install/local_setup.bash
ros2 launch malbut_gazebo humanoid_demo.launch.py perception:=true
```

검출 결과는 `/perception/person/detections_2d`, depth 기반 위치는
`/perception/person/detections_3d`, 확인용 영상은
`/perception/person/debug_image`에서 볼 수 있습니다. 인식 코드는 Gazebo의
휴머노이드 이름이나 실제 좌표를 사용하지 않습니다.

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
사용하려면 `use_composition:=True`를 전달합니다. `slam.launch.py`는 실행 중
`map→odom` 상태를 `~/.ros/malbut/localization_state.yaml`에 자동 기록하고,
정적 지도 Navigation은 같은 odom 세션의 상태와 지도 내용이 일치할 때 AMCL
초기 위치를 자동 복원합니다. 상태가 없거나 다른 지도이거나 시뮬레이션이
재시작된 경우에는 잘못된 위치로 주행하지 않고 복원을 거부합니다.

SLAM 탐색 직후에는 SLAM Toolbox를 끄지 않고 그 위치 추정을 그대로 사용해
Nav2 경로 계획·제어만 시작할 수 있습니다. 이 방식은 위치를 수동으로 다시
맞출 필요가 없고 SLAM과 AMCL이 동시에 `map→odom`을 발행하지도 않습니다.

```bash
ros2 launch malbut_gazebo navigation.launch.py \
  localization_source:=slam \
  zone_mask:=/absolute/path/to/zone-filter.yaml
```

`localization_source:=slam`은 실행 중인 `slam.launch.py`와 함께 사용합니다.
저장된 지도와 AMCL을 사용하는 기본 `static` 모드에서는 `map`과 필요하면
`zone_mask`를 전달합니다. 두 모드 모두 위치가 확정된 뒤 `Nav2 Goal`로 목표를
보냅니다.

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
SLAM 실행 중에는 Navigation으로 넘길 위치 기준도 자동 저장되므로 mapper를
강제로 종료하기 전에 별도의 `2D Pose Estimate`를 기록할 필요가 없습니다.

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
  --map ~/malbut_maps/my_home/user_map.geojson \
  --slam-map ~/malbut_maps/my_home/map.yaml
# http://127.0.0.1:8765/?map=user-map.geojson
```

새 User Map은 탐색된 주행 가능 영역 전체를 `공간 1` 하나로 시작합니다.
이 초기 공간을 선택하고 `방 나누기`를 누른 뒤 벽 두 곳을 차례로 누르면
두 점만 연결된 독립 분할선 하나가 생깁니다. 다음 두 점은 앞선 선과 연결되지
않는 별도 분할선이 됩니다. 대각선 중앙의 작은 핸들을 끌면 가장 가까운
직각 모서리가 생겨 즉시 ㄱ자 선으로 바뀝니다. 키보드 보조키 없이 제어점을
계속 드래그해 수평·수직 형태를 조정할 수 있습니다. 각 선의 시작점과 끝점은
벽에서 `0.25m` 이내를
누르면 가장 가까운 벽으로 자동 보정됩니다. `분할 적용`을 눌렀을 때 모든
독립 선을 함께 적용해 정확히 두 공간으로 나뉘는 경우만 빨간 실선과
`분할 가능`으로 표시되며 Room 경계에 반영할 수 있습니다. 아직 방이 나뉘지
않는 선은 빨간 점선과 `분할 불가`로 표시됩니다.
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

그다음 `새 Zone`을 누르면 주행 가능 영역 안에 사각형 Zone이 생성됩니다.
Zone 내부를 끌어 위치를 옮기고 네 모서리나 변의 핸들을 끌어 크기를
조절합니다. 지도 클릭으로 꼭짓점을 계속 추가하지 않습니다. Zone은 Room과
독립된 Nav2 주행 규칙이므로 여러 Room을 가로지를 수 있고, Room을 나누거나
합쳐도 경계와 동작이 바뀌지 않습니다. Zone은 현재 지도의 `map_id`와 함께
자동 저장되므로 다른 집의 지도에 잘못 적용되지 않습니다.

Zone 경계는 지도의 주행 가능 영역 안에 있어야 하며 자기 교차 경계와
`0.1m²` 미만 영역은 거부합니다. 각 Zone에는 면적, 중심점과 Zone 유형
(`통행 허용`·`우회 권장`·`진입 금지`)이 저장됩니다. 이전 편집기가 저장한
Room 연결 메타데이터는 불러올 때 제거됩니다. `주행에 적용`을 누르면 최신
Zone 파일과 `zone-filter.yaml/.pgm`이 User Map 옆에 자동 생성됩니다. Nav2
필터 서버가 실행 중이면 새 마스크를 즉시 다시 불러오고, 실행 전이면 다음
Nav2 실행부터 적용됩니다. `영역 내보내기`는 백업·이관용이며 주행 적용에
필수인 절차가 아닙니다.

CI나 자동화 환경에서는 동일한 변환을 CLI로 수행할 수도 있습니다. User Map
생성 시 `--map-id`를 지정했다면 같은 값을 전달해야 합니다.

```bash
ros2 run malbut_gazebo build_zone_filter_mask \
  ~/malbut_maps/my_home/map.yaml \
  ~/malbut_maps/my_home/my-home-zones.geojson \
  -o ~/malbut_maps/my_home/zone-filter.yaml \
  --map-id my-home
```

마스크에서 통행 허용은 비용 `0`, 우회 권장은 통행 가능한 고비용 `70`,
진입 금지는 치명적 비용 `100`으로 변환됩니다. 진입 금지 Zone에는 기본
`0.20m`의 하드 버퍼가 포함됩니다. 겹치는 Zone은 진입 금지, 우회 권장,
통행 허용 순으로 우선합니다.

벽 주변은 로봇 중심 기준 `0.24m`까지 통과 불가능한 최소 안전거리로,
`0.24~0.60m`는 벽에 가까울수록 비용이 높아지는 통과 가능한 선호 거리로
변환됩니다. 따라서 넓은 경로가 있으면 우선 사용하지만, 넓은 경로가 없을
때도 최소 안전거리만 확보되면 좁은 통로를 사용할 수 있습니다. 최소
안전거리에서도 경로를 만들 수 없다면 이를 더 줄여 충돌 위험을 높이지 않고
목표 이동 실패로 처리해야 합니다. 사용자 애플리케이션에서는 이 결과를
`안전거리를 확보할 수 있는 경로가 없습니다`로 안내해야 합니다. 생성된 마스크를
navigation launch에 전달하면 global/local costmap에 함께 적용됩니다.

```bash
ros2 launch malbut_gazebo navigation.launch.py \
  map:=~/malbut_maps/my_home/map.yaml \
  zone_mask:=~/malbut_maps/my_home/zone-filter.yaml
```

`zone_mask`를 생략하면 필터 서버와 플러그인이 비활성화되어 기존 Nav2와
동일하게 실행됩니다. 필터 마스크의 `raw` 모드는 원본 SLAM 지도의
`trinary` 모드와 다른 용도이며, Zone 비용값을 Nav2에 그대로 전달하기 위한
설정입니다.

| 단계 | 입력 | 출력 |
| --- | --- | --- |
| SLAM 탐색·저장 | `/scan`, TF | `map.yaml`, `map.pgm` |
| 사용자 지도 생성 | 저장된 SLAM 지도 | `user_map.geojson` |
| 방 경계 편집 | User Map + 사용자 분할선 | `*-user-map.geojson` |
| Zone 편집 | User Map + 사용자 입력 | `*-zones.geojson` |
| Nav2 필터 생성 | SLAM 지도 + Zone | `zone-filter.yaml`, `zone-filter.pgm` |

Room과 Zone의 의미는 자동 추론하지 않습니다. 사용자가 `공간 1`을 직접
나누고 거실·부엌·안방·자녀방 같은 Room 이름과 유형을 지정합니다. 그 위에
통행 허용·우회 권장·진입 금지 Zone을 별도로 그립니다. 따라서 집 구조가
달라져도 생성 알고리즘이 임의의 방 개수나 의미를 결정하지 않습니다.

```bash
ros2 run malbut_gazebo build_user_map ~/malbut_maps/my_home/map.yaml \
  -o ~/malbut_maps/my_home/user_map.geojson \
  --preview ~/malbut_maps/my_home/rooms.png
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
