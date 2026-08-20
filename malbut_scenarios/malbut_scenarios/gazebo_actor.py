"""Own one scripted Gazebo actor without allowing duplicate entities."""

from pathlib import Path
import subprocess
import time
from typing import Callable, Sequence


RunCommand = Callable[..., subprocess.CompletedProcess]


class GazeboActorController:
    """Create and remove one named actor through Gazebo Transport services."""

    def __init__(
        self,
        *,
        world: str,
        entity_name: str,
        actor_file: Path,
        spawn_helper: Path,
        service_prefix: str,
        x: float,
        y: float,
        z: float,
        yaw: float,
        timeout_s: float = 10.0,
        runner: RunCommand = subprocess.run,
    ) -> None:
        if not world or not entity_name or not service_prefix:
            raise ValueError(
                'world, entity name, and service prefix are required'
            )
        if timeout_s <= 0.0:
            raise ValueError('actor operation timeout must be positive')
        self.world = world
        self.entity_name = entity_name
        self.actor_file = Path(actor_file)
        self.spawn_helper = Path(spawn_helper)
        self.service_prefix = service_prefix.rstrip('/')
        self.pose = (x, y, z, yaw)
        self.timeout_s = timeout_s
        self._run = runner

    @property
    def _exists_service(self) -> str:
        return f'{self.service_prefix}/exists'

    @property
    def _remove_service(self) -> str:
        return f'{self.service_prefix}/remove'

    def _completed(self, command: Sequence[str], timeout: float):
        return self._run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def exists(self) -> bool:
        """Read actor presence from the world system's live ECM state."""
        result = self._completed(
            [
                'ign',
                'service',
                '-s',
                self._exists_service,
                '--reqtype',
                'ignition.msgs.Empty',
                '--reptype',
                'ignition.msgs.Boolean',
                '--timeout',
                str(int(self.timeout_s * 1000)),
                '--req',
                '',
            ],
            self.timeout_s + 1.0,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f'could not inspect Gazebo actor: {detail}')
        return 'data: true' in result.stdout

    def _wait_for_presence(self, expected: bool) -> None:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if self.exists() is expected:
                return
            time.sleep(0.1)
        state = 'appear' if expected else 'disappear'
        raise RuntimeError(
            f'{self.entity_name} did not {state} within {self.timeout_s:.1f}s'
        )

    def remove(self) -> bool:
        """Remove the actor and verify that no entity with its name remains."""
        if not self.exists():
            return False
        result = self._completed(
            [
                'ign',
                'service',
                '-s',
                self._remove_service,
                '--reqtype',
                'ignition.msgs.Empty',
                '--reptype',
                'ignition.msgs.Boolean',
                '--timeout',
                str(int(self.timeout_s * 1000)),
                '--req',
                '',
            ],
            self.timeout_s + 1.0,
        )
        if result.returncode != 0 or 'data: true' not in result.stdout:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f'Gazebo rejected actor removal: {detail}')
        self._wait_for_presence(False)
        return True

    def spawn(self) -> None:
        """Remove any stale copy, create one actor, and verify its presence."""
        self.remove()
        x, y, z, yaw = self.pose
        result = self._completed(
            [
                str(self.spawn_helper),
                '--world',
                self.world,
                '--entity-name',
                self.entity_name,
                '--file',
                str(self.actor_file),
                '--align-actor-script',
                '--x',
                str(x),
                '--y',
                str(y),
                '--z',
                str(z),
                '--yaw',
                str(yaw),
                '--timeout',
                str(self.timeout_s),
            ],
            self.timeout_s + 2.0,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f'actor spawn failed: {detail}')
        self._wait_for_presence(True)
