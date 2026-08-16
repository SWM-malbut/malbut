# Malbut Gazebo environments

This package provides four selectable Gazebo Fortress environments for the
Malbut ROS 2 Humble simulation.

## Monitor-room durable state core

`malbut_gazebo.gazebo_monitor_room_store` is a Gazebo-only SQLite state core
for monitor-room navigation, and `gazebo_monitor_room_nav2_adapter` is a
pure/injected controller for a future ROS Nav2 port. The store keeps
operation/sample state,
leases, fences, stable goal UUIDs, cancellation, delivery-unknown terminals,
and private millimetre coordinates for adapter use. Public observations expose
only navigation progress and explicit non-claims: no physical authorization,
camera coverage, viewer liveness, or room-coverage success is asserted.

These modules do not import ROS, start cameras, or connect to the Agent server.
The injected controller accepts only an operation ID and reads the private
sample plus map/semantic/zone/plan binding from the store. Preflight and goal
reports must echo their exact target. The controller durably records send
intent before calling `ensure_started`, rechecks the durable clock high-water,
owner, fence, lease, deadline, goal, and sample immediately before that call,
and never resends a send-intent or navigating goal after restart. Its in-memory
reservation only coalesces duplicate side effects within one controller
instance. A real port must atomically recheck its own current time against the
request lease/deadline, enforce the fence at the side-effect boundary, and
dedupe cross-controller start/cancel calls using the stable operation ID, goal
UUID, fence epoch, binding digest, and cancel request ID when present.
The database path must be absolute, have no symlinked
directory components, and resolve to a service-owned mode-0600 SQLite file with
the exact owned schema and row/event digests. Every read and write transaction
re-attests the pathname, inode, owner, mode, link count, SQLite main path, and
safety PRAGMAs before use and after commit. Unlink, replacement, permission
drift, connection/path mismatch, or uncertain post-commit durability poisons
the open handle and returns a typed failure instead of success.

This local check is not an external monotonic anchor. Whole-database deletion
or rollback while the process is down, and a malicious same-UID process inside
the protected parent, remain supervisor/deployment trust boundaries. The
focused storage + injected-adapter contract currently passes 86 tests; it does
not run Gazebo or send ROS actions:

```bash
PYTHONPATH=malbut_gazebo python3 -m pytest -q \
  malbut_gazebo/test/test_gazebo_monitor_room_store.py \
  malbut_gazebo/test/test_gazebo_monitor_room_nav2_adapter.py
```

| `world_name` | Purpose | Distribution status |
| --- | --- | --- |
| `empty` | Flat-floor robot physics baseline | Project-owned primitives |
| `test_arena` | Repeatable collision, LiDAR, camera, and ramp checks | Project-owned primitives |
| `robocup_home` | Simple indoor floor plan paired with the bundled baseline map | Hiwonder feature package; adapted for Fortress |
| `small_house` | Detailed multi-room household scenario | AWS assets; bundled license applies |

All four environments use the same launch argument:

```bash
ros2 launch malbut_gazebo worlds.launch.py world_name:=empty
ros2 launch malbut_gazebo worlds.launch.py world_name:=test_arena
ros2 launch malbut_gazebo worlds.launch.py world_name:=robocup_home
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
default 54 m circuit visits a deep point in each main room and behind the sofa,
then returns to its starting point without passing through walls or furniture:

```bash
ros2 launch malbut_gazebo humanoid_demo.launch.py
```

The actor starts about 1.5 m in front of the robot and completes one circuit in
about 141 seconds at 0.45 m/s, including turns. The route is validated against
the complete Small House 3D collision, visible mesh, and sphere geometry.
Its full path can be translated or rotated with launch arguments, but changed
offsets must be revalidated:

```bash
ros2 launch malbut_gazebo humanoid_demo.launch.py \
  actor_x:=-2.19 actor_y:=-1.17 actor_yaw:=0.0
```

No Gazebo ground-truth pose is bridged to ROS; later perception code must
locate the target from `/camera/color/image_raw` and
`/camera/depth/image_raw`. The humanoid is a kinematic camera target, not a
physics obstacle.

## SLAM mapping

For the product-style first run, use the managed entry point instead of asking
an end user to drive `map_saver_cli` and the User Map converter:

```bash
ros2 launch malbut_gazebo managed_home.launch.py
```

When real robot sensor and base drivers are already running, add
`simulation:=false use_sim_time:=false`; this selects the hardware-neutral
`map_onboarding.launch.py` stack and does not start Gazebo.

With no valid revision under `~/.local/share/malbut/maps`, it starts online
SLAM, Nav2 frontier exploration, and the setup UI at
`http://127.0.0.1:8765/`. Finishing in the UI atomically stores the occupancy
map, vector User Map, preview, and active manifest. The potentially large SLAM
pose graph is opt-in with `save_posegraph:=true`; static AMCL navigation does
not require it.
On later starts the same launch selects the saved map and static localization.
An interrupted or failed replacement never overwrites the previous active
revision.

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

The bundled maps are world-specific: `maps/robocup_home.yaml` matches only
`world_name:=robocup_home`, and `maps/small_house.yaml` matches only
`world_name:=small_house`. The Small House map comes from the same fixed AWS
ROS 2 source commit as the world and keeps its original 5 cm resolution and
coordinate origin.

Run the complete repeatable Small House navigation and autonomous-roaming
demonstration with one command:

```bash
ros2 launch malbut_gazebo roaming_demo.launch.py
```

The demo starts at the catalogued upstream test pose, initializes AMCL at that
same pose, and starts `malbut_roaming` only after Nav2 becomes active.

## Trusted RobotState observation boundary

`trusted_robot_state_observer` is a separate, read-only ROS node for the
Agent's local RobotState protocol. It observes only:

- lifecycle state for AMCL, BT Navigator, planner, controller, and the global
  costmap;
- readiness of ComputePathToPose, NavigateToPose, and the global-costmap
  service; and
- a finite, non-future, fresh `map` to `base_footprint` transform.

It never sends an action goal, calls the costmap service, publishes velocity,
or reads semantic zones. Observations are timestamped with host boot time and
published atomically through the `malbut_agent_server` RobotState collector.
Battery, emergency stop, camera, privacy, dock, and forbidden-zone fields stay
unknown until separate authoritative sources are implemented. Therefore this
node alone cannot satisfy `monitor_room` Safety.

All identity and freshness parameters are fixed for the node lifetime. The
default `physical_authority:=false` cannot satisfy the production Agent. When
`physical_authority:=true`, `use_sim_time:=true` is rejected both at startup
and as a runtime parameter change.

Example hardware-side invocation after creating a protected runtime directory:

```bash
ros2 run malbut_gazebo trusted_robot_state_observer --ros-args \
  -p device_id:=malbut-device-01 \
  -p map_id:=home-map \
  -p map_revision:=grid-7 \
  -p socket_path:=/run/malbut/robot-state.sock \
  -p expected_agent_uid:="$(id -u malbut-agent)" \
  -p physical_authority:=true \
  -p use_sim_time:=false
```

The collector deliberately refuses to delete an existing socket path. A
supervisor must own `/run/malbut`, start the observer before the Agent, and
remove a verified stale socket after an unclean exit. This node is not added to
`managed_home.launch.py` until authoritative battery and emergency-stop
publishers and the remaining camera/privacy evidence contract exist.

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
