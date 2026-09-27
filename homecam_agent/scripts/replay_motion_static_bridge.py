#!/usr/bin/env python3
"""No-tracking diagnostic: replay the SAME bridge using a fixed seed box.

No GT access. This is not an alternative production association algorithm.
"""
import argparse
from pathlib import Path

from replay_reviewed_pose_cloud import read, save, require, digest
from experimental_visual_subject_bridge import VisualSubjectBridge, Policy


def replay(root):
    plan = read(root/'plan.json'); done = read(root/'run/completed.json')
    require(done['complete'], 'complete original inference before baseline')
    for path, expected in {**plan['code'], **plan['sources']}.items():
        require(digest(path) == expected, 'frozen inputs changed')
    cases = []
    for record in done['results']:
        cid = record['case_id']; case = plan['cases'][cid]
        pose_path = root/'run'/cid/'pose.json'
        require(digest(pose_path)==record['pose_sha256'], 'Pose output changed')
        bridge = VisualSubjectBridge(cid, Policy(**plan['policy']))
        samples = []
        if case['seed']:
            for row in read(pose_path):
                if row['source_frame'] < case['seed']['source_frame']: continue
                samples.append(dict(source_frame=row['source_frame'],
                    bridge=bridge.step(row['captured_at'],case['seed']['box'],row['link_candidates'])))
        cases.append(dict(case_id=cid,samples=samples))
    save(root/'static-bridge.json',dict(cases=cases, plan_sha256=digest(root/'plan.json'),
        code_sha256=digest(__file__), human_labels_used=False, new_api_calls=0))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    replay(p.parse_args().output)
