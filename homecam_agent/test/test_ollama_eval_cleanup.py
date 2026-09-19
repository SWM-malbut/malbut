"""No actual model deletions: exact target and pre-existing-model safety checks."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cleanup_ollama_eval_models as cleanup  # noqa: E402


def fake_completed(tmp_path, monkeypatch, name='new:2b'):
    root = tmp_path / name.replace(':', '--')
    root.mkdir()
    for mode in ('full', 'gated'):
        out = root / mode
        out.mkdir()
        (out / 'completed.json').write_text('{}')
        (out / 'run.json').write_text(json.dumps(dict(contract=dict(model=dict(
            name=name, digest='a' * 64)))))
    monkeypatch.setattr(cleanup, 'verify_completed', lambda *args: None)
    planned = {name: dict(manifest_sha256='a' * 64)}
    installed = {name: dict(digest='a' * 64, size=100)}
    return root, planned, installed


def test_exact_new_completed_and_unloaded_tag_can_be_removed(tmp_path, monkeypatch):
    args = fake_completed(tmp_path, monkeypatch)
    assert cleanup.eligible_model(*args, set())['name'] == 'new:2b'
    assert cleanup.eligible_model(*args, {'new:2b'}) is None


@pytest.mark.parametrize('name', sorted(cleanup.PROTECTED))
def test_existing_models_are_never_removed(tmp_path, monkeypatch, name):
    args = fake_completed(tmp_path, monkeypatch, name)
    assert cleanup.eligible_model(*args, set()) is None


def test_changed_model_or_partial_run_is_preserved(tmp_path, monkeypatch):
    root, planned, installed = fake_completed(tmp_path, monkeypatch)
    installed['new:2b']['digest'] = 'b' * 64
    assert cleanup.eligible_model(root, planned, installed, set()) is None
    installed['new:2b']['digest'] = 'a' * 64
    (root / 'full' / 'completed.json').unlink()
    assert cleanup.eligible_model(root, planned, installed, set()) is None


def test_unplanned_model_is_preserved(tmp_path, monkeypatch):
    root, _, installed = fake_completed(tmp_path, monkeypatch)
    assert cleanup.eligible_model(root, {}, installed, set()) is None
