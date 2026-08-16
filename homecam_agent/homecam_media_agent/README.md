# Homecam media evidence

`homecam_media_agent_node` publishes `malbut_interfaces/msg/HomecamMediaEvidence`
on `/homecam/media_evidence` with reliable, volatile, KeepLast(1) QoS. The
message is observation evidence only; it does not authorize a robot action.

`privacy_mode_state` describes the software media gate. It is not evidence of
a physical camera shutter. `/homecam/monitoring_enabled`, web desired state,
and the legacy `mediaHealthy` heartbeat field are not authority for either
camera availability or privacy.

`physical_authority` defaults to `false`. Enabling it requires the Aurora
profile, a fixed valid device ID, a nonempty HTTPS backend, a valid device
token, libcurl and GStreamer builds, host `CLOCK_BOOTTIME`, and `use_sim_time`
disabled. The backend heartbeat response must echo the exact device ID. A
camera-available `TRUE` additionally requires a current applied camera-on
generation, a strictly validated image, and `GST_FLOW_OK`. Missing, stale,
out-of-order, or conflicting observations publish unknown/fail closed.
The ROS topic is not authentication by itself: a physical deployment must
also isolate the DDS graph with SROS2 or an equivalent supervisor-owned trust
boundary before the observer may enable physical authority.

The evidence TTL is at most five seconds. The publish interval must be no more
than half the TTL. Repeated publication of unchanged evidence reuses the exact
sequence and body and therefore never extends freshness. A physical setup must
also set the heartbeat interval to no more than half the TTL.

Battery and emergency-stop authority are outside this package. Until all
required authoritative fields are supplied to the collector, the overall
robot-state contract remains incomplete and room monitoring stays closed.
