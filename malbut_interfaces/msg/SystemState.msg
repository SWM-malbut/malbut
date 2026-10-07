uint8 BOOTING=0
uint8 IDLE=1
uint8 EXECUTING_MISSION=2
uint8 RECHARGING=3
uint8 EMERGENCY=4

uint8 AUTONOMOUS=0
uint8 MANUAL=1

uint8 system_state
uint8 control_mode
malbut_interfaces/MissionStatus[] active_foreground_missions
malbut_interfaces/MissionStatus[] active_background_missions
malbut_interfaces/MissionStatus[] suspended_missions
malbut_interfaces/MissionStatus[] pending_missions
# Every newly admitted global movement stop advances this lifetime-scoped epoch.
string movement_runtime_id
uint64 movement_epoch
