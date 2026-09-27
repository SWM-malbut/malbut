"""Bounded local streaming experiment; never imported by production nodes.

Only already received JPEGs are submitted. SAM's state layout is private and
version-sensitive; this adapter is tied to the recorded upstream commit.
No Pose identity, incident policy, or Cloud decisions are inferred here.
"""

from collections import deque
from dataclasses import dataclass
import io
import math
import tempfile
from pathlib import Path
import time


@dataclass(frozen=True)
class ReceivedFrame:
    source_frame: int
    observed_at: float
    jpeg: bytes


def valid_frame(frame):
    return (isinstance(frame, ReceivedFrame) and type(frame.source_frame) is int
            and frame.source_frame >= 0 and isinstance(frame.jpeg, bytes) and bool(frame.jpeg)
            and type(frame.observed_at) in (int, float) and math.isfinite(frame.observed_at))


class StreamQueue:
    """One owning event loop. Overflow stops the session instead of losing frames."""

    def __init__(self, *, max_frames=64, max_bytes=16 * 1024 * 1024):
        if type(max_frames) is not int or max_frames < 1 or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError('invalid queue capacity')
        self.max_frames, self.max_bytes = max_frames, max_bytes
        self.frames = deque()
        self.bytes = 0
        self.last_stamp = None
        self.closed = None
        self.peak_frames = 0

    def stop(self, reason):
        self.closed = reason
        self.frames.clear()
        self.bytes = 0

    def put(self, frame, *, now):
        if self.closed:
            return False
        if (not valid_frame(frame) or type(now) not in (int, float)
                or not math.isfinite(now) or frame.observed_at > now
                or (self.last_stamp is not None and frame.observed_at <= self.last_stamp)):
            self.stop('invalid_frame')
            raise ValueError('future, duplicate, or invalid frame')
        if self.last_stamp is not None and frame.observed_at - self.last_stamp > .5 + 1e-9:
            self.stop('capture_gap')
            return False
        if len(self.frames) >= self.max_frames or self.bytes + len(frame.jpeg) > self.max_bytes:
            self.stop('queue_capacity')
            return False
        self.frames.append(frame)
        self.last_stamp = frame.observed_at
        self.bytes += len(frame.jpeg)
        self.peak_frames = max(self.peak_frames, len(self.frames))
        return True

    def pop(self):
        if not self.frames:
            return None
        frame = self.frames.popleft()
        self.bytes -= len(frame.jpeg)
        return frame


class IncrementalSam:
    """One sequential worker; append only the next frame to the SAM video state.

    A 64-frame hard stop bounds model memory. This is not an indefinite tracker.
    No final video length or future tensors are supplied in advance.
    """

    def __init__(self, predictor, *, seed_box, seed_time, max_frames=64):
        if type(max_frames) is not int or not 1 <= max_frames <= 64:
            raise ValueError('invalid model capacity')
        self.predictor, self.seed_box, self.seed_time = predictor, seed_box, seed_time
        self.max_frames = max_frames
        self.state = None
        self.last_stamp = None
        self.count = 0

    def step(self, frame):
        if not valid_frame(frame):
            raise ValueError('invalid frame')
        if self.count >= self.max_frames:
            raise ValueError('model_capacity')
        if self.last_stamp is None and frame.observed_at != self.seed_time:
            raise ValueError('exact seed required')
        if self.last_stamp is not None and not 0 < frame.observed_at - self.last_stamp <= .5 + 1e-9:
            raise ValueError('invalid capture continuity')
        import numpy as np
        from PIL import Image
        import torch
        from evaluate_visual_person_linking import mask_box

        with Image.open(io.BytesIO(frame.jpeg)) as image:
            if image.size != (640, 400):
                raise ValueError('unexpected JPEG dimensions')
            # Match upstream's eager JPEG loader: divide uint8 then cast to fp32
            # before normalization. Only this received JPEG is decoded.
            array = np.array(image.convert('RGB').resize((self.predictor.image_size,
                                                         self.predictor.image_size))) / 255.0
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode(), torch.autocast('cuda', dtype=torch.bfloat16):
            if self.state is None:
                with tempfile.TemporaryDirectory(prefix='malbut-stream-seed-') as folder:
                    path = Path(folder) / '00000.jpg'
                    path.write_bytes(frame.jpeg)
                    self.state = self.predictor.init_state(
                        folder, offload_video_to_cpu=True, offload_state_to_cpu=True)
                self.state['images'] = [self.state['images'][0]]
                seed = np.array([v * (640 if i % 2 == 0 else 400)
                                 for i, v in enumerate(self.seed_box)], dtype=np.float32)
                self.predictor.add_new_points_or_box(self.state, frame_idx=0, obj_id=1, box=seed)
            else:
                tensor = torch.from_numpy(array).permute(2, 0, 1).to(torch.float32)
                tensor -= torch.tensor((.485, .456, .406))[:, None, None]
                tensor /= torch.tensor((.229, .224, .225))[:, None, None]
                self.state['images'].append(tensor)
                self.state['num_frames'] = len(self.state['images'])
            iterator = self.predictor.propagate_in_video(
                self.state, start_frame_idx=self.count, max_frame_num_to_track=0)
            index, ids, logits = next(iterator)
            if index != self.count or ids != [1] or next(iterator, None) is not None:
                raise ValueError('unexpected incremental SAM output')
            mask = (logits[0, 0] > 0).cpu().numpy()
            torch.cuda.synchronize()
            duration = time.perf_counter() - started
        self.last_stamp = frame.observed_at
        self.count += 1
        return dict(source_frame=frame.source_frame, observed_at=frame.observed_at,
                    box=mask_box(mask), mask=mask, compute_s=duration,
                    available_input_count=self.count)

    def close(self):
        self.state = None
