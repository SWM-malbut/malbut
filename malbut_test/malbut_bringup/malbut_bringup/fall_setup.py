"""Validate fall startup configuration without reading keys or opening a journal."""

from pathlib import Path
import stat


def prepare_fall_monitor(mode, config_file):
    """Return validated launch inputs, or None when fall startup is disabled."""
    if mode not in {'auto', 'true', 'false'}:
        raise RuntimeError('fall_monitor must be auto, true or false')
    if mode == 'false':
        return None
    if not config_file:
        raise RuntimeError('fall_config must name a runtime configuration file')
    path = Path(config_file).expanduser().absolute()
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        if mode == 'auto':
            return None
        raise RuntimeError(f'Fall configuration is missing: {path}') from exc
    except OSError as exc:
        raise RuntimeError('Cannot inspect fall_config') from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
        raise RuntimeError('fall_config must be a regular file of at most 16 KiB')
    try:
        from malbut_agent_server.fall_runtime import FallNodeSettings

        settings = FallNodeSettings.parse(path.read_text())
    except (ValueError, TypeError, OSError, ImportError, RuntimeError) as exc:
        # Configuration can contain private paths: do not echo JSON or secrets.
        raise RuntimeError('Invalid fall_config; complete the runtime settings first') from exc
    return str(path), settings.image_topic
