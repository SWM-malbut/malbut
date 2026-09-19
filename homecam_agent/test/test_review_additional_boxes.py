"""Draft validation only; fixtures are not real annotations or model results."""
import copy
import json
from html.parser import HTMLParser
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import review_additional_boxes as review  # noqa: E402


def fixture():
    cases = [dict(case_id=f'SYN{n+42:03d}', video=f'V{n+1:03d}',
                  source_sha256=str(n), width=1280, height=720, fps=24,
                  frames=review.FRAMES, label='normal_activity', label_ko='정상')
             for n in range(36)]
    index = dict(freeze_sha256='test-only', cases=copy.deepcopy(cases))
    for case in cases:
        case.update(reviewed_frames=review.FRAMES, target_person_id=None,
                    persons=[dict(person_id='P1', role='normal_activity',
                                  boxes=[[f, 10, 20, 100, 200] for f in review.FRAMES])])
    data = dict(schema_version=review.SCHEMA, new_labels_user_approved=False,
                freeze_sha256='test-only', box_format='source_pixel_xyxy_visible_extent',
                interpolation='forbidden', temporal_labels_changed=False,
                classification_labels_changed=False, cases=cases)
    return data, index


def test_draft_counts_only_fixture_boxes():
    data, index = fixture()
    assert review.validate(data, index) == dict(cases=36, frames=216, people=36, boxes=216)


@pytest.mark.parametrize('change', [
    'approval', 'label', 'time', 'source', 'geometry', 'duplicate_frame', 'outside', 'missing_frame',
])
def test_invalid_draft_rejected(change):
    data, index = fixture()
    case = data['cases'][0]
    boxes = case['persons'][0]['boxes']
    if change == 'approval': data['new_labels_user_approved'] = True
    elif change == 'label': case['label'] = 'observed_fall'
    elif change == 'time': data['temporal_labels_changed'] = True
    elif change == 'source': case['source_sha256'] = 'changed'
    elif change == 'geometry': case['width'] = 640
    elif change == 'duplicate_frame': boxes[1][0] = boxes[0][0]
    elif change == 'outside': boxes[0][3] = 1281
    else: boxes.pop()
    with pytest.raises(ValueError):
        review.validate(data, index)


def test_incomplete_transfer_creates_no_review_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(review, 'selected', lambda _: [
        dict(label=dict(case_id='SYN042'), media=dict(sha256='missing')),
    ])
    output = tmp_path/'review'
    with pytest.raises(ValueError, match='missing source video'):
        review.extract(tmp_path, tmp_path/'videos', output)
    assert not output.exists()


def test_viewer_keeps_unsafe_notes_in_json_and_has_36_options():
    data, _ = fixture()
    data['cases'][0]['notes'] = '</script><script>alert(1)</script>'
    page = review.review_html(data)
    assert '</script><script>alert(1)' not in page
    assert '\\u003c/script>' in page
    assert page.count('<option ') == 36
    assert 'fetch(' not in page
    assert '자료/frames/' in page
    assert '좌표 수정·승인·서버 전송을 하지 않습니다' in page
    HTMLParser().feed(page)


def test_publish_manifest_and_originals_unchanged(tmp_path, monkeypatch):
    from PIL import Image
    data, index = fixture()
    source = tmp_path/'review'
    (source/'frames').mkdir(parents=True)
    (source/'source_sheets').mkdir()
    for case in data['cases']:
        case.update(notes='시험 데이터', user_reviewed_frames=[])
        (source/'source_sheets'/f'{case["video"]}.jpg').write_bytes(b'fixture only')
        for frame in review.FRAMES:
            (source/'frames'/f'{case["video"]}-f{frame:03d}.jpg').write_bytes(b'fixture only')
    index['frame_sha256'] = {p.name: review.sha(p) for p in (source/'frames').iterdir()}
    for name, value in [('annotations.json', data), ('frames_index.json', index),
                        ('summary.json', review.validate(data, index))]:
        review.save(source/name, value)
    before = {str(p): review.sha(p) for p in source.rglob('*') if p.is_file()}
    monkeypatch.setattr(review, 'contact_sheet', lambda *args: Image.new('RGB', (8, 8)))
    target = tmp_path/'bundle'
    review.publish(source, target)
    assert len(list((target/'JPG').glob('*.jpg'))) == 36
    assert len(list((target/'자료'/'frames').glob('*.jpg'))) == 216
    manifest = json.loads((target/'자료'/'bundle_manifest.json').read_text())
    assert manifest['status'] == 'draft_pending_user_review'
    assert all(review.sha(target/name) == digest for name, digest in manifest['files'].items())
    assert {str(p): review.sha(p) for p in source.rglob('*') if p.is_file()} == before
    with pytest.raises(ValueError, match='bundle output exists'):
        review.publish(source, target)


def test_nonvisible_frame_does_not_create_a_box():
    data, index = fixture()
    person = data['cases'][0]['persons'][0]
    person['boxes'].pop(0)
    person['not_visible_frames'] = [0]
    assert review.validate(data, index)['boxes'] == 215


def test_viewer_accepts_unlabeled_batch_without_old_hardcoded_counts():
    data, _ = fixture()
    data['cases'] = data['cases'][:12]
    for case in data['cases']:
        case['label'] = case['label_ko'] = None
        case['target_person_id'] = None
        case['persons'][0]['role'] = 'observed_person'
    page = review.review_html(data)
    assert page.count('<option ') == 12
    assert '추가 12개 영상' in page
    assert '72개 프레임 · 72개 박스' in page
    assert '검토 이미지 12장' in page
    assert '239개 박스' not in page
    assert '라벨 작성과 별도' in page
    assert "observed_person:'화면 속 사람'" in page
