"""Start measuring before applications; recorder failure never stops the robot."""

import os

from launch.actions import LogInfo, OpaqueFunction, RegisterEventHandler, TimerAction
from launch.event_handlers import OnProcessExit, OnProcessIO
from launch_ros.actions import Node


def record_first(actions, root):
    recorder = Node(
        package='malbut_resource_monitor', executable='resource_recorder',
        name='resource_recorder', output='screen',
        parameters=[{'use_sim_time': False}],
        arguments=['--root', root, '--parent-pid', str(os.getpid())],
    )
    released = False
    output = ''

    def release(context=None, warning=None):
        nonlocal released
        if released or (context is not None and context.is_shutdown):
            return []
        released = True
        return ([LogInfo(msg=warning)] if warning else []) + list(actions)

    def stdout(event):
        nonlocal output
        output = (output + event.text.decode(errors='replace'))[-4096:]
        if 'MALBUT_RESOURCE_MONITOR_READY' in output:
            return [OpaqueFunction(function=lambda context: release(context))]
        return []

    def exited(event, context):
        warning = 'Resource recorder stopped; robot remains independent. Check resource log.'
        if released:
            return [] if context.is_shutdown else [LogInfo(msg=warning)]
        return release(context, warning)

    return [
        RegisterEventHandler(OnProcessIO(target_action=recorder, on_stdout=stdout)),
        RegisterEventHandler(OnProcessExit(target_action=recorder, on_exit=exited)),
        recorder,
        TimerAction(period=10.0, actions=[OpaqueFunction(function=lambda context: release(
            context, 'Resource recorder not ready after 10s; '
            'starting robot without measurement guarantee.'))]),
    ]
