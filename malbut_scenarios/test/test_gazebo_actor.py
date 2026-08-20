"""Tests for verified creation and removal of the scenario actor."""

from pathlib import Path
from types import SimpleNamespace

from malbut_gazebo.gazebo_actor import GazeboActorController


class FakeGazeboRunner:
    """Emulate Gazebo scene, create, and remove command responses."""

    def __init__(self, visible: bool = False) -> None:
        self.visible = visible
        self.commands = []

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        joined = ' '.join(command)
        if '/scenario_actor/exists' in joined:
            output = f'data: {str(self.visible).lower()}'
            return SimpleNamespace(returncode=0, stdout=output, stderr='')
        if '/remove' in joined:
            self.visible = False
            return SimpleNamespace(
                returncode=0,
                stdout='data: true',
                stderr='',
            )
        if command[0] == '/tmp/spawn_when_ready':
            self.visible = True
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        raise AssertionError(f'unexpected command: {command}')


def _controller(runner: FakeGazeboRunner) -> GazeboActorController:
    return GazeboActorController(
        world='small_house',
        entity_name='scenario_humanoid',
        actor_file=Path('/tmp/front_door_entry.sdf'),
        spawn_helper=Path('/tmp/spawn_when_ready'),
        service_prefix='/world/small_house/scenario_actor',
        x=6.0,
        y=-6.2,
        z=0.0,
        yaw=0.0,
        timeout_s=1.0,
        runner=runner,
    )


def test_spawn_removes_a_stale_actor_before_creating_exactly_one():
    runner = FakeGazeboRunner(visible=True)
    controller = _controller(runner)

    controller.spawn()

    assert runner.visible is True
    commands = [' '.join(command) for command in runner.commands]
    remove_index = next(
        index for index, command in enumerate(commands) if '/remove' in command
    )
    spawn_index = next(
        index
        for index, command in enumerate(commands)
        if command.startswith('/tmp/spawn_when_ready')
    )
    assert remove_index < spawn_index
    assert '--align-actor-script' in commands[spawn_index]
    assert '--x 6.0 --y -6.2' in commands[spawn_index]


def test_remove_is_idempotent_and_verifies_absence():
    runner = FakeGazeboRunner(visible=True)
    controller = _controller(runner)

    assert controller.remove() is True
    assert controller.remove() is False
    assert runner.visible is False
