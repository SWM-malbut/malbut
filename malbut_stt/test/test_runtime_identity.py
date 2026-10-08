"""Installed source identity is reproducible and never depends on private paths."""

from malbut_stt.runtime_identity import runtime_source_fingerprint, reported_source_revision


def test_fingerprint_covers_relative_python_names_and_bytes(tmp_path):
    first, second = tmp_path / 'a', tmp_path / 'private-home'
    for root in (first, second):
        (root / 'nested').mkdir(parents=True)
        (root / '__init__.py').write_text('')
        (root / 'nested' / 'worker.py').write_text('x = 1\n')
        (root / '.env').write_text('private-key=do-not-hash')
    original = runtime_source_fingerprint(first)
    assert len(original) == 64
    assert runtime_source_fingerprint(second) == original
    (second / 'nested' / 'worker.py').write_text('x = 2\n')
    assert runtime_source_fingerprint(second) != original
    (second / 'nested' / 'worker.py').write_text('x = 1\n')
    (second / 'nested' / 'worker.py').rename(second / 'nested' / 'renamed.py')
    assert runtime_source_fingerprint(second) != original


def test_source_label_requires_exact_full_sha_and_is_not_git_inferred():
    assert reported_source_revision({}) == 'unknown'
    assert reported_source_revision({'MALBUT_SOURCE_SHA': 'A' * 40}) == 'a' * 40
    for bad in ('private/path', 'a' * 7, 'a' * 40 + '\n', 'z' * 40):
        assert reported_source_revision({'MALBUT_SOURCE_SHA': bad}) == 'unknown'
