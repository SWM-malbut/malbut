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
                 assets=False)
    for path in paths:
        original_path = path
        if path == '.github/workflows/ci.yml' or path.startswith('.github/scripts/'):
            full = True
        if path.endswith('.md'):
            continue
        if path.startswith('homecam_web/'):
            flags['web'] = True
            flags['infra'] |= path.startswith('homecam_web/infra/')
        if path.startswith('homecam_agent/'):
            flags['homecam'] = True
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
        flags.update(web=True, infra=True, homecam=True, assets=True)
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
