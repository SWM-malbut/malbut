#!/usr/bin/env python3
"""Render recorded evidence; never change predictions, masks or reviewed labels."""
import argparse
from collections import Counter
from pathlib import Path
import statistics

from replay_reviewed_pose_cloud import read, require, save, digest
from evaluate_visual_person_linking import identify
from audit_paid_vlm_localization import exact_gt


def render(root, spatial):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 15)
    plan = read(root/'plan.json'); scores = read(root/'scores.json')
    static = read(root/'static-bridge.json')
    require(static['plan_sha256']==digest(root/'plan.json'), 'wrong baseline plan')
    require(digest(spatial/'evaluation_labels.json')==scores['labels_sha256'], 'labels changed')
    annotations={c['case_id']:c for c in read(spatial/'evaluation_labels.json')['annotations']['cases']}
    baselines={c['case_id']:c for c in static['cases']}
    baseline_scores=[]
    for case in scores['cases']:
        cid = case['case_id']; folder = root/'run'/cid
        result = read(folder/'result.json')
        samples = {s['source_frame']:s for s in result['samples']}
        rows = {r['source_frame']:r for r in read(folder/'pose.json')}
        fixed={s['source_frame']:s for s in baselines[cid]['samples']}
        checks=[]
        for a in case['audited']:
            if a['seed_frame']: continue
            frame=a['source_frame']; active=fixed[frame]['bridge']['active']
            c=next((c for c in rows[frame]['link_candidates'] if active and c['id']==active['pose_id']),None)
            gt=exact_gt(annotations[cid],frame,plan['cases'][cid]['meta'])
            who=identify(c['box'],gt,scores['criteria']) if c else None
            checks.append(dict(source_frame=frame, linked=active is not None, pose_person=who,
                correct=bool(active and who==a['static_person']==a['target']),
                wrong_person=bool(active and ((who is not None and who!=a['target']) or
                    (a['static_person'] is not None and a['static_person']!=a['target'])))))
        baseline_scores.append(dict(case_id=cid, checks=checks,
            linked_observations=sum(s['bridge']['active'] is not None for s in fixed.values()),
            linked_reviewed=sum(a['linked'] for a in checks),
            correct_links=sum(a['correct'] for a in checks),wrong_links=sum(a['wrong_person'] for a in checks)))
        seed = plan['cases'][cid]['seed']
        indices = sorted({a['source_frame'] for a in case['audited']} | ({seed['source_frame']} if seed else set()))
        if not indices: indices = list(rows)[::4]
        width, height = 480, 348
        sheet = Image.new('RGB',(width*3,height*((len(indices)+2)//3)), '#151b24')
        for n,index in enumerate(indices):
            original = Image.open(folder/'images'/f'{index:05d}.jpg').convert('RGB')
            draw = ImageDraw.Draw(original)
            sample = samples.get(index); active = sample['bridge']['active'] if sample else None
            def rectangle(box,color):
                if box:
                    draw.rectangle([v*(640 if i%2==0 else 400) for i,v in enumerate(box)],outline=color,width=3)
            if seed: rectangle(seed['box'],'#999999')
            if sample: rectangle(sample['box'],'#00ddff')
            for candidate in rows[index]['link_candidates']:
                if active and candidate['id']==active['pose_id']: rectangle(candidate['box'],'#ffff00')
            x,y = n%3*width,n//3*height
            sheet.paste(original.resize((480,300)),(x,y+48))
            top = ImageDraw.Draw(sheet)
            reason = sample['bridge']['reason'] if sample else 'no Cloud seed'
            top.text((x+5,y+2),f'{cid} f{index}: {reason}',font=font,fill='white')
            top.text((x+5,y+23),'cyan SAM / gray fixed seed / yellow linked Pose',font=font,fill='white')
        path = root/f'{cid}-contact.jpg'; require(not path.exists(),'preserve existing rendering')
        sheet.save(path,quality=90)
    totals = {}
    for name, selected in [('motion', [c for c in scores['cases'] if c['group'].startswith(('motion','camera'))]),
                           ('multiperson', [c for c in scores['cases'] if c['group']=='multiperson_control']),
                           ('all_seeded', [c for c in scores['cases'] if not c['no_seed']])]:
        totals[name] = {k:sum(c[k] for c in selected) for k in (
            'sam_frames','reviewed_postseed','sam_target','static_target','sam_wrong_person',
            'pose_target_available','linked_reviewed','correct_links','wrong_links','linked_observations')}
        totals[name]['clips_with_any_live_alias'] = sum(c['linked_observations']>0 for c in selected)
        totals[name]['clips'] = len(selected)
        totals[name]['linked_reviewed_unresolved'] = (totals[name]['linked_reviewed']-
            totals[name]['correct_links']-totals[name]['wrong_links'])
        selected_ids={c['case_id'] for c in selected}
        totals[name]['static_bridge']={k:sum(c[k] for c in baseline_scores if c['case_id'] in selected_ids)
                                      for k in ('linked_observations','linked_reviewed','correct_links','wrong_links')}
    times = [c['sam_elapsed_s'] for c in scores['cases'] if not c['no_seed']]
    reasons = Counter()
    for c in scores['cases']: reasons.update(c['bridge_reasons'])
    save(root/'summary.json',dict(totals=totals, bridge_reasons=dict(reasons),
        sam_clip_seconds=dict(minimum=min(times),median=statistics.median(times),maximum=max(times)),
        new_api_calls=0, incident_merge_tested=False, live_realtime_tested=False,
        scores_sha256=digest(root/'scores.json'),static_bridge_cases=baseline_scores,
        static_bridge_sha256=digest(root/'static-bridge.json'), report_code_sha256=digest(__file__)))
    print(totals)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--spatial',type=Path,required=True)
    args=parser.parse_args()
    render(args.output,args.spatial)
