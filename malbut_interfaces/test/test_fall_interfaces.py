"""Check generated fall interfaces without a robot, DDS graph, or Cloud call."""

from pathlib import Path
import re

import pytest
from rclpy.serialization import deserialize_message, serialize_message
import yaml

from malbut_interfaces.msg import (
    FallControlHeartbeat,
    FallRuntimeStatus,
    FallSettingsReport,
    FallSettingsSnapshot,
)
from malbut_interfaces.srv import ApplyFallSettings


PACKAGE = Path(__file__).resolve().parents[1]
REPOSITORY = PACKAGE.parent
DOCUMENT = (
    REPOSITORY / 'malbut_agent_server/docs/fall/fall_manager_contract.md'
)
TYPES = (
    ApplyFallSettings.Request,
    ApplyFallSettings.Response,
    FallRuntimeStatus,
    FallControlHeartbeat,
    FallSettingsSnapshot,
    FallSettingsReport,
)


def _fields_from_text(definition):
    """Ignore ROS comments/defaults, preserving the declared field order."""
    fields = {}
    for line in definition.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        kind, name, *_ = line.split()
        assert name not in fields, f'duplicate field: {name}'
        fields[name] = kind
    return fields


def _generated_fields(message_type):
    aliases = {'boolean': 'bool', 'double': 'float64'}
    return {
        name: aliases.get(kind, kind)
        for name, kind in message_type.get_fields_and_field_types().items()
    }


def _section(document, title):
    content = document.split(title, 1)[1]
    return re.split(r'^##(?:#)? ', content, maxsplit=1, flags=re.M)[0]


@pytest.mark.parametrize('message_type', TYPES)
def test_generated_types_serialize_without_network(message_type):
    """Exercise the generated native type support, not a Python stand-in."""
    message = message_type()
    for name, kind in _generated_fields(message_type).items():
        value = {
            'string': f'test_{name}',
            'uint64': 2**64 - 1,
            'bool': True,
            'float64': 123.25,
        }[kind]
        setattr(message, name, value)
    restored = deserialize_message(serialize_message(message), message_type)
    assert restored == message


@pytest.mark.parametrize('message_type', TYPES)
def test_default_messages_do_not_grant_permission(message_type):
    message = message_type()
    for name, kind in _generated_fields(message_type).items():
        if kind == 'bool':
            assert getattr(message, name) is False
        if kind == 'uint64':
            assert getattr(message, name) == 0
    if hasattr(message, 'runtime_id'):
        assert message.runtime_id == ''


def test_unknown_times_and_states_have_explicit_defaults():
    status = FallRuntimeStatus()
    assert status.last_frame_age_s == -1.0
    assert status.runtime_state == 'waiting_settings'
    assert status.pause_reason == 'waiting_settings'
    assert status.analysis_state == 'idle'
    assert status.request_id == status.request_purpose == status.last_error_code == ''
    assert FallControlHeartbeat().server_checked_at == -1.0
    snapshot = FallSettingsSnapshot()
    assert snapshot.server_checked_at == -1.0
    assert snapshot.check_state == 'waiting'
    assert snapshot.reason_code == 'waiting_server'
    assert ApplyFallSettings.Response().reason_code == 'invalid_request'
    assert FallSettingsReport().reason_code == 'invalid_request'


@pytest.mark.parametrize('message_type', TYPES)
def test_unsigned_numbers_reject_negative_and_overflow(message_type):
    message = message_type()
    for name, kind in _generated_fields(message_type).items():
        if kind == 'uint64':
            for value in (-1, 2**64):
                with pytest.raises(AssertionError):
                    setattr(message, name, value)


@pytest.mark.parametrize('message_type,title', [
    (FallRuntimeStatus, '### 3.3 '),
    (FallControlHeartbeat, '### 3.5 '),
    (FallSettingsSnapshot, '### 3.7 '),
    (FallSettingsReport, '### 3.8 '),
])
def test_message_definition_matches_document(message_type, title):
    document = DOCUMENT.read_text(encoding='utf-8')
    section = _section(document, title)
    definition = re.search(r'```msg\n(.*?)\n```', section, re.S).group(1)
    declared = _fields_from_text(definition)
    generated = _generated_fields(message_type)
    source = _fields_from_text(
        (PACKAGE / 'msg' / f'{message_type.__name__}.msg').read_text()
    )
    assert list(generated.items()) == list(declared.items())
    assert source == declared
    if title != '### 3.3 ':
        table = dict(re.findall(
            r'^\| `([^`]+)` \| `([^`]+)` \|',
            section.split('```msg', 1)[0], re.M,
        ))
        assert table == declared


def test_service_matches_document_and_manifest():
    document = DOCUMENT.read_text(encoding='utf-8')
    definition = re.search(r'```srv\n(.*?)\n```', document, re.S).group(1)
    source = (PACKAGE / 'srv/ApplyFallSettings.srv').read_text()
    for declared, stored, message_type in zip(
        definition.split('---'), source.split('---'),
        (ApplyFallSettings.Request, ApplyFallSettings.Response),
    ):
        fields = _fields_from_text(declared)
        assert _fields_from_text(stored) == fields
        assert list(_generated_fields(message_type).items()) == list(fields.items())
    manifest = yaml.safe_load(
        re.search(r'```yaml\n(.*?)\n```', document, re.S).group(1)
    )
    assert manifest['command'] == {
        'kind': 'SERVICE',
        'name': '/malbut/falls/settings/apply',
        'type': 'malbut_interfaces/srv/ApplyFallSettings',
    }
    assert _generated_fields(ApplyFallSettings.Request) == {
        name: spec['type'] for name, spec in manifest['input']['fields'].items()
    }
    assert all('default' not in spec for spec in manifest['input']['fields'].values())


def test_setting_change_is_not_exposed_as_a_general_agent_command():
    """A message definition is not authorization to change owner consent."""
    for path in (PACKAGE / 'capabilities').glob('*.yaml'):
        manifest = yaml.safe_load(path.read_text())
        assert manifest['capability']['id'] != 'apply_fall_settings'
        assert manifest['command']['name'] != '/malbut/falls/settings/apply'


@pytest.mark.parametrize('relative', [
    'CMakeLists.txt', 'package.xml',
    'srv/ApplyFallSettings.srv',
    'msg/FallRuntimeStatus.msg', 'msg/FallControlHeartbeat.msg',
    'msg/FallSettingsSnapshot.msg', 'msg/FallSettingsReport.msg',
])
def test_deployment_copy_matches_source(relative):
    # The deployment-only tree can also be tested on its own.
    deployment = REPOSITORY / 'malbut_test/malbut_interfaces'
    if deployment.is_dir():
        assert (PACKAGE / relative).read_bytes() == (deployment / relative).read_bytes()


def test_deployment_does_not_duplicate_source_tests():
    deployment = REPOSITORY / 'malbut_test/malbut_interfaces'
    if deployment.is_dir():
        assert not (deployment / 'test/test_fall_interfaces.py').exists()
