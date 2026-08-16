"""Static M0 scope and dependency contract tests."""

import sys
from pathlib import Path
from xml.etree import ElementTree


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT / 'malbut_voice'


def _production_text():
    return '\n'.join(
        path.read_text(encoding='utf-8')
        for path in sorted(SOURCE_ROOT.glob('*.py'))
    )


def test_package_has_no_wake_ros_http_tts_or_tool_authority_surface():
    """Keep M0 independent from continuous and effect-authorizing systems."""
    source = _production_text()
    forbidden = (
        'import rclpy',
        'requests.',
        'urllib.request',
        'http.server',
        'wake_word',
        'WakeWord',
        'text_to_speech',
        'speaker_identity_verified=True',
        'execution_authority=True',
    )

    for marker in forbidden:
        assert marker not in source
    assert not (PACKAGE_ROOT / 'launch').exists()


def test_capture_source_has_no_tempfile_shell_or_unbounded_communicate():
    """Keep PCM in memory and subprocess I/O explicitly bounded."""
    source = _production_text()

    assert 'tempfile' not in source
    assert 'NamedTemporaryFile' not in source
    assert '.communicate(' not in source
    assert 'shell=True' not in source
    assert 'shell=False' in source
    assert 'CLOCK_BOOTTIME' in source
    assert 'SIGTERM' in source
    assert 'SIGKILL' in source


def test_optional_faster_whisper_is_not_imported_at_package_import():
    """Allow ordinary environments to import the package without STT extras."""
    assert 'faster_whisper' not in sys.modules

    import malbut_voice

    assert malbut_voice.VoiceConfig
    assert 'faster_whisper' not in sys.modules


def test_package_dependencies_do_not_add_transport_or_wake_authority():
    """Declare only the independent local runtime boundary dependencies."""
    root = ElementTree.parse(PACKAGE_ROOT / 'package.xml').getroot()
    dependencies = {
        element.text
        for tag in ('depend', 'exec_depend')
        for element in root.findall(tag)
    }

    assert 'malbut_agent_server' in dependencies
    assert 'alsa-utils' in dependencies
    assert 'rclpy' not in dependencies
    assert 'std_msgs' not in dependencies
    assert 'wake_word' not in dependencies


def test_console_entrypoint_exposes_only_operator_one_shot_cli():
    """Install one explicit CLI and no ROS node or continuous listener."""
    setup_text = (PACKAGE_ROOT / 'setup.py').read_text(encoding='utf-8')
    cli_text = (SOURCE_ROOT / 'cli.py').read_text(encoding='utf-8')

    assert 'malbut-microphone-stt' in setup_text
    assert '--microphone' in cli_text
    assert '--check' in cli_text
    assert '--show-transcript' in cli_text
    assert '--device' not in cli_text
    assert '--model' not in cli_text
    assert '--wake' not in cli_text
