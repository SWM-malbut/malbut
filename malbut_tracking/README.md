# Malbut Target Tracking

`malbut_tracking` uses RGB-D detections to identify and follow one person.
When the same person appears as a dynamic obstacle in Nav2's global costmap,
the costmap track refines the camera estimate. Gazebo entity poses are never
read, and the package never publishes velocity commands directly.

## Runtime contract

- Input detections: `/perception/person/detections_3d`
- Global obstacle grid: `/global_costmap/costmap_raw`
- Saved static map: `/map`
- Follow action: `/follow_person` (`malbut_interfaces/action/FollowPerson`)
- State: `/tracking/person/status`
- Estimated map pose: `/tracking/person/estimated_target_pose`
- RViz costmap track labels: `/tracking/person/costmap_tracks`
- Motion: Nav2 `ComputePathToPose`, `FollowPath`, and `SpeedLimit`

The first visible person is acquired automatically. The tracker subtracts the
saved static map from lethal global-costmap cells, groups sparse LiDAR returns,
and maintains every dynamic obstacle with a planar constant-velocity Kalman
filter. Mahalanobis gating and globally optimal Hungarian assignment preserve
track identity between costmap updates. New tracks remain tentative until they
receive repeated measurements, and confirmed tracks coast for a bounded time
through short occlusion.

The tracking package only consumes the standard Nav2 raw costmap contract; it
does not depend on Gazebo or on a particular costmap plugin. The simulation
uses STVL to age moving RGB-D obstacles, while a physical robot may use its
existing Nav2 obstacle stack as long as it publishes the configured raw
costmap topic. Generic perception and tracking launches use wall time by
default. The Gazebo demonstration explicitly enables simulation time.

RGB-D is the primary long-range position source, so a visible person remains
followable even outside the LiDAR/costmap observation area. Camera-only motion
continuously derives standoff poses from current sensor observations. Each
accepted sensor update asks Nav2 for an ordinary global path. The follower
passes an unchanged bounded prefix of that path to Controller Server and only
sets its terminal camera orientation. A newer path directly preempts the
running `FollowPath` goal without an explicit cancel/stop gap.
A failed individual path is discarded so the next camera observation can try a
better goal while the outer follow action remains active. A confirmed costmap
match only refines a fresh near-range measurement; RGB-D remains the continuous
source between costmap updates. Inflation cells, saved walls and furniture,
localization noise beside static geometry, and wall-sized components are
excluded. A zero numeric action setting uses the value from
`config/person_following.yaml`.

## Run on a robot

Start the robot's camera driver, localization, and Nav2 first. Then run the
sensor and tracking packages with the robot's topic names if they differ from
the defaults:

```bash
ros2 launch malbut_perception person_detection.launch.py \
  rgb_topic:=/camera/color/image_raw \
  depth_topic:=/camera/depth/image_raw \
  camera_info_topic:=/camera/color/camera_info
ros2 launch malbut_tracking person_following.launch.py
```

Neither launch uses Gazebo time by default. The camera driver must publish
aligned depth and CameraInfo with a valid optical TF, while the robot's Nav2
stack must publish `/map`, TF, and `/global_costmap/costmap_raw`.

## Start automatic person following

```bash
ros2 action send_goal \
  /follow_person malbut_interfaces/action/FollowPerson \
  "{desired_distance_m: 1.2, minimum_distance_m: 0.65, maximum_linear_speed_mps: 0.30, target_lost_timeout: {sec: 8, nanosec: 0}}" \
  --feedback
```

Cancel the command with `Ctrl-C`, or use an action client to cancel its goal.
Before the first person is acquired, the action remains active and the robot
waits stationary. The target-loss timer starts only after an RGB-D target has
actually been acquired. A brief detection gap does not cancel the movement
already selected. If the loss continues, the robot follows an ordinary Nav2
path to the last short-horizon predicted target position. It replans halfway
from the changed camera viewpoint instead of starting a blind alternating
rotation. A spatially continuous
camera observation preserves the person even if its detector ID changed.
The robot advances when the person is beyond the configured distance band and
holds inside it. When the person approaches too closely, the same Nav2 planner
computes a safe short retreat path. The normal holonomic Nav2 controller
follows those planner-produced positions and owns both translation and body
heading; the follower does not inject a second camera-yaw command.
Navigation failures are retried with fresh sensor goals instead of invoking
Nav2's generic fixed-direction recovery sequence. The follower cancels Nav2
and ends in `TARGET_LOST` only when the visibility timeout expires.

## Algorithm basis

- Nav2 Humble `Costmap2DPublisher`: the raw master costmap is a full grid and
  contains costs, not object IDs or velocities.
- Bewley et al. SORT / Wojke et al. DeepSORT: constant-velocity Kalman tracks,
  tentative/confirmed lifecycle, Mahalanobis gating, and Hungarian assignment.
- Rexin et al., *Fusion of Object Tracking and Dynamic Occupancy Grid Map*:
  associate object tracks with a grid representation instead of treating grid
  cells themselves as persistent identities.
