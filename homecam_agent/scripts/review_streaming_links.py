#!/usr/bin/env python3
"""Read-only review page: original JPEGs with separate SVG box overlays.

Never edits source pixels, changes reviewed ground truth, or runs inference.
"""
import argparse
import base64
import html
import json
from pathlib import Path

from replay_reviewed_pose_cloud import digest, read, require, save


def rectangle(box, color, label, dashed=False):
    left, top, right, bottom = [x * (640 if i % 2 == 0 else 400)
                              for i, x in enumerate(box)]
    return (f'<rect x="{left}" y="{top}" width="{right-left}" height="{bottom-top}" '
            f'fill="none" stroke="{color}" stroke-width="3" '
            f'stroke-dasharray="{6 if dashed else 0}"/>'
            f'<text x="{left+2}" y="{max(14, top-4)}" fill="{color}" '
            f'stroke="#000" stroke-width=".6" font-size="14">{html.escape(label)}</text>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('run', 'cached', 'audit', 'output'):
        parser.add_argument('--' + key, required=True, type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve existing review')
    done = read(args.run / 'completed.json')
    require(done['complete'], 'inference incomplete')
    for name, expected in done['files'].items():
        require(digest(args.run / name) == expected, 'run changed')
    args.output.mkdir(mode=0o700, parents=True)
    cases, sections, sources = [], [], {str(args.audit): digest(args.audit)}
    for case in read(args.audit)['cases']:
        if case['mode'] != 'early_fixture':
            continue
        for link in case['link_audit']:
            if link['exact_review_available']:
                continue
            cid = case['case_id']
            source = args.run / 'early_fixture' / cid / 'result.json'
            result = read(source)
            pose_path = args.cached / 'run' / cid / 'pose.json'
            rows = {r['source_frame']: r for r in read(pose_path)}
            sources.update({str(source): digest(source), str(pose_path): digest(pose_path)})
            match = next(i for i, sample in enumerate(result['samples'])
                         if sample['source_frame'] == link['source_frame'])
            selected = result['samples'][max(0, match-1):match+2]
            cells, records = [], []
            for sample in selected:
                index = sample['source_frame']
                row = rows[index]
                image = args.cached / 'run' / cid / 'images' / f'{index:05d}.jpg'
                require(digest(image) == row['jpeg_sha256'], 'JPEG changed')
                sources[str(image)] = digest(image)
                uri = 'data:image/jpeg;base64,' + base64.b64encode(image.read_bytes()).decode()
                shapes = []
                for track in row['candidate_payload']['tracks']:
                    chosen = 'pose:0:' + track['targetTrackId'] == link['subject_key']
                    shapes.append(rectangle(track['box'], '#ffb545' if chosen else '#ccc',
                        ('Pose target ' if chosen else 'Other ') + track['targetTrackId'].split('-')[-1], True))
                if sample['box'] is not None:
                    shapes.append(rectangle(sample['box'], '#00ffff', 'SAM'))
                at_link = index == link['source_frame']
                cells.append(f'<article><header>{cid} f{index:03d} '
                    f'{"LINK" if at_link else "before" if index < link["source_frame"] else "after"}'
                    f'</header><svg viewBox="0 0 640 400"><image width="640" height="400" '
                    f'href="{uri}"/><g class="boxes">{"".join(shapes)}</g></svg></article>')
                records.append(dict(source_frame=index, jpeg_path=str(image), jpeg_sha256=digest(image),
                    sam_box=sample['box'], tracks=row['candidate_payload']['tracks'], at_link=at_link))
            sections.append('<section>' + ''.join(cells) + '</section>')
            cases.append(dict(case_id=cid, linked_subject=link['subject_key'], frames=records,
                              review_status='pending_human_review'))
    page = ('<!doctype html><meta charset="utf-8"><title>Streaming link review</title>'
        '<style>body{margin:12px;background:#111820;color:#fff;font:16px sans-serif}'
        'h1{font-size:22px;margin:8px 0}p{margin:8px 0}section{display:flex;gap:8px;margin:10px 0}'
        'article{width:480px}header{padding:6px;background:#273442}svg{width:480px;display:block}'
        'body.raw .boxes{display:none}</style>'
        '<h1>Exact link instant and adjacent received frames</h1>'
        '<p>Cyan: SAM / orange dashed: linked Pose / gray: other Pose. '
        'Original JPEG pixels preserved. This page is not approved ground truth.</p>'
        '<button onclick="document.body.classList.toggle(\'raw\')">Show / hide boxes</button>'
        + ''.join(sections))
    (args.output / 'review.html').write_text(page)
    (args.output / 'review.html').chmod(0o600)
    save(args.output / 'manifest.json', dict(cases=cases, sources=sources,
        approved_ground_truth_modified=False, new_api_calls=0,
        html_sha256=digest(args.output / 'review.html')))
    print(json.dumps(dict(cases=len(cases), html=str(args.output / 'review.html'))))


if __name__ == '__main__':
    main()
