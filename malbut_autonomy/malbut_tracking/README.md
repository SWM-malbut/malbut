# Malbut Target Tracking

`malbut_tracking` uses RGB-D detections to identify and follow one person.
A map-subtracted 2-D LiDAR tracker briefly supports the same person through
camera occlusion. Gazebo entity poses are never read, and the package never
publishes velocity commands directly.

## Runtime contract

- Input detections: `/perception/person/detections_3d`
- Raw 2-D LiDAR: `/scan`
- Saved static map: `/map`
- Global navigation grid: `/global_costmap/costmap_raw` (goal safety only)
- Follow action: `/follow_person` (`malbut_interfaces/action/FollowPerson`)
- State: `/tracking/person/status`
- Estimated map pose: `/tracking/person/estimated_target_pose`
- RViz LiDAR track labels: `/tracking/person/lidar_tracks`
- Motion: Nav2 `ComputePathToPose`, `FollowPath`, `Spin`, and `SpeedLimit`

The first visible person is acquired automatically. On receipt of `/map`, the
node builds one static-obstacle distance field in memory. Each `/scan` return
is queued until its measurement-time `odom` transform exists; the latest
`map -> odom` localization correction is composed through TF's fixed-frame
lookup. This preserves exact fast ego motion without requiring AMCL to publish
at the LiDAR rate. A physical scan with a nonzero `time_increment` is deskewed
by interpolating the scan-start and scan-end poses. Each endpoint is then
rejected by one cached lookup when it
belongs to saved geometry and clustered with adjacent angular returns. A
compact cluster requires multiple sufficiently dense rays, so one jittering
wall return cannot become an object. This avoids searching Nav2's merged
master costmap, where static walls, inflation, keepout zones, and current
observations share the same cell.

Map subtraction produces foreground *candidates*, not dynamic-object labels.
Only clusters inside a bounded gate around the camera-confirmed person or its
short prediction enter the planar constant-velocity tracker. Mahalanobis
gating and globally optimal Hungarian assignment preserve that target between
scan updates. New tracks remain tentative until repeated measurements confirm
them, and confirmed tracks coast for a bounded time through short occlusion.
The follower therefore does not misrepresent every scene residual as a moving
object, and LiDAR can never select a person before RGB-D establishes identity.
The global costmap remains an input only for selecting safe Nav2 goals and
paths.

RGB-D is the primary long-range position source, so a visible person remains
followable even outside the LiDAR/costmap observation area. Camera-only motion
continuously derives targets from current sensor observations. Each accepted
sensor update asks Nav2 for a global path toward the observed person, using the
nearest admissible costmap cell when the person's cell is occupied. The
follower passes the existing bounded prefix of that path to Controller Server;
the measured distance band still decides when to advance or hold. Nav2 owns
both translation and body rotation; there is no downstream camera-yaw mixer.
A newer path directly preempts the
running `FollowPath` goal without an explicit cancel/stop gap.
A failed individual path is discarded so the next camera observation can try a
better goal while the outer follow action remains active. A confirmed LiDAR
match supports only a short camera gap; RGB-D remains authoritative whenever
it is visible. Saved walls and furniture, localization noise beside static
geometry, and wall-sized components are excluded before association. A zero
numeric action setting uses the value from
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
stack must publish `/scan`, `/map`, TF, and
`/global_costmap/costmap_raw`.

## Start automatic person following

```bash
ros2 action send_goal \
  /follow_person malbut_interfaces/action/FollowPerson \
  "{desired_distance_m: 1.0, minimum_distance_m: 0.65, maximum_linear_speed_mps: 0.30, target_lost_timeout: {sec: 8, nanosec: 0}}" \
  --feedback
```

Cancel the command with `Ctrl-C`, or use an action client to cancel its goal.
Before the first person is acquired, the action remains active and the robot
waits stationary. Loss recovery starts only after an RGB-D target has actually
been acquired. A confirmed LiDAR obstacle that was labeled by RGB-D continues
the same target for a bounded three-second camera gap; LiDAR never selects a
new person by itself. While camera observations are current, LiDAR only
provides fast near-range ALIGN/RETREAT decisions from distance and radial
velocity. RGB-D continues to own identity, visible map position, and forward
tracking. If both sensors lose the target, the
follower finishes
the frozen waypoint (or accepts it within the recovery-only 0.08 m tolerance),
turns toward the motion-predicted exit direction, and
then requests one complete Nav2 path to the last safe standoff position. It
finally performs one collision-checked 270-degree `Spin` in the direction of
the person's last camera bearing. A spatially continuous camera observation
preserves the person even if its detector ID changed.
The robot advances when the person is beyond the configured distance band and
holds inside it. When the person approaches too closely, the same Nav2 planner
computes a safe short retreat path. The normal holonomic `FollowPath`
controller follows all planner-produced positions and orientations, including
forward, lateral, reverse, and turning motion, without an independent command
overriding its angular velocity.
Navigation failures are retried with fresh sensor goals instead of invoking
Nav2's generic fixed-direction recovery sequence. Every recovery step remains
preemptible: a new RGB-D observation immediately resumes normal tracking. The
follower cancels Nav2 and waits stationary in `TARGET_LOST` after the
last-position recovery and directional scan complete without reacquisition.
The follow Action remains active until explicitly canceled, and a later RGB-D
observation immediately resumes `TRACKING` without a new Action goal.

## Algorithm basis

- Nav2 Humble `ObstacleLayer`: range observations are transformed into the
  costmap frame internally; the merged master grid contains costs, not the
  original measurements, identities, or velocities.
- Static distance transform: the invariant map is preprocessed once so each
  LiDAR endpoint needs only a cached lookup.
- Bewley et al. SORT / Wojke et al. DeepSORT: constant-velocity Kalman tracks,
  tentative/confirmed lifecycle, Mahalanobis gating, and Hungarian assignment.
- Rexin et al., *Fusion of Object Tracking and Dynamic Occupancy Grid Map*:
  associate object tracks with a grid representation instead of treating grid
  cells themselves as persistent identities.
