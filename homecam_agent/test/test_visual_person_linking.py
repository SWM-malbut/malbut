import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('visual_link_experiment',SCRIPTS/'evaluate_visual_person_linking.py')
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def track(tid='a', state='tracked', box=None, usable=True):
    return dict(targetTrackId=tid,trackingState=state,
                box=[.1,.1,.4,.6] if box is None else box,associationUsable=usable)


def frames():
    return mod.candidate_frames([
        dict(source_frame=2,candidate_payload=dict(tracks=[track()])),
        dict(source_frame=13,candidate_payload=dict(tracks=[track(),track('b')]))], [2,13])


def reply():
    return dict(assessment='suspected_fall',explanation='lying down',findings=[dict(
        assessment='suspected_fall',track_id='T1',evidence_frames=[0,1],explanation='same marked person')])


def test_ids_are_stable_and_missing_observations_not_copied():
    rows=[dict(source_frame=2,candidate_payload=dict(tracks=[track('a'),track('b')])),
          dict(source_frame=13,candidate_payload=dict(tracks=[track('b'),track('a','missing')])),
          dict(source_frame=24,candidate_payload=dict(tracks=[track('a',usable=False)]))]
    result=mod.candidate_frames(rows,[2,13,24])
    assert [[c['id'] for c in f['candidates']] for f in result]==[['T1','T2'],['T2'],['T1']]
    assert result[-1]['candidates'][0]['association_usable'] is False


@pytest.mark.parametrize('box',[[0,0,0,1],[0,0,1,0],[-.1,0,1,1],[0,0,1.1,1],
                                [0,0,float('nan'),1],[False,0,1,1],[0,0,1]])
def test_invalid_box_rejected(box):
    assert not mod.valid_box(box)


def test_duplicate_ids_rejected():
    with pytest.raises(ValueError):
        mod.candidate_frames([dict(source_frame=1,candidate_payload=dict(tracks=[track(),track()]))],[1])


def test_valid_and_one_fence():
    text=json.dumps(reply())
    assert mod.parse_reply(text,frames())==reply()
    assert mod.parse_reply('```json\n'+text+'\n```',frames())==reply()


@pytest.mark.parametrize('tid,indices', [('T9',[0]),('T2',[0]),('null',[0]),(True,[0]),
                                       ('T1',[False]),('T1',[2]),('T1',[1,0]),('T1',[0,0]),('T1',[])])
def test_wrong_id_or_evidence_rejected(tid,indices):
    value=reply(); value['findings'][0].update(track_id=tid,evidence_frames=indices)
    with pytest.raises(ValueError):
        mod.parse_reply(json.dumps(value),frames())


def test_unmatched_positive_allowed():
    value=reply(); value['findings'][0]['track_id']=None
    assert mod.parse_reply(json.dumps(value),frames())['findings'][0]['track_id'] is None


def test_conflicting_normal_or_duplicate_json_rejected():
    value=reply(); value['assessment']='normal_activity'
    with pytest.raises(ValueError):mod.parse_reply(json.dumps(value),frames())
    with pytest.raises(ValueError):mod.parse_reply('{"assessment":"normal_activity","assessment":"suspected_fall"}',frames())


def test_render_keeps_dimensions_and_input():
    from PIL import Image
    image=Image.new('RGB',(640,400),(90,100,120)); stream=io.BytesIO();image.save(stream,'JPEG')
    original=stream.getvalue(); saved=original[:]
    marked=mod.draw_marks(original,frames()[0]['candidates'])
    assert original==saved and marked!=original
    assert Image.open(io.BytesIO(marked)).size==(640,400)


def test_mask_bbox_exclusive_edges_and_empty():
    import numpy as np
    mask=np.zeros((400,640),dtype=bool)
    assert mod.mask_box(mask) is None
    mask[80:320,64:384]=True
    assert mod.mask_box(mask)==[.1,.2,.6,.8]


def test_bridge_preserves_strength_and_ambiguity_guards():
    a=dict(id='T1',box=[.1,.1,.4,.6],association_usable=True)
    assert mod.pose_bridge(a['box'],[a]) is a
    assert mod.pose_bridge(a['box'],[dict(a,association_usable=False)]) is None
    assert mod.pose_bridge(a['box'],[a,dict(a,id='T2')]) is None
    assert mod.pose_bridge(None,[a]) is None


def test_gt_identity_does_not_borrow_other_person():
    criteria=dict(visible_coverage=.5,prediction_coverage=.25,margin=.1)
    gt=[dict(person_id='target',box=[0,0,100,100]),dict(person_id='other',box=[320,0,640,400])]
    assert mod.identify([.5,0,1,1],gt,criteria)=='other'
    assert mod.identify([.25,.5,.3,.6],gt,criteria) is None


def test_paid_execution_requires_explicit_opt_in(tmp_path):
    import asyncio
    from argparse import Namespace
    with pytest.raises(ValueError):
        asyncio.run(mod.run_cloud(Namespace(execute=False,approve_upload=True,output=tmp_path)))


def test_cpu_sam_disables_only_optional_cuda_postprocessor(monkeypatch):
    from types import SimpleNamespace
    calls=[]
    predictor=SimpleNamespace(fill_hole_area=8,keep_other_options=True)
    def build(*args,**kwargs):
        calls.append((args,kwargs));return predictor
    monkeypatch.setitem(sys.modules,'sam2.build_sam',SimpleNamespace(build_sam2_video_predictor=build))
    assert mod.cpu_sam_predictor(Path('/fake/checkpoint')) is predictor
    assert predictor.fill_hole_area==0 and predictor.keep_other_options
    assert calls[0][1]=={'device':'cpu'}


@pytest.mark.parametrize('cost,timeout,expected_calls', [('0.001',False,8),(None,False,1),('0.10',False,1),(None,True,1)])
def test_cloud_budget_unknown_usage_timeout_and_no_retries(tmp_path,monkeypatch,cost,timeout,expected_calls):
    import asyncio
    from argparse import Namespace
    from paid_vlm import runner,providers,metrics
    plan=dict(cases={cid:dict(text='candidates only',frames=frames()) for cid in mod.CASES},
              system=mod.PROMPT,budget_usd='.10',request_reserve_usd='.01')
    mod.save(tmp_path/'plan.json',plan)
    rate_path=tmp_path/'rates.json';mod.save(rate_path,dict(rates={'gemma4:31b':{}}))
    key_path=tmp_path/'test-key.json';mod.save(key_path,{'OLLAMA_API_KEY':'test-only-no-live-network'})
    for cid in mod.CASES:
        for arm in ('original','marked'):
            (tmp_path/cid/arm).mkdir(parents=True)
            for i in range(12):(tmp_path/cid/arm/f'{i:05d}.jpg').write_bytes(b'fixture')
    monkeypatch.setattr(mod,'verify_inputs',lambda *a:None)
    monkeypatch.setattr(metrics,'validate_rate',lambda r:None)
    monkeypatch.setattr(metrics,'estimate_cost',lambda *a:cost)
    monkeypatch.setattr(providers,'normalize',lambda *a:dict(outcome='response',text=json.dumps(reply()),usage={}))
    calls=[]
    async def post(*a,**kw):
        calls.append(kw)
        if timeout:raise asyncio.TimeoutError
        return 200,b'fixture'
    monkeypatch.setattr(runner,'https_post',post)
    args=Namespace(output=tmp_path,execute=True,approve_upload=True,rate_run=rate_path,key_file=key_path)
    asyncio.run(mod.run_cloud(args))
    assert len(calls)==expected_calls
    assert all(c['timeout_s']==20 for c in calls)
    assert len(list((tmp_path/'cloud').glob('*.started.json')))==expected_calls
