"""Launch dependent groups only after their read-only startup probe succeeds."""

import json

from launch.actions import LogInfo
from launch.actions import RegisterEventHandler, SetLaunchConfiguration
from launch.event_handlers import OnProcessExit
from launch_ros.actions import Node


def sequential_startup(stages, parameters, *, timeout_s, speech_timeout_s):
    """Return launch actions and the one-shot probes exempt from exit guards."""
    gates = []
    for index, (kind, label, _actions, nodes) in enumerate(stages, start=1):
        gates.append(Node(
            package='malbut_bringup', executable='wait_for_robot',
            name=f'bringup_stage_{index}', output='screen', parameters=[{
                **parameters,
                'navigation': kind in ('navigation', 'applications'),
                'perception': parameters['perception'] and kind != 'sensors',
                'startup_stage': kind, 'startup_label': label,
                'startup_index': index, 'startup_total': len(stages),
                'startup_nodes': ','.join(nodes),
                'startup_timeout_s': speech_timeout_s if kind == 'speech' else timeout_s,
            }]))
        gates[-1]._malbut_readiness_probe = True
    completed = set()

    def begin(index):
        _kind, label, actions, _nodes = stages[index]
        return [SetLaunchConfiguration('malbut_startup_stage', json.dumps([index, label])),
                LogInfo(msg=f'Bringup [{index}/{len(stages)}] 준비 중: {label}'),
                *actions, gates[index]]

    def finished(index):
        def advance(context):
            completed.add(index)
            context.extend_globals({'malbut_startup_failed': False})
            actions = [LogInfo(msg=(
                f'Bringup [{index + 1}/{len(stages)}] 완료: {stages[index][1]}'))]
            if index + 1 < len(stages):
                actions.extend(begin(index + 1))
            else:
                context.extend_globals({'malbut_startup_complete': True})
            return actions

        def on_exit(event, context):
            if context.is_shutdown or index in completed:
                return []
            if event.returncode != 0:
                if getattr(context.locals, 'malbut_recovery_owner', False):
                    # Keep already started processes observable. The manual
                    # owner rechecks all reached stages, then continues here.
                    remaining = sum(speech_timeout_s if stage[0] == 'speech' else timeout_s
                                    for stage in stages[index + 1:])
                    context.extend_globals({
                        'malbut_startup_failed': True,
                        'malbut_startup_resume': (advance, remaining + timeout_s),
                    })
                    return [LogInfo(msg=f'Bringup stage failed: {stages[index][1]}; '
                                    'startup paused for manual recovery; not READY')]
                raise RuntimeError(f'Bringup stage failed: {stages[index][1]}')
            return advance(context)
        return on_exit

    return ([RegisterEventHandler(OnProcessExit(
        target_action=gate, on_exit=finished(index)))
        for index, gate in enumerate(gates)] + begin(0), gates)
