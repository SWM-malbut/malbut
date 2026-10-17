"""The robot writes only what the web accepts: malbut_interfaces/contracts/fall_events.json."""
import ast
import json
from pathlib import Path

import pytest

from malbut_agent_server.application import cloud_fall_monitor
from malbut_agent_server.domain.fall_monitoring import (
    CloudAnalysisExplanation, IncidentState, NotificationLevel, VideoAssessment, VoiceAnswer)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / 'malbut_interfaces/contracts/fall_events.json').read_text())


@pytest.mark.parametrize('enum, key', [
    (IncidentState, 'states'), (VideoAssessment, 'assessments'), (VoiceAnswer, 'answers'),
    (NotificationLevel, 'notificationLevels')])
def test_robot_values_are_the_contract_values(enum, key):
    assert [item.value for item in enum] == CONTRACT[key]


def notifications():
    tree = ast.parse(Path(cloud_fall_monitor.__file__).read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('_notify', '_notify_once')):
            level, reason = node.args[1], node.args[2]
            assert isinstance(level, ast.Attribute) and isinstance(reason, ast.Constant)
            yield node.func.attr, NotificationLevel[level.attr].value, reason.value


def test_every_notification_the_robot_sends_is_in_the_contract():
    sent = list(notifications())
    assert sent
    reasons = CONTRACT['notificationReasons']
    for call, level, reason in sent:
        assert reasons.get(reason) == level, reason
        assert reason not in CONTRACT['webOnlyNotificationReasons'], reason
        # Sent once per reason on the robot exactly when the web lets it past
        # the "only a higher level" rule.
        assert (call == '_notify_once') == (reason in CONTRACT['perReasonNotificationReasons']), reason


def test_explanation_limit_is_the_contract_limit():
    limit = CONTRACT['analysisExplanationMaxChars']
    assert CloudAnalysisExplanation('r1', 'incident', VideoAssessment.NORMAL_ACTIVITY, 'x' * limit)
    with pytest.raises(ValueError):
        CloudAnalysisExplanation('r1', 'incident', VideoAssessment.NORMAL_ACTIVITY, 'x' * (limit + 1))
