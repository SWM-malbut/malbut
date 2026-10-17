"""The robot-to-web fall contract: malbut_interfaces/contracts/fall_events.json.

One source with the web (its copy is generated from the same file). A source
checkout (tests, tools, a symlinked robot build) reads the file next to this
package; an installed robot reads the copy installed with malbut_interfaces.
"""

import functools
import json
from pathlib import Path

RELATIVE = Path('contracts') / 'fall_events.json'


def _candidates():
    for parent in Path(__file__).resolve().parents:
        yield parent / 'malbut_interfaces' / RELATIVE
    try:
        from ament_index_python.packages import (
            get_package_share_directory, PackageNotFoundError)
    except ImportError:
        return
    try:
        yield Path(get_package_share_directory('malbut_interfaces')) / RELATIVE
    except PackageNotFoundError:
        return


@functools.lru_cache(maxsize=1)
def fall_contract():
    """The parsed contract; a missing file is a broken install, never a default."""
    for path in _candidates():
        if path.is_file():
            return json.loads(path.read_text(encoding='utf-8'))
    raise FileNotFoundError('fall contract not found: malbut_interfaces/' + str(RELATIVE))
