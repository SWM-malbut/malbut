"""Linux /proc and Jetson tegrastats measurements, without root privileges."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time


def read(path):
    try:
        return Path(path).read_text()
    except (OSError, UnicodeError):
        return ''


def kilobytes(text):
    return {key: int(value) * 1024 for key, value in
            re.findall(r'^(\w+):\s+(\d+)\s+kB', text, re.M)}


def cpu_ticks(text):
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r'cpu\d*', parts[0]):
            ticks = [int(v) for v in parts[1:9]]  # guest already included in user/nice
            result[parts[0]] = (sum(ticks), ticks[3] + ticks[4])
    return result


def cpu_percent(current, previous):
    if previous is None:
        return None
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    return 100 * (total - idle) / total if total > 0 and 0 <= idle <= total else None


def parse_tegrastats(line):
    """Numbers retain NVIDIA's units: MHz, percent, degrees C and mW."""
    out = {'gpu_percent': None, 'emc_percent': None, 'emc_mhz': None}
    for token, key in (('GR3D_FREQ', 'gpu_percent'), ('EMC_FREQ', 'emc_percent')):
        match = re.search(r'\b' + token + r'\s+(\d+(?:\.\d+)?)%', line)
        if match:
            out[key] = float(match[1])
    match = re.search(r'EMC_FREQ\s+\d+(?:\.\d+)?%@([\d.]+)', line)
    if match:
        out['emc_mhz'] = float(match[1])
    match = re.search(r'GR3D_FREQ\s+\d+(?:\.\d+)?%@(?:\[([\d.,]+)\]|([\d.]+))', line)
    if match:
        for i, value in enumerate((match[1] or match[2]).split(',')):
            out[f'gpu{i}_mhz'] = float(value)
    for name, value in re.findall(r'([\w-]+)@(-?[\d.]+)C\b', line):
        out[f'temp.{name}_c'] = float(value)
    for name, now, unit, average, avg_unit in re.findall(
            r'\b((?:VDD|VIN|POM)_[\w]+)\s+(\d+(?:\.\d+)?)(mW|W)?/'
            r'(\d+(?:\.\d+)?)(mW|W)?\b', line):
        # The documented suffix-free legacy representation is also milliwatts.
        out[f'power.{name}_mw'] = float(now) * (1000 if unit == 'W' else 1)
        out[f'power_avg.{name}_mw'] = float(average) * (1000 if avg_unit == 'W' else 1)
    return out


class Tegra:
    def __init__(self, store, interval):
        self.store, self.interval = store, interval
        self.lock = threading.Lock()
        self.latest = None
        self.process = None
        self.thread = None
        self.error = None
        executable = shutil.which('tegrastats')
        if not executable:
            self.error = 'tegrastats not installed/visible; Jetson GPU/EMC/power unavailable'
            return
        try:
            self.process = subprocess.Popen(
                [executable, '--interval', str(max(100, int(interval * 1000)))],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                start_new_session=True,
            )
            self.thread = threading.Thread(target=self._read, daemon=True)
            self.thread.start()
        except OSError as error:
            self.error = str(error)

    def _read(self):
        try:
            for line in self.process.stdout:
                with self.lock:
                    self.latest = (time.monotonic(), parse_tegrastats(line))
                self.store.write('tegrastats', {'raw': line.strip()})
        except (OSError, ValueError) as error:
            self.error = str(error)

    def sample(self):
        with self.lock:
            latest = self.latest
        age = None if latest is None else time.monotonic() - latest[0]
        usable = age is not None and age <= max(3, 3 * self.interval)
        return {
            **(latest[1] if usable else {
                'gpu_percent': None, 'emc_percent': None, 'emc_mhz': None}),
            'tegrastats_age_s': age,
            'tegrastats_available': usable,
        }

    def close(self):
        if self.process:
            # Never `tegrastats --stop`: it would affect somebody else's capture.
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.thread.join(timeout=2)
            self.process.stdout.close()


GROUPS = {
    'perception': ('yolo_node', 'tracking_node', 'debug_node', 'detect_3d_node',
                   'person_localizer', 'person_reidentifier'),
    'follow_person': ('person_follower', 'lidar_foreground_preprocessor'),
    'patrol': ('patrol_manager',),
    'autoslam': ('autoslam', 'map_saver_server'),
    'localization': ('relocalization', 'zone_filter', 'sync_slam_toolbox_node',
                     'async_slam_toolbox_node'),
    'manager': ('system_manager', 'manual_control'),
    'speech': ('stt', 'stt_node', 'tts_node', 'tts_receiver',
               'agent_communication', 'speech_runtime'),
    'web_media': ('homecam_media_agent_node', 'robot_cloud_sync', 'robot_web_panel'),
    'fall': ('malbut-fall-monitor', 'fall_coordinator', 'homecam_detector_node'),
    'observer': ('resource_recorder', 'resource_viewer', 'tegrastats'),
}


def classify(argv, pid, collector_pid):
    # Inspect only executable/script/module and ROS node name, not arbitrary options.
    names = {Path(arg).name for arg in argv[:2]}
    names.update(Path(arg).stem for arg in argv[:2] if arg.endswith('.py'))
    names.update(arg.split(':=', 1)[1] for arg in argv if arg.startswith('__node:='))
    if pid == collector_pid or any('malbut_resource_monitor' in a for a in argv[:3]):
        return 'observer'
    if any('component_container' in name for name in names):
        return 'nav2_shared' if 'nav2_container' in names else 'components_shared'
    for group, candidates in GROUPS.items():
        if names.intersection(candidates):
            return group
    for arg in argv[:3]:
        if 'malbut_stt' in arg or 'malbut_tts' in arg or 'malbut_agent_server' in arg:
            return 'speech'
    return 'other_robot'


class LinuxSampler:
    def __init__(self, parent_pid, store, proc='/proc', sys='/sys'):
        self.parent_pid, self.store = parent_pid, store
        self.proc, self.sys = Path(proc), Path(sys)
        self.last_cpu, self.last_proc = {}, {}
        self.tracked = set()  # (pid, start ticks); retain reparented children, not reused PIDs
        self.hz = os.sysconf('SC_CLK_TCK')
        self.last_time = None

    def sample(self):
        now = time.monotonic()
        dt = None if self.last_time is None else now - self.last_time
        ticks = cpu_ticks(read(self.proc / 'stat'))
        memory = kilobytes(read(self.proc / 'meminfo'))
        system = {'cpu_percent': cpu_percent(ticks['cpu'], self.last_cpu.get('cpu'))
                  if 'cpu' in ticks else None}
        for core, value in ticks.items():
            if core != 'cpu':
                system[f'{core}_percent'] = cpu_percent(value, self.last_cpu.get(core))
                mhz = read(self.sys / f'devices/system/cpu/{core}/cpufreq/scaling_cur_freq').strip()
                system[f'{core}_mhz'] = float(mhz) / 1000 if mhz.isdigit() else None
        for key, value in memory.items():
            if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                system[f'{key}_mib'] = value / 1048576
        for total, free, name in (('MemTotal', 'MemAvailable', 'ram_used_mib'),
                                  ('SwapTotal', 'SwapFree', 'swap_used_mib')):
            system[name] = ((memory[total] - memory[free]) / 1048576
                            if total in memory and free in memory else None)
        for zone in (self.sys / 'class/thermal').glob('thermal_zone*'):
            value = read(zone / 'temp').strip()
            label = read(zone / 'type').strip() or zone.name
            try:
                system[f'temp.{label}.{zone.name}_c'] = float(value) / 1000
            except ValueError:
                pass
        found = {}
        for directory in self.proc.iterdir():
            if not directory.name.isdigit():
                continue
            text = read(directory / 'stat')
            try:
                fields = text[text.rindex(')') + 2:].split()
                found[int(directory.name)] = (
                    int(fields[1]), int(fields[19]), int(fields[11]) + int(fields[12]), fields[0])
            except (ValueError, IndexError):
                continue
        selected = {pid for pid, (_, start, _, _) in found.items()
                    if (pid, start) in self.tracked or pid in (self.parent_pid, os.getpid())}
        # Include Malbut services started outside this launch (e.g. cloud bridge).
        commands = {}
        for pid in found:
            try:
                argv = (self.proc / str(pid) / 'cmdline').read_bytes().decode(
                    'utf-8', errors='replace').strip('\0').split('\0')
            except OSError:
                continue
            commands[pid] = argv
            if any('/malbut' in arg or '/homecam_agent/' in arg for arg in argv[:2]):
                selected.add(pid)
        changed = True
        while changed:
            children = {pid for pid, (ppid, _, _, _) in found.items() if ppid in selected}
            changed = not children.issubset(selected)
            selected.update(children)
        current = {}
        for pid in sorted(selected):
            ppid, start, used, state = found[pid]
            argv = commands.get(pid, [])
            identity = f'{pid}-{start}'
            previous = self.last_proc.get(identity)
            status = kilobytes(read(self.proc / str(pid) / 'status'))
            group = classify(argv, pid, os.getpid())
            try:
                executable = os.readlink(self.proc / str(pid) / 'exe')
                cwd = os.readlink(self.proc / str(pid) / 'cwd')
            except OSError:
                executable, cwd = None, None
            # No full argv/env: credentials can be embedded in command options.
            entrypoint = argv[0] if argv else None
            if len(argv) > 1 and not argv[1].startswith('-') and (
                    argv[1].endswith('.py') or '/lib/' in argv[1]):
                entrypoint = argv[1]
            if entrypoint and cwd and '/' in entrypoint and not entrypoint.startswith('/'):
                entrypoint = str(Path(cwd) / entrypoint)
            node_names = [arg.partition(':=')[2] for arg in argv
                          if arg.startswith('__node:=')]
            self.store.process(identity, {
                'pid': pid, 'start_ticks': start, 'group': group, 'executable': executable,
                'entrypoint': entrypoint, 'ros_node_names': node_names,
                'shared_process': group in ('nav2_shared', 'components_shared'),
            })
            channel = 'processes/' + group
            self.store.register(channel, kind='process', label=group)
            self.store.write(channel, {
                'identity': identity, 'pid': pid, 'ppid': ppid, 'state': state,
                'cpu_percent': (100 * (used - previous) / self.hz / dt
                                if previous is not None and dt and used >= previous else None),
                'ram_rss_mib': status.get('VmRSS', 0) / 1048576 if 'VmRSS' in status else None,
                'swap_mib': status.get('VmSwap', 0) / 1048576 if 'VmSwap' in status else None,
                'gpu_percent': None,
            })
            current[identity] = used
        self.tracked = {(pid, found[pid][1]) for pid in selected}
        self.last_proc, self.last_cpu, self.last_time = current, ticks, now
        system['sample_window_s'] = dt
        return system
