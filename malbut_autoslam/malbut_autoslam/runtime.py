"""Plan missing mapping components and own only processes started for a goal."""

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import time


DEFAULT_READY_TIMEOUT_S = 30.0
PROCESS_SHUTDOWN_STAGES = ((signal.SIGINT, 4.0), (signal.SIGTERM, 2.0),
                           (signal.SIGKILL, 1.0))
# Parent launch must allow child cancellation plus all process-group shutdown
# stages to complete. One extra second covers the executor/polling handover.
LAUNCH_SHUTDOWN_OVERHEAD_S = sum(timeout for _, timeout in PROCESS_SHUTDOWN_STAGES) + 1.0


@dataclass(frozen=True)
class RuntimeGraph:
    """Describe the relevant ROS graph without treating discovery as readiness."""

    nodes: tuple
    map_publishers: tuple
    scan_publishers: tuple
    odom_publishers: tuple
    normalized_scan_publishers: tuple
    navigation_present: bool


def missing_components(graph):
    """Reject conflicting ownership and return only safely missing components."""
    names = [name.rsplit('/', 1)[-1] for name in graph.nodes]
    if 'amcl' in names or any('map_server' == name for name in names):
        raise RuntimeError(
            'Saved-map localization is running; stop navigation Bringup before AutoSLAM')
    critical = ('slam_toolbox', 'controller_server', 'planner_server', 'bt_navigator')
    if any(names.count(name) > 1 for name in critical):
        raise RuntimeError('Duplicate mapping/navigation nodes; stop duplicate launches first')
    if len(graph.map_publishers) > 1:
        raise RuntimeError('Multiple map publishers; cannot choose a mapping owner safely')
    if any(name.rsplit('/', 1)[-1] != 'slam_toolbox'
           for name in graph.map_publishers):
        raise RuntimeError(
            'Unknown map publisher; use auto_start:=false for an externally managed mapper')
    for label, publishers in (
            ('scan', graph.scan_publishers), ('odometry', graph.odom_publishers),
            ('normalized scan', graph.normalized_scan_publishers)):
        if len(publishers) > 1:
            raise RuntimeError(f'Multiple {label} publishers; resolve duplicate owners first')

    hardware = bool(graph.scan_publishers) and bool(graph.odom_publishers)
    if bool(graph.scan_publishers) != bool(graph.odom_publishers):
        raise RuntimeError(
            'Only part of hardware is running; prepare scan and odometry together '
            'instead of starting duplicate drivers')
    # Factory nodes may have appeared before their first publisher. Do not race
    # them with a second full hardware launch when their sensors are unhealthy.
    hardware_nodes = {'controller', 'odom_publisher', 'ros_robot_controller',
                      'robot_state_publisher'}
    if not hardware and hardware_nodes.intersection(names):
        raise RuntimeError('Hardware nodes exist but scan/odometry are not ready')
    slam = 'slam_toolbox' in names or bool(graph.map_publishers)
    navigation = graph.navigation_present or bool(
        {'controller_server', 'planner_server', 'bt_navigator',
         'nav2_container'}.intersection(names))
    return {
        'start_hardware': not hardware,
        'start_slam': not slam,
        'start_navigation': not navigation,
        # An externally managed SLAM can use its original scan. The adapter is
        # needed only when our helper will introduce an SLAM/Nav2 consumer.
        'start_scan_adapter': (not slam or not navigation)
        and not bool(graph.normalized_scan_publishers),
    }


class OwnedRuntime:
    """Serialize local mapping requests and terminate only an owned process group."""

    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        self.lock_file = None
        self.process = None
        self.log_file = None
        self.log_path = None
        self.components = {}

    def acquire(self):
        """Prevent cooperating AutoSLAM servers from racing their graph snapshots."""
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock_file = (self.directory / 'mapping.lock').open('a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError('Another AutoSLAM server owns mapping startup') from None

    def start(self, components, scan_topic, odom_topic, normalized_scan_topic):
        """Launch a predefined mapping composition; never execute arbitrary shell text."""
        self.components = components
        if not any(components.values()):
            return
        command = ['ros2', 'launch', 'malbut_bringup', 'mapping_backend.launch.py',
                   'use_sim_time:=false', f'scan_topic:={scan_topic}',
                   f'odom_topic:={odom_topic}',
                   f'normalized_scan_topic:={normalized_scan_topic}']
        command.extend(f'{name}:={str(enabled).lower()}'
                       for name, enabled in components.items())
        self.log_path = self.directory / f'mapping-{time.time_ns()}.log'
        self.log_file = self.log_path.open('w')
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=self.log_file,
                stderr=subprocess.STDOUT, start_new_session=True)
        except Exception:
            self.log_file.close()
            self.log_file = None
            raise

    def check(self):
        """Fail readiness or exploration when the owned launch exits unexpectedly."""
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError(
                f'Mapping launch exited ({self.process.returncode}); see {self.log_path}')

    def _group_alive(self):
        if self.process is None:
            return False
        self.process.poll()  # Reap the leader even if a descendant remains alive.
        try:
            os.killpg(self.process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def stop(self):
        """Escalate shutdown within a bounded time, limited to our new session."""
        if self.process is not None:
            for sig, timeout in PROCESS_SHUTDOWN_STAGES:
                if not self._group_alive():
                    break
                try:
                    os.killpg(self.process.pid, sig)
                except ProcessLookupError:
                    break
                deadline = time.monotonic() + timeout
                while self._group_alive() and time.monotonic() < deadline:
                    time.sleep(0.05)
            if self._group_alive():
                raise RuntimeError('Owned mapping processes have not exited; ownership retained')
            self.process = None
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None

    def close(self):
        """Release the mapping lock only after all owned processes have exited."""
        self.stop()
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None
