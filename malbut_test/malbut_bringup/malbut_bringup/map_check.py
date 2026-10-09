"""
Judge how well the robot's pose fits each saved map it switches to (목업 24번).

The manager finds the pose after every map switch (saved pose first, then a
search while rotating) and names the share of the LiDAR scan that lands on
the map. Below MATCH_MIN the robot searches once more on its own; a still
poor match is reported to the web, which asks the user to check before
sending the robot anywhere. Each switch gets one automatic retry at most.
"""

from pathlib import Path
import re

import yaml


# Measured 2026-10-08 at home: 87% where destinations worked, 45-58% where the
# robot was misplaced. relocalization_node accepts 50%, too low to trust.
MATCH_MIN = 0.75
# The retry is a manager mission; it shows up there shortly after it is sent.
RETRY_APPEAR_S = 10.0
PERCENT = re.compile(r'(\d{1,3})% of the scan')
# A resident Manager keeps this state while Bringup is off; it is not a result.
STANDBY = 'robot runtime is stopped'
# Background work the resident Manager runs; it does not hold the base.
BACKGROUND = frozenset({'device_operation', 'get_weather', 'set_weather_location'})


def message_match(message):
    """Read the last 'N% of the scan' the manager's localization message names."""
    found = PERCENT.findall(message or '')
    return int(found[-1]) / 100 if found else None


def result_match(record):
    """Return (success, match ratio or None) from a relocalize mission result."""
    try:
        result = yaml.safe_load(record.get('result_yaml') or '')
    except yaml.YAMLError:
        result = None
    if not isinstance(result, dict):
        return False, message_match(record.get('message'))
    ratio = result.get('match_ratio')
    ratio = float(ratio) if type(ratio) in (int, float) else message_match(result.get('message'))
    return result.get('success') is True, ratio


# Startup checks that only a found pose can satisfy (readiness.py wording).
POSE_WAITS = ('TF:map->', 'TF:scan->map@')


def can_relocalize(status):
    """
    Ready, or waiting only for the map pose a relocalization would give.

    A pose search that failed at startup used to wait for full readiness, which
    itself needs that pose, so the automatic retry never ran (2026-10-09).
    """
    if status.get('ready'):
        return True
    waiting = status.get('waiting') or []
    return (status.get('state') == 'RUNNING' and bool(waiting)
            and all(isinstance(item, str) and item.startswith(POSE_WAITS) for item in waiting))


def _relocalizing(system):
    missions = [*(system.get('active_foreground_missions') or []),
                *(system.get('pending_missions') or [])]
    return any(isinstance(item, dict) and item.get('capability_id') == 'relocalize'
               for item in missions)


def _busy(system):
    return bool(system.get('active_foreground_missions')) or any(
        not isinstance(item, dict) or item.get('capability_id') not in BACKGROUND
        for item in system.get('pending_missions') or [])


class MapCheck:
    """Follow one Bringup's map switches; ask for at most one retry per switch."""

    def __init__(self):
        self.started()

    def started(self, auto_map=None):
        """Follow a new Bringup; auto_map names the last map it loaded by itself."""
        self.auto_map = auto_map
        # Until the first switch ends, a blank SWITCHING is that map loading.
        self.settled = False
        self.reset()

    def chosen(self):
        """Forget the automatic load once the user picks a map."""
        self.auto_map = None

    def reset(self):
        self.transition = None
        self.match = None
        self.good = None
        self.retry = None
        self.retried = False
        self.seen = set()

    def update(self, now, *, running, ready, localization, saved, system, results):
        """
        Return the web view and whether to send the automatic retry now.

        ``saved`` tells whether the localization map is a saved map (not the
        blank default map). ``results`` are the manager's recent mission results.
        """
        if not running:
            self.reset()
            return {'phase': None}, False
        mode = localization.get('mode')
        if mode == 'ERROR' and localization.get('message') == STANDBY:
            mode = None
        name = Path(localization['map']).name if localization.get('map') else None
        if mode in (None, 'SWITCHING'):
            loading = (self.auto_map is not None and not self.settled
                       and name in (None, self.auto_map))
            if loading or saved:
                return {'phase': 'loading' if loading else 'locating',
                        'map': name or self.auto_map, 'auto': loading}, False
            return {'phase': None}, False
        self.settled = True
        if mode != 'LOCALIZATION':
            return {'phase': None}, False
        if not saved:
            return {'phase': 'none'}, False
        key = (localization.get('runtime_id'), localization.get('transition_id'))
        if key != self.transition:
            self.reset()
            self.transition = key
            self.seen = {item.get('mission_id') for item in results}
            self.match = message_match(localization.get('message'))
            self._judge(localization.get('pose_ready') is True)
            self.retry = None if self.good else 'due'
        for item in results:
            if item.get('mission_id') in self.seen or item.get('capability_id') != 'relocalize':
                continue
            self.seen.add(item.get('mission_id'))
            if isinstance(self.retry, tuple):
                self.retry, self.retried = 'done', True
            if item.get('state') == 'SUCCEEDED' or item.get('state') == 'ABORTED':
                success, ratio = result_match(item)
                self.match = ratio if ratio is not None else self.match
                self._judge(success)
        relocalizing = _relocalizing(system)
        send = False
        if self.retry == 'due' and ready:
            if _busy(system):
                self.retry = 'done'  # The user already started something; only warn.
            else:
                self.retry, send = ('sent', now), True
        elif (isinstance(self.retry, tuple) and not relocalizing
              and now - self.retry[1] > RETRY_APPEAR_S):
            self.retry, self.retried = 'done', True  # Rejected or lost; only warn.
        phase = ('retrying' if relocalizing or send or isinstance(self.retry, tuple)
                 else 'ok' if self.good else 'low')
        match = None if self.match is None else round(self.match, 2)
        return {'phase': phase, 'map': name, 'match': match,
                'auto': name == self.auto_map, 'retried': self.retried}, send

    def _judge(self, pose_ready):
        self.good = pose_ready and (self.match is None or self.match >= MATCH_MIN)
