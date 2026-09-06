"""Unit tests for the central Capability Manifest registry."""

from copy import deepcopy

import pytest
import yaml

from malbut_system_manager.manifest_registry import (
    ManifestError,
    ManifestRegistry,
    RequestValidationError,
)
from malbut_system_manager.models import (
    CommandKind,
    ExecutionMode,
    MissionPriority,
)


class _FakeFollowPersonGoal:
    """Small generated-message stand-in used by rosidl_runtime_py."""

    __slots__ = (
        '_target_mode',
        '_target_person_id',
        '_desired_distance_m',
    )
    SLOT_TYPES = (None, None, None)

    def __init__(self) -> None:
        self._target_mode = 0
        self._target_person_id = ''
        self._desired_distance_m = 0.0

    @classmethod
    def get_fields_and_field_types(cls) -> dict[str, str]:
        """Match the type strings emitted by a generated ROS message."""
        return {
            'target_mode': 'uint8',
            'target_person_id': 'string',
            'desired_distance_m': 'float',
        }

    @property
    def target_mode(self) -> int:
        return self._target_mode

    @target_mode.setter
    def target_mode(self, value: int) -> None:
        if not isinstance(value, int) or not 0 <= value <= 255:
            raise ValueError('target_mode must be a uint8')
        self._target_mode = value

    @property
    def target_person_id(self) -> str:
        return self._target_person_id

    @target_person_id.setter
    def target_person_id(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError('target_person_id must be a string')
        self._target_person_id = value

    @property
    def desired_distance_m(self) -> float:
        return self._desired_distance_m

    @desired_distance_m.setter
    def desired_distance_m(self, value: float) -> None:
        if not isinstance(value, float):
            raise TypeError('desired_distance_m must be a float')
        self._desired_distance_m = value


class _FakeFollowPerson:
    Goal = _FakeFollowPersonGoal


def _document() -> dict:
    return {
        'schema_version': 1,
        'capability': {
            'id': 'follow_person',
            'title': '사람 추적',
            'description': '선택한 사람을 안전거리에서 따라간다.',
        },
        'command': {
            'kind': 'ACTION',
            'name': '/follow_person',
            'type': 'malbut_interfaces/action/FollowPerson',
        },
        'input': {
            'fields': {
                'target_mode': {
                    'type': 'uint8',
                    'description': '추적 대상 선택 방식',
                    'default': 0,
                },
                'target_person_id': {
                    'type': 'string',
                    'description': '등록된 사람 식별자',
                    'default': '',
                },
                'desired_distance_m': {
                    'type': 'float32',
                    'description': '유지할 희망 거리',
                    'default': 1.0,
                },
            },
        },
        'execution': {
            'mode': 'FOREGROUND',
            'priority': 'NORMAL',
        },
    }


def _write_manifest(directory, document, name='follow_person.yaml'):
    path = directory / name
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )
    return path


def _registry(directory) -> ManifestRegistry:
    def resolve_action(interface_name):
        if interface_name != 'malbut_interfaces/action/FollowPerson':
            raise ValueError(f'unknown fake interface: {interface_name}')
        return _FakeFollowPerson

    return ManifestRegistry(
        directory,
        action_resolver=resolve_action,
        service_resolver=lambda name: (_ for _ in ()).throw(
            ValueError(f'unknown fake service: {name}')
        ),
    )


def test_loads_follow_person_manifest_and_canonicalizes_float32(
    tmp_path,
) -> None:
    """Manifest float32 matches the generated Python type name float."""
    document = _document()
    document['command']['name'] = 'follow_person/'
    _write_manifest(tmp_path, document)

    registry = _registry(tmp_path)
    manifest = registry.get('follow_person')

    assert registry.all() == (manifest,)
    assert manifest.command_kind is CommandKind.ACTION
    assert manifest.command_name == '/follow_person'
    assert manifest.execution_mode is ExecutionMode.FOREGROUND
    assert manifest.priority is MissionPriority.NORMAL
    assert manifest.input_fields['desired_distance_m'].ros_type == 'float32'
    assert manifest.interface_type is _FakeFollowPerson


def test_parse_arguments_applies_defaults_and_accepts_overrides(
    tmp_path,
) -> None:
    _write_manifest(tmp_path, _document())
    registry = _registry(tmp_path)
    manifest = registry.get('follow_person')

    defaults, default_goal = registry.parse_arguments(manifest, '')
    assert defaults == {
        'target_mode': 0,
        'target_person_id': '',
        'desired_distance_m': 1.0,
    }
    assert default_goal.target_mode == 0
    assert default_goal.target_person_id == ''
    assert default_goal.desired_distance_m == pytest.approx(1.0)

    values, goal = registry.parse_arguments(
        manifest,
        'target_mode: 1\n'
        'target_person_id: family-01\n'
        'desired_distance_m: 1.25\n',
    )
    assert values['desired_distance_m'] == pytest.approx(1.25)
    assert goal.target_mode == 1
    assert goal.target_person_id == 'family-01'
    assert goal.desired_distance_m == pytest.approx(1.25)

    rebuilt = registry.build_message(manifest, values)
    assert rebuilt is not goal
    assert rebuilt.target_person_id == 'family-01'


@pytest.mark.parametrize(
    ('second_id', 'second_name', 'message'),
    [
        ('follow_person', '/another_action', 'duplicate capability id'),
        ('another_capability', '///follow_person/', 'command name'),
    ],
)
def test_rejects_duplicate_ids_and_normalized_command_names(
    tmp_path,
    second_id,
    second_name,
    message,
) -> None:
    _write_manifest(tmp_path, _document(), 'first.yaml')
    second = _document()
    second['capability']['id'] = second_id
    second['command']['name'] = second_name
    _write_manifest(tmp_path, second, 'second.yaml')

    with pytest.raises(ManifestError, match=message):
        _registry(tmp_path)


@pytest.mark.parametrize(
    ('mutation', 'message'),
    [
        (('schema_version',), 'schema_version must be 1'),
        (('missing_execution',), 'missing key.*execution'),
        (('field_type',), 'declares.*string.*ROS interface uses.*float'),
        (('recursive_endpoint',), 'cannot recursively call'),
    ],
)
def test_rejects_invalid_manifest_contracts(
    tmp_path,
    mutation,
    message,
) -> None:
    document = deepcopy(_document())
    if mutation == ('schema_version',):
        document['schema_version'] = 2
    elif mutation == ('missing_execution',):
        del document['execution']
    elif mutation == ('field_type',):
        document['input']['fields']['desired_distance_m']['type'] = 'string'
    elif mutation == ('recursive_endpoint',):
        document['command']['name'] = '/malbut/mission/execute'
    _write_manifest(tmp_path, document)

    with pytest.raises(ManifestError, match=message):
        _registry(tmp_path)


def test_rejects_non_string_manifest_keys(tmp_path) -> None:
    document = _document()
    document[1] = 'invalid key'
    _write_manifest(tmp_path, document)

    with pytest.raises(ManifestError, match='keys must be strings'):
        _registry(tmp_path)


@pytest.mark.parametrize(
    ('arguments_yaml', 'message'),
    [
        ('[', 'not valid YAML'),
        ('- target_mode\n', 'root must be a mapping'),
        ('1: value\n', 'field names must be strings'),
        ('unknown_field: 1\n', 'Unknown input field'),
        (
            'desired_distance_m: not-a-number\n',
            'Input values do not match',
        ),
        (
            'target_mode:\n  invalid: type\n',
            'Input values do not match',
        ),
    ],
)
def test_rejects_invalid_request_arguments(
    tmp_path,
    arguments_yaml,
    message,
) -> None:
    _write_manifest(tmp_path, _document())
    registry = _registry(tmp_path)

    with pytest.raises(RequestValidationError, match=message):
        registry.parse_arguments(
            registry.get('follow_person'),
            arguments_yaml,
        )


def test_excessively_nested_arguments_are_request_validation_error(
    tmp_path,
) -> None:
    """Untrusted request depth errors never escape as ManifestError."""
    _write_manifest(tmp_path, _document())
    registry = _registry(tmp_path)
    nested = {}
    for _level in range(20):
        nested = {'level': nested}
    arguments_yaml = yaml.safe_dump({'desired_distance_m': nested})

    with pytest.raises(
        RequestValidationError,
        match='arguments_yaml: YAML nesting exceeds',
    ):
        registry.parse_arguments(
            registry.get('follow_person'),
            arguments_yaml,
        )


def test_rejects_missing_required_request_field(tmp_path) -> None:
    document = _document()
    del document['input']['fields']['target_person_id']['default']
    _write_manifest(tmp_path, document)
    registry = _registry(tmp_path)

    with pytest.raises(
        RequestValidationError,
        match='Missing required input field.*target_person_id',
    ):
        registry.parse_arguments(registry.get('follow_person'), '{}')
