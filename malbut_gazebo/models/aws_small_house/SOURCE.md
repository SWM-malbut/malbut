# AWS Small House import record

- Upstream repository:
  <https://github.com/aws-robotics/aws-robomaker-small-house-world>
- Upstream commit: `ff9631ca6d1db9c1ba656498151464b5ab74aafe`
- Commit date: 2021-08-20
- Imported on: 2026-07-26
- Upstream world: `worlds/small_house.world`
- Upstream map: `maps/turtlebot3_waffle_pi/map.{yaml,pgm}`
- License: the adjacent `LICENSE` file is copied verbatim from the commit

## Selected content

The adapted `small_house.sdf` references 67 static instances from 43 unique
upstream model directories. Only those 43 directories were copied.

The import intentionally excludes:

- all 20 `Portrait` and `DeskPortrait` model variants;
- the upstream `photos/` directory;
- five model directories not used by the adapted world;
- four unused Collada source exports containing broken Windows-local texture
  paths;
- the unused `aws_Bed_01.png` texture, which is not referenced by the selected
  Bed model's Collada files.

No AWS launch file, ROS package manifest, route, or Gazebo Classic dependency
was imported. The occupancy map paired with this exact world was imported as
`../../maps/small_house.{yaml,pgm}`. Its geometry, resolution, origin, and
thresholds are unchanged; only the generic image filename was made explicit.

## Local adaptations

- Added Gazebo Fortress Physics, UserCommands, SceneBroadcaster, Sensors, and
  Imu systems.
- Renamed the SDF world from `default` to `small_house`.
- Removed all portrait model instances.
- Replaced 67 nested `<model><include>` wrappers with top-level `<include>`
  instances. This preserves each model name and pose while avoiding invalid
  nested visual-parent relationships in Gazebo Fortress.
- Removed empty legacy pose frame attributes and changed the physics engine
  selector to Fortress's engine-neutral `type="ignored"`.
- Corrected the ShoeRack inertia typo from a duplicate `ixx` element to
  `izz`; this is required for libsdformat validation.
- Replaced the Dumbbell's invalid zero axial inertia with a small positive
  value; the object remains static in this world.
- Added package-relative Fortress resource paths in `worlds.launch.py`.

## Integrity audit

The selected files were compared with the recorded commit on 2026-08-07.
All 67 active non-portrait instances retain the upstream name, URI, and pose.
All 43 selected model directories contain collision geometry, including the
Ball model used by both active Ball instances. Every selected model file is
byte-identical to upstream except for the two SDF validation corrections
documented above. The bundled Small House PGM is byte-identical to the map in
the same upstream commit.

The upstream package manifest says `Apache 2.0`, but the upstream `LICENSE`
file contains MIT-style permission terms. Both facts are recorded here; the
license text itself has not been rewritten.
