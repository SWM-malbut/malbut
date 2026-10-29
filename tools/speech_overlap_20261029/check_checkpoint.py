"""Verify the archived code/results; optionally run fake-device tests in a temp tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def checked(path, digest):
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f'Checkpoint hash mismatch: {path}')
    return path


def verify():
    manifest = json.loads((HERE / 'manifest.json').read_text())
    for name, digest in manifest['published_files'].items():
        checked(HERE / name, digest)
    for row in manifest['code_files']:
        path = (HERE / 'runtime19/code' / row['path'] if row['source'] == 'overlay'
                else REPO / row['repository_path'])
        checked(path, row['sha256'])
    for row in manifest['fixture_files']:
        checked(REPO / row['repository_path'], row['sha256'])
    result = json.loads((HERE / 'evidence/summary.json').read_text())
    overlap = [r for r in result['rows'] if r['phase'] == 'overlap' and r['playback_valid']]
    first = [r for r in overlap if not r['retest']]
    counts = {k: sum(r['intent'] == k for r in first) for k in ('pass', 'partial', 'fail')}
    if len(first) != 11 or counts != {'pass': 7, 'partial': 1, 'fail': 3}:
        raise ValueError('First-trial intent denominator or grading changed')
    if len(overlap) != 13 or not all(r['robot_stopped'] for r in overlap):
        raise ValueError('Overlap detection count changed')
    if result['candidate_deployed'] or result['goal_complete']:
        raise ValueError('This checkpoint is not an accepted deployment')
    print(json.dumps(dict(code_files=len(manifest['code_files']),
                          first_trials=len(first), intent=counts, deployed=False)))
    return manifest


def run_tests(manifest):
    with tempfile.TemporaryDirectory(prefix='malbut-overlap-checkpoint-') as directory:
        target = Path(directory)
        for row in manifest['code_files']:
            source = (HERE / 'runtime19/code' / row['path'] if row['source'] == 'overlay'
                      else REPO / row['repository_path'])
            dest = target / 'code' / row['path']
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
        shutil.copytree(HERE / 'tests', target / 'tests')
        fixtures = target / 'fixtures'
        fixtures.mkdir()
        for row in manifest['fixture_files']:
            source = REPO / row['repository_path']
            shutil.copyfile(source, fixtures / source.name)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(target / 'code'), str(fixtures)]),
                   PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable, '-m', 'pytest', '-q', str(target / 'tests')],
                              cwd=target, env=env, check=False).returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test', action='store_true', help='Run offline tests; no SSH or audio devices')
    args = parser.parse_args()
    snapshot = verify()
    if args.test:
        raise SystemExit(run_tests(snapshot))
