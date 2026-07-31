# Malbut Simulation

ROS 2 Humble과 Gazebo Fortress에서 Malbut 로봇 모델과 시뮬레이션 환경을 실행하기 위한 패키지입니다.

- 저장소: [SWM-malbut/malbut](https://github.com/SWM-malbut/malbut)
- 로봇 모델 패키지: `malbut_description`
- 시뮬레이션 패키지: `malbut_gazebo`

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

## 6. 센서 모델

현재 제공되는 로봇 프로필은 `Aurora930 Pro` RGB-D 카메라를 사용합니다.
센서 형상과 시뮬레이션 파라미터는
`malbut_description/config/ultimate_orin_nx_super_mecanum.yaml`에서 관리합니다.
다른 카메라 프로필은 아직 제공하지 않습니다.

## 7. 수정 후 다시 빌드

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

## 8. 기본 점검

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

## 9. 라이선스

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
