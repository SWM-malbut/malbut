"""Reconstruct and verify the published source snapshot offline; no devices or models."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / 'speech_overlap_20261029'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exact_tree(root, expected):
    files = [p for p in root.rglob('*') if p.is_file() or p.is_symlink()]
    if any(p.is_symlink() for p in files):
        raise ValueError('Snapshot contains a symlink')
    actual = {p.relative_to(root).as_posix(): digest(p) for p in files}
    if actual != expected:
        raise ValueError('Snapshot file set or SHA-256 mismatch')


def reconstruct(target, previous, manifest, snapshot, patch):
    code = {f"code/{r['path']}": r for r in manifest['code_files']}
    for name in snapshot['base_files']:
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Snapshot path leaves its tree')
        row = code.get(name)
        source = (previous.REPO / row['repository_path'] if row and row['source'] == 'repository'
                  else previous.HERE / 'runtime19' / name)
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    exact_tree(target, snapshot['base_files'])
    subprocess.run(['git', 'apply', '--whitespace=nowarn', str(patch.resolve())],
                   cwd=target, check=True, capture_output=True, text=True)
    exact_tree(target, snapshot['candidate_files'])


def assess(result):
    if result['goal_complete'] or result['candidate_deployed'] or not result['cohorts']:
        raise ValueError('This checkpoint is not an accepted deployment')
    for cohort in result['cohorts']:
        rows = cohort['rows']; valid = [r for r in rows if r['valid']]
        counts = dict(attempts=len(rows), valid=len(valid), stopped=sum(r['stopped'] for r in valid),
                      transcripts=sum(bool(r['transcript']) for r in valid), exact=sum(r['exact'] for r in valid))
        if cohort['counts'] != counts:
            raise ValueError(f"Cohort counts mismatch: {cohort['id']}")


def verify():
    spec = importlib.util.spec_from_file_location('previous_checkpoint', PREVIOUS / 'check_checkpoint.py')
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)
    old_manifest = previous.verify()
    published = json.loads((HERE / 'manifest.json').read_text())
    for name, expected in published['published_files'].items():
        if digest(HERE / name) != expected:
            raise ValueError(f'Published checkpoint hash mismatch: {name}')
    snapshot = json.loads((HERE / 'snapshot-map.json').read_text())
    if tuple(len(snapshot[k]) for k in ('base_files', 'candidate_files', 'excluded_assets')) != (143, 179, 55):
        raise ValueError('Snapshot denominators changed')
    result = json.loads((HERE / 'evidence/summary.json').read_text())
    assess(result)
    with tempfile.TemporaryDirectory(prefix='malbut-runtime39-check-') as directory:
        reconstruct(Path(directory), previous, old_manifest, snapshot, HERE / 'runtime39.patch')
    print(json.dumps(dict(base_files=143, candidate_files=179, excluded_assets=55, cohorts=len(result['cohorts']),
                         public_manifest_sha256=digest(HERE / 'manifest.json'),
                         original_candidate_manifest_reference='bd116e0a1438fe529c89ebe642e00120a53aaec476f928cff6c71efd548948dc',
                         scope='Published source reconstruction; original full asset manifest is reference only')))
    return snapshot


if __name__ == '__main__':
    verify()
