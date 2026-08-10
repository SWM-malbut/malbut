# Hiwonder ROSOrin mesh source

- Documentation: <https://docs.hiwonder.com/projects/ROSOrin/en/jetson-orin-nano-version/docs/9_Gazebo_Simulation.html>
- Download: Hiwonder Google Drive `3. Feature Package/simulations.zip`
- Downloaded: 2026-08-07
- `simulations.zip` SHA-256: `a4d23a901dd24472cfdb4655304fd4409bdb5982a7ebdea13f70ec42aef8dad2`
- Source directory: `simulations/rosorin_description/meshes/`
- Per-file checksums: `SHA256SUMS`

The STL files in this directory are preserved byte-for-byte from the feature
package. Malbut changes only the Xacro integration around them. In particular,
the visible chassis, mecanum wheels, camera, LiDAR, and microphone use these
files directly. The four supplied wheel collision meshes remain available as
reference geometry, while Gazebo Fortress uses a documented spherical contact
proxy for stable directional-friction mecanum dynamics.
