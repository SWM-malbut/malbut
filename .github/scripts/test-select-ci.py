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
    'malbut_agent_server', 'malbut_interfaces', 'malbut_stt', 'malbut_system_manager',
    'malbut_tts',
}
CMAKE = '''project(malbut_interfaces)
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/SpeechRequest.msg"
  DEPENDENCIES std_msgs
)
ament_package()
'''
FLAGS = ('web', 'infra', 'ros', 'ros_full', 'homecam')


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


class ModuleSelectionTests(unittest.TestCase):
    """General module matrix, formerly duplicated in the shell test runner."""

    def test_module_flags_and_mixed_changes(self):
        # Five bits follow FLAGS; package ownership is tested separately below.
        cases = (
            ('malbut_web/app/page.tsx', '10000'),
            ('malbut_web/infra/cdk/lib/stack.ts', '11000'),
            ('homecam_agent/homecam_media_agent/src/node.cpp', '00001'),
            ('malbut_agent_server/malbut_agent_server/memory.py', '00100'),
            ('malbut_description/urdf/robot.xacro', '00100'),
            ('malbut_gazebo/launch/navigation.launch.py', '00110'),
            ('malbut_autonomy/malbut_patrol/patrol.py', '00100'),
            ('malbut_tracking/src/lidar_foreground_preprocessor.cpp', '00100'),
            ('malbut_reid/malbut_reid/person_reidentifier_node.py', '00100'),
            ('malbut_yolo/scripts/prepare_yolo26_model.sh', '00100'),
            ('malbut_gazebo_plugins/src/actor_pose_system.cpp', '00100'),
            ('malbut_yolo/vendor/yolo_ros/yolo_ros/package.xml', '00100'),
            ('malbut_interfaces/action/FollowPerson.action', '00110'),
            ('malbut_scenarios/malbut_scenarios/text_agent_server.py', '00110'),
            ('malbut_system_manager/malbut_system_manager/system_manager_node.py', '00100'),
            ('malbut_autoslam/malbut_autoslam/autoslam_node.py', '00100'),
            ('malbut_bringup/launch/robot.launch.py', '00100'),
            ('malbut_stt/malbut_stt/node.py', '00100'),
            ('malbut_tts/malbut_tts/receiver.py', '00100'),
            ('README.md', '00000'),
            ('.github/workflows/ci.yml', '11111'),
            ('.github/scripts/detect-ci-modules.sh', '11111'),
            ('.github/scripts/test-detect-ci-modules.sh', '11111'),
            ('', '00000'),
            ('malbut_agent_server/memory.py malbut_web/app/page.tsx', '10100'),
            ('malbut_agent_server/memory.py malbut_stt/node.py', '00100'),
            ('malbut_stt/node.py malbut_agent_server/memory.py', '00100'),
            ('.github/workflows/ci.yml malbut_agent_server/memory.py', '11111'),
        )
        for paths, expected in cases:
            with self.subTest(paths=paths):
                result = SELECTOR.selection(paths.split())
                self.assertEqual(''.join('1' if result[key] == 'true' else '0'
                                         for key in FLAGS), expected)

    def test_build_dependencies_do_not_expand_the_test_owners(self):
        cases = (
            ('malbut_bringup/launch/robot.launch.py', 'ros_test_packages', 'malbut_bringup'),
            ('malbut_bringup/config/nav2_params.yaml', 'agent', 'false'),
            ('malbut_tracking/src/lidar_foreground_preprocessor.cpp',
             'ros_test_packages', 'malbut_tracking'),
            ('malbut_test/malbut_bringup/config/nav2_params.yaml',
             'ros_test_packages', 'malbut_bringup'),
            ('malbut_test/COLCON_IGNORE', 'ros_test_packages', 'malbut_bringup'),
            ('malbut_test/malbut_patrol/launch/patrol.launch.py',
             'ros_test_packages', 'malbut_patrol'),
            ('malbut_agent_server/malbut_agent_server/node.py', 'agent', 'true'),
            ('malbut_yolo/vendor/yolo_ros/yolo_ros/yolo_node.py',
             'ros_test_packages', 'malbut_yolo'),
            ('malbut_bringup/README.md', 'ros_test_packages', ''),
            ('malbut_gazebo/web/index.html', 'assets', 'false'),
            ('malbut_gazebo/models/humanoid_actor/model.sdf', 'assets', 'true'),
            ('homecam_agent/scripts/spawn_event_test_person.sh',
             'ros_test_packages', 'malbut_gazebo'),
            ('homecam_agent/homecam_media_agent/src/node.cpp', 'ros_packages', ''),
            ('homecam_agent/homecam_media_agent/src/node.cpp', 'ros_test_packages', ''),
        )
        for path, key, expected in cases:
            with self.subTest(path=path, key=key):
                self.assertEqual(SELECTOR.selection([path])[key], expected)
        result = SELECTOR.selection(['malbut_interfaces/action/FollowPerson.action'])
        self.assertTrue({'malbut_system_manager', 'malbut_tracking', 'malbut_patrol'}
                        <= set(result['ros_test_packages'].split()))
        for path in ('malbut_bringup/launch/robot.launch.py', '.github/workflows/ci.yml'):
            with self.subTest(path=path):
                self.assertIn('homecam_agent/homecam_media_agent',
                              SELECTOR.selection([path])['ros_paths'].split())

    def test_shell_entrypoint_uses_git_diff_and_conservative_base_fallbacks(self):
        def invoke(*args):
            output = subprocess.check_output(
                ['bash', str(SCRIPT.with_name('detect-ci-modules.sh')), *args], text=True)
            return dict(line.split('=', 1) for line in output.splitlines())

        # Real Git fixtures preserve spaces, deletion, rename and event bases.
        with cmake_diff('changed', path='malbut_agent_server/memory source.py',
                        before='base') as base:
            for revision in (base, '', 'invalid-base', '0' * 40):
                with self.subTest(revision=revision):
                    self.assertEqual(invoke(revision), SELECTOR.selection(
                        SELECTOR.changed_paths(revision), base=revision))
            self.assertEqual(invoke('HEAD')['ros'], 'false')
            target = Path('malbut_agent_server/memory source.py')
            before_delete = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                     text=True).strip()
            target.unlink()
            subprocess.run(['git', 'add', '.'], check=True)
            subprocess.run(['git', 'commit', '--quiet', '-m', 'Deleted'], check=True)
            self.assertEqual(invoke(before_delete)['agent'], 'true')
            node = Path('malbut_stt/node.py')
            node.parent.mkdir()
            node.write_text('node')
            subprocess.run(['git', 'add', '.'], check=True)
            subprocess.run(['git', 'commit', '--quiet', '-m', 'Node'], check=True)
            before_move = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                   text=True).strip()
            subprocess.run(['git', 'mv', str(node), 'malbut_agent_server/node.py'], check=True)
            subprocess.run(['git', 'commit', '--quiet', '-m', 'Moved'], check=True)
            self.assertEqual(invoke(before_move)['agent'], 'true')
            self.assertEqual(invoke(before_move)['ros'], 'true')
        self.assertEqual(invoke('--all'), SELECTOR.selection([], full=True))
        self.assertEqual(invoke('--paths'), SELECTOR.selection([]))


class SpeechSelectionTests(unittest.TestCase):
    """Keep all speech consumers without selecting unrelated robot suites."""

    def assert_speech(self, result):
        self.assertEqual(set(result['ros_packages'].split()), SPEECH_BUILD)
        # The shared interface package now contains its own wire-contract tests.
        self.assertEqual(result['ros_test_packages'], 'malbut_interfaces malbut_stt malbut_tts')
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
                         'malbut_interfaces malbut_stt malbut_tracking malbut_tts')
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

    def test_fall_contract_selects_web_and_offline_fall_tests_only(self):
        for prefix in ('', 'malbut_test/'):
            with self.subTest(prefix=prefix):
                result = SELECTOR.selection([f'{prefix}malbut_interfaces/contracts/fall_events.json'])
                self.assertEqual((result['web'], result['fall_python'], result['ros']),
                                 ('true', 'true', 'false'))

    def test_homecam_is_retested_for_its_new_settings_messages(self):
        for prefix in ('', 'malbut_test/'):
            for name in ('FallSettingsSnapshot', 'FallSettingsReport'):
                result = SELECTOR.selection([f'{prefix}malbut_interfaces/msg/{name}.msg'])
                self.assertEqual(result['homecam'], 'true')

    def test_bringup_builds_speech_dependencies_without_selecting_their_tests(self):
        for path in ('malbut_bringup/launch/robot.launch.py',
                     'malbut_test/malbut_bringup/launch/robot.launch.py',
                     'malbut_test/setup.sh',
                     'malbut_test/build.sh'):
            with self.subTest(path=path):
                result = SELECTOR.selection([path])
                builds = set(result['ros_packages'].split())
                self.assertTrue(SPEECH_BUILD <= builds)
                self.assertTrue({'homecam_detector', 'homecam_media_agent', 'yolo_msgs',
                                 'malbut_tracking', 'malbut_system_manager'} <= builds)
                self.assertIn('homecam_agent/homecam_detector', result['ros_paths'].split())
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


class DeploymentToolSelectionTests(unittest.TestCase):
    """Test deployment-only measurement without selecting the robot stack."""

    def test_resource_monitor_is_built_and_tested_from_its_actual_path(self):
        result = SELECTOR.selection([
            'malbut_test/malbut_resource_monitor/malbut_resource_monitor/collector.py',
        ])
        self.assertEqual(result['ros_packages'], 'malbut_interfaces malbut_resource_monitor')
        self.assertEqual(result['ros_test_packages'], 'malbut_resource_monitor')
        self.assertEqual(result['ros_paths'],
                         'malbut_interfaces malbut_test/malbut_resource_monitor')
        self.assertEqual(result['ros_full'], 'false')
        self.assertEqual(result['agent'], 'false')

    def test_main_packages_still_take_precedence_over_deployment_copies(self):
        packages = SELECTOR.package_index()
        self.assertEqual(packages['malbut_bringup']['path'], 'malbut_bringup')
        self.assertEqual(packages['malbut_interfaces']['path'], 'malbut_interfaces')


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
        result = SELECTOR.selection([fall, 'malbut_web/app/page.tsx'])
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
