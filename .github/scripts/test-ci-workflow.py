#!/usr/bin/env python3
"""Exercise CI shell commands with isolated tools, without ROS or downloads."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkflowTests(unittest.TestCase):
    def run_homecam(self, fail_build=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'fixture repo'
            scripts = root / '.github/scripts'
            scripts.mkdir(parents=True)
            script = scripts / 'test-homecam.sh'
            shutil.copy2(ROOT / '.github/scripts/test-homecam.sh', script)
            helper = root / 'homecam_agent/scripts/lib/portable_runtime.sh'
            helper.parent.mkdir(parents=True)
            helper.write_text('homecam_source_setup_file() { :; }\n'
                              'homecam_prepare_media_runtime() { :; }\n')
            for name in ('scripts/build_kvs_webrtc_sdk.sh', 'test/test_portable_runtime.sh'):
                path = root / 'homecam_agent' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('exit 0\n')
            binary = root / 'bin'
            binary.mkdir()
            log = root / 'calls.jsonl'
            # Neither fake command builds ROS, imports tests nor contacts a server.
            tool = (f'#!{sys.executable}\n'
                    'import json, os, sys\n'
                    'from pathlib import Path\n'
                    'with open(os.environ["CALL_LOG"], "a") as out:\n'
                    '    out.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")\n'
                    'if os.environ.get("FAIL_BUILD") == "1" and sys.argv[1:2] == ["build"]:\n'
                    '    sys.exit(7)\n')
            for name in ('colcon', 'python3'):
                path = binary / name
                path.write_text(tool)
                path.chmod(0o700)
            workspace = root / 'workspace'
            workspace.mkdir()
            env = dict(os.environ, HOMECAM_CI_WORKSPACE=str(workspace), CALL_LOG=str(log),
                       PATH=f'{binary}:{os.defpath}', FAIL_BUILD='1' if fail_build else '0')
            result = subprocess.run(['bash', str(script)], env=env, capture_output=True,
                                    text=True, timeout=10)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            return result, calls

    def test_homecam_builds_and_tests_only_its_two_packages(self):
        result, calls = self.run_homecam()
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [call for call in calls if call[:2] == ['colcon', 'build']]
        tests = [call for call in calls if call[:2] == ['colcon', 'test']]
        self.assertEqual(len(builds), 1)
        self.assertEqual(len(tests), 1)
        for command in builds + tests:
            first = command.index('--packages-select') + 1
            self.assertEqual(command[first:first + 2], ['homecam_detector', 'homecam_media_agent'])
            scope = command[command.index('--base-paths') + 1]
            self.assertTrue(scope.endswith('/homecam_agent'))
            self.assertNotIn('malbut_gazebo', command)
        self.assertIn('-DHOMECAM_ENABLE_KVS=ON', builds[0])
        self.assertIn('--return-code-on-test-failure', tests[0])
        self.assertEqual(calls[-1], ['colcon', 'test-result', '--verbose'])
        self.assertTrue(any(call[:3] == ['python3', '-m', 'pytest'] for call in calls))

    def test_homecam_build_failure_stops_before_tests(self):
        result, calls = self.run_homecam(fail_build=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(len(calls), 1)

    def test_workflow_keeps_required_ros_tests_and_offline_dependencies(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('test/test_fall_runtime.py', workflow)
        self.assertIn('--from-paths src/malbut/homecam_agent', workflow)
        self.assertNotIn('--from-paths src \\', workflow)
        self.assertNotIn('./homecam_agent/scripts/setup_portable_sim.sh', workflow)
        self.assertIn('python -m pip install -r .github/requirements/agent-tests.txt', workflow)
        self.assertIn('python -m pip install -r .github/requirements/fall-tests.txt', workflow)
        self.assertIn('cache: pip', workflow)
        agent_requirements = (ROOT / '.github/requirements/agent-tests.txt').read_text()
        self.assertIn('Pillow', agent_requirements)
        self.assertIn('aiohttp', agent_requirements)
        offline = (ROOT / '.github/scripts/test-fall-python.sh').read_text()
        self.assertIn('homecam_agent/test', offline)
        self.assertIn('--ignore=homecam_agent/test/test_robot_launch.py', offline)
        self.assertNotIn('--execute', offline)


if __name__ == '__main__':
    unittest.main()
