"""Free-tier permission checks and protocol; no real provider calls."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_free_cloud_fall_suite as cloud  # noqa: E402


def test_no_inference_or_identity_request_without_billing_confirmation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('must not call API')
    monkeypatch.setattr(cloud, 'api', forbidden)
    with pytest.raises(ValueError, match='user must confirm'):
        cloud.free_account(SimpleNamespace(no_paid_balance_confirmed=False))


@pytest.mark.parametrize('plan', [None, 'pro', 'max', 'team'])
def test_paid_or_unknown_plan_rejected_even_if_user_previously_confirmed(plan, monkeypatch):
    monkeypatch.setattr(cloud, 'api', lambda *a, **k: dict(plan=plan))
    with pytest.raises(cloud.FreeAccessStopped):
        cloud.free_account(SimpleNamespace(no_paid_balance_confirmed=True, endpoint='unused'))


def test_free_account_check_does_not_store_identity(monkeypatch):
    account = dict(plan='free', email='private', id='private')
    monkeypatch.setattr(cloud, 'api', lambda *a, **k: account)
    result = cloud.free_account(SimpleNamespace(no_paid_balance_confirmed=True, endpoint='unused'))
    assert result == dict(plan='free', no_paid_balance_user_confirmed=True)


def test_cloud_uses_same_text_schema_but_does_not_claim_server_enforcement():
    payload = cloud.cloud_payload('gemma4:31b-cloud', ['image'], [dict(timestamp_s=0)],
                                  1.0, dict(temperature=0), 'disabled')
    assert 'format' not in payload
    assert 'JSON Schema' in payload['messages'][1]['content']
    assert payload['think'] is False
    assert payload['messages'][1]['images'] == ['image']


def test_arbitrary_cloud_model_cannot_receive_video():
    with pytest.raises(ValueError, match='explicit evaluation list'):
        cloud.cloud_payload('arbitrary:paid', [], [], 1, {}, 'unsupported')
