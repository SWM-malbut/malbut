# Malbut autonomous roaming

`malbut_roaming` is a reusable high-level mode above ROS 2 Humble Nav2. It
selects destinations but never publishes velocity commands. Every destination
is first checked by Nav2 `ComputePathToPose` and then executed by
`NavigateToPose`, so the same package can run against simulation or the real
robot's Nav2 stack.

## Behavior

- Samples collision-clear destinations from the current `/map` occupancy grid.
- Usually selects broad free space and occasionally visits safe peripheral
  space.
- Scores candidates using time since last visit (idleness), useful travel
  distance, clearance, and separation from recent goals.
- Randomly chooses among the best candidates to avoid a mechanical fixed loop.
- Temporarily suppresses goals that Nav2 cannot plan or reach.
- Optionally follows a moving target localized by perception while maintaining
  a configurable standoff distance.
- Lets an external decision maker preempt roaming with a map-frame coordinate.

The policy is stochastic, but `random_seed` makes a demonstration repeatable.
All distances, weights, timing, target-interest behavior, and topic/action names
are parameters in `config/roaming.yaml`.

## Run with an existing map and Nav2

Start the map/localization/Nav2 stack first, then:

```bash
ros2 launch malbut_roaming roaming.launch.py autostart:=true
```

Control the mode:

```bash
ros2 service call /roaming/start std_srvs/srv/Trigger '{}'
ros2 service call /roaming/pause std_srvs/srv/Trigger '{}'
ros2 service call /roaming/resume std_srvs/srv/Trigger '{}'
ros2 service call /roaming/stop std_srvs/srv/Trigger '{}'
ros2 topic echo /roaming/status
```

Send a coordinate from an LLM/action layer. This uses only the standard
map-frame pose interface and resumes autonomous roaming afterward by default:

```bash
ros2 topic pub --once /roaming/goal geometry_msgs/msg/PoseStamped \
  "{header: {frame_id: map}, pose: {position: {x: 1.0, y: 2.0}, \
  orientation: {w: 1.0}}}"
```

## Moving-target input

Publish a sensor-derived, map-frame `PoseStamped` to
`/roaming/interest_target`. The node estimates motion from successive
observations, chooses a safe standoff goal, and periodically replans through
Nav2. It does not subscribe to Gazebo entity poses, model coordinates, or any
other ground-truth channel. A perception/localization node must produce this
input from deployed sensors in the same form on the real robot.
