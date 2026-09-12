# malbut_reid

공용 YOLO 검출 결과에 사람 재식별 ID를 연결하는 독립 ROS 2 패키지다.
YOLO 추론, Depth 투영, 로봇 주행은 수행하지 않는다. 기존 Malbut의
OSNet-AIN x1.0 인코더와 ByteTrack-style/appearance-gallery 연결 로직을
옮겼으며, 공식 ByteTrack 또는 OSNet 자체가 신원을 보장한다는 의미는 아니다.

## 실행

```bash
ros2 launch malbut_reid person_reidentification.launch.py use_sim_time:=false \
  rgb_topic:=/depth_cam/rgb0/image_raw
```

빌드·환경 설정은 상위 `malbut_test/README.md`를 따른다. 같은 카메라를 사용하는
`malbut_yolo`가 먼저 실행되어 있어야 검출 결과가 들어온다. 이 노드는
Bringup에서 한 번 실행하며 FollowPerson 실행·취소와 수명을 공유하지 않는다.
매니저용 Manifest 또는 실행/종료 Action은 제공하지 않는다.

## Topic 계약

| 방향 | 기본 Topic | 자료형 | 의미 |
| --- | --- | --- | --- |
| 입력 | `/camera/color/image_raw` | `sensor_msgs/msg/Image` | YOLO에 입력한 원본 RGB |
| 입력 | `/yolo/detections` | `yolo_msgs/msg/DetectionArray` | 원본 RGB Header를 보존한 검출 결과 |
| 출력 | `/perception/person/detections_2d` | `vision_msgs/msg/Detection2DArray` | 관측된 사람의 재식별 ID·신뢰도·픽셀 bbox |

출력 `Detection2D.id`는 갤러리가 할당한 숫자의 문자열이다. 객체 종류는
`results[].hypothesis.class_id=person`, 신뢰도는 YOLO 값을 유지한다.
배열과 각 검출의 Header도 입력 RGB와 같다. 사람 없는 프레임도 빈 배열로
발행한다. ID는 재시작마다 초기화되며 가족 계정 ID나 영구 등록 신원이 아니다.

RGB/검출은 ROS `message_filters.TimeSynchronizer`로 **같은 센서 시각**만
연결하고 frame ID도 확인한다. 검출이 늦게 도착해도 더 최신 영상으로 대체하지
않는다. `sync_queue_size`는 이 지연을 흡수할 이미지 개수(기본 60)이며 처리 Hz를
제한하지 않는다. 해당 RGB가 유실되거나 이미 버퍼에서 빠졌으면 그 결과는
처리할 수 없다. 입력 구독은 Sensor Data QoS, 출력은 Reliable/depth 10이다.

## 재식별 기억과 계산

- OSNet은 사람 crop을 512차원 정규화 특징으로 변환한다.
- 기존 IoU/코사인 유사도 기반 연결 및 ID 복원 정책을 유지한다.
- 안정적인 관측은 기본 3개 검출 프레임마다 특징 갱신, 새로운/멀리 이동한
  대상처럼 기하학적 연결만으로 부족하면 즉시 특징을 구한다.
- 추론이 필요 없으면 RGB 변환과 OSNet 계산을 생략한다. 반복 타이머는 없다.
- 사람별 특징은 기본 최대 30개 보관한다. `reid_max_inactive_frames: 0`은
  오래 안 보인 사람의 갤러리를 이 노드가 살아 있는 동안 삭제하지 않는 정책이다.
- **사람당 특징 수만 제한된다.** 신규 ID가 계속 생기면 전체 갤러리 메모리는
  증가할 수 있다. 자동 신원 삭제와 디스크 영구 저장은 이번 범위에 추가하지 않는다.
- 사람이 보인다고 신원이 확정되는 것은 아니다. 옷·자세·가림·화질에 따라
  오연결 또는 새 ID가 생길 수 있고, 재식별 품질은 별도 실제 관측으로 확인한다.

## 모델 및 실행 환경

기존 다운로드 모델을 다시 만들지 않도록 기본 경로는
`~/.cache/malbut_perception/osnet_ain_x1_0_msmt17.onnx`로 유지한다.
`reid_model_path` launch 인자로 다른 경로를 지정할 수 있다.

```bash
cd ~/ros2_ws/src/malbut
bash malbut_test/malbut_reid/scripts/prepare_osnet_model.sh
bash malbut_test/malbut_reid/scripts/prepare_inference_runtime.sh
```

위 명령은 로봇에 Git clone한 저장소의 복사본을 사용하며 Bash 스크립트이므로
Zsh에서도 그대로 실행한다. 준비는 명시적인 설치/변환 단계이며 노드 시작 시
다운로드하지 않는다. 추론 의존성은 `~/.cache/malbut_reid/runtime`의
`--system-site-packages` 가상환경에만 설치하고 기존 사용자·시스템 Python
패키지를 제거하거나 변경하지 않는다. 모델 변환용 CPU PyTorch도 별도 cache
환경에 유지하므로 로봇에 설치된 CUDA PyTorch를 바꾸지 않는다.

launch는 이 추론 환경의 Python을 기본 사용한다. 다른 환경은
`python_executable:=/path/to/runtime/bin/python`으로 지정한다.
설치·단독 launch는 `MALBUT_REID_RUNTIME` 또는 `XDG_CACHE_HOME`도 지원한다.
Bringup에서는 `reid_python_executable`로 해당 경로를 전달한다.
모델 cache를 변경하면 `reid_model_path`도 명시한다.
Jetson에서는 JetPack/L4T와 wheel 호환성을 확인하고 실제 OSNet 추론으로
GPU 동작을 검증해야 한다. 설치 스크립트의 provider 목록 확인만으로 실제
추론 성공을 보장하지 않으며 TensorRT 캐시는 해당 로봇에서 생성한다.

`reid_backend=auto`는 기존처럼 모델이 없으면 HSV fallback을 경고하고 사용한다.
OSNet 사용을 반드시 확인하려면 `reid_backend:=osnet`으로 실행한다. 이 경우
모델 로딩이 실패하면 시작이 실패한다. 실제 backend는 시작 로그에 표시된다.

모델 출처: [공식 Torchreid OSNet](https://github.com/KaiyangZhou/deep-person-reid),
OSNet-AIN x1.0 MSMT17 체크포인트. 소스 커밋과 가중치 SHA-256은
`scripts/prepare_osnet_model.sh`에 고정되어 있다. 특징 추출 모델만 가져오며
이 패키지의 기존 갤러리 연결은 Malbut 구현이다.
