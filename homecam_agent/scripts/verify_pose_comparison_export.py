#!/usr/bin/env python3
"""Offline real-image PyTorch/ONNX equivalence and official letterbox pixel check."""
import argparse
import os
from pathlib import Path

from replay_fall_baseline import sha, write_json
from experimental_pose_input import letterbox
from review_fall_annotations import require


def run(args):
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    import cv2
    import numpy as np
    import onnxruntime as ort
    import torch
    from ultralytics import YOLO
    from ultralytics.data.augment import LetterBox

    require(not args.output.exists(), 'output exists')
    expected = {
        'n': 'eb3bb8268828aeaf515cec23a4bfafd793944a86fe9af94ba7823609c14522a9',
        's': 'a083adb42303728ae14c4bd6bd56d80da46f82fb2564dbd6f31dcc92ea321646',
    }
    exports = {
        'n': '1d6045d98de0fb825c50fa29eb6d2323b1e03be0c666ddb5ecbe02dcd37a0dfa',
        's': '324afc556c8c4dc15b91cce773b3165558796e889a63988e612edb86698b3023',
    }
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    cv2.setNumThreads(1)
    checks = []
    for size in [(640, 400), (1280, 720), (321, 239), (400, 640), (640, 640)]:
        image = np.arange(size[0]*size[1]*3, dtype=np.uint8).reshape(size[1], size[0], 3)
        actual, _ = letterbox(image)
        official = LetterBox(new_shape=(640, 640), auto=False, scale_fill=False,
                             scaleup=True)(image=image)
        require(np.array_equal(actual, official), 'letterbox pixel mismatch')
    for name in ('n', 's'):
        weights = getattr(args, name+'weights')
        onnx = weights.with_suffix('.onnx')
        require(sha(weights) == expected[name], 'wrong checkpoint')
        require(sha(onnx) == exports[name], 'wrong ONNX export')
        model = YOLO(str(weights)).model.eval().float().fuse()
        head = model.model[-1]
        head.export, head.format, head.dynamic, head.max_det = True, 'onnx', False, 300
        opt = ort.SessionOptions()
        opt.intra_op_num_threads, opt.inter_op_num_threads = 2, 1
        opt.add_session_config_entry('session.intra_op.allow_spinning', '0')
        session = ort.InferenceSession(str(onnx), sess_options=opt,
                                       providers=['CPUExecutionProvider'])
        for cid, frame in [('SYN007', 43), ('SYN011', 10), ('SYN082', 82)]:
            cap = cv2.VideoCapture(str(args.media/(cid+'.mp4')))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
            ok, raw = cap.read()
            cap.release()
            require(ok, 'decode failure')
            for padded in (False, True):
                image = letterbox(raw)[0] if padded else raw
                blob = cv2.dnn.blobFromImage(image, 1/255, (640, 640), swapRB=True, crop=False)
                with torch.inference_mode():
                    prediction = model(torch.from_numpy(blob))
                if isinstance(prediction, tuple):
                    prediction = prediction[0]
                pt = prediction.detach().cpu().numpy()[0]
                out = session.run(None, {session.get_inputs()[0].name: blob})[0][0]
                require(pt.shape == out.shape == (300, 57), 'output shape mismatch')
                a, b = pt[pt[:, 4] >= .10], out[out[:, 4] >= .10]
                require(a.shape == b.shape, 'qualifying pose count mismatch')
                if len(a):
                    a, b = a[np.argsort(-a[:, 4])], b[np.argsort(-b[:, 4])]
                    require(np.allclose(a, b, rtol=1e-4, atol=.02), 'ONNX output mismatch')
                peak = abs(float(pt[:, 4].max())-float(out[:, 4].max()))
                require(peak < 1e-4, 'peak confidence mismatch')
                checks.append(dict(model=name, case_id=cid, frame_index=frame, letterbox=padded,
                                   qualifying_poses=len(a), peak_confidence_difference=peak,
                                   maximum_absolute_difference=float(np.abs(a-b).max())
                                   if len(a) else None))
    write_json(args.output, dict(letterbox_pixel_checks=5, actual_image_checks=checks,
                                 weights_sha256=expected, onnx_sha256=exports,
                                 script_sha256=sha(Path(__file__)), passed=True))
    print(f'PASS: 5 pixel checks; {len(checks)} real-image export checks', flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('nweights', 'sweights', 'media', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(parser.parse_args())
