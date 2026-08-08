# Bundled maps

`robocup_home.yaml` and `robocup_home.pgm` belong only to
`world_name:=robocup_home`. The PGM is byte-identical to Hiwonder
`simulations/robot_gazebo/maps/map_01.pgm`:

`0f6e74f0c9fd732807b3fd10207309369ac272d184bac17932c1be0b52e3593e`

The YAML keeps the official resolution, origin, and occupancy thresholds; its
generic `map_01.pgm` image name was changed to `robocup_home.pgm` so the map
cannot be mistaken for a `small_house` map. Generate a separate occupancy map
for Small House or a real deployment and pass its YAML to
`navigation.launch.py map:=...`.
