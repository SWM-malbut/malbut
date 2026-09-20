"""Synthetic fixture tests. Actual videos are separately decode-checked on the Mac."""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import prepare_fall_evaluation_84 as prep  # noqa: E402
from replay_fall_baseline import verify_freeze  # noqa: E402
from replay_vlm_frames import check_evaluation_labels  # noqa: E402


def record(text):
    return dict(text=text, sha256=hashlib.sha256(text.encode()).hexdigest())


def js(value):
    return record(json.dumps(value, ensure_ascii=False))


def csv_record(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return record(stream.getvalue())


@pytest.fixture
def sample(tmp_path, monkeypatch):
    dataset = tmp_path/'dataset'
    old = dataset/'evaluation_sets'/'old77'
    (old/'media').mkdir(parents=True)
    frames, width, height, fps = 123, 1280, 720, 24.
    info = dict(width=width, height=height, fps=fps, frames=frames, duration_s=frames/fps,
                fps_rational='24/1', first_pts_s=0., constant_frame_rate=True,
                codec='h264', audio_present=True)
    labels, media, annotations, boxes36 = [], [], [], []
    video_ids = [f'V{n:03d}' for n in range(1, 40) if n not in (13, 36, 38)]
    fall_ids = set(range(1, 15)) | {42, 43, 50, 59}
    suspect_ids = set(n for n in range(15, 78) if n not in fall_ids and n != 66)
    suspect_ids = set(sorted(suspect_ids)[:25])

    def boxes(cid, video, digest, label):
        return dict(case_id=cid, video=video, source_sha256=digest,
                    width=width, height=height, fps=fps, frames=prep.SAMPLES,
                    label=label, target_person_id='P1', persons=[dict(
                        person_id='P1', role='target', boxes=[[f, 1, 2, 30, 40] for f in prep.SAMPLES])])

    for n in range(1, 78):
        cid = f'SYN{n:03d}'
        path = old/'media'/f'{cid}.mp4'
        path.write_bytes(f'fake clip {n}'.encode())
        digest = prep.sha(path)
        label = 'observed_fall' if n in fall_ids else 'suspected_fall' if n in suspect_ids else 'normal_activity'
        c = dict(case_id=cid, label=label, reason='original', source_sha256=digest,
                 source_path=f'media/{cid}.mp4', scene_group=cid,
                 original_source_path=path.name, source_group='legacy41' if n <= 41 else 'additional36')
        if n > 41:
            c.update(video=video_ids[n-42]+'.mp4', review_case_id='GAP-' + video_ids[n-42])
            boxes36.append(boxes(cid, video_ids[n-42], digest, label))
        if n == 66:
            c.update(reason_supplement='original supplement', agreed_reason='original agreement')
        labels.append(c)
        media.append(dict(case_id=cid, source_path=c['source_path'], sha256=digest,
                          size_bytes=path.stat().st_size, decoded_rgb_sha256=digest, **info))
        annotations.append(dict(case_id=cid, persons=[], target_person_id=None,
                                onset_frames=[10, 12] if n <= 14 else None,
                                landing_frames=[20, 22] if n <= 10 else None,
                                spatial_annotations_available=n <= 41))
    criteria = js({})
    amendment = js(dict(base_file='scoring_criteria_v2.json', base_sha256=criteria['sha256'],
                        criteria_version=prep.previous.VERSION))
    prior = {name: js({}) for name in prep.previous.FILES}
    prior.update({'media.json': js(dict(schema_version='malbut.fall-evaluation-media.v2', cases=media)),
                  'evaluation_labels.json': js(dict(classifications=dict(
                      schema_version='malbut.synthetic-video-human-review.v2',
                      full_dataset_finalized=True, counts=prep.previous.COUNTS, cases=labels, exclusions={}),
                      annotations=dict(cases=annotations))),
                  'scoring_criteria_v2.json': criteria, 'scoring_criteria_v2_r1.json': amendment})
    prior['freeze.json'] = js(dict(schema_version=prep.previous.SCHEMA, criteria_version=prep.previous.VERSION,
                                   files={n: r['sha256'] for n, r in prior.items()}, match={}))
    monkeypatch.setattr(prep, 'PREVIOUS_SHA', prior['freeze.json']['sha256'])
    for name, r in prior.items():
        (old/name).write_text(r['text'])
    inp = {n: js({}) if n.endswith('.json') else record('history') for n in prep.INPUT_NAMES}
    inp['box36.json'] = js(dict(cases=boxes36))
    rows, index, boxes7 = [], [], []
    for v in sorted(prep.ACCEPTED + prep.EXCLUDED):
        path = dataset/(v+'.mp4')
        path.write_bytes(v.encode())
        digest = prep.sha(path)
        rows.append(dict(video=v+'.mp4', label='observed_fall' if v in prep.ACCEPTED else '',
                         reason='approved reason' if v in prep.ACCEPTED else '',
                         quality_exclusion_reason='' if v in prep.ACCEPTED else 'bad physics'))
        index.append(dict(video=v+'.mp4', path=str(path), sha256=digest, status='ready', scene_id=v))
        b = boxes('GEN-'+v, v, digest, None)
        b['target_person_id'] = None
        b['persons'][0]['role'] = 'observed_person'
        boxes7.append(b)
    inp['new_labels.csv'], inp['new_index.json'] = csv_record(rows), js(dict(videos=index))
    inp['box7.json'] = js(dict(cases=boxes7))
    for group, count in (('36', 36), ('7', 7)):
        scope = dict(videos=count, sampled_frames_per_video=prep.SAMPLES,
                     sampled_frames_total=count*6, boxes=count*6, within_clip_person_ids=count)
        if group == '7':
            scope.update(review_video_ids=prep.ACCEPTED, case_ids=['GEN-'+v for v in prep.ACCEPTED])
        approval = dict(status='user_approved', annotation_sha256=inp[f'box{group}.json']['sha256'], scope=scope)
        if group == '7':
            approval.update(source_index_sha256=inp['new_index.json']['sha256'],
                            approved_label_csv_sha256=inp['new_labels.csv']['sha256'], excluded_video_ids=prep.EXCLUDED)
        inp[f'box{group}_approval.json'] = js(approval)
    by_video = {b['video']: b for b in boxes36 + boxes7}
    timed = []
    for v in sorted(prep.TIMED):
        b = by_video[v]
        timed.append(dict(video=v, case_id=b['case_id'], sha256=b['source_sha256'],
                          width=width, height=height, fps=fps, decoded_frames=frames, source_pts_verified=True,
                          onset_frames=[24, 30], landing_frames=[48, 54], onset_seconds=[1., 1.25],
                          landing_seconds=[2., 2.25], onset_status='range', landing_status='range', note='approved'))
    inp['timing_review.json'] = js(dict(cases=timed))
    inp['timing_approval.json'] = js(dict(status='user_approved', scope=dict(review_video_ids=sorted(prep.TIMED)),
                                         approved_snapshot={n: inp[n]['sha256'] for n in (
                                             'timing_review.json', 'timing_review.csv', 'bundle_manifest.json')}))
    inp['v026_clarification.json'] = js(dict(case_id='SYN066', review_video_id='V026', label='normal_activity',
                                           source_video_sha256=labels[65]['source_sha256'], additional_reason='new supplement'))
    inp['input_manifest.json'] = js(dict(files={n: r['sha256'] for n, r in inp.items()}))
    inputs = tmp_path/'inputs'
    inputs.mkdir()
    for n, r in inp.items():
        (inputs/n).write_text(r['text'])
    monkeypatch.setattr(prep.previous, 'inspect_video', lambda path, *a: dict(decoded_rgb_sha256=prep.sha(path), **info))
    monkeypatch.setattr(prep.previous, 'command', lambda *a: 'mock ffmpeg')
    args = SimpleNamespace(dataset=dataset, previous=old, inputs=inputs, output=old.parent/'new84',
                           workers=1, ffmpeg='never-invoked', ffprobe='never-invoked')
    return dict(previous=prior, inputs=inp), args


def test_assembly_preserves_labels_reasons_intervals_and_history(sample):
    records, _ = sample
    before = copy.deepcopy(records)
    bundle, media = prep.assemble(records)
    labels = prep.unique(bundle['classifications']['cases'], 'case_id')
    annotations = prep.unique(bundle['annotations']['cases'], 'case_id')
    assert len(media) == 84 and bundle['classifications']['counts'] == prep.COUNTS
    assert labels['SYN083']['label'] == 'observed_fall'
    assert labels['SYN066']['reason'] == 'original'
    assert labels['SYN066']['reason_supplement'] == 'original supplement'
    assert labels['SYN066']['agreed_reason'] == 'original agreement'
    assert labels['SYN066']['reason_additions'][0]['text'] == 'new supplement'
    assert annotations['SYN078']['onset_frames'] == [24, 30]
    assert annotations['SYN078']['first_down_frames'] is None
    assert annotations['SYN011']['landing_frames'] is None
    assert annotations['SYN078']['target_person_id'] == 'P1'
    assert records == before
    assert len(bundle['classifications']['exclusions']['observed_fall12']) == 5


@pytest.mark.parametrize('name', ['box36.json', 'new_labels.csv', 'timing_review.json', 'v026_clarification.json'])
def test_changed_review_evidence_rejected(sample, name):
    records, _ = sample
    records['inputs'][name]['text'] += ' '
    with pytest.raises(ValueError, match='record hash mismatch'):
        prep.assemble(records)


@pytest.mark.parametrize('box', [[0, -1, 0, 10, 10], [0, 0, 0, 1300, 10],
                                  [123, 0, 0, 10, 10], [0, 10, 0, 1, 10], [True, 0, 0, 10, 10]])
def test_bad_boxes_rejected(box):
    with pytest.raises(ValueError):
        prep.validate_annotation(dict(persons=[dict(person_id='P1', boxes=[box])]),
                                 dict(frames=123, width=1280, height=720))


def test_not_visible_is_preserved_not_interpolated():
    a = dict(persons=[dict(person_id='P1', not_visible_frames=[0],
                          boxes=[[f, 1, 2, 30, 40] for f in prep.SAMPLES[1:]])])
    prep.validate_annotation(a, dict(frames=123, width=1280, height=720), sampled=True)
    a['persons'][0]['not_visible_frames'] = []
    with pytest.raises(ValueError, match='coverage'):
        prep.validate_annotation(a, dict(frames=123, width=1280, height=720), sampled=True)


@pytest.mark.parametrize('value', [[-1, 2], [4, 2], [0, 123], [0.5, 2], [1], [True, 2]])
def test_invalid_time_intervals(value):
    with pytest.raises(ValueError):
        prep.interval(value, 123, 'onset')


def test_freeze_preflight_without_models_and_no_overwrite(sample):
    records, args = sample
    prep.prepare(args)
    verify_freeze(args.output)
    check_evaluation_labels(args.output, 'v2')
    frozen = json.loads((args.output/'freeze.json').read_text())
    assert frozen['cases'] == 84 and not frozen['model_evaluation_run']
    assert not frozen['whole_dataset_target_and_timing_metrics_ready']
    payload = (args.output/'media.json').read_text()
    for forbidden in ('reason', 'observed_fall', 'GEN-', 'source_group', 'V040'):
        assert forbidden not in payload
    assert all(prep.sha(args.previous/n) == r['sha256'] for n, r in records['previous'].items())
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in args.output.iterdir() if p.is_file())
    with pytest.raises(ValueError, match='already exists'):
        prep.prepare(args)
    (args.output/'media'/'SYN084.mp4').write_bytes(b'tamper')
    with pytest.raises(ValueError, match='changed media'):
        prep.verify(args.output)
