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
`~/.cache/malbut_perception/yolov5n.onnx`. When the file is absent,
`detector_backend:=auto` uses OpenCV's built-in HOG pedestrian detector. HOG
is useful for wiring checks, but the prepared YOLO model is required for the
animated humanoid and recommended for the real robot.

## YOLO model

The repository does not commit a model binary. Prepare the tested FP32
YOLOv5n ONNX file in an isolated cache environment:

```bash
cd ~/ros2_ws/src/malbut
./malbut_perception/scripts/prepare_yolov5_model.sh
./malbut_perception/scripts/prepare_osnet_model.sh
```

The one-time export installs its Python dependencies only under
`~/.cache/malbut_perception/yolov5-export-env`. It pins the official YOLOv5
v7.0 source, exports opset 12 FP32, simplifies constant operations, and checks
a forward pass with the system OpenCV before installing the model. The same
model file may also be passed to `homecam_detector`.

The standard launch selects the cached model automatically. To require YOLO
and reject a missing or incompatible model explicitly:

```bash
ros2 launch malbut_perception person_detection.launch.py \
  detector_backend:=yolo \
  model_path:=$HOME/.cache/malbut_perception/yolov5n.onnx
```

The OSNet preparation script pins the official Torchreid source and the
official OSNet x0.25 checkpoint trained on MSMT17, exports a 512-dimensional
opset 12 descriptor, and validates it with the system OpenCV. The model is only
0.08 GFLOPs at its 256x128 input size and runs only for detected people.

`dnn_target:=auto` is the default. It uses OpenCV CUDA DNN when CUDA is actually
available and otherwise uses CPU. OpenCL is explicit-only because device
discovery does not guarantee that a particular model compiles successfully.
Explicit `cuda`, `cuda_fp16`, `opencl`, and `opencl_fp16` modes fail at startup
instead of silently falling back.

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
- OSNet, ICCV 2019: lightweight omni-scale person Re-ID descriptors
- OpenCV DNN: CPU and CUDA execution backends for the ONNX models
