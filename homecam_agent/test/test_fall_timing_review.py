"""Timing review presentation checks, not proof of visual label correctness."""
import importlib
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def timing(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    return importlib.import_module('review_fall_timing')


@pytest.fixture
def cases():
    draft = json.loads((ROOT / 'evaluations/synthetic_fall_v1/'
                       'spatial_temporal_draft.json').read_text())
    return {c['case_id']: c for c in draft['cases']}


@pytest.mark.parametrize('cid', ['SYN003', 'SYN012', 'SYN016'])
def test_existing_down_has_no_fabricated_fall_time(timing, cases, cid):
    rows = timing.timing_rows(cases[cid], 12)
    assert [r['frames'] for r in rows] == [None, None, [0, 0]]
    assert rows[0]['text'] == '영상에 없음(촬영 전)'
    assert rows[2]['text'] == '0초'


@pytest.mark.parametrize('cid', ['SYN007', 'SYN009', 'SYN011',
                                 'SYN014', 'SYN019', 'SYN020'])
def test_occluded_contact_stays_unknown(timing, cases, cid):
    row = timing.timing_rows(cases[cid], 12)[1]
    assert row['frames'] is None
    assert '확인 불가' in row['text']


def test_range_is_preserved_not_replaced_with_midpoint(timing, cases):
    rows = timing.timing_rows(cases['SYN001'], 12)
    assert rows[0]['frames'] == [30, 46]
    assert '2.5–3.8333초 사이' == rows[0]['text']


def test_new_307_contact_precedes_later_torso_recline(cases):
    case = cases['SYN004']
    assert case['onset_frames'] == [8, 10]
    assert case['landing_frames'] == [10, 13]
    assert case['first_down_frames'] == [12, 15]


def test_review_html_is_offline_and_escapes_notes(timing):
    malicious = '</script><img src=x onerror=alert(1)>'
    record = dict(title=malicious, target='P01', notes=malicious, fps=12,
                  times=[dict(key='onset', label='동작 시작', frames=None, text='촬영 전')],
                  frames=['a'], sheet='b')
    page = timing.html_document([record])
    assert malicious not in page
    match = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                      page, re.S)
    assert json.loads(match.group(1))[0]['notes'] == malicious
    assert 'fetch(' not in page and '<script src=' not in page
    assert 't.frames===null' in page
    assert 'String.fromCharCode(10)' in page


def test_export_does_not_overwrite_existing_folder(timing, tmp_path):
    with pytest.raises(ValueError, match='output already exists'):
        timing.export(tmp_path, tmp_path / 'missing.json', tmp_path)


def test_missing_font_fails_before_creating_output(timing, tmp_path):
    output = tmp_path / 'new'
    with pytest.raises(ValueError, match='Korean font missing'):
        timing.export(tmp_path, tmp_path / 'missing.json', output, tmp_path / 'missing.ttf')
    assert not output.exists()
