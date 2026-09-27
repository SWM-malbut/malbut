#!/usr/bin/env python3
"""Posthoc report only; never makes inference requests or changes old results."""
import argparse
import html
from pathlib import Path
import shutil
import statistics

from evaluate_visual_person_linking import read, save, digest, require, CASES
from audit_paid_vlm_localization import exact_gt


def summary(scores):
    cloud=[]
    for arm in ('original','marked'):
        rows=[r for r in scores['cloud_selections'] if r['arm']==arm]
        cloud.append(dict(arm=arm,calls=len(rows),
            target_selected=sum(any(f['selected_target_on_reviewed_frames'] for f in r['findings']) for r in rows),
            wrong_person=sum(any(f['wrong_person'] for f in r['findings']) for r in rows),
            null_selection=sum(any(f['track_id'] is None for f in r['findings']) for r in rows),
            invalid_response=sum(r['outcome']!='classified' for r in rows),
            median_s=statistics.median(r['elapsed_s'] for r in rows) if rows else None))
    sam=[]
    for r in scores['sam']:
        later=[x for x in r['audited'] if not x['seed_frame']]
        sam.append(dict(case_id=r['case_id'],model=r['model'],
            reviewed=len(r['audited']),target=sum(x['target'] for x in r['audited']),
            wrong=sum(x['wrong_person'] for x in r['audited']),
            unresolved=sum(x['gt_person'] is None for x in r['audited']),
            later_reviewed=len(later),later_target=sum(x['target'] for x in later),
            static_seed_later_target=sum(x['static_seed_box_target'] for x in later),
            bridge_frames=len(r['pose_bridge_observations']),bridge_ids=r['bridge_track_ids'],
            bridge_reviewed=sum(x['gt_person'] is not None for x in r['pose_bridge_observations']),
            elapsed_s=r['elapsed_s']))
    return dict(cloud=cloud,sam=sam,cloud_cost_usd=scores['cloud_run']['known_cost_usd'],
                measured_incident_merges=None,continuous_identity_verified=False)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--spatial',type=Path,required=True)
    args=p.parse_args(); root=args.output
    scores=read(root/'scores.json'); plan=read(root/'plan.json')
    result=summary(scores); save(root/'summary.json',result)
    snapshot=root/'inference-code-snapshot.py'
    if not snapshot.exists():
        script=Path(__file__).with_name('evaluate_visual_person_linking.py')
        require(digest(script)==plan['script_sha256'],'inference source changed')
        shutil.copyfile(script,snapshot)
    require(digest(snapshot)==plan['script_sha256'],'inference snapshot changed')
    annotations={c['case_id']:c for c in read(args.spatial/'evaluation_labels.json')['annotations']['cases']}
    metas={c['case_id']:c for c in read(args.spatial/'media.json')['cases']}
    lines=['<!doctype html><meta charset="utf-8"><title>Visual person linking pilot</title>',
           '<style>body{background:#101820;color:#eee;font:16px sans-serif;margin:24px} '
           '.grid{display:grid;grid-template-columns:repeat(3,minmax(240px,1fr));gap:16px} '
           'svg{width:100%;height:auto} pre{white-space:pre-wrap} h2{margin-top:48px}</style>',
           '<h1>번호 선택 / Cloud 박스로 시작한 SAM 추적</h1>',
           '<p>청록: 추적 출력 / 초록: 검수한 대상 / 노랑: 검수한 다른 사람. '
           '정확히 검수한 프레임만 표시. 정답은 추론 이후 채점에만 사용.</p>',
           '<pre>'+html.escape(__import__('json').dumps(result,ensure_ascii=False,indent=2))+'</pre>']
    from PIL import Image, ImageDraw
    panels=[]
    for r in scores['sam']:
        cid=r['case_id']; model=r['model']; name=f"{model}-{cid}-{r['finding_index']}"
        raw=read(root/'sam'/f'{name}.json')
        lookup={s['frame_index']:s for s in raw['samples']}
        lines.append(f'<h2>{html.escape(name)}</h2><div class="grid">')
        for audit in r['audited']:
            i=audit['frame_index']; source=plan['cases'][cid]['frames'][i]['source_frame']
            gt=exact_gt(annotations[cid],source,metas[cid]); box=lookup[i]['box']
            pixels=[x*(640 if j%2==0 else 400) for j,x in enumerate(box)] if box else None
            overlays=[(g['box'],'#66ff66' if g['role']=='target' else '#ffff55',g['person_id']) for g in gt]
            if pixels:overlays.append((pixels,'#00ffff','SAM'))
            title=f"{cid} {model} source {source} / {'seed' if audit['seed_frame'] else 'propagated'} / {audit['gt_person']}"
            lines.append('<div><p>'+html.escape(title)+'</p><svg viewBox="0 0 640 400">'
                         f'<image href="{cid}/original/{i:05d}.jpg" width="640" height="400"/>')
            picture=Image.open(root/cid/'original'/f'{i:05d}.jpg').convert('RGB'); draw=ImageDraw.Draw(picture)
            for b,color,label in overlays:
                l,t,right,bottom=b
                lines.append(f'<rect x="{l}" y="{t}" width="{right-l}" height="{bottom-t}" '
                             f'fill="none" stroke="{color}" stroke-width="2"/>')
                lines.append(f'<text x="{l}" y="{max(14,t-3)}" fill="{color}" '
                             f'stroke="black" stroke-width=".3" font-size="14">{html.escape(label)}</text>')
                draw.rectangle(b,outline=color,width=2)
                draw.text((l,max(0,t-12)),label,fill=color,stroke_width=1,stroke_fill='black')
            lines.append('</svg></div>')
            if cid=='SYN063' and i in (0,4,11):
                panel=Image.new('RGB',(640,430),'#101820');panel.paste(picture,(0,30))
                ImageDraw.Draw(panel).text((5,8),title,fill='white');panels.append(panel)
        lines.append('</div>')
    with (root/'index.html').open('x') as f:f.write('\n'.join(lines))
    if panels:
        sheet=Image.new('RGB',(640*3,430*((len(panels)+2)//3)),'#101820')
        for i,panel in enumerate(panels):sheet.paste(panel,((i%3)*640,(i//3)*430))
        sheet.save(root/'SYN063-sam-contact.jpg',quality=92)
    files={str(p.relative_to(root)):digest(p) for p in root.rglob('*') if p.is_file()}
    save(root/'report-provenance.json',dict(files=files))
    print(__import__('json').dumps(result,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
