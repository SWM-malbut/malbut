"""Tests for fail-closed device credential loading."""

from homecam_detector.credentials import is_valid_device_token, load_device_token


VALID_TOKEN = (
    "hc1.123e4567-e89b-42d3-a456-426614174000." + "a" * 64
)


def test_reads_token_file_and_trims_only_line_endings(tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(VALID_TOKEN + "\n", encoding="utf-8")
    token = load_device_token(
        {
            "HOMECAM_DEVICE_TOKEN_FILE": str(token_file),
            "HOMECAM_DEVICE_TOKEN": "must-not-win",
        }
    )
    assert token == VALID_TOKEN


def test_explicit_missing_file_does_not_fall_back_to_environment(tmp_path) -> None:
    token = load_device_token(
        {
            "HOMECAM_DEVICE_TOKEN_FILE": str(tmp_path / "missing"),
            "HOMECAM_DEVICE_TOKEN": "must-not-win",
        }
    )
    assert token == ""


def test_environment_fallback_is_available_for_manual_development() -> None:
    assert load_device_token({"HOMECAM_DEVICE_TOKEN": VALID_TOKEN}) == VALID_TOKEN


def test_rejects_header_injection_and_wrong_token_shape() -> None:
    assert is_valid_device_token(VALID_TOKEN)
    assert not is_valid_device_token(VALID_TOKEN + "\r\nInjected: value")
    assert not is_valid_device_token(
        "hc1.123e4567-e89b-12d3-a456-426614174000." + "a" * 64
    )
    assert load_device_token({"HOMECAM_DEVICE_TOKEN": "not-a-token"}) == ""


def test_rejects_credential_file_larger_than_bound(tmp_path) -> None:
    token_file = tmp_path / "oversized"
    token_file.write_bytes(b"a" * 4097)
    assert load_device_token({"HOMECAM_DEVICE_TOKEN_FILE": str(token_file)}) == ""
