# Humanoid actor source

- Source: Gazebo Fuel, `OpenRobotics / actor - relative paths`, version 2
- URL: <https://fuel.gazebosim.org/1.0/OpenRobotics/models/actor%20-%20relative%20paths>
- Retrieved: 2026-08-04
- Included files:
  - `meshes/walk.dae`
    - SHA-256: `49af0df3a319d1cb8ca2cebf02dbd00f625e5d5bec820bc5e109925b18b65c6e`
  - `meshes/stand.dae`
    - SHA-256: `ba1e23e637389ab10dfb029ebf9e5bef9771562a15cf0ff444b88e27043989be`

Only the walking skin and the walking / standing COLLADA animations needed by
the Malbut perception test actor are included. The standing animation keeps
in-place turns from replaying the walking cycle. The project-owned `model.sdf`
adds a deterministic
indoor path for repeatable RGB-D perception tests. The upstream
Fuel metadata does not declare a per-model license; this asset therefore
remains identified as `LicenseRef-Gazebo-Fuel-Actor` and is not relicensed by
the project's Apache-2.0 license.
