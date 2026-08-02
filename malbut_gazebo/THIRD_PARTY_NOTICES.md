# Third-party notices and license scope

Project-owned additions and modifications in this package are licensed under
the Apache License 2.0 in `LICENSE`. The exceptions below retain their own
terms.

## Hiwonder ROSOrin

- Project: Hiwonder ROSOrin / ROSOrin Pro
- Public project page: <https://github.com/Hiwonder/ROSOrin-Pro>
- Copyright owner: Hiwonder
- Repository identifier: `LicenseRef-Hiwonder-ROSOrin`

The public upstream page describes the project as available for educational
and research purposes but supplies no standard license file or express
Apache-2.0 relicensing permission. Hiwonder-originated robot configuration,
maps, visualization settings, URDF/Gazebo integration, teleoperation logic,
and their adaptations remain subject to Hiwonder's terms and are excluded
from the Apache-2.0 grant. This notice grants no additional rights to that
material.

## AWS RoboMaker Small House

- Source:
  <https://github.com/aws-robotics/aws-robomaker-small-house-world>
- Imported commit: `ff9631ca6d1db9c1ba656498151464b5ab74aafe`
- Copyright notice: `Copyright 2019 Amazon.com, Inc. or its affiliates.`
- License: preserved verbatim at `models/aws_small_house/LICENSE`
- Import details: `models/aws_small_house/SOURCE.md`

The upstream `LICENSE` file contains MIT-style permission terms, while its
`package.xml` labels the package `Apache 2.0`. This repository preserves the
actual upstream license text and records the discrepancy instead of silently
changing it.

## TurtleBot Maze Small House navigation map

- Source: <https://github.com/pantelis/turtlebot-maze>
- Imported commit: `d306a5c94a359b595c76800e7b8766fa9e2503b0`
- Imported file: `tb_worlds/maps/house_world_map.pgm`
- Local file and import details: `maps/small_house.pgm`, `maps/SOURCE.md`
- License: MIT

The imported occupancy grid is used with the matching AWS RoboMaker Small
House model layout. The legacy `map_01.*` files are not used by the Small
House navigation default.

## Intel Corporation and Open Source Robotics Foundation

The Nav2 launch adaptation and ament lint test templates retain their original
Apache-2.0 copyright and license notices. Malbut's modification notice is
preserved in the Nav2 launch file.
