"""Offline checks for experiment frame ownership and mark-to-source conversion."""

import copy
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/patrol_method_comparison.py'
SPEC = importlib.util.spec_from_file_location('patrol_method_comparison', SCRIPT)
comparison = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comparison)


def plan(method):
    return {'method': method, 'frames': [
        {'frame_id': 'F01', 'observation_id': 'a',
         'marks': [{'id': 1, 'bbox_1000': [10, 20, 30, 40]}],
         'tile': {'photo_rect': [0, 0, 100, 100]},
         'grid': {'width': 200, 'height': 100}},
        {'frame_id': 'F02', 'observation_id': 'b',
         'marks': [{'id': 1, 'bbox_1000': [500, 600, 700, 800]}],
         'tile': {'photo_rect': [100, 0, 200, 100]},
         'grid': {'width': 200, 'height': 100}},
    ]}


def reply(key, value):
    return {'frames': [dict(frame_id=f'F{n:02d}', summary='관찰', uncertainties=[], objects=[
        dict(name='물건', location='앞', confidence='uncertain', **{key: value})])
                       for n in (1, 2)]}


def test_mark_numbers_are_scoped_to_each_photo():
    raw = reply('mark_id', 1)
    before = copy.deepcopy(raw)
    results, audit = comparison.normalize(raw, plan('som24'))
    assert results['a']['objects'][0]['image_bbox'] == [10, 20, 30, 40]
    assert results['b']['objects'][0]['image_bbox'] == [500, 600, 700, 800]
    assert all(item['valid'] for item in audit)
    assert raw == before


@pytest.mark.parametrize('mark,unknown', [(None, False), (2, True), (True, True)])
def test_unavailable_mark_does_not_borrow_a_box(mark, unknown):
    results, audit = comparison.normalize(reply('mark_id', mark), plan('som24'))
    assert all(row['objects'][0]['image_bbox'] is None for row in results.values())
    assert all(item['unknown_id'] == unknown for item in audit)


def test_grid_box_outside_its_frame_is_not_assigned_depth():
    results, _ = comparison.normalize(reply('sheet_bbox', [10, 20, 400, 800]), plan('grid6'))
    assert results['a']['objects'][0]['image_bbox'] == [20, 20, 800, 800]
    assert results['b']['objects'][0]['image_bbox'] is None


@pytest.mark.parametrize('bad', ['duplicate', 'missing', 'unknown'])
def test_frame_identity_is_exact(bad):
    raw = reply('image_bbox', None)
    if bad == 'missing':
        raw['frames'].pop()
    else:
        raw['frames'][1]['frame_id'] = 'F01' if bad == 'duplicate' else 'F99'
    with pytest.raises(ValueError):
        comparison.normalize(raw, plan('separate24'))


def test_cost_partitions_read_write_and_uncached_tokens():
    usage = dict(input_tokens=12000, output_tokens=1000, input_tokens_details=dict(
        cached_tokens=4000, cache_write_tokens=7000,
        cache_write_tokens_details=dict(ttl_30m_tokens=3000, ttl_12h_tokens=4000)))
    assert comparison.standard_cost(usage) == pytest.approx(.00423)
    usage['input_tokens_details'] = dict(cached_tokens=0, cache_write_tokens=11997)
    assert comparison.standard_cost(usage) == pytest.approx(.00419985)
