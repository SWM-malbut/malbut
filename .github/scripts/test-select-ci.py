#!/usr/bin/env python3
"""Check speech selection and conservative fallback with real Git diffs."""

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('select-ci.py')
SPEC = importlib.util.spec_from_file_location('select_ci', SCRIPT)
SELECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SELECTOR)
SPEECH_BUILD = {
    'malbut_agent_server', 'malbut_interfaces', 'malbut_stt',
    'malbut_system_manager', 'malbut_tts',
}
CMAKE = '''project(malbut_interfaces)
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/SpeechRequest.msg"
  DEPENDENCIES std_msgs
)
ament_package()
'''


@contextmanager
def cmake_diff(after, path='malbut_interfaces/CMakeLists.txt', before=CMAKE):
    """Commit a minimal fixture while retaining the real package index."""
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as temporary:
        os.chdir(temporary)
        try:
            def git(*args):
                return subprocess.check_output(['git', *args], text=True).strip()

            git('init', '--quiet')
            git('config', 'user.name', 'CI selector test')
            git('config', 'user.email', 'ci-selector@example.invalid')
            git('config', 'commit.gpgsign', 'false')
            target = Path(path)
            target.parent.mkdir(parents=True)
            target.write_text(before)
            git('add', '.')
            git('commit', '--quiet', '-m', 'Base fixture')
            base = git('rev-parse', 'HEAD')
            target.write_text(after)
            git('add', '.')
            git('commit', '--quiet', '--allow-empty', '-m', 'Changed fixture')
            yield base
        finally:
            os.chdir(previous)


class SpeechSelectionTests(unittest.TestCase):
    """Keep all speech consumers without selecting unrelated robot suites."""

    def assert_speech(self, result):
        self.assertEqual(set(result['ros_packages'].split()), SPEECH_BUILD)
        self.assertEqual(result['ros_test_packages'], 'malbut_stt malbut_tts')
        self.assertEqual(result['agent'], 'true')
        self.assertEqual(result['ros_full'], 'false')

    def assert_broad(self, result):
        self.assertEqual(result['ros_full'], 'true')
        self.assertTrue({'malbut_tracking', 'malbut_patrol', 'malbut_system_manager'}
                        <= set(result['ros_test_packages'].split()))

    def test_each_speech_interface_and_deployment_copy(self):
        for prefix in ('', 'malbut_test/'):
            for interface in SELECTOR.SPEECH_INTERFACES:
                path = f'{prefix}malbut_interfaces/{interface}'
                with self.subTest(path=path):
                    self.assert_speech(SELECTOR.selection([path]))

    def test_unknown_interfaces_and_package_metadata_remain_broad(self):
        for path in ('msg/NewSpeech.msg', 'action/FollowPerson.action',
                     'srv/Unknown.srv', 'package.xml', 'CMakeLists.txt'):
            with self.subTest(path=path):
                result = SELECTOR.selection([
                    'malbut_interfaces/msg/SpeechRequest.msg',
                    f'malbut_interfaces/{path}',
                ])
                self.assert_broad(result)

    def test_mixed_tracking_change_keeps_tracking_tests(self):
        result = SELECTOR.selection([
            'malbut_interfaces/msg/SpeechRequest.msg',
            'malbut_tracking/src/lidar_foreground_preprocessor.cpp',
        ])
        self.assertTrue(SPEECH_BUILD <= set(result['ros_packages'].split()))
        self.assertEqual(result['ros_test_packages'],
                         'malbut_stt malbut_tracking malbut_tts')
        self.assertEqual(result['ros_full'], 'false')

    def test_shared_ci_and_explicit_full_remain_full(self):
        for path in ('.github/workflows/ci.yml', '.github/scripts/select-ci.py',
                     '.github/scripts/test-select-ci.py', 'malbut_removed/package.xml'):
            with self.subTest(path=path):
                result = SELECTOR.selection([path])
                self.assert_broad(result)
                self.assertEqual(result['web'], 'true')
                self.assertEqual(result['homecam'], 'true')
        self.assert_broad(SELECTOR.selection([], full=True))

    def test_yolo_contract_keeps_existing_consumers(self):
        result = SELECTOR.selection([
            'malbut_yolo/vendor/yolo_ros/yolo_msgs/msg/Detection.msg',
        ])
        self.assertTrue({'malbut_yolo', 'malbut_tracking'}
                        <= set(result['ros_packages'].split()))

    def test_bringup_builds_speech_dependencies_without_selecting_their_tests(self):
        for path in ('malbut_bringup/launch/robot.launch.py',
                     'malbut_test/malbut_bringup/launch/robot.launch.py',
                     'malbut_test/setup.sh',
                     'malbut_test/build.sh'):
            with self.subTest(path=path):
                result = SELECTOR.selection([path])
                builds = set(result['ros_packages'].split())
                self.assertTrue(SPEECH_BUILD <= builds)
                self.assertFalse({'malbut_gazebo', 'malbut_scenarios'} & builds)
                self.assertEqual(result['ros_test_packages'], 'malbut_bringup')
                self.assertEqual(result['agent'], 'false')
                self.assertEqual(result['ros_full'], 'false')

    def test_only_added_speech_registrations_are_narrow(self):
        after = CMAKE.replace('  DEPENDENCIES',
                              '  "msg/SpeechPlaybackStatus.msg"\n'
                              '  "srv/ClassifySpeechAddressee.srv"\n'
                              '  "srv/ControlSpeechPlayback.srv"\n'
                              '  DEPENDENCIES')
        for prefix in ('', 'malbut_test/'):
            path = f'{prefix}malbut_interfaces/CMakeLists.txt'
            with self.subTest(path=path), cmake_diff(after, path) as base:
                self.assert_speech(SELECTOR.selection(
                    SELECTOR.changed_paths(base), base=base))

    def test_cmake_removals_and_other_changes_are_broad(self):
        variants = {
            'removal': CMAKE.replace('  "msg/SpeechRequest.msg"\n', ''),
            'dependency': CMAKE.replace('DEPENDENCIES std_msgs',
                                        'DEPENDENCIES std_msgs geometry_msgs'),
            'unknown': CMAKE.replace('  DEPENDENCIES',
                                     '  "msg/NewSpeech.msg"\n  DEPENDENCIES'),
            'after_dependencies': CMAKE.replace(
                '  DEPENDENCIES std_msgs\n',
                '  DEPENDENCIES std_msgs\n  "msg/SpeechPlaybackStatus.msg"\n'),
            'outside_generator': CMAKE + '  "msg/SpeechPlaybackStatus.msg"\n',
            'other_command': CMAKE + 'find_package(geometry_msgs REQUIRED)\n',
            'unchanged': CMAKE,
        }
        for label, after in variants.items():
            with self.subTest(label=label), cmake_diff(after) as base:
                self.assert_broad(SELECTOR.selection(
                    ['malbut_interfaces/CMakeLists.txt'], base=base))

    def test_missing_diff_evidence_remains_broad(self):
        after = CMAKE.replace('  DEPENDENCIES',
                              '  "msg/SpeechPlaybackStatus.msg"\n  DEPENDENCIES')
        with cmake_diff(after):
            for base in (None, 'invalid-base', '0' * 40):
                with self.subTest(base=base):
                    self.assert_broad(SELECTOR.selection(
                        SELECTOR.changed_paths(base), base=base))


class FallSelectionTests(unittest.TestCase):
    """Do not install ROS for reviewed fall code or lose checks on mixed PRs."""

    def test_every_reviewed_module_uses_offline_tests(self):
        paths = [*('malbut_agent_server/malbut_agent_server/' + p
                   for p in SELECTOR.FALL_MODULES),
                 *('malbut_agent_server/test/' + p for p in SELECTOR.FALL_TESTS),
                 *('homecam_agent/scripts/' + p + '.py'
                   for p in SELECTOR.FALL_EVAL_SCRIPTS),
                 *('homecam_agent/test/test_' + p + '.py'
                   for p in SELECTOR.FALL_EVAL_TESTS)]
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue((SELECTOR.ROOT / path).is_file())
                result = SELECTOR.selection([path])
                self.assertEqual(result['fall_python'], 'true')
                self.assertEqual(result['ros'], 'false')
                self.assertEqual(result['homecam'], 'false')
                self.assertEqual(result['ros_packages'], '')

    def test_ros_wiring_shared_types_and_unknown_files_keep_ros(self):
        for path in ('malbut_agent_server/malbut_agent_server/ros_fall_monitor.py',
                     'malbut_agent_server/malbut_agent_server/fall_runtime.py',
                     'malbut_agent_server/malbut_agent_server/domain/fall_monitoring.py',
                     'malbut_agent_server/malbut_agent_server/application/fall_detector_input.py',
                     'malbut_agent_server/malbut_agent_server/application/fall_new_node.py',
                     'malbut_agent_server/test/test_fall_runtime.py',
                     'malbut_agent_server/config/fall_runtime.example.json',
                     'malbut_agent_server/package.xml', 'malbut_agent_server/setup.py'):
            with self.subTest(path=path):
                result = SELECTOR.selection([path])
                self.assertEqual(result['ros'], 'true')
                self.assertEqual(result['agent'], 'true')
        for path in ('homecam_agent/test/test_robot_launch.py',
                     'homecam_agent/scripts/new_fall_node.py',
                     'homecam_agent/homecam_detector/homecam_detector/detector_node.py'):
            with self.subTest(path=path):
                self.assertEqual(SELECTOR.selection([path])['homecam'], 'true')

    def test_mixed_order_keeps_all_required_jobs(self):
        fall = 'malbut_agent_server/malbut_agent_server/adapters/outbound/ollama_cloud_fall.py'
        for paths in ([fall, 'malbut_stt/malbut_stt/node.py'],
                      ['malbut_stt/malbut_stt/node.py', fall]):
            result = SELECTOR.selection(paths)
            self.assertEqual(result['fall_python'], 'true')
            self.assertEqual(result['ros'], 'true')
            self.assertIn('malbut_stt', result['ros_test_packages'].split())
        result = SELECTOR.selection([fall, 'homecam_web/app/page.tsx'])
        self.assertEqual(result['web'], 'true')
        self.assertEqual(result['fall_python'], 'true')
        self.assertEqual(result['ros'], 'false')

    def test_simulation_bridges_still_select_gazebo(self):
        for path in SELECTOR.SIMULATION_BRIDGES:
            with self.subTest(path=path):
                self.assertTrue((SELECTOR.ROOT / path).is_file())
                result = SELECTOR.selection([path])
                self.assertIn('malbut_gazebo', result['ros_packages'].split())
                self.assertIn('malbut_gazebo', result['ros_test_packages'].split())
                self.assertEqual(result['homecam'], 'true')

    def test_documents_data_and_full_ci(self):
        self.assertEqual(SELECTOR.selection([
            'malbut_agent_server/docs/fall_runtime.md'])['fall_python'], 'false')
        self.assertEqual(SELECTOR.selection([
            'homecam_agent/evaluations/synthetic_fall_v1/labels.jsonl'
        ])['fall_python'], 'true')
        for path in ('.github/workflows/ci.yml', '.github/requirements/fall-tests.txt'):
            result = SELECTOR.selection([path])
            self.assertEqual(result['ros_full'], 'true')
            self.assertEqual(result['fall_python'], 'true')
        self.assertEqual(SELECTOR.selection([], full=True)['fall_python'], 'true')

    def test_deleted_renamed_and_earlier_pr_files_are_not_lost(self):
        old = 'malbut_agent_server/malbut_agent_server/adapters/outbound/ollama_cloud_fall.py'
        new = 'malbut_agent_server/malbut_agent_server/ros_fall_monitor.py'
        # --no-renames gives both the removed and added names to the selector.
        result = SELECTOR.selection([old, new])
        self.assertEqual(result['fall_python'], 'true')
        self.assertEqual(result['ros'], 'true')
        with cmake_diff('', path=old, before='old provider') as base:
            self.assertIn(old, SELECTOR.changed_paths(base))
            Path('notes.md').write_text('later documentation change')
            subprocess.run(['git', 'add', 'notes.md'], check=True)
            subprocess.run(['git', 'commit', '--quiet', '-m', 'Documentation'], check=True)
            self.assertIn(old, SELECTOR.changed_paths(base))
            self.assertEqual(SELECTOR.selection(
                SELECTOR.changed_paths(base), base=base)['fall_python'], 'true')


if __name__ == '__main__':
    unittest.main()
