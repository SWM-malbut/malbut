"""The one fall check's local log (SWM25-261, 2026-10-18): a day file per local date,
seven days kept, a size cap, never an exception. Unit tests, not robot checks."""

import json
import time

from malbut_agent_server.application.fall_flow_log import FallFlowLog

DAY = 86400


class Wall:
    def __init__(self):
        self.value = time.mktime((2026, 10, 18, 12, 0, 0, 0, 0, -1))

    def __call__(self):
        return self.value


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_each_message_is_one_line_in_the_days_file(tmp_path):
    wall = Wall()
    log = FallFlowLog(tmp_path / 'log', wall_clock=wall)
    log.write('to_rules', dict(kind='fall_sighting', sighting=dict(point=[1.2, 0.5], floor=0.21)))
    log.write('from_rules', dict(action='link', sighting_id='s-1', incident_id='i-1'))
    row1, row2 = lines(tmp_path / 'log' / '2026-10-18.jsonl')
    assert row1['dir'] == 'to_rules' and row1['sighting'] == dict(point=[1.2, 0.5], floor=0.21)
    assert row2['dir'] == 'from_rules' and row2['action'] == 'link' and row2['at'] == wall.value


def test_days_older_than_seven_are_removed_when_a_new_day_starts(tmp_path):
    wall = Wall()
    log = FallFlowLog(tmp_path, wall_clock=wall)
    for _ in range(9):
        log.write('to_rules', dict(kind='fall_sighting'))
        wall.value += DAY
    log.write('to_rules', dict(kind='fall_sighting'))
    days = sorted(p.stem for p in tmp_path.glob('*.jsonl'))
    assert days[0] == '2026-10-20' and days[-1] == '2026-10-27' and len(days) == 8


def test_a_full_day_file_stops_growing(tmp_path):
    log = FallFlowLog(tmp_path, max_day_bytes=200, wall_clock=Wall())
    for _ in range(20):
        log.write('to_rules', dict(kind='fall_sighting', sighting=dict(track='pose:1:a')))
    assert (tmp_path / '2026-10-18.jsonl').stat().st_size < 300 and log.dropped > 0


def test_it_never_raises(tmp_path):
    blocked = tmp_path / 'file'
    blocked.write_text('not a directory')
    log = FallFlowLog(blocked / 'log', wall_clock=Wall())
    log.write('to_rules', dict(kind='fall_sighting'))
    log.write('to_rules', dict(value=float('nan')))
    assert log.failures == 2
