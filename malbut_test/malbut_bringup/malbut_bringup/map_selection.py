"""Remember one successfully loaded map, shared by CLI and web Bringup."""

import os
from pathlib import Path
import tempfile


def remembered_map(selection_file):
    """Return an existing saved YAML, or let Bringup use its default map."""
    if not selection_file:
        return ''
    try:
        value = Path(selection_file).expanduser().read_text(encoding='utf-8').strip()
        if not value:
            return ''
        path = Path(value).expanduser()
        if path.is_absolute() and path.suffix in ('.yaml', '.yml') and path.is_file():
            return str(path.resolve())
    except (OSError, ValueError):
        pass
    return ''


def remember_map(selection_file, map_path):
    """Atomically replace a tiny preference file after a successful map load."""
    if not selection_file:
        return
    target = Path(selection_file).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=target.parent,
                prefix=target.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(str(Path(map_path).expanduser().resolve()) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
