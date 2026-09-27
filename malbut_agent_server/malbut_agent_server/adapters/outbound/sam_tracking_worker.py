"""Pinned, local-only SAM2.1 tiny GPU tracker. Executed in an isolated process.

No network loading, image files or identity inference. Private incremental
SAM state is deliberately tied to the tested source revision and checkpoint.
"""

from collections import OrderedDict
import argparse
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

SAM_REVISION = '2b90b9f5ceec907a1c18123530e92e794ad901a4'
CHECKPOINT_SHA256 = '7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69'
MAX_LINE = 1400000


def validate_assets(source, checkpoint):
    source, checkpoint = Path(source).resolve(), Path(checkpoint).resolve()
    revision = subprocess.check_output(
        ['git', '-C', str(source), 'rev-parse', 'HEAD'], timeout=5,
        stderr=subprocess.DEVNULL).decode().strip()
    changed = subprocess.check_output(
        ['git', '-C', str(source), 'status', '--porcelain', '--', 'sam2'],
        timeout=5, stderr=subprocess.DEVNULL)
    if revision != SAM_REVISION or changed.strip():
        raise ValueError('unvalidated tracking source')
    digest = hashlib.sha256()
    with checkpoint.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != CHECKPOINT_SHA256:
        raise ValueError('unvalidated tracking checkpoint')


class IncrementalSam:
    """In-memory received frames; no complete/future video is loaded."""

    def __init__(self, predictor):
        self.predictor = predictor
        self.state = None
        self.last_time = None
        self.count = 0

    def step(self, data):
        from malbut_agent_server.domain.fall_monitoring import CloudPersonRegion, timestamp
        expected = {'captured_at', 'jpeg'} | ({'seed_box'} if self.count == 0 else set())
        if not isinstance(data, dict) or set(data) != expected or self.count >= 64:
            raise ValueError('invalid frame request')
        stamp = data['captured_at']
        timestamp(stamp)
        if (self.last_time is not None
                and not 0 < stamp - self.last_time <= .5 + 1e-9):
            raise ValueError('capture discontinuity')
        if not isinstance(data['jpeg'], str):
            raise ValueError('invalid image')
        jpeg = base64.b64decode(data['jpeg'], validate=True)
        if not 0 < len(jpeg) <= 1024 * 1024:
            raise ValueError('image capacity')
        seed = None
        if self.count == 0:
            seed = tuple(data['seed_box'])
            CloudPersonRegion(0, seed)
        import numpy as np
        from PIL import Image
        import torch

        with Image.open(io.BytesIO(jpeg)) as image:
            if image.format != 'JPEG' or image.size != (640, 400):
                raise ValueError('unexpected JPEG')
            # Same eager normalization as the pinned upstream JPEG loader.
            array = np.array(image.convert('RGB').resize(
                (self.predictor.image_size, self.predictor.image_size))) / 255.0
        tensor = torch.from_numpy(array).permute(2, 0, 1).to(torch.float32)
        tensor -= torch.tensor((.485, .456, .406))[:, None, None]
        tensor /= torch.tensor((.229, .224, .225))[:, None, None]
        with torch.inference_mode(), torch.autocast('cuda', dtype=torch.bfloat16):
            if self.state is None:
                self.state = dict(
                    images=[tensor], num_frames=1, video_height=400, video_width=640,
                    offload_video_to_cpu=True, offload_state_to_cpu=True,
                    device=self.predictor.device, storage_device=torch.device('cpu'),
                    point_inputs_per_obj={}, mask_inputs_per_obj={}, cached_features={},
                    constants={}, obj_id_to_idx=OrderedDict(), obj_idx_to_id=OrderedDict(),
                    obj_ids=[], output_dict_per_obj={}, temp_output_dict_per_obj={},
                    frames_tracked_per_obj={})
                self.predictor._get_image_feature(self.state, frame_idx=0, batch_size=1)
                pixel_box = np.array([v * (640 if i % 2 == 0 else 400)
                                      for i, v in enumerate(seed)], dtype=np.float32)
                self.predictor.add_new_points_or_box(
                    self.state, frame_idx=0, obj_id=1, box=pixel_box)
            else:
                self.state['images'].append(tensor)
                self.state['num_frames'] = len(self.state['images'])
            iterator = self.predictor.propagate_in_video(
                self.state, start_frame_idx=self.count, max_frame_num_to_track=0)
            index, ids, logits = next(iterator)
            if index != self.count or ids != [1] or next(iterator, None) is not None:
                raise ValueError('invalid model output')
            mask = (logits[0, 0] > 0).cpu().numpy()
            if mask.shape != (400, 640):
                raise ValueError('invalid mask dimensions')
            y, x = np.where(mask)
            box = None if not len(x) else (
                float(x.min() / 640), float(y.min() / 400),
                float((x.max() + 1) / 640), float((y.max() + 1) / 400))
        self.count += 1
        self.last_time = stamp
        return {'captured_at': stamp, 'box': box}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--checkpoint', required=True)
    args = parser.parse_args()
    output = sys.stdout
    try:
        # Keep third-party status text away from the protocol.
        with contextlib.redirect_stdout(sys.stderr):
            validate_assets(args.source, args.checkpoint)
            import torch
            import sam2
            if Path(sam2.__file__).resolve().parent != Path(args.source).resolve() / 'sam2':
                raise ValueError('unexpected model source')
            if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
                raise ValueError('validated GPU mode unavailable')
            torch.set_num_threads(4)
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
            from sam2.build_sam import build_sam2_video_predictor
            predictor = build_sam2_video_predictor(
                'configs/sam2.1/sam2.1_hiera_t.yaml', args.checkpoint, device='cuda')
            predictor.fill_hole_area = 0
            tracker = IncrementalSam(predictor)
        output.write('{"ready":true}\n')
        output.flush()
        while True:
            line = sys.stdin.buffer.readline(MAX_LINE + 1)
            if not line:
                return 0
            if len(line) > MAX_LINE or not line.endswith(b'\n'):
                raise ValueError('protocol capacity')
            with contextlib.redirect_stdout(sys.stderr):
                result = tracker.step(json.loads(line))
            output.write(json.dumps(result, allow_nan=False) + '\n')
            output.flush()
    except Exception:
        # The parent records only a code. Never return images or free exception text.
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

