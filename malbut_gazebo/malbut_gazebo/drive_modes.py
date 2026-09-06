"""Shared helpers for browser-controlled autonomous drive modes."""

AUTONOMOUS_MODES = {"patrol", "person_following", "roaming"}
TRIGGER_DRIVE_MODES = {"roaming"}


def common_mode_state(mode: str, status: dict) -> str:
    """Normalize patrol and roaming implementation states for the web API."""
    raw = str(status.get("state", "idle")).lower()
    if raw == "idle" or (mode == "patrol" and raw == "completed"):
        return "idle"
    if raw == "pausing":
        return "pausing"
    if raw == "paused":
        return "paused"
    if raw == "stopping":
        return "stopping"
    if mode == "patrol" and raw == "aborted":
        return "failed"
    return "active"
