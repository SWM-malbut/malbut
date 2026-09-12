# Third-party notices and license scope

Unless a file or component is identified below, contributions owned by
Malbut Contributors are licensed under the Apache License 2.0 in `LICENSE`.
The Apache license is not a relicensing grant for third-party material.

## Hiwonder ROSOrin

- Project: Hiwonder ROSOrin / ROSOrin Pro
- Public project page: <https://github.com/Hiwonder/ROSOrin-Pro>
- Copyright owner: Hiwonder
- Repository identifier: `LicenseRef-Hiwonder-ROSOrin`

The public upstream page states that the project is available for educational
and research purposes, but it does not provide a standard license file or an
express grant allowing Malbut to relicense Hiwonder material under
Apache-2.0. Consequently, Hiwonder-originated material and adaptations remain
subject to Hiwonder's terms and are excluded from Malbut's Apache-2.0 grant.
This notice grants no additional rights to that material.

The affected content includes the ROSOrin robot description and associated
robot-specific configuration, visualization, map, and simulation integration
derived from the original ROSOrin import. Package-level details are recorded
in:

- `malbut_description/THIRD_PARTY_NOTICES.md`
- `malbut_gazebo/THIRD_PARTY_NOTICES.md`

## AWS RoboMaker Small House

- Source: <https://github.com/aws-robotics/aws-robomaker-small-house-world>
- Imported commit: `ff9631ca6d1db9c1ba656498151464b5ab74aafe`
- Copyright 2019 Amazon.com, Inc. or its affiliates.
- License: MIT-style terms preserved at
  `malbut_gazebo/models/aws_small_house/LICENSE`

See `malbut_gazebo/models/aws_small_house/SOURCE.md` for the selected content
and local adaptations.

## YOLO ROS

- Source: <https://github.com/mgonzs13/yolo_ros>
- Imported source: `eb11e81f7dbdb81c23a61d743ce0bac3cf9b07eb`
- Included at `malbut_yolo/vendor/yolo_ros` and in the real-robot copy under
  `malbut_test/malbut_yolo/vendor/yolo_ros`.
- License: GPL-3.0, preserved in each source directory's `LICENSE` file.

The upstream source and its copyright notices retain their original license;
they are not covered by Malbut's Apache-2.0 grant. The runtime dependency
Ultralytics has separate AGPL-3.0/commercial licensing terms.

## Apache-2.0 upstream material

Files retaining copyright notices from Intel Corporation or the Open Source
Robotics Foundation remain under their existing Apache License 2.0 terms.
Malbut modification notices are preserved where applicable.
