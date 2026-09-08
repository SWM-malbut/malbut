# Malbut Autonomy

`malbut_autonomy`는 시뮬레이션과 실제 ROSOrin 로봇에서 함께 사용하는
자율주행 응용 ROS 패키지를 기능별로 묶은 디렉터리다. 이 디렉터리 자체는
ROS 패키지가 아니며, 내부 패키지는 각각 독립적으로 빌드하고 실행한다.

| 패키지 | 역할 |
|---|---|
| `malbut_roaming` | 지도 안에서 목적지를 선택하는 자율 순회 |
| `malbut_patrol` | 고정 좌표 없이 지도에서 관측 위치를 선택하는 카메라 순찰 Action |

공유 ROS 인터페이스와 Capability Manifest는 저장소 최상위의
`malbut_interfaces`에서 관리한다.

객체 검출, 재식별, 사람 추적은 각각 저장소 최상위의 `malbut_yolo`,
`malbut_reid`, `malbut_tracking`에서 관리한다. 추적 전용 LiDAR 전처리는
별도 패키지가 아니라 `malbut_tracking` 안에 포함한다.

```bash
colcon build --symlink-install --packages-select \
  malbut_interfaces \
  malbut_roaming \
  malbut_patrol

ros2 launch malbut_roaming roaming.launch.py
ros2 launch malbut_patrol patrol.launch.py
```

`colcon`과 `rosdep`은 하위 디렉터리를 재귀적으로 탐색하므로 저장소 루트에서
기존과 동일하게 빌드할 수 있다. 소스 파일을 경로로 직접 실행하지 말고 ROS
패키지 이름을 통해 실행한다.

구조 원칙은 다음과 같다.

- 로봇 형상과 센서 장착 정보는 `malbut_description`에서 관리한다.
- Gazebo 월드와 시뮬레이션 전용 연결은 `malbut_gazebo`에서 관리한다.
- 순찰·순회 로직은 이 디렉터리에서 독립 ROS 패키지로 관리한다.
- 여러 패키지가 공유하는 ROS 타입만 최상위 `malbut_interfaces`에 둔다.
- 한 패키지의 내부 구현을 다른 패키지가 파일 경로로 직접 참조하지 않는다.
