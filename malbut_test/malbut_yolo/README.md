# malbut_yolo

공유 객체 검출기. upstream [yolo_ros](https://github.com/mgonzs13/yolo_ros)의
`YoloNode`를 그대로 사용하고 설정·실행·실측 시간 기록만 Malbut에서 담당한다.
사람 외에도 COCO 객체를 `/yolo/detections` (`yolo_msgs/msg/DetectionArray`)로 발행한다.
구독자는 필요한 클래스만 선택한다. upstream의 별도 tracking/3D 노드는 실행하지 않는다.

## 준비

실로봇은 [상위 안내](../README.md)의 Git clone·ROS 의존성·선택 빌드를 먼저 따른다.
upstream 소스는 이 패키지의 `vendor/yolo_ros`에 포함되어 있다.
적용본을 복사할 때 함께 가져오며 별도 clone이나 import는 하지 않는다.

```bash
bash ~/ros2_ws/src/malbut/malbut_test/malbut_yolo/scripts/prepare_runtime.sh
```

`python3-venv`가 필요하다. PyTorch/Ultralytics는 별도
`~/.cache/malbut_yolo/runtime`에 설치한다. 기본 ROS·드라이버는 변경하지 않는다.
upstream의 `uv sync` 기반 composite launch는 사용하지 않는다. 빌드 시에도
위 PATH로 upstream 자동 환경 재설정을 피한다.

데스크톱 NVIDIA GPU는 공식 PyTorch CUDA 12.8 wheel을 사용한다.
Jetson Orin NX는 **설치된 JetPack에 맞는 NVIDIA PyTorch/torchvision**이 먼저 필요하다.
준비 스크립트는 이를 확인하고 유지하며, ARM 기기에 데스크톱 wheel을 설치하지 않는다.
실제 NX 성능/메모리 검증은 기기에서 별도로 해야 한다.
[PyTorch 설치](https://pytorch.org/get-started/locally/),
[NVIDIA Jetson 설치](https://docs.nvidia.com/deeplearning/frameworks/install-pytorch-jetson-platform/index.html).

## 실행

```bash
ros2 launch malbut_yolo yolo.launch.py use_sim_time:=false
```

실기기는 `use_sim_time:=false`. `rgb_topic`, `model_path`, `device`,
`python_executable`을 지정할 수 있다. 기본 모델은 YOLO26n `.pt`, CUDA FP16이다.
TensorRT `.engine`은 upstream의 `.to(device)` 경로가 지원하지 않으므로 이 launch에
그대로 넣지 않는다. CPU는 필요한 경우에만 `device:=cpu`로 명시한다.
카메라 topic·프레임은 원본 그대로 유지하고 새 입력을 처리한다. 별도 Hz 제한은 없다.

YOLO·ReID는 Bringup 상시 노드이며 시스템 관리자 Manifest를 만들지 않는다.
upstream lifecycle/enable 서비스는 그대로 있지만 현재 관리자 관리 대상은 아니다.
`/perception/yolo_processing_trace`는 추론 직전/후 Linux monotonic 시간이며,
tracking localizer가 3D 발행 시점과 합쳐 기존 E2E 평가에 전달한다.
센서 헤더 시각은 식별 키이고, 시뮬레이션 시각으로 연산 지연을 계산하지 않는다.

`prepare_yolo26_model.sh`는 기존 홈캠 ONNX 소비자용 도구로, **이 실기기 적용본의
준비 과정에서는 실행하지 않는다.** 홈캠 소스가 필요하며 새 공유 검출기는 그
ONNX 경로를 쓰지 않는다. 기존 `malbut_perception` 모델 캐시
이름은 다운로드/엔진 재생성을 피하려고 유지한다.

upstream yolo_ros 라이선스는 GPL-3.0, Ultralytics는 AGPL-3.0/상용 라이선스다.
배포 시 각 라이선스 조건을 확인해야 한다.
