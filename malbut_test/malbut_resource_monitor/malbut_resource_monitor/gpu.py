"""Optional read-only jtop client; never adjust clocks, power modes or fans."""

import math
from pathlib import Path
import threading
import time

from .store import slug


def number(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 else None)


def start_ticks(proc, pid):
    try:
        stat = (Path(proc) / str(pid) / 'stat').read_text()
        return int(stat[stat.rindex(')') + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def snapshot(jetson, proc='/proc'):
    """jtop process column 8 is GPU memory in KiB, NOT GPU utilization."""
    system, processes = {}, {}
    for name, gpu in jetson.gpu.items():
        key = 'gpu.' + slug(name)
        load = number(gpu.get('status', {}).get('load'))
        system[key + '_percent'] = load if load is not None and load <= 100 else None
        frequency = number(gpu.get('freq', {}).get('cur'))
        system[key + '_mhz'] = frequency / 1000 if frequency is not None else None
    for row in jetson.processes:
        if not isinstance(row, (list, tuple)) or len(row) < 10:
            continue
        pid, memory = row[0], number(row[8])
        if type(pid) is not int or pid <= 0 or memory is None:
            continue
        start = start_ticks(proc, pid)
        if start is not None:
            # Do not keep a PID-only cache across process exit / PID reuse.
            processes[f'{pid}-{start}'] = {'gpu_memory_mib': memory / 1024}
    return system, processes


class JtopSampler:
    """Observe an existing jtop service without blocking resource collection."""

    def __init__(self, interval, proc='/proc'):
        self.interval, self.proc = interval, proc
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.latest, self.error = None, None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _capture(self, jetson):
        try:
            system, processes = snapshot(jetson, self.proc)
            with self.lock:
                self.latest = (time.monotonic(), system, processes)
                self.error = None
        except Exception as error:
            with self.lock:
                self.error = f'{type(error).__name__}: {error}'

    def _run(self):
        try:
            from jtop import jtop
            # Only use the read API. No restore(), clocks, fan or power setters.
            with jtop(interval=self.interval) as jetson:
                jetson.attach(self._capture)
                while not self.stop.wait(self.interval):
                    if not jetson.ok(spin=False):
                        raise RuntimeError('jtop connection stopped')
                jetson.detach(self._capture)
        except Exception as error:
            with self.lock:
                self.error = f'{type(error).__name__}: {error}'

    def sample(self):
        with self.lock:
            latest, error = self.latest, self.error
        age = None if latest is None else time.monotonic() - latest[0]
        usable = error is None and age is not None and age <= max(3, 3 * self.interval)
        return ({**(latest[1] if usable else {}), 'jtop_available': usable,
                 'jtop_age_s': age, 'jtop_error': error}, latest[2] if usable else {})

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
