"""Preparation tests use fake media bytes/mock decoding; no inference/providers."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import prepare_fall_evaluation_v2 as prep  # noqa: E402
from replay_fall_baseline import verify_freeze  # noqa: E402
from replay_vlm_frames import check_evaluation_labels  # noqa: E402


def sample(tmp_path, monkeypatch):
    base = tmp_path/'dataset'; base.mkdir()
    old, reviewed, metadata, annotations = [], [], [], []
    for n, label in enumerate(['observed_fall']*14 + ['suspected_fall']*7 + ['normal_activity']*20, 1):
        cid = f'SYN{n:03d}'
        path = base/f'original_s{n}_legacy.mp4'; path.write_bytes(f'original legacy {n}'.encode())
        old.append(dict(case_id=cid, label=label, reason='원래 이유', source_path=path.name,
                        source_sha256=prep.sha(path)))
        reviewed.append(dict(case_id=cid, label=label, reason='원래 이유'))
        metadata.append(dict(case_id=cid, sha256=prep.sha(path), width=10, height=10, fps=12., frames=2))
        annotations.append(dict(case_id=cid, persons=[], onset_frames=None))
    index, additional, excluded = [], [], []
    classes = iter(['observed_fall']*4 + ['suspected_fall']*18 + ['normal_activity']*14)
    for n in range(1, 40):
        video = f'V{n:03d}.mp4'
        path = base/f'original_additional_{n}.mp4'; path.write_bytes(f'original gap {n}'.encode())
        index.append(dict(video=video, path=str(path), sha256=prep.sha(path), scene_id=f'scene{n}'))
        if n in (13, 36, 38):
            excluded.append(dict(video=video, video_sha256=prep.sha(path), reason='품질 문제'))
        else:
            label = next(classes)
            additional.append(dict(video=video, label=prep.NAMES[label], reason='추가 이유'))
            reviewed.append(dict(case_id=f'GAP-V{n:03d}', video=video, label=label, reason='추가 이유',
                                 reason_supplement='사용자 원문', agreed_reason='합의 이유',
                                 judgment_evidence_limited=True, judgment_note='한계 메모'))
    sources = dict(old=dict(cases=old, exclusions=[]), audit=dict(cases=reviewed),
                   additional=additional, index=dict(videos=index), exclusions=dict(videos=excluded),
                   legacy_media=dict(cases=metadata), legacy_labels=dict(annotations=dict(cases=annotations)),
                   base={}, amendment={}, base_bytes=b'{}',
                   amendment_bytes=json.dumps(dict(base_file='scoring_criteria_v2.json',
                       base_sha256=hashlib.sha256(b'{}').hexdigest(), criteria_version=prep.VERSION)).encode())
    protected = {p: prep.sha(p) for p in base.glob('*.mp4')}
    monkeypatch.setattr(prep, 'load_sources', lambda *a: (copy.deepcopy(sources), {}, protected))
    monkeypatch.setattr(prep, 'command', lambda *a: 'fake media validator version')
    monkeypatch.setattr(prep, 'inspect_video', lambda path, *a: dict(
        width=10, height=10, fps=12., frames=2, duration_s=2/12.,
        decoded_rgb_sha256=prep.sha(path), audio_present=False))
    args = SimpleNamespace(dataset=base, output=base/'evaluation_sets'/'r1', legacy_freeze=base,
                           workers=1, ffprobe='not-invoked', ffmpeg='not-invoked')
    return sources, args, protected


def test_77_freeze_preserves_originals_and_carries_all_reasons(tmp_path, monkeypatch):
    sources, args, protected = sample(tmp_path, monkeypatch)
    prep.prepare(args)
    verify_freeze(args.output)
    check_evaluation_labels(args.output, 'v2')
    data = json.loads((args.output/'evaluation_labels.json').read_text())
    labels = data['classifications']['cases']
    assert len(labels) == 77 and data['classifications']['counts'] == prep.COUNTS
    assert data['classifications']['full_dataset_finalized'] is True
    assert data['annotations']['whole_dataset_target_and_timing_metrics_ready'] is False
    assert len(list((args.output/'media').glob('*.mp4'))) == 77
    assert all(prep.sha(path) == expected for path, expected in protected.items())
    mapping = {c['review_case_id']: c for c in labels}
    assert mapping['GAP-V020']['case_id'] == 'SYN060'
    assert mapping['GAP-V021']['case_id'] == 'SYN061'
    assert mapping['GAP-V039']['case_id'] == 'SYN077'
    assert not set(mapping) & {'GAP-V013', 'GAP-V036', 'GAP-V038'}
    for original in sources['audit']['cases']:
        new = mapping[original['case_id']]
        for key in ('label', 'reason', 'reason_supplement', 'agreed_reason', 'judgment_note'):
            assert new.get(key) == original.get(key)
    payload_manifest = (args.output/'media.json').read_text()
    for forbidden in ('GAP-', 'observed_fall', 'suspected_fall', 'reason', 'scene_group', 'original_'):
        assert forbidden not in payload_manifest
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in args.output.iterdir() if p.is_file())
    with pytest.raises(ValueError, match='output already exists'):
        prep.prepare(args)


@pytest.mark.parametrize('change', ['duplicate_bytes', 'missing_review', 'changed_reason', 'missing_index'])
def test_annotation_identity_mismatch_blocks_freeze(tmp_path, monkeypatch, change):
    sources, _, _ = sample(tmp_path, monkeypatch)
    if change == 'duplicate_bytes':
        sources['index']['videos'][1]['sha256'] = sources['index']['videos'][0]['sha256']
    elif change == 'missing_review': sources['audit']['cases'].pop()
    elif change == 'changed_reason': sources['additional'][0]['reason'] = '변경됨'
    else: sources['index']['videos'].pop()
    with pytest.raises((ValueError, KeyError)):
        prep.assemble(sources)


@pytest.mark.parametrize('file', ['inventory.csv', 'media/SYN001.mp4', 'evaluation_labels.json'])
def test_changed_frozen_content_is_rejected(tmp_path, monkeypatch, file):
    _, args, _ = sample(tmp_path, monkeypatch); prep.prepare(args)
    with (args.output/file).open('ab') as stream: stream.write(b'changed')
    with pytest.raises(ValueError): verify_freeze(args.output)


def test_decoded_duplicates_do_not_silently_shrink_dataset(tmp_path, monkeypatch):
    _, args, _ = sample(tmp_path, monkeypatch)
    monkeypatch.setattr(prep, 'inspect_video', lambda *a: dict(
        width=10, height=10, fps=12., frames=2, duration_s=2/12.,
        decoded_rgb_sha256='same-pixels', audio_present=False))
    with pytest.raises(ValueError, match='duplicate accepted videos'): prep.prepare(args)
    assert not args.output.exists()


def test_path_escape_is_rejected(tmp_path):
    base = tmp_path/'dataset'; base.mkdir()
    (tmp_path/'outside.mp4').write_bytes(b'not-in-dataset')
    with pytest.raises(ValueError, match='outside'): prep.inside(base, '../outside.mp4')


def test_criteria_original_bytes_and_binding_are_preserved(tmp_path, monkeypatch):
    sources, args, _ = sample(tmp_path, monkeypatch); prep.prepare(args)
    assert (args.output/'scoring_criteria_v2.json').read_bytes() == sources['base_bytes']
    assert (args.output/'scoring_criteria_v2_r1.json').read_bytes() == sources['amendment_bytes']
    path = args.output/'scoring_criteria_v2.json'
    path.write_bytes(b'{ }')
    manifest = json.loads((args.output/'freeze.json').read_text())
    manifest['files'][path.name] = prep.sha(path)
    (args.output/'freeze.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='binding mismatch'): prep.verify(args.output)


@pytest.mark.parametrize('pts,declared', [([0., .05], '2'), ([0., .04], '3')])
def test_variable_rate_or_partial_decode_is_rejected(monkeypatch, pts, declared):
    values = iter([json.dumps(dict(streams=[dict(codec_type='video', width=10, height=10,
                        avg_frame_rate='25/1', nb_frames=declared)])),
                   json.dumps(dict(frames=[dict(best_effort_timestamp_time=t, width=10, height=10) for t in pts]))])
    monkeypatch.setattr(prep, 'command', lambda *a: next(values))
    with pytest.raises(ValueError): prep.inspect_video(Path('fake.mp4'), 'fakeprobe', 'fakeffmpeg')


def test_whole_dataset_target_scoring_rejects_partial_annotations(tmp_path, monkeypatch):
    import score_fall_baseline as scorer
    _, args, _ = sample(tmp_path, monkeypatch); prep.prepare(args)
    monkeypatch.setattr(sys, 'argv', ['score', '--frozen', str(args.output),
                        '--run', str(tmp_path/'no-model-run'), '--output', str(tmp_path/'no-report')])
    with pytest.raises(ValueError, match='target/timing annotations unavailable'): scorer.main()
    assert not (tmp_path/'no-report').exists()
