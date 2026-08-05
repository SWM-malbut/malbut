# Malbut person perception

`malbut_perception` detects people from the robot's RGB image and estimates
their camera-relative 3D position from the aligned depth image. It does not
subscribe to Gazebo actor names, model poses, scripted routes, or any other
ground-truth source.

## Data flow

```text
/camera/color/image_raw ──> person detector ──> image-space track ID
/camera/depth/image_raw ──> robust ROI median ──> 3D position
/camera/color/camera_info ─> pinhole projection ─┘
```

Outputs:

- `/perception/person/detections_2d`: `vision_msgs/Detection2DArray`
- `/perception/person/detections_3d`: `vision_msgs/Detection3DArray`
- `/perception/person/debug_image`: bounding boxes, IDs, and measured distance
- `/perception/person/healthy`: whether the latest synchronized frame ran

The ID is an image-space short-term track ID. It is not a biometric identity.
This package does not choose whom to follow and never publishes `/cmd_vel`;
SWM25-83 can consume the standard 3D detections without changing perception.

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

Use `dnn_target:=cuda` or `cuda_fp16` only when the installed OpenCV build has
the CUDA DNN backend. Otherwise keep `dnn_target:=cpu`.

## Verify sensor-only operation

```bash
ros2 topic hz /camera/color/image_raw
ros2 topic hz /camera/depth/image_raw
ros2 topic echo /perception/person/detections_3d --once
rqt_image_view /perception/person/debug_image
```

The generic launch works with both Gazebo and the physical robot as long as
the three RGB-D topic arguments are mapped to aligned images and CameraInfo.
