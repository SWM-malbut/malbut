"""What each person in view looks like, as numbers, for the person tracking (SWM25-267).

ReID only splits (design decision 10): when someone shows up where an unseen person
was, the Agent asks how alike the two looked; clearly unlike means someone else.
This keeps OSNet-AIN descriptors (malbut_reid's encoder, installed with the Bringup for
following) of each person in view, by YOLO track, for 30 minutes: two looks a second
apart every five seconds. A comparison averages a pair on each side: the last pair
before the person was gone and the first pair after someone reappeared (two against
two: on the 10-17 pairs on the Jetson single looks wobbled, 5 of 9 same people fell
below 0.64; pairs did not). When the Agent asks about someone who has no pair yet
since they reappeared, their pair is taken at once instead of on the five-second round
(10-19 18:28: a lying person's track began as a sliver at the screen edge, its pair was
taken then, the Agent asked 2 s later and the next pair was due after the 3 s wait).
No identity is decided here. Without the encoder or its model, or without a pair on
both sides, the answer is "none" and the Agent goes by place alone. Never raises into
the fall check.

A track gets its first look only after half a second in view (11-17): on the robot a
detection flickering under new track numbers (220 new tracks in ten minutes) took a
look each time, 186 ms of CPU apiece, for looks that never became a pair. The price:
a track now needs 1.5 s in view for its pair, not 1 s. On that day's log (5,721
tracks, 463 comparisons) the looks halved, 9,682 to 5,023, and the comparisons that
got a value went from 6 to 1; the four of those lost that had been answered were the
same person (0.95-0.98), which decides as no answer does.
"""

from collections import OrderedDict, deque
import math

EVERY_S = 5.0          # a pair of looks per person every five seconds
PAIR_GAP_S = 1.0       # its two looks a second apart (as tested on the Jetson, 10-19)
KEEP_S = 1800.0        # as long as the tracking remembers someone unseen
MAX_TRACKS = 128
PER_TRACK = 32         # 16 pairs (~80 s): exceeds LINK_S/SOLO_S (60 s) verdict windows
SEEN_FOR_S = 0.5       # in view this long before a track's first look
SEEN_GAP_S = 1.0       # unseen longer than this: its time in view counts anew
ENCODER_THREADS = 2    # per look on the robot: 6 threads 33 ms/184 ms CPU, 2 63 ms/123 ms
MODEL = '~/.cache/malbut_perception/osnet_ain_x1_0_msmt17.onnx'


def _load_encoder(model_path, set_threads=None):
    """malbut_reid's OSNet encoder as encode(bgr, pixel boxes) -> vectors (or ImportError).

    On the CPU with OpenCV, as REID_SPLIT was set on the Jetson (10-19). The encoder's
    "auto" took TensorRT on the robot: 1.3-1.5 GB more in this process with 485 MB of
    memory left, and minutes of engine building at the first start. On the CPU a look
    took about 47 ms, two per person every five seconds.

    OpenCV's threads are set for the whole process: six of them finished a look
    sooner but spent half as much CPU again as two (11-17), with the same vectors.
    """
    from malbut_reid.models import BoundingBox, ImageDetection
    from malbut_reid.reid.osnet_encoder import OsNetPersonEncoder

    if set_threads is None:
        import cv2
        set_threads = cv2.setNumThreads
    set_threads(ENCODER_THREADS)
    encoder = OsNetPersonEncoder(model_path, dnn_target='cpu', inference_backend='opencv')

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
        # Earliest completed pair per track, kept until the track is forgotten so
        # long-running group verdict checks still have their initial reference look.
        self._first_pair = {}
        self._wanted = {}   # track -> since when a comparison waits for its pair
        self._in_view = OrderedDict()  # track -> (in view since, last seen)
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

    def see(self, at, tracks):
        """The tracks of one frame: how long each has been in view without a break."""
        for track in tracks:
            if not track:
                continue
            since, last = self._in_view.get(track, (at, at))
            if at < last:
                continue  # An older frame.
            self._in_view[track] = (at if at - last > SEEN_GAP_S else since, at)
            self._in_view.move_to_end(track)
        while self._in_view and (
                len(self._in_view) > MAX_TRACKS
                or at - next(iter(self._in_view.values()))[1] > SEEN_GAP_S):
            self._in_view.popitem(last=False)

    def due(self, track, at):
        looks = self._tracks.get(track)
        if not looks:
            # Its first look: not for a detection that flickers and is gone.
            seen = self._in_view.get(track)
            return seen is not None and at - seen[0] >= SEEN_FOR_S
        last_at, _, first = looks[-1]
        if first:
            return at - last_at >= PAIR_GAP_S  # The second of the pair.
        if track in self._wanted:
            return True  # A comparison waits: the next pair now.
        return at - last_at >= EVERY_S - PAIR_GAP_S  # The next pair.

    def want(self, track, since):
        """A comparison waits for this track's pair from ``since`` on: take it now."""
        if not self.ready((track, since)):
            self._wanted[track] = since
            while len(self._wanted) > MAX_TRACKS:
                self._wanted.pop(next(iter(self._wanted)))

    def observe(self, at, jpeg, people):
        """``people``: (track, box in 1/1 of the frame) of one frame, its JPEG."""
        self.see(at, [track for track, _ in people])
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
            if not first and len(looks) >= 2 and looks[-2][2] and track not in self._first_pair:
                self._first_pair[track] = (looks[-2], looks[-1])
            self._tracks.move_to_end(track)
            if track in self._wanted and self.ready((track, self._wanted[track])):
                del self._wanted[track]
        for track in [k for k, v in self._tracks.items() if at - v[-1][0] > KEEP_S]:
            del self._tracks[track]
            self._first_pair.pop(track, None)
            self._wanted.pop(track, None)
        while len(self._tracks) > MAX_TRACKS:
            dropped = self._tracks.popitem(last=False)[0]
            self._first_pair.pop(dropped, None)
            self._wanted.pop(dropped, None)

    def _pairs(self, track):
        looks = list(self._tracks.get(track, ()))
        pairs = [(looks[i], looks[i + 1]) for i in range(len(looks) - 1)
                 if looks[i][2] and not looks[i + 1][2]]
        first = self._first_pair.get(track)
        if first is not None and (not pairs or pairs[0][0][0] > first[0][0]):
            pairs.insert(0, first)
        return pairs

    def in_view(self, track, at=None):
        """Whether ``track`` was seen in the active frame window."""
        seen = self._in_view.get(track)
        if seen is None:
            return False
        return at is None or at - seen[1] <= SEEN_GAP_S

    def _pair_at_or_before(self, pairs, at):
        before = [p for p in pairs if p[1][0] <= at + 1.0]
        return before[-1] if before else None

    def _pair_for_check_before(self, pairs, at):
        chosen = self._pair_at_or_before(pairs, at)
        if chosen is not None:
            return chosen
        # A track linked right after entering view finishes its first pair a second later.
        soon = [p for p in pairs if at - 1.0 <= p[0][0] <= at + EVERY_S + PAIR_GAP_S]
        return soon[0] if soon else None

    def _pair_for_check_after(self, pairs, at):
        chosen = self._pair_at_or_before(pairs, at)
        if chosen is not None:
            return chosen
        # If the track just started and want() captured a fresh pair right after ``at``.
        fresh = [p for p in pairs if p[0][0] >= at - 1.0]
        return fresh[0] if fresh else None

    def compare(self, before, after, *, mode='reappear'):
        """Similarity of two appearance pairs, or None while a side has no pair.

        ``mode='reappear'`` compares the last pair before ``before[1]`` against the
        first pair after ``after[1]`` (unseen person returning).
        ``mode='check'`` compares the reference pair around ``before[1]`` against
        the latest pair up to ``after[1]`` (group verdict after LINK_S/SOLO_S).
        """
        if mode == 'check':
            old_pair = self._pair_for_check_before(self._pairs(before[0]), before[1])
            new_pair = self._pair_for_check_after(self._pairs(after[0]), after[1])
            if old_pair is None or new_pair is None:
                return None
            if before[0] == after[0] and new_pair[0][0] <= old_pair[0][0]:
                return None  # Never trivially compare a pair against itself.
        elif mode == 'reappear':
            old = [p for p in self._pairs(before[0]) if p[1][0] <= before[1] + 1.0]
            new = [p for p in self._pairs(after[0]) if p[0][0] >= after[1] - 1.0]
            if not old or not new:
                return None
            old_pair, new_pair = old[-1], new[0]
        else:
            raise ValueError('unsupported appearance comparison mode')
        a, b = _mean(old_pair), _mean(new_pair)
        return None if a is None or b is None else sum(x * y for x, y in zip(a, b))

    def ready(self, after, *, mode='reappear'):
        """Whether ``after`` already has a pair usable for ``mode``."""
        pairs = self._pairs(after[0])
        if mode == 'check':
            return self._pair_for_check_after(pairs, after[1]) is not None
        if mode == 'reappear':
            return any(p[0][0] >= after[1] - 1.0 for p in pairs)
        raise ValueError('unsupported appearance comparison mode')
