"""Best-effort normal scheduling priority for the isolated audio worker.

Call before creating native, capture or IPC feeder threads: Linux threads
inherit the creating thread's nice value. This never changes a scheduling
policy, affinity, another process, or a process group. The launch owner's
RLIMIT_NICE controls whether the requested priority is permitted.
"""

import json
import os
import sys


TARGET_NICE = -5


def configure_audio_worker_priority():
    """Adjust only this worker's current thread, retaining stronger priority.

    Unsupported APIs and insufficient permission leave audio operational at
    its existing priority. A successful setter is not proof of application;
    always read the effective value back before reporting success.
    """
    result = dict(target_nice=TARGET_NICE, effective_nice=None, status='unsupported')
    if all(hasattr(os, name) for name in ('PRIO_PROCESS', 'getpriority', 'setpriority')):
        try:
            result['effective_nice'] = os.getpriority(os.PRIO_PROCESS, 0)
        except Exception:
            result['status'] = 'priority_unavailable'
        else:
            if result['effective_nice'] <= TARGET_NICE:
                result['status'] = 'preserved'
            else:
                try:
                    os.setpriority(os.PRIO_PROCESS, 0, TARGET_NICE)
                except PermissionError:
                    result['status'] = 'permission_denied'
                except (AttributeError, NotImplementedError):
                    result['status'] = 'unsupported'
                except Exception:
                    result['status'] = 'set_failed'
                else:
                    result['status'] = 'unverified'
                try:
                    result['effective_nice'] = os.getpriority(os.PRIO_PROCESS, 0)
                except Exception:
                    result['effective_nice'] = None
                else:
                    if result['status'] == 'unverified':
                        result['status'] = ('applied' if result['effective_nice'] <= TARGET_NICE
                                            else 'not_applied')
    level = 'INFO' if result['status'] in ('applied', 'preserved') else 'WARNING'
    try:
        print(f'AEC scheduling {level} ' + json.dumps(result, sort_keys=True),
              file=sys.stderr, flush=True)
    except Exception:
        pass  # A closed log destination must not prevent audio startup.
    return result
