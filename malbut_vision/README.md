# malbut_vision

ROS 2 and OpenCV practice tools for the Malbut simulated camera.

## LAB threshold viewer

LAB separates image brightness (`L`) from two color axes:

- `A`: green to red
- `B`: blue to yellow

Run the Gazebo camera first, then start the viewer:

```bash
ros2 run malbut_vision lab_threshold
```

The default image topic is `/depth_cam/depth_cam`. Override it with:

```bash
ros2 run malbut_vision lab_threshold --ros-args \
  -p image_topic:=/another/image_topic
```

Adjust the six sliders to make only the target color white in `LAB mask`.
Press `P` to print the current threshold values. Press `Q` or `Esc` to exit.

## Red color detector

The detector uses the red LAB range calibrated in the simulator:

```text
lower = [33, 151, 130]
upper = [255, 255, 255]
```

Run it with:

```bash
ros2 run malbut_vision color_detect
```

It follows the Hiwonder lesson flow: resize, Gaussian blur, LAB conversion,
threshold mask, erosion, dilation, largest-contour selection, and result
overlay. The annotated image is also published on
`/malbut_vision/red_detection/image`.
