"""What each person in view looks like, as numbers, for the person tracking (SWM25-267).

ReID only splits (design decision 10): when someone shows up where an unseen person
was, the Agent asks how alike the two looked; clearly unlike means someone else.
This keeps OSNet-AIN descriptors (malbut_reid's encoder, installed with the Bringup for
following) of each person in view, by YOLO track, for 30 minutes: two looks a second
apart every five seconds. A comparison averages a pair on each side: the last pair
before the person was gone and the first pair after someone reappeared (two against
two: on the 10-17 pairs on the Jetson single looks wobbled, 5 of 9 same people fell
below 0.64; pairs did not). No identity is decided here. Without the encoder or its
model, or without a pair on both sides, the answer is "none" and the Agent goes by
place alone. Never raises into the fall check.
"""

from collections import OrderedDict, deque
import math

EVERY_S = 5.0          # a pair of looks per person every five seconds
PAIR_GAP_S = 1.0       # its two looks a second apart (as tested on the Jetson, 10-19)
KEEP_S = 1800.0        # as long as the tracking remembers someone unseen
MAX_TRACKS = 128
PER_TRACK = 6
MODEL = '~/.cache/malbut_perception/osnet_ain_x1_0_msmt17.onnx'


def _load_encoder(model_path):
    """malbut_reid's OSNet encoder as encode(bgr, pixel boxes) -> vectors (or ImportError)."""
    from malbut_reid.models import BoundingBox, ImageDetection
    from malbut_reid.reid.osnet_encoder import OsNetPersonEncoder

    encoder = OsNetPersonEncoder(model_path)

    def encode(image, boxes):
        return encoder.encode(image, [ImageDetection(BoundingBox(*box), 1.0) for box in boxes])
    return encode


def _decode(jpeg):
    import cv2
    import numpy as np

    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def _mean(looks):
    total = [sum(values) for values in zip(*(vector for _, vector, _ in looks))]
    norm = math.sqrt(sum(x * x for x in total))
    return [x / norm for x in total] if norm > 1e-12 else None


class FallAppearance:
    def __init__(self, *, encode=None, decode=_decode, model_path=MODEL, loader=_load_encoder):
        self._encode, self._decode = encode, decode
        self._model_path, self._loader = model_path, loader
        self._tried = encode is not None
        # YOLO track -> deque[(at, unit vector, first of its pair)]
        self._tracks = OrderedDict()
        self.failures = 0

    @property
    def available(self):
        return self._ready()

    def _ready(self):
        if not self._tried:
            self._tried = True
            try:
                from pathlib import Path
                self._encode = self._loader(str(Path(self._model_path).expanduser()))
            except Exception:
                self._encode = None  # No malbut_reid or model: by place alone.
        return self._encode is not None

    def due(self, track, at):
        looks = self._tracks.get(track)
        if not looks:
            return True
        last_at, _, first = looks[-1]
        if first:
            return at - last_at >= PAIR_GAP_S  # The second of the pair.
        return at - last_at >= EVERY_S - PAIR_GAP_S  # The next pair.

    def observe(self, at, jpeg, people):
        """``people``: (track, box in 1/1 of the frame) of one frame, its JPEG."""
        wanted = [(track, box) for track, box in people
                  if track and box is not None and self.due(track, at)]
        if not wanted or not self._ready():
            return
        try:
            image = self._decode(jpeg)
            height, width = image.shape[:2]
            vectors = self._encode(image, [(b[0] * width, b[1] * height, b[2] * width,
                                            b[3] * height) for _, b in wanted])
        except Exception:
            self.failures += 1
            return
        for (track, _), vector in zip(wanted, vectors):
            if vector is None:
                continue  # Too small to describe.
            looks = self._tracks.setdefault(track, deque(maxlen=PER_TRACK))
            first = not (looks and looks[-1][2] and at - looks[-1][0] < EVERY_S)
            looks.append((at, [float(v) for v in vector], first))
            self._tracks.move_to_end(track)
        for track in [k for k, v in self._tracks.items() if at - v[-1][0] > KEEP_S]:
            del self._tracks[track]
        while len(self._tracks) > MAX_TRACKS:
            self._tracks.popitem(last=False)

    def _pairs(self, track):
        looks = list(self._tracks.get(track, ()))
        return [(looks[i], looks[i + 1]) for i in range(len(looks) - 1)
                if looks[i][2] and not looks[i + 1][2]]

    def compare(self, before, after):
        """Similarity of the last pair of (track, at) before and the first pair after,
        or None while a side has no pair."""
        old = [p for p in self._pairs(before[0]) if p[1][0] <= before[1] + 1.0]
        new = [p for p in self._pairs(after[0]) if p[0][0] >= after[1] - 1.0]
        if not old or not new:
            return None
        a, b = _mean(old[-1]), _mean(new[0])
        return None if a is None or b is None else sum(x * y for x, y in zip(a, b))

    def ready(self, after):
        """Whether the one who reappeared has a pair of looks yet."""
        return any(p[0][0] >= after[1] - 1.0 for p in self._pairs(after[0]))
