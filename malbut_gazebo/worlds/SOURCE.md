# Hiwonder RoboCup Home source

- Documentation: <https://docs.hiwonder.com/projects/ROSOrin/en/jetson-orin-nano-version/docs/9_Gazebo_Simulation.html>
- Download: Hiwonder Google Drive `3. Feature Package/simulations.zip`
- Downloaded: 2026-08-07
- `simulations.zip` SHA-256: `a4d23a901dd24472cfdb4655304fd4409bdb5982a7ebdea13f70ec42aef8dad2`
- Original file: `simulations/robot_gazebo/worlds/robocup_home.sdf`
- Original file SHA-256: `e1ffd92506c00e0ac69d5b1a23a9f73fe6af448d3d8df1af8a8721f4e90bc7e0`

`robocup_home.sdf` preserves the original wall dimensions and poses. Malbut
changes only the SDF version, world name, physics timestep, Fortress sensor
systems, GUI start state, and Teleop topic required by the canonical
`/cmd_vel` interface. The detailed `small_house.sdf` remains an independent
AWS-based project environment rather than being presented as a Hiwonder file.
