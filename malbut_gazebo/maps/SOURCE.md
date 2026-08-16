# Small House navigation map

`small_house.pgm` is the pre-built navigation map for the AWS RoboMaker
Small House layout used by this package.

- Source repository: <https://github.com/pantelis/turtlebot-maze>
- Imported commit: `d306a5c94a359b595c76800e7b8766fa9e2503b0`
- Upstream path: `tb_worlds/maps/house_world_map.pgm`
- SHA-256: `4406c72e26c2ef743c8976406495bebc975327f3e322b57c8344f9076a2fe41c`
- License: MIT

The 67 floor-plan and furniture models retained in `small_house.sdf` have the
same names, poses, and rotations as the upstream house world. The 20 omitted
upstream models are wall-mounted portrait assets and do not change the
driveable floor plan.

`map_01.*` is retained only as a legacy Hiwonder-originated map. It does not
match the AWS Small House world and must not be used for Small House
navigation or patrol routes.
