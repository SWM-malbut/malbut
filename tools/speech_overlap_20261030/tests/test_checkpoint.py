"""Small offline reconstruction failures; no candidate runtime is imported."""
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
import copy

import pytest

PATH = Path(__file__).resolve().parents[1] / 'check_checkpoint.py'
SPEC = importlib.util.spec_from_file_location('new_checkpoint', PATH)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


@pytest.fixture
def sample(tmp_path):
    old = SimpleNamespace(HERE=tmp_path / 'old', REPO=tmp_path / 'repo')
    repository = old.REPO / 'module/a.py'
    overlay = old.HERE / 'runtime19/code/b.py'
    entry = old.HERE / 'runtime19/start.sh'
    for path, content in ((repository, 'before\n'), (overlay, 'overlay\n'), (entry, 'entry\n')):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    manifest = {'code_files': [
        {'path': 'a.py', 'source': 'repository', 'repository_path': 'module/a.py'},
        {'path': 'b.py', 'source': 'overlay'}]}
    base = {name: CHECK.digest(path) for name, path in
            [('code/a.py', repository), ('code/b.py', overlay), ('start.sh', entry)]}
    after = tmp_path / 'after'; after.write_text('after\n')
    snapshot = {'base_files': base, 'candidate_files': dict(base, **{'code/a.py': CHECK.digest(after)})}
    patch = tmp_path / 'change.patch'
    patch.write_text('diff --git a/code/a.py b/code/a.py\n--- a/code/a.py\n+++ b/code/a.py\n@@ -1 +1 @@\n-before\n+after\n')
    target = tmp_path / 'target'; target.mkdir()
    return target, old, manifest, snapshot, patch


def test_reconstructs_repository_overlay_entry_and_patch(sample):
    CHECK.reconstruct(*sample)
    CHECK.exact_tree(sample[0], sample[3]['candidate_files'])


def test_corrupted_patch_is_rejected(sample):
    sample[4].write_text('invalid patch\n')
    with pytest.raises(subprocess.CalledProcessError):
        CHECK.reconstruct(*sample)


def test_missing_source_is_rejected(sample):
    (sample[1].REPO / 'module/a.py').unlink()
    with pytest.raises(FileNotFoundError):
        CHECK.reconstruct(*sample)


def test_extra_result_file_is_rejected(sample):
    (sample[0] / 'unexpected.py').write_text('extra\n')
    with pytest.raises(ValueError, match='file set'):
        CHECK.reconstruct(*sample)


def test_counts_exclude_invalid_attempts_and_reject_wrong_aggregate():
    result = {'goal_complete': False, 'candidate_deployed': False, 'cohorts': [{
        'id': 'first', 'rows': [
            {'valid': True, 'stopped': True, 'transcript': 'text', 'exact': True},
            {'valid': False, 'stopped': True, 'transcript': 'invalid', 'exact': True}],
        'counts': {'attempts': 2, 'valid': 1, 'stopped': 1, 'transcripts': 1, 'exact': 1}}]}
    CHECK.assess(result)
    wrong = copy.deepcopy(result); wrong['cohorts'][0]['counts']['stopped'] = 2
    with pytest.raises(ValueError, match='Cohort counts'):
        CHECK.assess(wrong)


def test_checkpoint_cannot_claim_adoption():
    with pytest.raises(ValueError, match='accepted deployment'):
        CHECK.assess({'goal_complete': False, 'candidate_deployed': True, 'cohorts': [{}]})
