"""Cloud comparison: original inputs, strict scoring, free-account gate. No network."""
import copy
import io
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import replay_fall84_cloud_pair as cp
import run_free_cloud_fall_suite as cloud
from replay_vlm_frames import digest, request_payload


@pytest.mark.parametrize('thinking', ['disabled', 'unsupported'])
def test_only_model_transport_fields_change(monkeypatch, thinking):
    images, frames = ['frame0'], [dict(frame_index=0, timestamp_s=0, jpeg_sha256='image')]
    c = dict(model=dict(name='gemma4:12b'), thinking='disabled',
             options=dict(temperature=0, seed=0), wire_schema={'original': 'wire'})
    a = request_payload('gemma4:12b', images, frames, 1., c['options'],
                        schema_in_prompt=True, evaluation_version='v2')
    a.update(format=c['wire_schema'], think=False, keep_alive='10m')
    original = dict(frames=frames, media_sha256='video', available_through_frame=0,
                    duration_s=1., contract_sha256=digest(c), request_sha256=digest(a))
    monkeypatch.setattr(cp.pair.suite, 'extract_prefix', lambda *a: (images, frames))
    b = cp.rebuild(Path('.'), dict(sha256='video'), original, c, thinking)
    expected = copy.deepcopy(a)
    expected.update(model=cp.MODEL)
    del expected['format']
    del expected['keep_alive']
    if thinking == 'unsupported':
        expected.pop('think')
    assert b == expected
    assert b['messages'] == a['messages']
    assert b == cloud.cloud_payload(cp.MODEL, images, frames, 1., c['options'], thinking, 'v2')
    original['request_sha256'] = 'broken'
    with pytest.raises(ValueError, match='original A request changed'):
        cp.rebuild(Path('.'), dict(sha256='video'), original, c, thinking)


@pytest.mark.parametrize('plan', ['pro', 'max', None])
def test_nonfree_account_stops_before_media_and_chat(tmp_path, monkeypatch, plan):
    monkeypatch.setattr(cloud, 'api', lambda *a, **k: dict(plan=plan))
    monkeypatch.setattr(cloud, 'extract_prefix', lambda *a: pytest.fail('must not extract/send'))
    with pytest.raises(cloud.FreeAccessStopped):
        cloud.invoke(SimpleNamespace(no_paid_balance_confirmed=True, endpoint='unused'), tmp_path,
                     {}, dict(case_id='SYN001'), 0)
    assert not list(tmp_path.iterdir())


def test_credit_confirmation_required_before_account_api(monkeypatch):
    monkeypatch.setattr(cloud, 'api', lambda *a, **k: pytest.fail('no confirmation'))
    with pytest.raises(ValueError, match='user must confirm'):
        cloud.free_account(SimpleNamespace(no_paid_balance_confirmed=False))


@pytest.mark.parametrize('status', [401, 402, 403, 429])
def test_auth_payment_quota_no_retry_and_failure_preserved(tmp_path, monkeypatch, status):
    calls = []
    def api(endpoint, path, payload, **kw):
        calls.append(path)
        if path == '/api/me':
            return dict(plan='free')
        raise HTTPError('http://127.0.0.1/api/chat', status, 'stopped', {}, io.BytesIO(b'quota'))
    monkeypatch.setattr(cloud, 'api', api)
    monkeypatch.setattr(cloud, 'extract_prefix', lambda *a: ([], []))
    c = dict(model=dict(name=cp.MODEL), mode='gated', options={}, thinking='disabled',
             evaluation_version='v2', timeout_s=300)
    a = SimpleNamespace(no_paid_balance_confirmed=True, endpoint='unused', dataset=tmp_path)
    with pytest.raises(cloud.FreeAccessStopped):
        cloud.invoke(a, tmp_path, c, dict(case_id='SYN001', fps=24, sha256='video'), 0)
    assert calls == ['/api/me', '/api/chat']
    r = cp.read(tmp_path / 'SYN001.result.json')
    assert r['status'] == 'request_failed' and not r['valid'] and r['http_status'] == status
    assert not (tmp_path / 'SYN001.response.json').exists()


def test_text_only_metadata_refused(monkeypatch):
    monkeypatch.setattr(cp, 'api', lambda *a, **k: dict(capabilities=['completion']))
    with pytest.raises(ValueError, match='lacks vision'):
        cp.cloud_identity('unused')
