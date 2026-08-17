uint8 UNKNOWN=0
uint8 FALSE=1
uint8 TRUE=2

uint32 schema_version
string device_id
string source_instance_id
uint64 sequence
uint64 control_plane_generation
uint64 observed_boottime_ns
uint64 valid_until_boottime_ns
uint8 camera_available_state
# Software media gate only; this is not a physical shutter observation.
uint8 privacy_mode_state
uint64 last_valid_frame_boottime_ns
uint64 frame_generation
string active_session_id
uint64 active_session_generation
bool backend_device_bound
bool physical_authority
