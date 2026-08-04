# Malbut Gazebo environments

This package provides three selectable Gazebo Fortress environments for the
Malbut ROS 2 Humble simulation.

| `world_name` | Purpose | Distribution status |
| --- | --- | --- |
| `empty` | Flat-floor robot physics baseline | Project-owned primitives |
| `test_arena` | Repeatable collision, LiDAR, camera, and ramp checks | Project-owned primitives |
| `small_house` | Detailed multi-room household scenario | AWS assets; bundled license applies |

All three environments use the same launch argument:

```bash
ros2 launch malbut_gazebo worlds.launch.py world_name:=empty
ros2 launch malbut_gazebo worlds.launch.py world_name:=test_arena
ros2 launch malbut_gazebo worlds.launch.py world_name:=small_house
```

For an environment-only server smoke test:

```bash
ros2 launch malbut_gazebo worlds.launch.py \
  world_name:=small_house gui:=false headless:=true \
  spawn_robot:=false bridge:=false iterations:=200
```

`gui:=false headless:=true` starts the Fortress server with EGL headless
rendering so camera and LiDAR rendering remains available without an X11
window.

Run `ros2 launch malbut_gazebo worlds.launch.py --show-args` for every
available option. Robot spawn poses come from `config/worlds.yaml`; command
line values such as `x:=1.0 y:=2.0 yaw:=1.57` override them. The Small House
default is the upstream test location
`x=-3.665503, y=-0.4874, z=0.002, yaw=0`.

## Humanoid perception target

Start the Small House with the robot and a looping animated pedestrian. The
default 35 m circuit visits the mapped rooms and corridors, then returns to its
starting point without passing through walls or furniture:

```bash
ros2 launch malbut_gazebo humanoid_demo.launch.py
```

The actor starts about 1.5 m in front of the robot and completes one circuit in
about two minutes at 0.35 m/s, including turns. The route is specifically
validated against the bundled Small House map. Its full path can be translated
or rotated with launch arguments, but changed offsets must be revalidated:

```bash
ros2 launch malbut_gazebo humanoid_demo.launch.py \
  actor_x:=-2.19 actor_y:=-1.17 actor_yaw:=0.0
```

No Gazebo ground-truth pose is bridged to ROS; later perception code must
locate the target from `/camera/color/image_raw` and
`/camera/depth/image_raw`. The humanoid is a kinematic camera target, not a
physics obstacle.

## SLAM mapping

Start the household world, robot, ROS-Gazebo bridge, SLAM Toolbox, and the
project RViz mapping view with one command:

```bash
ros2 launch malbut_gazebo slam.launch.py
```

Use the controlled test arena instead:

```bash
ros2 launch malbut_gazebo slam.launch.py world_name:=test_arena
```

For a headless mapping run without either GUI:

```bash
ros2 launch malbut_gazebo slam.launch.py \
  gui:=false headless:=true rviz:=false
```

Drive the robot from Gazebo's Teleop panel to extend the map. RViz shows the
occupancy map on `/map` and the current LiDAR measurements on `/scan`.
`slam_params_file:=...` and `rviz_config:=...` can override the checked-in
defaults when later tuning is required.

## AWS Small House

The detailed home is adapted from
[`aws-robotics/aws-robomaker-small-house-world`](https://github.com/aws-robotics/aws-robomaker-small-house-world)
at commit `ff9631ca6d1db9c1ba656498151464b5ab74aafe`.
Only the 43 model types referenced by the adapted world are bundled. All 20
portrait / desk-portrait model types and the standalone `photos/` directory
were excluded so the package contains no upstream people photographs.

The exact upstream license and import record are installed under
`models/aws_small_house/`. The source package declared Gazebo Classic
dependencies, but none of its launch code or dependencies are included here;
the adapted world uses Gazebo Fortress systems and `ros_gz`.

## Validation

After building and sourcing the workspace:

```bash
pkg_share="$(ros2 pkg prefix malbut_gazebo)/share/malbut_gazebo"
SDF_PATH="$pkg_share/models/aws_small_house" ign sdf -k \
  "$pkg_share/worlds/small_house.sdf"
IGN_GAZEBO_RESOURCE_PATH="$pkg_share/models/aws_small_house" \
  ign gazebo -s -r --iterations 200 "$pkg_share/worlds/small_house.sdf"
```
