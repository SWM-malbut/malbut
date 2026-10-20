#!/usr/bin/env python3
"""Verify suite ownership and runner commands without importing ROS or pytest."""

import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    'ci_python_suites', Path(__file__).with_name('run-python-tests.py'))
SUITES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUITES)


class PythonSuiteTests(unittest.TestCase):
    def files(self, suite, fall_job=False):
        return set(SUITES.test_files(suite, fall_job=fall_job))

    def test_standalone_suites_keep_the_previous_file_coverage(self):
        # Resolve against the repository, even when this test is run elsewhere.
        agent = {p.relative_to(SUITES.ROOT) for p in
                 (SUITES.ROOT / SUITES.AGENT_TESTS).glob('test_*.py')}
        homecam = {p.relative_to(SUITES.ROOT) for p in
                   (SUITES.ROOT / SUITES.HOMECAM_TESTS).glob('test_*.py')}
        fall = {p.relative_to(SUITES.ROOT) for pattern in SUITES.FALL_PATTERNS
                for p in (SUITES.ROOT / SUITES.AGENT_TESTS).glob(pattern)}
        self.assertEqual(self.files('agent'), agent)
        self.assertEqual(self.files('homecam'), homecam)
        self.assertEqual(self.files('fall'), fall | (homecam - {SUITES.HOMECAM_LAUNCH}))

    def test_full_ci_runs_each_offline_file_once_without_losing_any(self):
        agent = self.files('agent', fall_job=True)
        fall = self.files('fall')
        homecam = self.files('homecam', fall_job=True)
        self.assertFalse(agent & fall)
        self.assertFalse(agent & homecam)
        self.assertFalse(fall & homecam)
        self.assertEqual(agent | fall | homecam,
                         self.files('agent') | self.files('homecam'))
        self.assertEqual(homecam, {SUITES.HOMECAM_LAUNCH})

    def test_partial_ci_preserves_fallback_coverage(self):
        for has_agent in (False, True):
            for has_fall in (False, True):
                for has_homecam in (False, True):
                    with self.subTest(agent=has_agent, fall=has_fall, homecam=has_homecam):
                        before, after = set(), set()
                        for enabled, suite in ((has_agent, 'agent'), (has_fall, 'fall'),
                                               (has_homecam, 'homecam')):
                            if enabled:
                                before |= self.files(suite)
                                after |= self.files(suite, fall_job=has_fall)
                        self.assertEqual(before, after)

    def test_ros_backed_contracts_remain_an_independent_suite(self):
        expected = {SUITES.AGENT_TESTS / name for name in (
            'test_node_communication_ros.py', 'test_ros_memory_communication.py',
            'test_ros_weather_communication.py', 'test_weather_action.py',
            'test_speech_addressee_service_ros.py', 'test_fall_runtime.py',
            'test_situation_action_ros.py', 'test_situation_recovery_ros.py',
            'test_ros_speech_missions.py', 'test_homecam_query.py',
            'test_manager_boundary_ros.py',
            'test_voice_map_actions_ros.py', 'test_voice_lab_graph.py',
        )}
        self.assertEqual(self.files('agent-ros'), expected)
        self.assertEqual(self.files('agent-ros', fall_job=True), expected)

    def test_runner_preserves_the_existing_suite_order(self):
        paths = SUITES.test_files('fall')
        agent = [p for p in paths if p.parts[0] == 'malbut_agent_server']
        homecam = [p for p in paths if p.parts[0] == 'homecam_agent']
        self.assertEqual(paths, sorted(agent) + sorted(homecam))
        self.assertEqual(SUITES.test_files('agent-ros'),
                         [SUITES.AGENT_TESTS / name for name in SUITES.AGENT_ROS_TESTS])

    def test_new_nested_tests_are_not_silently_omitted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ('malbut_agent_server/test/nested/test_new.py',
                             'malbut_agent_server/test/nested/new_test.py',
                             'homecam_agent/test/nested/test_new.py'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self.assertEqual(len(SUITES.test_files('agent', root=root)), 2)
            self.assertEqual(len(SUITES.test_files('homecam', root=root)), 1)

    def test_empty_or_missing_required_tests_fail_loudly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for suite in ('agent', 'fall', 'homecam', 'agent-ros'):
                with self.subTest(suite=suite), self.assertRaises(ValueError):
                    SUITES.test_files(suite, root=root)
        with self.assertRaises(ValueError):
            SUITES.test_files('unknown')

    def test_runner_preserves_python_overlay_cwd_and_failure_exit_code(self):
        with patch.dict(SUITES.os.environ, {'PYTHONPATH': '/test/ros/overlay'}), \
                patch.object(SUITES.subprocess, 'call', return_value=7) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(SUITES.main(['agent', '--fall-job', 'true',
                                         '--junitxml', '/tmp/ci-report.xml']), 7)
        command = run.call_args.args[0]
        self.assertEqual(command[:5], [SUITES.sys.executable, '-m', 'pytest', '-q', '-rs'])
        self.assertIn('--junitxml=/tmp/ci-report.xml', command)
        self.assertEqual(run.call_args.kwargs['cwd'], SUITES.ROOT / 'malbut_agent_server')
        self.assertEqual(run.call_args.kwargs['env']['PYTHONPATH'].split(SUITES.os.pathsep),
                         [str(SUITES.ROOT / 'malbut_agent_server'),
                          str(SUITES.ROOT / 'homecam_agent/homecam_detector'),
                          str(SUITES.ROOT / 'malbut_autoslam'),
                          '/test/ros/overlay'])

    def test_runners_include_map_view_dependency_without_a_ros_overlay(self):
        for suite in ('agent', 'fall', 'homecam', 'agent-ros'):
            with self.subTest(suite=suite), \
                    patch.dict(SUITES.os.environ, {'PYTHONPATH': ''}), \
                    patch.object(SUITES.subprocess, 'call', return_value=0) as run, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(SUITES.main([suite]), 0)
                roots = run.call_args.kwargs['env']['PYTHONPATH'].split(SUITES.os.pathsep)
                self.assertIn(str(SUITES.ROOT / 'malbut_autoslam'), roots)

    def test_list_mode_does_not_run_tests_or_load_models(self):
        with patch.object(SUITES.subprocess, 'call') as run, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(SUITES.main(['homecam', '--fall-job', 'true', '--list']), 0)
        run.assert_not_called()
        self.assertEqual(output.getvalue().strip(), str(SUITES.HOMECAM_LAUNCH))


if __name__ == '__main__':
    unittest.main()
