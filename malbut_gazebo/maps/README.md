# Bundled maps

Each map is paired with one specific simulation world. Do not use a map with
a different world even when both look like indoor environments.

`robocup_home.yaml` and `robocup_home.pgm` belong only to
`world_name:=robocup_home`. The PGM is byte-identical to Hiwonder
`simulations/robot_gazebo/maps/map_01.pgm`:

`0f6e74f0c9fd732807b3fd10207309369ac272d184bac17932c1be0b52e3593e`

The YAML keeps the official resolution, origin, and occupancy thresholds; its
generic `map_01.pgm` image name was changed to `robocup_home.pgm`.

`small_house.yaml` and `small_house.pgm` belong only to
`world_name:=small_house`. They are byte-identical in map content and metadata
to the TurtleBot3 Waffle Pi map shipped with the same AWS Small House ROS 2
source commit recorded in `../models/aws_small_house/SOURCE.md`. The upstream
PGM SHA-256 is:

`4406c72e26c2ef743c8976406495bebc975327f3e322b57c8344f9076a2fe41c`

Only the YAML image name changed from the generic `map.pgm` to
`small_house.pgm`. For a real deployment, generate a map in the real house and
pass its YAML to `navigation.launch.py map:=...`.
