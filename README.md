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
ros2 launch malbut_gazebo room_worlds.launch.py
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

조작 키는 `w`, `a`, `s`, `d`이며 종료는 `Ctrl+C`입니다.

## 6. 센서 모델 선택

기본 카메라는 `aurora`입니다. Dabai 모델을 사용할 때는 실행 전에 다음 값을 설정합니다.

```bash
export DEPTH_CAMERA_TYPE=Dabai
```

기본값으로 돌아가려면 다음을 실행합니다.

```bash
unset DEPTH_CAMERA_TYPE
```

## 7. 수정 후 다시 빌드

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
