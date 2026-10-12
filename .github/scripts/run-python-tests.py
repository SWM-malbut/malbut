#!/usr/bin/env python3
"""One source of truth for Python CI suites, with optional duplicate delegation."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
AGENT_TESTS = Path('malbut_agent_server/test')
HOMECAM_TESTS = Path('homecam_agent/test')
HOMECAM_LAUNCH = HOMECAM_TESTS / 'test_robot_launch.py'
FALL_PATTERNS = (
    'test_cloud_fall_monitor.py', 'test_fall*.py',
    'test_ollama_cloud_fall.py', 'test_vlm*.py',
)
# Preserve the existing ROS-backed suite. Some files also contain offline
# cases; running them with ROS exercises a different boundary, not a duplicate.
AGENT_ROS_TESTS = (
    'test_node_communication_ros.py', 'test_ros_memory_communication.py',
    'test_ros_weather_communication.py', 'test_weather_action.py',
    'test_speech_addressee_service_ros.py', 'test_fall_runtime.py',
    'test_situation_action_ros.py', 'test_situation_recovery_ros.py',
    'test_ros_speech_missions.py', 'test_homecam_query.py',
    'test_manager_boundary_ros.py',
)


def test_files(suite, *, fall_job=False, root=ROOT):
    """Delegate only when the workflow actually schedules the fall job."""
    agent = {path.relative_to(root) for pattern in ('test_*.py', '*_test.py')
             for path in (root / AGENT_TESTS).rglob(pattern)}
    fall = {path.relative_to(root) for pattern in FALL_PATTERNS
            for path in (root / AGENT_TESTS).glob(pattern)}
    homecam = {path.relative_to(root) for pattern in ('test_*.py', '*_test.py')
               for path in (root / HOMECAM_TESTS).rglob(pattern)}
    if suite == 'agent':
        selected = agent - fall if fall_job else agent
    elif suite == 'fall':
        selected = fall | (homecam - {HOMECAM_LAUNCH})
    elif suite == 'homecam':
        selected = {HOMECAM_LAUNCH} if fall_job else homecam
    elif suite == 'agent-ros':
        selected = {AGENT_TESTS / name for name in AGENT_ROS_TESTS}
    else:
        raise ValueError(f'Unknown suite: {suite}')
    if not selected:
        raise ValueError(f'Empty test suite: {suite}')
    missing = [str(path) for path in sorted(selected) if not (root / path).is_file()]
    if missing:
        raise ValueError('Missing CI tests: ' + ', '.join(missing))
    return sorted(selected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite', choices=('agent', 'fall', 'homecam', 'agent-ros'))
    parser.add_argument('--fall-job', choices=('true', 'false'), default='false',
                        help='Whether this workflow also runs the offline fall job')
    parser.add_argument('--list', action='store_true', help='List files without running pytest')
    parser.add_argument('--junitxml', help='Write a pytest JUnit report')
    args = parser.parse_args(argv)
    try:
        paths = test_files(args.suite, fall_job=args.fall_job == 'true')
    except ValueError as error:
        parser.error(str(error))
    if args.list:
        print('\n'.join(map(str, paths)))
        return 0
    env = os.environ.copy()
    roots = [str(ROOT / 'malbut_agent_server'), str(ROOT / 'homecam_agent/homecam_detector')]
    if env.get('PYTHONPATH'):
        roots.append(env['PYTHONPATH'])
    env['PYTHONPATH'] = os.pathsep.join(roots)
    command = [sys.executable, '-m', 'pytest', '-q', '-rs']
    if args.junitxml:
        command.append('--junitxml=' + args.junitxml)
    print(f'CI Python suite: {args.suite} ({len(paths)} files)', flush=True)
    # Agent tests historically run from their package root; retain relative
    # fixture paths and subprocess behavior rather than changing test semantics.
    cwd = ROOT / AGENT_TESTS.parent if args.suite in ('agent', 'agent-ros') else ROOT
    targets = [str(ROOT / path) for path in paths]
    return subprocess.call(command + targets, cwd=cwd, env=env)


if __name__ == '__main__':
    sys.exit(main())
