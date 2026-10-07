# Resident voice and existing robot operations

This integration extends the existing single-tool speech turn. The Agent proposes
one operation and a fixed server-side workflow prepares it. Robot movement stays
under the Manager; the resident bridge owns runtime, map and device API operations.

## Launch and deployment

Build and restart `malbut_interfaces`, Manager, Bringup, Agent, and the media agent
together. Apply web migration `0024_voice_agent.sql` before enabling voice delegation.
Old devices default to delegation disabled. Existing conversation, personal-memory,
story-memory and cloud-analysis consent settings are unchanged.

`ros2 launch malbut_bringup cloud.launch.py` now keeps the cloud bridge and a separate
speech LaunchService resident. It uses the same prepared speech cache and environment
variables as `bringup.launch.py`; explicit overrides use the speech launch names
`python_executable`, `stt_model_path`, `stt_library_path`, `agent_user_id`,
`agent_conversation_db`, `navigation_targets`, `input_device`, and `output_device`.
`resident_voice:=false` retains cloud-only operation. The normal direct
`bringup.launch.py` entry point retains its default speech behavior.
The current `robot.launch.py` starts only the robot core, as on main.

The resident profile launches robot children with `speech:=false`. A process-owned
voice lock rejects duplicate resident LaunchServices. Each launch has a unique ROS
namespace; only that namespace's single voice node instances are exempt from child
startup conflict checks. XFM selection happens once in the resident profile and its
Pulse source is inherited by both STT and restarted media children. An unavailable
microphone or a failed speech child does not terminate cloud control.

Robot execution readiness and voice readiness are independent. Standby clears stale
Manager/localization/sensor state and keeps voice available. The owner web view shows
`대기 중 · 음성으로 다시 시작할 수 있습니다.` when the observed voice publisher remains ready.
The Homecam microphone setting controls streaming audio, not STT wake listening.
The resident Agent calls the existing weather and weather-location Actions directly,
so weather and ordinary conversation remain available without the robot Manager.

## Public contracts

- `/malbut/device/operate` (`DeviceOperation.action`): request ID, fixed operation,
  and JSON arguments. Results distinguish success/code/data/message. There is no
  supplied URL, shell command, ROS endpoint, or new polygon geometry.
- `/malbut/device/state` (`std_msgs/String`): current runtime/voice and cached
  Manager/sensor observations. The richer `status` operation includes valid maps,
  last explicit valid map, capabilities, and bounded recent Manager results.
- `/malbut/mission/stop_movement` (`StopMovement.srv`): stops BASE missions except
  fall confirmation, including waiting and late-accepted goals. Same-ID requests
  retain their original target set. An unresolved downstream cancellation keeps
  movement admission closed. Internal map relocalization participates in this check.
- Integrated movement requests bind the observed Manager lifetime and movement epoch
  before queueing. A new stop increments the epoch atomically; a request delivered
  after that stop cannot restart motion. Idempotent and denied stops do not increment it.
- `/malbut/localization/prepare` (`PrepareLocalization.srv`) applies the same epoch
  check while reserving a map/mapping transition. Standard LoadMap/Trigger endpoints
  remain available for existing direct clients. Agent preparation carries its original
  epoch through the bridge; a new Manager must still be at its initial epoch zero.
- Conditional preemption binds approval to exact mission IDs. `shutdown_runtime`
  additionally closes all Manager admission before the bridge shuts down the robot
  group; weather/speech live in the resident group. Normal stop keeps fall dialogue.
- `/malbut/localization/status` (`LocalizationState`) and enriched existing JSON
  carry runtime/transition IDs and `pose_ready`; a loaded map is not proof of a pose.
- `/malbut/mission/recent_results` contains bounded, timestamped Manager-wide
  history and `downstream_terminal`, separate from the Agent's own request journal.

Device operations: `status`, `runtime_start`, `runtime_stop`, `map_list`, `map_select`,
`map_delete`, `zones_get`, `zones_update`, `homecam_status`, `homecam_events`,
`homecam_recordings`, `homecam_falls`, `homecam_settings`, `result_publish`.

Map deletion requires a confirmed filename plus the catalog's content revision.
Zone edits require the selected map, existing index and revision; only name/behavior
attributes change. Named navigation remains unavailable without the existing real
map-bound destination configuration. No coordinates are inferred.

The existing deployment sequence remains `malbut_test/` to the real
`/home/ubuntu/ros2_ws/src/malbut/` directory, then `build.sh --executor sequential
--cmake-args -DBUILD_TESTING=OFF`, followed by `cloud.launch.py`. The source must
first include this integration: pulling unchanged `main` does not deploy a local
worktree branch. Never replace a dirty robot checkout or restart an active robot
without first checking its state and preserving its changes. The web migration,
media agent and generated ROS interfaces must be deployed as one compatible set.

## Outcomes and recovery

The Agent journals a request and its step/goal identity before dispatch. Bridge
operations also have a SQLite journal at
`~/.local/state/malbut/device-operations.sqlite3`. An interrupted send is unknown,
not retried. Retained Manager results may reconcile known goal identities after a
restart; reconciliation never advances a workflow or issues movement.

Fixed preparation checks runtime, explicit map selection and pose readiness.
With no saved map selected, runtime starts on the existing unknown default map.
AutoSLAM owns SLAM start/stop; Agent preparation never starts SLAM in advance.
Preparation failures and cancellation prevent subsequent movement. Long work does
not block status queries or stop commands. Map transition AUTO localization is
observed rather than repeated. Held manual input must return to neutral after stop.

Owner-authorized Homecam APIs use the bridge's device credential; a speech user ID
is not web authentication. Settings are saved on the server and applied through
heartbeat. Save receipts, waiting state and reported application are separate.
Media and fall receipts retain revision, runtime identity and observation age.
Videos/maps are represented by IDs and authenticated application links, never
long-lived copies of expiring playback URLs.

## Validation boundary

Unit tests cover dispatch idempotence, interrupted sends, fixed preparation,
confirmation target changes, observation freshness, setting authority, and receipts.
ROS tests use generated interfaces and fake application servers to exercise
acceptance/cancellation races, admission fences and responsive device operations.
LLM routing evaluations must use a fixed corpus with fake execution only.

These checks do not establish deployed Jetson audio or physical stopping. The device
acceptance run must record the voice request, workflow step/goal UUID, Manager
mission, stop request and downstream terminal outcome together. It must alternate
web/voice runtime start and stop, inspect duplicate nodes and the shared input, and
observe the wheels plus audible speech. No unattended physical run is triggered by
the offline test suite.
