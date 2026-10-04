"""Portable offline checks for the console robot proposal simulator."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

import test_agent_console_support  # noqa: F401

from malbut_agent_server.config import Settings
from malbut_agent_server.prompting import prepare_model_input
from console_robot import RobotProbe


class RobotProbeTest(unittest.TestCase):
    def test_navigation_confirmation_never_starts_robot(self):
        with TemporaryDirectory() as directory:
            probe = RobotProbe(Settings(
                provider='mock', database_path=str(Path(directory) / 'robot.sqlite3'),
            ))
            try:
                with patch.object(probe.runtime.provider, 'complete',
                                  wraps=probe.runtime.provider.complete) as complete:
                    for reply, status in [('네', 'approved'), ('취소', 'canceled')]:
                        proposal = probe.turn('거실로 가줘')
                        self.assertEqual(proposal['status'], 'awaiting_confirmation')
                        request = complete.call_args.args[0]
                        prompt = prepare_model_input(request, []).text
                        context = json.loads(prompt.split('\n', 1)[1])
                        state = context['robot_state_untrusted']
                        self.assertIs(state['navigation_available'], True)
                        self.assertIs(state['localization_ok'], True)
                        self.assertEqual(state['battery_percent'], 90)
                        self.assertEqual(context['available_tools'], ['navigate'])
                        self.assertEqual(
                            [tool.name for tool in complete.call_args.args[3]],
                            ['navigate'],
                        )
                        result = probe.turn(reply)
                        self.assertEqual(result['status'], status)
                        for value in (proposal, result):
                            self.assertIs(value['simulated'], True)
                            self.assertIs(value['execution']['physical_authorized'], False)
                            self.assertEqual(value['execution']['nav2_start_count'], 0)
                        previous = probe.conversation_id
                        self.assertNotEqual(probe.reset(), previous)
            finally:
                probe.close()


if __name__ == '__main__':
    unittest.main()
