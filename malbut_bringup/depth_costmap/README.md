# Bringup depth costmap plugin

`malbut_bringup::DepthVoxelLayer` is an internal Nav2 costmap plugin built
and installed by Bringup, not a separate ROS package or point-cloud node.
It projects a complete depth image once with the official
`depth_image_proc::convertDepth`, then passes that in-process cloud to two
standard Nav2 ObservationBuffers. The inherited VoxelLayer retains voxel
marking, ray clearing, rolling-window, and footprint behavior. No PointCloud2
publisher/subscriber is created by this adapter. Nav2's optional inherited
voxel/clearing-endpoint debug publishers are unchanged.

## Input contract

- `depth_topic`: `/depth_cam/depth0/image_raw`; `camera_info_topic`:
  `/depth_cam/depth0/camera_info`. Both subscriptions use sensor-data QoS with
  depth 1 and the costmap's callback group. `observation_sources` must be empty.
- `depth_is_rectified` defaults to `false`. Raw distorted depth is rectified
  using official `image_geometry::PinholeCameraModel::rectifyImage` with nearest
  interpolation, never by treating raw pixels as already rectified. Set this
  flag only when the supplied topic is genuinely rectified.
- Full-resolution `16UC1`/`mono16` millimeter or `32FC1` meter axial depth only. Every
  output pixel has an XYZ slot; no spatial decimation or rate cap is applied.
  Rectification resamples on the same-sized pixel grid. Invalid/nonpositive
  depths become NaNs, not synthetic maximum-range clearing rays. Native row
  padding is supported; endian/unaligned rows are normalized when necessary.
- Image and CameraInfo must have identical nonempty optical frame and image
  dimensions. The latest CameraInfo is cached and snapshotted for each queued
  image: Aurora stamps its fixed IR calibration with the last SDK frame in a
  batch, which need not have the depth measurement's timestamp. Calibration
  timestamps are therefore not used as image synchronization or freshness.
  K/P must be calibrated, finite and compatible with
  full-resolution monocular axial depth. Binning/cropping, nonidentity R, and
  translated/skewed P are rejected; RGB-camera calibration is not substituted.
  Image frame and measurement timestamp are preserved in the generated cloud.

The Aurora SDK's `depth_correction`/alignment flags do not establish whether
the published depth raster is already rectified. Nonzero CameraInfo D and an
`image_raw` topic name alone are not proof. Verify the actual depth geometry
against its IR calibration before selecting `depth_is_rectified` on the robot;
the explicit raw default must not be treated as a completed hardware validation.

## Safety and scheduling

TF is checked at the image's exact timestamp with zero blocking timeout before
buffering; ObservationBuffer also uses zero TF timeout. One TF-waiting image is
retained while only its newest successor is replaced, preventing continuous
input from starving a slightly delayed transform. A 10 ms timer retries only
pending TF work, bounded by the existing costmap `transform_tolerance`.
Transport overload may coalesce frames; image resolution is never reduced.

`expected_update_rate` uses Nav2's seconds-between-observations convention and
defaults to 0.5 s (must be positive). The layer is not current until the first
valid depth observation, and becomes non-current on stale/missing images,
calibration, or TF. Activation, deactivation, and reset clear pending frames and
recreate observation buffers; pre-activation/reset image stamps are rejected.

The role-specific source parameters are:

| Parameter | Default |
| --- | ---: |
| `marking.min_obstacle_height` | 0.05 m |
| `marking.max_obstacle_height` | 0.20 m |
| `marking.obstacle_min_range` | 0.0 m |
| `marking.obstacle_max_range` | 2.5 m |
| `clearing.min_obstacle_height` | -0.05 m |
| `clearing.max_obstacle_height` | 0.48 m |
| `clearing.raytrace_min_range` | 0.0 m |
| `clearing.raytrace_max_range` | 3.0 m |

Normal VoxelLayer parameters (`origin_z`, `z_resolution`, `z_voxels`, thresholds,
`max_obstacle_height`, etc.) still apply. Source parameters are read during
configuration, not changed dynamically while buffers are active.

## Scope and upstream implementation

This removes the large cloud DDS receive path, not projection, TF, point
iteration, or Nav2's internal buffer copies. Multiple costmaps still process
their own images. Same-process depth input is not a performance guarantee;
robot TF/controller latency and marking/clearing require runtime validation.

The plugin depends on official ROS 2 Humble `depth_image_proc` and
`image_geometry`; no vendor/Nav2 source is copied or patched. Projection and
rectification are provided by their BSD-licensed implementations:
[convertDepth](https://github.com/ros-perception/image_pipeline/blob/humble/depth_image_proc/include/depth_image_proc/conversions.hpp),
[PinholeCameraModel](https://github.com/ros-perception/vision_opencv/blob/humble/image_geometry/src/pinhole_camera_model.cpp).
