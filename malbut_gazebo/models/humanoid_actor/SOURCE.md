# Humanoid actor source

- Source: Gazebo Fuel, `OpenRobotics / actor - relative paths`, version 2
- URL: <https://fuel.gazebosim.org/1.0/OpenRobotics/models/actor%20-%20relative%20paths>
- Retrieved: 2026-08-04
- Included file: `meshes/walk.dae`
- SHA-256: `49af0df3a319d1cb8ca2cebf02dbd00f625e5d5bec820bc5e109925b18b65c6e`

Only the walking COLLADA skin and animation needed by the Malbut perception
test actor is included. The project-owned `model.sdf` and `robocup_home.sdf`
reuse that one asset with deterministic, map-specific indoor paths for
repeatable RGB-D perception and tracking measurements. The upstream
Fuel metadata does not declare a per-model license; this asset therefore
remains identified as `LicenseRef-Gazebo-Fuel-Actor` and is not relicensed by
the project's Apache-2.0 license.
