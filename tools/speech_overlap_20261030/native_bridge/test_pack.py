"""Fake orchestration only; does not run the full raw replay or native timings."""
import importlib.util
import json
from pathlib import Path
import subprocess

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('bridge_pack', HERE/'run.py')
pack = importlib.util.module_from_spec(spec); spec.loader.exec_module(pack)


def setup(monkeypatch):
    monkeypatch.setattr(pack, 'native_environment', lambda: {'fake_only':True})
    monkeypatch.setattr(pack, 'provenance', lambda: {'frozen':'abc'})
    monkeypatch.setattr(pack, 'validate', lambda: {str(i):{'exact':True} for i in range(9)})


def row(arm):
    return dict(arm=arm, frames=960000, cpu_s=1., wall_s=1.1, rss_peak_raw=12345,
                rss_peak_raw_unit='KiB', rss_peak_bytes=12345*1024,
                provenance={'frozen':'abc'}, last_output_sha256='a'*64)


def test_six_fresh_children_only_after_all_parity(monkeypatch, tmp_path):
    setup(monkeypatch); calls=[]
    def child(argv, **kw):
        assert kw['check'] and kw['timeout']==30
        assert kw['env']['OPENBLAS_NUM_THREADS']==kw['env']['OMP_NUM_THREADS']=='1'
        current=json.loads((tmp_path/'out/result.json').read_text())
        assert current['stage']=='timing' and len(current['cases'])==9 and not current['complete']
        calls.append(argv[-1]); return subprocess.CompletedProcess(argv, 0, json.dumps(row(argv[-1])), '')
    monkeypatch.setattr(pack.subprocess, 'run', child)
    pack.main(tmp_path/'out')
    result=json.loads((tmp_path/'out/result.json').read_text())
    assert calls==['original','proposed','proposed','original','original','proposed']
    assert result['complete'] and len(result['measurements'])==6


def test_parity_failure_never_starts_timing(monkeypatch, tmp_path):
    setup(monkeypatch)
    def fail(): raise AssertionError('bridge_float mismatch at512')
    monkeypatch.setattr(pack, 'validate', fail)
    monkeypatch.setattr(pack.subprocess, 'run', lambda *a, **kw:pytest.fail('No timed process allowed'))
    with pytest.raises(AssertionError, match='bridge_float'): pack.main(tmp_path/'out')
    result=json.loads((tmp_path/'out/result.json').read_text())
    assert not result['complete'] and result['stage']=='parity' and not result['measurements']


def test_child_timeout_preserves_incomplete_prior_measurement(monkeypatch, tmp_path):
    setup(monkeypatch); calls=[]
    def child(argv, **kw):
        calls.append(argv[-1])
        if len(calls)==2: raise subprocess.TimeoutExpired(argv, 30, stderr=b'bounded child incomplete')
        return subprocess.CompletedProcess(argv, 0, json.dumps(row(argv[-1])), '')
    monkeypatch.setattr(pack.subprocess, 'run', child)
    with pytest.raises(subprocess.TimeoutExpired): pack.main(tmp_path/'out')
    result=json.loads((tmp_path/'out/result.json').read_text())
    assert not result['complete'] and len(result['measurements'])==1
    assert (tmp_path/'out/child-error.txt').read_text()=='bounded child incomplete'


def test_wrong_child_provenance_cannot_complete(monkeypatch, tmp_path):
    setup(monkeypatch)
    def child(argv, **kw):
        value=row(argv[-1]); value['provenance']={'wrong':'abc'}
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), '')
    monkeypatch.setattr(pack.subprocess, 'run', child)
    with pytest.raises(AssertionError): pack.main(tmp_path/'out')
    assert not json.loads((tmp_path/'out/result.json').read_text())['complete']


def test_timed_linux_rss_units_on_one_fake_block(monkeypatch):
    # This small fake calculation validates only measurement units/shape, not DSP.
    monkeypatch.setattr(pack, 'raw_input', lambda:np.zeros((512,6),'<i2'))
    class Phase:
        def prepare(self, proc, raw, start): return proc, raw
        def written(self, frame): pass
    monkeypatch.setattr(pack, 'phase', lambda *args:(Phase(), object(), np.zeros(384,'f4')))
    monkeypatch.setattr(pack, 'original_step', lambda *args:np.zeros((512,2),'<f4'))
    monkeypatch.setattr(pack.resource, 'getrusage', lambda *args:type('R',(),{'ru_maxrss':789})())
    result=pack.timed('original')
    assert result['rss_peak_raw_unit']=='KiB' and result['rss_peak_bytes']==789*1024
    assert result['frames']==512 and result['cpu_s']>=0 and result['wall_s']>=0
