#!/usr/bin/env python3
"""Download a digest-verified official checkpoint; export without training."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import urllib.request

URL = 'https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26s-pose.pt'
DIGEST = 'a083adb42303728ae14c4bd6bd56d80da46f82fb2564dbd6f31dcc92ea321646'


def run(folder):
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    import numpy as np
    import onnxruntime as ort
    import torch
    import ultralytics
    from ultralytics import YOLO

    folder.mkdir(mode=0o700, exist_ok=False)
    weights = folder/'yolo26s-pose.pt'
    urllib.request.urlretrieve(URL, weights)
    if hashlib.sha256(weights.read_bytes()).hexdigest() != DIGEST:
        raise ValueError('official checkpoint digest mismatch; not loading')
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    args = dict(format='onnx', imgsz=640, opset=12, simplify=True,
                dynamic=False, end2end=True, device='cpu')
    output = Path(YOLO(str(weights)).export(**args))
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
    session = ort.InferenceSession(str(output), sess_options=options,
                                   providers=['CPUExecutionProvider'])
    inp = session.get_inputs()[0]
    result = session.run(None, {inp.name: np.zeros((1, 3, 640, 640), np.float32)})[0]
    if inp.shape != [1, 3, 640, 640] or result.shape != (1, 300, 57):
        raise ValueError('unexpected model contract')
    if not np.isfinite(result).all():
        raise ValueError('non-finite export output')
    metadata = dict(url=URL, weights_sha256=DIGEST, export_args=args,
                    model_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                    versions=dict(python=platform.python_version(), torch=torch.__version__,
                                  ultralytics=ultralytics.__version__, ort=ort.__version__),
                    input_shape=inp.shape, output_shape=list(result.shape))
    with (folder/'export.json').open('x') as stream:
        json.dump(metadata, stream, indent=2)
    print(json.dumps(metadata), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    run(parser.parse_args().output)
