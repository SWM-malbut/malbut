"""Standalone collector: monotonic 1 Hz sampling, passive ROS subscriptions."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import threading
import time

from .resources import LinuxSampler, Tegra
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='~/.ros/malbut/resource_logs')
    parser.add_argument('--parent-pid', type=int, default=os.getppid())
    parser.add_argument('--interval', type=float, default=1.0)
    parser.add_argument('--topics', type=Path, default=Path(__file__).with_name('topics.json'))
    parser.add_argument('--no-ros', action='store_true',
                        help='Resource-only baseline, no subscriptions')
    args, ros_args = parser.parse_known_args(argv)
    if not 0.2 <= args.interval <= 60:
        parser.error('interval must be between 0.2 and 60 seconds')
    topics = json.loads(args.topics.read_text())
    if not isinstance(topics, list) or not all(
            isinstance(t, str) and t.startswith('/') for t in topics):
        parser.error('topics must be a JSON array of absolute topic names')
    store = Store(args.root, args.interval, args.parent_pid)
    store.register('system', kind='system', label='Whole robot computer')
    store.register('observer', kind='diagnostic', label='Measurement health (not performance)')
    store.register('tegrastats', kind='raw', label='Original NVIDIA tegrastats')
    print(f'Resource log: {store.path}', flush=True)
    stop = threading.Event()
    reason = 'signal'

    def interrupted(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    tegra, observer, rclpy = None, None, None
    try:
        tegra = Tegra(store, args.interval)
        if tegra.error:
            store.write('observer', {'notice': tegra.error})
        sampler = LinuxSampler(args.parent_pid, store)
        if not args.no_ros:
            import rclpy
            from rclpy.signals import SignalHandlerOptions
            from .ros_observer import Observer
            rclpy.init(args=ros_args, signal_handler_options=SignalHandlerOptions.NO)
            observer = Observer(store, topics)
        store.update_metadata(ros_observation=not args.no_ros, topics=topics,
                              ros_domain_id=os.environ.get('ROS_DOMAIN_ID', '0'),
                              ros_distro=os.environ.get('ROS_DISTRO'),
                              tegrastats_executable=shutil.which('tegrastats'))
        store.write('system', {**sampler.sample(), **tegra.sample()})
        print('MALBUT_RESOURCE_MONITOR_READY', flush=True)
        deadline = time.monotonic() + args.interval
        while not stop.wait(max(0, deadline - time.monotonic())):
            started = time.monotonic()
            if shutil.disk_usage(store.path).free < 256 * 1024 * 1024:
                raise RuntimeError('Less than 256 MiB free; stop recording, robot remains running')
            if observer and observer.error:
                raise RuntimeError('ROS observer stopped: ' + observer.error)
            system = {**sampler.sample(), **tegra.sample()}
            if observer:
                observer.sample()
            system['collection_duration_ms'] = (time.monotonic() - started) * 1000
            system['schedule_lateness_ms'] = max(0, (started - deadline) * 1000)
            store.write('system', system)
            deadline += args.interval
            if deadline < time.monotonic():
                # Never catch up with a burst of stale samples. Record actual intervals.
                deadline = time.monotonic() + args.interval
    except Exception as error:
        reason = f'{type(error).__name__}: {error}'
        print('RESOURCE RECORDING STOPPED: ' + reason, flush=True)
        raise
    finally:
        if observer:
            observer.close()
        if rclpy and rclpy.ok():
            rclpy.shutdown()
        if tegra:
            tegra.close()
        store.close(reason)


if __name__ == '__main__':
    main()
