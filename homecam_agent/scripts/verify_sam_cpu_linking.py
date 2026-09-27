#!/usr/bin/env python3
"""Check explicit CPU postprocessor disable against the preserved first run."""
import argparse
from pathlib import Path
import time

from evaluate_visual_person_linking import cpu_sam_predictor,read,save,digest,require


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--checkpoint',type=Path,required=True)
    args=p.parse_args()
    path=args.output/'sam-cpu-postprocess-verification.json'
    require(not path.exists(),'preserve check')
    import numpy as np
    import torch
    from PIL import Image
    torch.set_num_threads(4)
    old=read(args.output/'sam/gemma-SYN063-0.json')
    require(digest(args.checkpoint)==read(args.output/'sam/run.json')['checkpoint_sha256'],'changed model')
    predictor=cpu_sam_predictor(args.checkpoint)
    started=time.perf_counter(); checks=[]
    with torch.inference_mode():
        state=predictor.init_state(str(args.output/'SYN063/original'),offload_video_to_cpu=True,offload_state_to_cpu=True)
        box=np.array([v*(640 if i%2==0 else 400) for i,v in enumerate(old['seed']['box'])],dtype=np.float32)
        predictor.add_new_points_or_box(state,frame_idx=old['seed']['frame_index'],obj_id=1,box=box)
        for index,ids,logits in predictor.propagate_in_video(state,start_frame_idx=old['seed']['frame_index']):
            expected=np.asarray(Image.open(args.output/'sam/gemma-SYN063-0'/f'{index:05d}.png'))>0
            actual=(logits[0,0]>0).cpu().numpy()
            checks.append(dict(frame_index=index,identical=bool(np.array_equal(expected,actual))))
    value=dict(fill_hole_area=predictor.fill_hole_area,frames=checks,
               all_identical=all(x['identical'] for x in checks),elapsed_s=time.perf_counter()-started,
               interpretation='CPU explicit disable compared with upstream failed-extension no-op; no new Cloud call')
    save(path,value);print(value)


if __name__=='__main__':main()
