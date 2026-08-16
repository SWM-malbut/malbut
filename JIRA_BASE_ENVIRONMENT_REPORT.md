# ROSOrin 기준 환경 보고서

## 확정 대상 제품

| 항목 | 기준 |
|---|---|
| 제품 구성 | Hiwonder ROSOrin Ultimate Kit |
| 메인 컨트롤러 | Jetson Orin NX Super 8GB |
| 표시 가격 | USD 1,399.99 |
| 카메라 | Aurora930 Pro 3D 깊이 카메라 |
| LiDAR | STL-19P D500 TOF LiDAR |
| 음성 장치 | 원형 6마이크 어레이 |
| 섀시 | 메카넘·애커먼·4륜 차동 전환형 |
| 운영체제·ROS | Ubuntu 22.04 / ROS2 Humble |
| 주요 기능 | SLAM, 내비게이션, 장애물 회피, YOLO26, OpenCV, MediaPipe, 음성 AI, OpenClaw |
| 공식 설명상 시뮬레이션 | Gazebo 시뮬레이션 및 RViz/URDF 지원 |

## 개발 PC 기준

- Windows 11과 Ubuntu 22.04.5 LTS 듀얼부팅
- 가상머신이 아닌 실제 SSD의 Ubuntu 사용
- ROS 배포판: ROS2 Humble
- Gazebo Sim: 6.18.0

## 현재 저장소의 시뮬레이션 구현 상태

| 기능 | 현재 상태 |
|---|---|
| 메카넘 구동 | 구현됨 |
| RViz/URDF 모델 | 구현됨 |
| Gazebo 카메라 | 일반 카메라 센서로 구현됨: 640×400, 30Hz |
| 3D 깊이 데이터 | 아직 확인되지 않음. 현재 센서 형식은 `camera`이며 깊이 카메라 형식이 아님 |
| LiDAR | GPU LiDAR 구현됨: `/scan`, 10Hz, 0.15~12m |
| LiDAR 360° 재현 | 미구현. 현재 수평 시야각은 약 156° |
| 애커먼·4륜 차동 구동 | 제품은 지원하지만 현재 Gazebo 구동 플러그인은 메카넘 기준 |
| 6마이크·음성 | 외형 메시는 있으나 음성 센서 시뮬레이션은 없음 |
| YOLO26·OpenClaw·대규모 AI 모델 | 제품 목표 기능이며 Gazebo 기본 모델에는 아직 통합되지 않음 |

## 기준 결정

이후 개발은 위 Ultimate Kit 사양을 최종 목표로 삼는다. 우선 현재 메카넘 Gazebo 모델에서 주행·카메라·LiDAR·SLAM을 안정화하고, 실제 제품과의 차이인 깊이 카메라, 360° LiDAR, 애커먼·차동 구동, 음성 및 AI 기능을 단계적으로 보완한다.
