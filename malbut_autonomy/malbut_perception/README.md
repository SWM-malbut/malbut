# Malbut person perception

`malbut_perception` detects people from the robot's RGB image and estimates
their camera-relative 3D position from the aligned depth image. It does not
subscribe to Gazebo actor names, model poses, scripted routes, or any other
ground-truth source.

## Data flow

```text
/camera/color/image_raw ──> person detector ──> ByteTrack association
                         └─> OSNet appearance ─> re-entry ID restoration
/camera/depth/image_raw ──> robust ROI median ──> 3D position
/camera/color/camera_info ─> pinhole projection ─┘
```

Outputs:

- `/perception/person/detections_2d`: `vision_msgs/Detection2DArray`
- `/perception/person/detections_3d`: `vision_msgs/Detection3DArray`
- `/perception/person/debug_image`: bounding boxes, IDs, and measured distance
- `/perception/person/healthy`: whether the latest synchronized frame ran

The ID is a session-local visual track ID. ByteTrack-style IoU and low-score
association preserve it in view, while an OSNet appearance gallery can restore
it after the person leaves and re-enters the image. It does not perform face
recognition or assign a real-world identity. This package does not choose whom
to follow and never publishes `/cmd_vel`; SWM25-83 can consume the standard 3D
detections without changing perception.

## Run

Build and start the sensor-only pipeline:

```bash
cd ~/ros2_ws
colcon build --symlink-install --packages-select malbut_perception
source install/local_setup.bash
ros2 launch malbut_perception person_detection.launch.py
```

The launch looks for
`~/.cache/malbut_perception/yolo26n.onnx`. When the file is absent,
`detector_backend:=auto` uses OpenCV's built-in HOG pedestrian detector. HOG
is useful for wiring checks, but the prepared YOLO model is required for the
animated humanoid and recommended for the real robot.

## Models and inference runtime

The repository does not commit model binaries. Prepare the tested FP32
YOLO26n and OSNet x0.5 ONNX files in isolated cache environments, then install
the inference runtime used by the ROS process:

```bash
cd ~/ros2_ws/src/malbut
./malbut_autonomy/malbut_perception/scripts/prepare_yolo26_model.sh
./malbut_autonomy/malbut_perception/scripts/prepare_osnet_model.sh
./malbut_autonomy/malbut_perception/scripts/prepare_inference_runtime.sh
```

The YOLO export dependencies stay under
`~/.cache/malbut_perception/yolo26-export-env`. The script pins Ultralytics
8.4.55 and the official YOLO26n weights, exports the end-to-end one-to-one
opset 12 head, and validates its `(1, 300, 6)` output with ONNX Runtime. This
head returns final detections without external NMS.

The standard launch selects the cached model automatically. To require YOLO
and reject a missing or incompatible model explicitly:

```bash
ros2 launch malbut_perception person_detection.launch.py \
  detector_backend:=yolo \
  inference_backend:=onnxruntime \
  model_path:=$HOME/.cache/malbut_perception/yolo26n.onnx
```

The OSNet preparation script pins the official Torchreid source and the
official OSNet x0.5 checkpoint trained on MSMT17, exports a 512-dimensional
opset 12 descriptor, and validates it with the system OpenCV. It is 0.27
GFLOPs at 256x128 and runs only for detected people. Compared with x0.25, the
official MSMT17 same-domain result improves from 61.4/29.5 to 69.7/37.5
Rank-1/mAP while staying small relative to YOLO26n's 5.4 GFLOPs.

`inference_backend:=auto dnn_target:=auto` is the default. When ONNX Runtime is
installed, an Orin NX selects TensorRT FP16 first, CUDA second, and CPU only as
a final fallback. TensorRT engines are cached locally after the first load and
are never committed or copied between devices. An x86_64 NVIDIA development
PC also selects TensorRT FP16 when the preparation script has installed its
pinned TensorRT runtime, then falls back to CUDA. The legacy OpenCV backend
remains available for older compatible models, but Ubuntu 22.04's OpenCV
4.5.4 cannot execute YOLO26.

The physical target is the ROSOrin Jetson Orin NX Super on Ubuntu 22.04 and
JetPack 6/L4T R36. `prepare_inference_runtime.sh` installs the official ARM64
ONNX Runtime GPU wheel documented for JetPack 6 and verifies that TensorRT or
CUDA is exposed. Check the robot before deployment:

```bash
cat /etc/nv_tegra_release
python3 -c 'import onnxruntime as o; print(o.get_available_providers())'
```

The result must contain `TensorrtExecutionProvider` or
`CUDAExecutionProvider`; otherwise the robot is not using its GPU. Do not build
or distribute a TensorRT cache from the desktop because engines depend on the
target GPU and TensorRT/CUDA versions.

The main tracking and Re-ID controls are:

- `reid_cosine_threshold`: maximum OSNet cosine distance for restoring an ID
- `reid_max_inactive_frames`: how long retired IDs stay in the gallery
- `reid_feature_budget`: maximum descriptors retained per ID
- `tracker_appearance_weight`: appearance contribution to active association

Lower cosine thresholds are stricter. Similar clothing can still be ambiguous,
so a restored ID is a visual association rather than proof of identity.

## Verify sensor-only operation

```bash
ros2 topic hz /camera/color/image_raw
ros2 topic hz /camera/depth/image_raw
ros2 topic echo /perception/person/detections_3d --once
ros2 launch malbut_gazebo humanoid_demo.launch.py \
  perception:=true debug_image_transport:=raw
ros2 run rqt_image_view rqt_image_view \
  /perception/person/debug_image
```

The standard configuration publishes JPEG-compressed debug frames to avoid
dropping large raw images in the ROS middleware. `rqt_image_view` needs the raw
launch option shown above; use it only while visually inspecting the detector.

The generic launch works with both Gazebo and the physical robot as long as
the three RGB-D topic arguments are mapped to aligned images and CameraInfo.
Depth pixels are projected in REP-103 optical coordinates. Leave
`projection_frame` empty when the camera message already names its optical
frame. The Gazebo humanoid demo sets it to `camera_depth_optical_frame`
because Fortress shares the `camera_link` label with its body-frame point
cloud; the Nav2 PointCloud frame itself is unchanged.

## Design references

- ByteTrack, ECCV 2022: low-score detection association
- Deep SORT, ICIP 2017: motion and deep appearance association
- [Torchreid OSNet model zoo](https://github.com/KaiyangZhou/deep-person-reid/blob/master/docs/MODEL_ZOO.md): lightweight Re-ID descriptors
- [Ultralytics Jetson guide](https://docs.ultralytics.com/guides/nvidia-jetson): YOLO26 and JetPack deployment
- [ONNX Runtime TensorRT provider](https://onnxruntime.ai/docs/execution-providers/TensorRT-ExecutionProvider.html): Orin NX GPU execution
