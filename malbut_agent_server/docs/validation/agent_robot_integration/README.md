# Agent robot integration validation

> Historical record of the resident integration, not validation of the current
> SWM25-222 structure. Resident workflows and their evaluation scripts have been
> removed. To reproduce the checks below, use the Git revision recorded with the
> original result files. Current contracts are tested by `test_speech_missions.py`,
> `test_homecam_query.py` and `test_manager_boundary_ros.py`.

The original integration was recorded in `43b2b5d`, starting from `b60d0b5`,
a snapshot preserving the existing dirty checkout. The historical sections
below describe that source, not the later PR port on main `723c901`.
No physical robot, microphone, speaker, live Homecam data, cloud setting change,
or motor was used by these Agent provider/offline/container checks.

## PR port on main 723c901

The merged PR worktree preserves main's memory provider, API-key refresh and
weather-unavailable handling. Its initial complete Agent offline run passed
3332 tests, skipped 35, and passed 71 subtests. The three historical baseline
failure files were independently tested from an archive of main `723c901` and
passed all 34 tests: the historical ten failures are no longer current.

The port also preserves main's default unknown map and AutoSLAM ownership:
runtime preparation waits for a settled default/saved localization state;
only the admitted AutoSLAM mission starts SLAM. An unknown default map does
not satisfy the Agent's saved-map catalog requirement. Localization identity,
movement epoch, cancellation and result checks still apply.

After that compatibility change, the complete Agent suite plus
`malbut_bringup/test/test_device_operations.py` passed 3372 tests, skipped 35,
and passed 71 subtests in 42.01 seconds. The focused workflow/device subset
passed 102 tests. `pr-offline-tests.txt` preserves the complete local output.

```sh
PYTHONPATH=malbut_agent_server:malbut_bringup /Users/sinhyeonjae/Documents/ChatGPT/malbut/.runtime/fall-voice-20260925/venv/bin/python -m pytest -q malbut_agent_server/test malbut_bringup/test/test_device_operations.py --tb=short --disable-warnings
```

The four Agent ROS files were also rerun against the freshly built PR Manager
interfaces in a local ROS 2 Humble container: all 38 passed in 28.18 seconds.
`pr-ros-tests.txt` records the output. The generated Goal IDs, preemption and
movement epoch fields, resident status speech and resident weather transport
are covered without a physical runtime.

```sh
docker exec -e ROS_DOMAIN_ID=202 malbut-agent-pr-ros bash -lc 'source /opt/ros/humble/setup.bash; source /tmp/malbut-pr-manager/install/setup.bash; cd /repo; PYTHONPATH=malbut_agent_server:malbut_system_manager:malbut_tts:$PYTHONPATH python3 -m pytest -q malbut_agent_server/test/test_robot_device_ros.py malbut_agent_server/test/test_node_communication_ros.py malbut_agent_server/test/test_ros_speech_missions.py malbut_agent_server/test/test_resident_weather_query_ros.py --tb=short'
```

These are PR source/local-container checks. Earlier provider evaluations below
were not rerun as current PR-device proof; no robot reconnection or deployment
was performed during the PR port.

## Historical offline checks

Use the existing Python 3.12 environment from the original checkout:

```sh
PYTHONPATH=malbut_agent_server /Users/sinhyeonjae/Documents/ChatGPT/malbut/.runtime/fall-voice-20260925/venv/bin/python -m pytest -q malbut_agent_server/test --tb=no --disable-warnings
```

`offline-tests.log` records the full collection: 3021 passed, 31 skipped,
ten failed, and 24 subtests passed. The ten failures also reproduce
in the original checkout before integration. All concern the existing
`StoryMemoryProvider` wrapper: older tests expect the underlying Routed/Reliable
provider directly or call the wrapper without creating a context receipt.
No memory implementation or consent rule was changed to make those tests pass.

The baseline reproduction command, run from the original checkout, is:

```sh
PYTHONPATH=malbut_agent_server /Users/sinhyeonjae/Documents/ChatGPT/malbut/.runtime/fall-voice-20260925/venv/bin/python -m pytest -q malbut_agent_server/test/test_role_model_routing.py malbut_agent_server/test/test_runtime.py malbut_agent_server/test/test_voice_lab_conversation.py --tb=no
```

It produces ten failures and 24 passes. The exact failing test names are included
in `offline-tests.log`; this is a known baseline limitation, not a passing suite.

New deterministic checks cover journal-before-send, wire UUID/policy identity,
duplicate delivery, restart reconciliation without replay, bound confirmation,
same-map lost pose, late Goal acceptance after stop, device transition
cancellation, result freshness and consent-preserving publication. They also
exercise committed dialogue dispatch and observed outcomes in the next turn.
After the final preparation transport fence was added, the targeted workflow,
Manager client and device client set passed 97 tests; this includes one test
added after the full collection above.

## Historical ROS communication

The shared Ubuntu ARM64 ROS 2 Humble container uses generated interfaces in
`/tmp/malbut-ws/install`. Agent tests use isolated domain `193` (the existing
speech-mission fixture overrides it with `196`), with test-only
Action servers and no physical runtime:

```sh
docker exec -e ROS_DOMAIN_ID=193 malbut-agent-integration-ros bash -lc 'source /opt/ros/humble/setup.bash; source /tmp/malbut-ws/install/setup.bash; cd /repo; PYTHONPATH=malbut_agent_server:malbut_system_manager:malbut_tts:$PYTHONPATH python3 -m pytest -q malbut_agent_server/test/test_robot_device_ros.py malbut_agent_server/test/test_node_communication_ros.py malbut_agent_server/test/test_ros_speech_missions.py malbut_agent_server/test/test_resident_weather_query_ros.py --tb=short'
```

The typed tests inspect DeviceOperation and conditional StopMovement round trips,
and exact Manager Goal UUID, preemption confirmation, localization identity, and
movement epoch fields. They prove ROS contract behavior; they do not prove
physical movement or acoustic behavior.
The final command passed all 38 tests; its output is preserved in `ros-tests.log`.
The resident profile also routes an actual SpeechTranscript through committed
dialogue to DeviceOperation and speech notices with no Manager present; resident
weather/location checks run against the existing weather Actions with no Manager.

The later notification correlation fix is recorded in
`notification-correlation-tests.log`: 68 tests passed (the three typed/profile
ROS checks plus the existing TTS runtime suite). A workflow NOTIFICATION now
keeps its `speech-request-<sha256(utterance_id)>` request ID; the initial dialogue
acknowledgement keeps its original utterance ID, and each has a distinct playback
ID. TTS deduplicates playback IDs, while finalized request IDs suppress later
interim progress only; they do not suppress a later non-interim notification.

## Historical actual model intent selection

`evaluate_intents.py` calls the existing OpenAI Responses provider directly with
synthetic Korean inputs. It creates no runtime, ROS client, or cloud adapter.
Model and reasoning effort are the current source/environment settings:
`gpt-5.6-luna`, `low`. API keys are never written to artifacts.

The initial frozen 30-case set scored 29/30 on exact expected tool selections.
One negative-follow utterance selected the legacy voice cancellation tool rather
than the new global stop tool. Both reached the same global stop runner in the
new profile; the duplicate legacy tool was then removed from that profile only.
`cases.initial.json` and `results.initial.json` preserve the first run.

The second set preserves all 30 utterances and expected values, adding two weather
regressions. The final runtime tool surface scored 32/32. `cases.json` and
`results.json` retain every expected value, actual decision, response model,
latency and reported token count. The second run used 194743 input tokens and
1615 output tokens; the first used 179445 and 1465. The API response did not
provide billed monetary cost. These finite samples are not an accuracy estimate
for arbitrary speech, speaker identity, or STT noise.

## Historical repeated current-state questions

A later device probe reused the conversation database and answered a repeated
status question from its old STOPPED response without issuing a new workflow.
The conditional tool instructions now require a fresh query for each current
state/list/observation request, including repeated questions without the word
"now". Explicit recall of a past result remains a conversational answer.
Only read tools actually supplied to the provider appear in this instruction.
The focused prompting, speech prompting, OpenAI provider and semantic prompting
test files passed all 58 tests, including eight historical-context/tool-scope
regressions. Product source and its mirrored copy have identical SHA-256 hashes.

`evaluate_repeat_queries.py` freezes six synthetic prior conversations and
`robot_operation_results` in `repeat-query-cases.json`. All six passed with
`gpt-5.6-luna`, `low`: repeated robot status, continuing patrol status, current
person observations, saved maps, Homecam settings, and past-result recall.
`repeat-query-results.json` includes the exact prompt hash and decisions; the
run used 37741 input and 249 output tokens. No conversation database or runtime
adapter is accessed by this evaluator.

The same 32-case corpus was then run once with the new instructions and passed
32/32; `results.fresh-query-regression.json` preserves that separate run
(203735 input and 1592 output tokens). The original corpus and results remain
unchanged. To repeat without replacing the original result file, call
`evaluate_intents.main('results.fresh-query-regression.json')` explicitly.
These provider-only samples do not establish deployed or physical behavior.
