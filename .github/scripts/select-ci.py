#!/usr/bin/env python3
"""Select changed package tests and only their required ROS build dependencies."""

from difflib import SequenceMatcher
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
SPEECH_INTERFACES = {
    'msg/SpeechRequest.msg', 'msg/SpeechTranscript.msg',
    'msg/SpeechPlaybackStatus.msg', 'srv/ControlSpeechPlayback.srv',
    'srv/ClassifySpeechAddressee.srv',
}
SPEECH_CONSUMERS = {'malbut_agent_server', 'malbut_stt', 'malbut_tts'}
# Only reviewed ROS-independent code can skip ROS builds. Unknown files,
# package metadata, shared runtime types and ROS bridges retain the broad path.
FALL_MODULES = {
    'application/cloud_fall_monitor.py', 'application/fall_cloud_association.py',
    'application/fall_frame_buffer.py', 'application/fall_normal_closure.py',
    'application/fall_subject_evidence.py', 'application/vlm_analysis.py',
    'adapters/outbound/bedrock_nova_vlm.py', 'adapters/outbound/gemini_vlm.py',
    'adapters/outbound/ollama_cloud_fall.py', 'adapters/outbound/openai_compatible_vlm.py',
    'adapters/outbound/homecam_fall_events.py', 'adapters/outbound/sqlite_fall_journal.py',
    'domain/vlm.py', 'ports/cloud_fall.py', 'ports/fall_event_journal.py',
    'ports/vlm_provider.py', 'vlm_eval_schema.py', 'vlm_eval_metrics.py',
    'vlm_eval_prompt.py', 'vlm_eval_runner.py', 'vlm_inference_runner.py', 'vlm_factory.py',
}
FALL_TESTS = {
    'test_cloud_fall_monitor.py', 'test_fall_cloud_association.py',
    'test_fall_detector_input.py', 'test_fall_frame_buffer.py', 'test_fall_journal.py',
    'test_fall_normal_closure.py', 'test_fall_subject_evidence.py',
    'test_ollama_cloud_fall.py', 'test_vlm_eval.py', 'test_vlm_runtime.py',
}
FALL_EVAL_SCRIPTS = set('''
audit_fall84_misses audit_pose_comparison audit_pose_requests cleanup_ollama_eval_models
compare_pose_detectors diagnose_fall84_json_fences diagnose_fall_evidence_format
diagnose_fall_misses evaluate_runtime_cloud_frames experimental_fall_recheck
experimental_leg_change experimental_partial_pose experimental_pose_disagreement
experimental_pose_gap experimental_pose_input experimental_pose_retention
experimental_pose_stability experimental_request_coalescing experimental_request_continuity
experimental_request_dedup experimental_roi_pose extend_ollama_suite_catalog
fall_evaluation_v2 fall_evidence_decision finalize_partial_pose_review
prepare_detection_comparison_model prepare_fall_evaluation_84 prepare_fall_evaluation_v2
replay_fall84_cloud_pair replay_fall84_facts replay_fall84_model replay_fall84_prompt_ab
replay_fall_baseline replay_fall_evidence_decision replay_fall_rechecks
replay_improved_gemma_cloud replay_leg_change replay_parallel_pose_cloud replay_partial_pose
replay_pose_retention replay_pose_stability replay_request_coalescing replay_request_continuity
replay_request_dedup replay_roi_pose replay_vlm_frames review_additional_boxes
review_candidate_associations review_fall_annotations review_fall_timing
review_pose_detection_comparison review_request_coalescing review_request_dedup review_roi_pose
run_fall84_realtime run_free_cloud_fall_suite run_ollama_fall_suite score_fall_baseline
score_pose_comparison_review score_vlm_frames summarize_ollama_suite verify_pose_comparison_export
'''.split())
FALL_EVAL_TESTS = set('''
candidate_association_review fall84_cloud_pair fall84_facts fall84_json_fences fall84_miss_audit
fall84_model fall84_prompt_ab fall84_realtime fall_baseline fall_evaluation_v2
fall_evaluation_v2_reports fall_evidence_decision fall_miss_diagnosis fall_recheck
fall_timing_review fall_video_annotations free_cloud_fall_suite improved_gemma_cloud leg_change
ollama_eval_cleanup ollama_fall_suite ollama_suite_report parallel_pose_cloud partial_pose
pose_comparison_review pose_disagreement pose_gap pose_input_comparison pose_retention
pose_stability prepare_fall_evaluation_84 prepare_fall_evaluation_v2 request_coalescing
request_coalescing_replay request_continuity request_dedup review_additional_boxes roi_pose
runtime_cloud_frames vlm_frames
'''.split())
SIMULATION_BRIDGES = {
    'homecam_agent/homecam_media_agent/launch/homecam_sim.launch.py',
    'homecam_agent/scripts/setup_portable_sim.sh',
    'homecam_agent/scripts/lib/portable_runtime.sh',
}


def fall_python_only(path):
    """Select the offline fall suite without guessing about new ROS modules."""
    return (path in {'malbut_agent_server/malbut_agent_server/' + p for p in FALL_MODULES}
            or path in {'malbut_agent_server/test/' + p for p in FALL_TESTS}
            or path in {'homecam_agent/scripts/' + p + '.py' for p in FALL_EVAL_SCRIPTS}
            or path in {'homecam_agent/test/test_' + p + '.py' for p in FALL_EVAL_TESTS}
            or (path.startswith('homecam_agent/evaluations/')
                and Path(path).suffix in {'.json', '.jsonl', '.csv'}))


def speech_cmake_additions(path, base):
    """Prove CMake changed only by registering known speech interfaces."""
    if not base:
        return False
    versions = []
    for revision in (base, 'HEAD'):
        result = subprocess.run(['git', 'show', f'{revision}:{path}'],
                                capture_output=True, text=True)
        if result.returncode:
            return False
        versions.append(result.stdout.splitlines())
    before, after = versions
    starts = [index for index, line in enumerate(after)
              if line.strip() == 'rosidl_generate_interfaces(${PROJECT_NAME}']
    if len(starts) != 1:
        return False
    start = starts[0]
    end = next((index for index in range(start + 1, len(after))
                if after[index].strip() == ')'
                or after[index].split()[:1] == ['DEPENDENCIES']), start)
    registrations = {f'"{name}"' for name in SPEECH_INTERFACES}
    changed = False
    for tag, _, _, first, last in SequenceMatcher(
            None, before, after, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        if (tag != 'insert' or not start < first < last <= end
                or any(line.strip() not in registrations for line in after[first:last])):
            return False
        changed = True
    return changed


def package_index():
    """Read actual ROS contracts, excluding the deployment copy."""
    files = [*ROOT.glob('malbut_*/package.xml'),
             *ROOT.glob('malbut_autonomy/*/package.xml'),
             *ROOT.glob('malbut_yolo/vendor/yolo_ros/*/package.xml')]
    result = {}
    for path in files:
        xml = ET.parse(path).getroot()
        result[xml.findtext('name')] = {
            'path': path.parent.relative_to(ROOT).as_posix(),
            'dependencies': {item.text for item in xml
                             if item.tag == 'depend' or item.tag.endswith('_depend')},
        }
    return result


def selection(paths, full=False, base=None):
    """Test owners; shared wire contracts also select their consumers."""
    packages = package_index()
    owners = sorted(packages, key=lambda name: len(packages[name]['path']), reverse=True)
    selected = set()
    broad_interfaces = False
    flags = dict(web=False, infra=False, ros=False, ros_full=False, homecam=False,
                 assets=False, fall_python=False)
    for path in paths:
        original_path = path
        if (path == '.github/workflows/ci.yml'
                or path.startswith(('.github/scripts/', '.github/requirements/'))):
            full = True
        if path.endswith('.md'):
            continue
        if fall_python_only(path):
            flags['fall_python'] = True
            continue
        if path.startswith('malbut_web/'):
            flags['web'] = True
            flags['infra'] |= path.startswith('malbut_web/infra/')
        if path.startswith('homecam_agent/'):
            flags['homecam'] = True
        if path in SIMULATION_BRIDGES:
            selected.add('malbut_gazebo')
        if (path.startswith(('malbut_gazebo/models/', 'malbut_gazebo/worlds/'))
                or path in {'malbut_gazebo/launch/humanoid_demo.launch.py',
                            'malbut_gazebo/test/test_humanoid_route_collisions.py',
                            'homecam_agent/scripts/spawn_event_test_person.sh'}):
            flags['assets'] = True
            selected.add('malbut_gazebo')
        if path.startswith('malbut_test/'):
            path = path.removeprefix('malbut_test/')
            if path in {'setup.sh', 'build.sh', 'COLCON_IGNORE'}:
                selected.add('malbut_bringup')
            if path.startswith('malbut_patrol/'):
                path = 'malbut_autonomy/' + path
        if path.startswith('malbut_interfaces/'):
            interface = path.removeprefix('malbut_interfaces/')
            speech_only = interface in SPEECH_INTERFACES or (
                interface == 'CMakeLists.txt' and speech_cmake_additions(original_path, base))
            if speech_only:
                selected.update(SPEECH_CONSUMERS)
            else:
                broad_interfaces = True
        for name in owners:
            if path.startswith(packages[name]['path'] + '/'):
                selected.add(name)
                break
        else:
            if path.startswith('malbut_') and path.endswith('package.xml'):
                full = True  # Removing/moving a package can break old dependents.
    if full:
        flags.update(web=True, infra=True, homecam=True, assets=True, fall_python=True)
        selected.update(packages)
    consumers = selected.intersection({'malbut_interfaces', 'yolo_msgs'})
    if not full and not broad_interfaces:
        consumers.discard('malbut_interfaces')
    while True:
        affected = {name for name, package in packages.items()
                    if package['dependencies'] & consumers} - consumers
        if not affected:
            break
        consumers.update(affected)
    selected.update(consumers)
    if 'yolo_ros' in selected:
        selected.add('malbut_yolo')  # Not upstream model-download tests.
    build = set(selected)
    if 'malbut_agent_server' in selected:
        build.add('malbut_stt')  # Existing Agent ROS communication-test fixture.
    while True:
        dependencies = {dependency for name in build
                        for dependency in packages[name]['dependencies']
                        if dependency in packages}
        if dependencies <= build:
            break
        build.update(dependencies)
    flags['ros'] = bool(build)
    flags['ros_full'] = 'malbut_gazebo' in build
    tests = sorted(name for name in selected
                   if name.startswith('malbut_') and name != 'malbut_agent_server'
                   and (ROOT / packages[name]['path'] / 'test').is_dir())
    result = {name: str(value).lower() for name, value in flags.items()}
    result['agent'] = str('malbut_agent_server' in selected).lower()
    result['ros_packages'] = ' '.join(sorted(build))
    result['ros_paths'] = ' '.join(packages[name]['path'] for name in sorted(build))
    result['ros_test_packages'] = ' '.join(tests)
    return result


def changed_paths(base):
    """Include deleted/renamed paths and handle PR/main-push event bases."""
    if not base or set(base) == {'0'} or subprocess.run(
            ['git', 'cat-file', '-e', base + '^{commit}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        previous = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD^'],
                                  capture_output=True, text=True)
        if previous.returncode:
            return None
        base = previous.stdout.strip()
    output = subprocess.check_output([
        'git', 'diff', '--no-renames', '--name-only', '--diff-filter=ACMRD',
        '-z', base, 'HEAD'])
    return output.decode().rstrip('\0').split('\0') if output else []


def main():
    """Print GitHub step outputs without loading ROS or running builds."""
    args = sys.argv[1:]
    if args[:1] == ['--all']:
        result = selection([], full=True)
    else:
        base = args[0] if args and args[0] != '--paths' else None
        paths = args[1:] if args[:1] == ['--paths'] else changed_paths(base)
        result = selection(paths or [], full=paths is None, base=base)
    for name, value in result.items():
        print(f'{name}={value}')


if __name__ == '__main__':
    main()
