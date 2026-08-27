# Malbut LiDAR Preprocessor

This ROS 2 C++ package converts `/scan` into compact map-frame foreground
clusters for person tracking.

- Input: `/scan`, `/map`, TF
- Output: `/perception/lidar/foreground_clusters`
- Projection: ROS 2 Humble `laser_geometry`
- Static subtraction: one cached OpenCV distance transform per map
- Clustering: adjacent scan returns with size, density, and extent limits

It does not classify people and does not read Nav2's merged costmap. Semantic
association remains in `malbut_tracking`, where RGB-D is authoritative.
