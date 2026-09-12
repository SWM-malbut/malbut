# 공유 인식 파이프라인 연결 확인

`check_shared_pipeline.py`는 명시적으로 실행하는 GPU 통합 검사다.
일반 `pytest`/CI 실행에서는 자동으로 돌지 않는다.

```zsh
cd ~/ros2_ws
source /opt/ros/humble/setup.zsh
source install/setup.zsh
source install/malbut_test/local_setup.zsh
python3 src/malbut/malbut_yolo/test/check_shared_pipeline.py
```

준비된 YOLO26n·OSNet 모델과 CUDA 실행 환경이 필요하다. 이미 설치된
Ultralytics `assets/bus.jpg`를 읽으며 이미지나 모델을 내려받지 않는다.
다른 위치의 같은 이미지를 쓰려면 `--image <경로>`를 지정한다.

## 검사 범위

- ROS domain `161`, localhost 전용으로 실제 YOLO·OSNet·RGB-D localizer를 실행.
  그 domain이 사용 중이면 실패하며, `--domain`으로 다른 빈 번호를 선택할 수 있다.
- 녹화된 정지 RGB 이미지와 **합성 pinhole CameraInfo 및 합성 Depth**를 5 Hz로
  약 30초간 발행. 앞 절반은 2 m, 뒤 절반은 NaN Depth다. 해당 사진의 실제
  카메라 보정값이나 실제 거리 정보가 아니다.
- YOLO person 검출, 같은 이미지에서의 ID 유지, 2 m 투영,
  기존 3 m bearing-only 관측과 큰 공분산을 확인.
- YOLO → 재식별 → 3D 위치 → JPEG 확인 영상 → 처리시간 trace의
  **동일 원본 timestamp 연결**을 확인.
- 자기 launch 프로세스에만 먼저 SIGINT를 전달해 자식 프로세스를 정상 종료.
  Gazebo·다른 ROS domain·사용자가 띄운 프로세스는 건드리지 않는다.

결과 JSON과 launch 로그는 실행 마지막에 출력되는
`/tmp/malbut-shared-pipeline-*/` 안에 저장된다. 각 단계별 받은 프레임 수와
검증된 프레임 수도 출력한다. 일부 프레임의 전달 누락은 감추지 않는다.

**이것은 연결·자료형·투영·시간 기록 검사이지, 사람 추종 성능 벤치마크나
카메라 FPS 보장 검사가 아니다.** 실제 사람의 이동/가림/다중 인물 재식별,
카메라 보정 정확도, Nav2 주행, Jetson 성능은 이 검사로 검증하지 않는다.
처리시간의 경계는 `YOLO 이미지 콜백 진입 → 3D 검출 발행`이다.
센서 촬영부터 ROS 전달까지의 지연이나 주행 명령 전송까지를 포함하지 않는다.

## 2026-09-08 개발 PC 실행 기록

NVIDIA GPU에서 YOLO와 OSNet TensorRT FP16을 실제 실행한 결과다.

| 항목 | 결과 |
| --- | --- |
| 입력 RGB | 145개 |
| YOLO 결과 | 85개 |
| 재식별 결과 | 74개 |
| 3D / JPEG / trace | 각각 56개 |
| 모든 단계가 연결된 2 m / bearing 관측 | 33개 / 23개 |
| 유지된 사람 ID | 1, 2, 3, 4 |
| 위 구간 처리시간 median / p95 / max | 12.42 / 23.62 / 27.51 ms |

두 Depth 조건과 메시지 연결 검사는 통과했다. 그러나 입력 전체가 끝까지
전달된 것은 아니므로 이 수치를 전체 입력 프레임 처리율이나 실제 로봇의
지연 보장으로 해석하지 않는다. 로그에는 세 노드의 정상 종료도 확인되었다.
