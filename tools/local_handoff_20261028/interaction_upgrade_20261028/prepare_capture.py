"""Make an isolated copy for paired microphone/reference diagnostics on the robot."""
from pathlib import Path
import shutil

base = Path('/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028')
root = Path('/tmp/malbut_interaction_20261028')
root.mkdir(mode=0o700, exist_ok=True)
package = base/'install/malbut_stt/lib/python3.10/site-packages/malbut_stt'
shutil.copytree(package, root/'code/malbut_stt', dirs_exist_ok=True)
p = root/'code/malbut_stt/echo/worker.py'
s = p.read_text()
s = s.replace('        processor = anchors = calibration = None',
'''        paired = open('/tmp/malbut_interaction_20261028/paired.f32', 'wb')
        stamps = open('/tmp/malbut_interaction_20261028/stamps.f64', 'wb')
        processor = anchors = calibration = None''')
s = s.replace('                processor.process_array(mic, aligned)',
'''                np.savez('/tmp/malbut_interaction_20261028/training.npz', mic=mic, far=aligned,
                         gain=calibration['microphone_gain'])
                processor.process_array(mic, aligned)''')
s = s.replace('            clean = processor.process_array(x, far)',
'''            paired.write(np.column_stack((x, far)).astype('<f4').tobytes())
            stamps.write(np.array([timestamp], dtype='<f8').tobytes())
            paired.flush()
            stamps.flush()
            clean = processor.process_array(x, far)''')
s = s.replace('        local_stop.set()\n        # The parent',
'''        if 'paired' in locals():
            paired.close()
            stamps.close()
        local_stop.set()
        # The parent''')
p.write_text(s)
s=(base/'start-injection.sh').read_text()
s=s.replace('export MALBUT_STT_DIAGNOSTIC_DIR=/tmp/malbut_aec_fix_20261028/e2e_dual_diagnostics',
            'export MALBUT_STT_DIAGNOSTIC_DIR=/tmp/malbut_interaction_20261028/diagnostics')
s=s.replace('source "$release/environment.sh"', 'source "$release/environment.sh"\nexport PYTHONPATH=/tmp/malbut_interaction_20261028/code:$PYTHONPATH')
(root/'start-capture.sh').write_text(s)
(root/'start-capture.sh').chmod(0o700)
print('Isolated diagnostic package ready')
