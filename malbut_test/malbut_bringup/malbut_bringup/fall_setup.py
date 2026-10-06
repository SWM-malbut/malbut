"""Validate fall startup configuration without reading keys or opening a journal."""

from pathlib import Path
import stat
from urllib.parse import urlsplit


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


def prepare_fall_upload(config_file, environment):
    """
    Compose the metadata worker from the monitor and existing web settings.

    No credentials are read and no DB/network is opened. Environment origin and
    token must be a complete pair; never send a key-sync token to another origin.
    """
    from malbut_agent_server.fall_preflight import read_settings
    from malbut_agent_server.adapters.outbound.homecam_fall_events import HomecamFallEventClient

    try:
        settings = read_settings(Path(config_file))
        backend = environment.get('HOMECAM_BACKEND_URL', '').strip()
        token_file = environment.get('HOMECAM_DEVICE_TOKEN_FILE', '').strip()
        media_id = environment.get('HOMECAM_DEVICE_ID', '').strip()
        if media_id and media_id != settings.device_id:
            raise ValueError('device mismatch')
        if backend or token_file:
            if not backend or not token_file:
                raise ValueError('incomplete web settings')
            host = urlsplit(backend).hostname
            if not host:
                raise ValueError('web host required')
            hosts = (host,)
        elif settings.key_sync is not None:
            backend = settings.key_sync.base_url
            token_file = str(settings.key_sync.token_file)
            hosts = settings.key_sync.allow_hosts
        else:
            return None
        if not Path(token_file).is_absolute():
            raise ValueError('absolute token path required')
        HomecamFallEventClient(
            base_url=backend, device_id=settings.device_id,
            device_token='validation-only', allowed_hosts=set(hosts))
        return [
            '--journal', str(settings.journal_path), '--device-id', settings.device_id,
            '--base-url', backend,
            *[part for host in hosts for part in ('--allow-host', host)],
            '--token-file', token_file, '--execute', '--upload-clips',
        ]
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        # Do not echo a URL containing credentials or private configuration text.
        raise RuntimeError(
            'Fall upload configuration invalid: check the web origin, absolute device '
            'token path and matching device IDs; detection remains enabled.') from exc
