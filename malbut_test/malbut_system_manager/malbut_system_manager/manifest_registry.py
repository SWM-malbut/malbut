"""Load and validate the central Malbut Capability Manifest registry."""

from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from ament_index_python.packages import get_package_share_directory
from rosidl_runtime_py import set_message_fields
from rosidl_runtime_py.utilities import get_action, get_service
import yaml

from .models import (
    CapabilityManifest,
    CommandKind,
    ExecutionMode,
    ExecutionResource,
    InputField,
    MissionPriority,
)


SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_YAML_DEPTH = 16
CAPABILITY_ID_PATTERN = re.compile(r'^[a-z][a-z0-9_]*$')
MANAGER_ACTION_NAME = '/malbut/mission/execute'


class ManifestError(ValueError):
    """A Capability Manifest does not satisfy the project contract."""


class RequestValidationError(ValueError):
    """A public request cannot construct the registered ROS message."""


class ManifestRegistry:
    """Validated allowlist loaded from malbut_interfaces package share."""

    def __init__(
        self,
        manifest_directory: str | Path | None = None,
        *,
        action_resolver: Callable[[str], Any] = get_action,
        service_resolver: Callable[[str], Any] = get_service,
    ) -> None:
        if manifest_directory is None:
            share = Path(get_package_share_directory('malbut_interfaces'))
            manifest_directory = share / 'capabilities'
        self.directory = Path(manifest_directory)
        self._action_resolver = action_resolver
        self._service_resolver = service_resolver
        self._manifests: dict[str, CapabilityManifest] = {}
        self.reload()

    def reload(self) -> None:
        """Atomically replace the registry with all valid central files."""
        if not self.directory.is_dir():
            raise ManifestError(
                f'Capability Manifest directory does not exist: '
                f'{self.directory}'
            )
        candidates = sorted(
            path
            for path in self.directory.iterdir()
            if path.is_file() and path.suffix in ('.yaml', '.yml')
        )
        if not candidates:
            raise ManifestError(
                f'No Capability Manifests found in {self.directory}'
            )

        loaded: dict[str, CapabilityManifest] = {}
        command_names: dict[str, str] = {}
        for path in candidates:
            manifest = self._load_file(path)
            if manifest.capability_id in loaded:
                previous = loaded[manifest.capability_id].source_path
                raise ManifestError(
                    f'{path}: duplicate capability id '
                    f'{manifest.capability_id!r}; first declared by {previous}'
                )
            previous_id = command_names.get(manifest.command_name)
            if previous_id is not None:
                raise ManifestError(
                    f'{path}: command name {manifest.command_name!r} is '
                    f'already registered by {previous_id!r}'
                )
            loaded[manifest.capability_id] = manifest
            command_names[manifest.command_name] = manifest.capability_id
        self._manifests = loaded

    def get(self, capability_id: str) -> CapabilityManifest:
        """Return a registered capability or raise a useful request error."""
        try:
            return self._manifests[capability_id]
        except KeyError as error:
            raise RequestValidationError(
                f'Unknown capability_id: {capability_id!r}'
            ) from error

    def all(self) -> tuple[CapabilityManifest, ...]:
        """Return manifests in deterministic capability-id order."""
        return tuple(self._manifests[key] for key in sorted(self._manifests))

    def parse_arguments(
        self,
        manifest: CapabilityManifest,
        arguments_yaml: str,
    ) -> tuple[dict[str, Any], Any]:
        """Apply defaults and build a concrete Goal or Request message."""
        raw = arguments_yaml or '{}'
        if len(raw.encode('utf-8')) > MAX_ARGUMENT_BYTES:
            raise RequestValidationError(
                f'arguments_yaml exceeds {MAX_ARGUMENT_BYTES} bytes'
            )
        try:
            values = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            raise RequestValidationError(
                f'arguments_yaml is not valid YAML: {error}'
            ) from error
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise RequestValidationError(
                'arguments_yaml root must be a mapping'
            )
        try:
            _check_depth(values, MAX_YAML_DEPTH, 'arguments_yaml')
        except ManifestError as error:
            raise RequestValidationError(str(error)) from error
        invalid_keys = [key for key in values if not isinstance(key, str)]
        if invalid_keys:
            raise RequestValidationError(
                'arguments_yaml field names must be strings'
            )

        unknown = sorted(set(values) - set(manifest.input_fields))
        if unknown:
            raise RequestValidationError(
                'Unknown input field(s): ' + ', '.join(unknown)
            )

        merged: dict[str, Any] = {}
        missing: list[str] = []
        for name, field_spec in manifest.input_fields.items():
            if name in values:
                merged[name] = values[name]
            elif field_spec.has_default:
                merged[name] = deepcopy(field_spec.default)
            else:
                missing.append(name)
        if missing:
            raise RequestValidationError(
                'Missing required input field(s): ' + ', '.join(missing)
            )

        message_class = _request_class(manifest)
        message = message_class()
        try:
            set_message_fields(message, merged)
        except (
            AssertionError,
            AttributeError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise RequestValidationError(
                f'Input values do not match {manifest.command_type}: {error}'
            ) from error
        return merged, message

    def build_message(
        self,
        manifest: CapabilityManifest,
        arguments: Mapping[str, Any],
    ) -> Any:
        """Build a fresh downstream message for a resumed execution."""
        message = _request_class(manifest)()
        try:
            set_message_fields(message, deepcopy(dict(arguments)))
        except (
            AssertionError,
            AttributeError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise RequestValidationError(
                f'Stored inputs no longer match {manifest.command_type}: '
                f'{error}'
            ) from error
        return message

    def _load_file(self, path: Path) -> CapabilityManifest:
        payload = path.read_bytes()
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ManifestError(
                f'{path}: file exceeds {MAX_MANIFEST_BYTES} bytes'
            )
        try:
            document = yaml.safe_load(payload.decode('utf-8'))
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise ManifestError(f'{path}: invalid YAML: {error}') from error
        if not isinstance(document, dict):
            raise ManifestError(f'{path}: root must be a mapping')
        _check_depth(document, MAX_YAML_DEPTH, str(path))
        _expect_keys(
            document,
            required={
                'schema_version',
                'capability',
                'command',
                'input',
                'execution',
            },
            context=str(path),
        )
        if document['schema_version'] != SCHEMA_VERSION:
            raise ManifestError(
                f'{path}: schema_version must be {SCHEMA_VERSION}'
            )

        capability = _mapping(document['capability'], path, 'capability')
        _expect_keys(
            capability,
            required={'id', 'title', 'description'},
            context=f'{path}: capability',
        )
        capability_id = _string(
            capability['id'], path, 'capability.id'
        )
        if CAPABILITY_ID_PATTERN.fullmatch(capability_id) is None:
            raise ManifestError(
                f'{path}: capability.id must use lowercase letters, digits, '
                'and underscores'
            )

        command = _mapping(document['command'], path, 'command')
        _expect_keys(
            command,
            required={'kind', 'name', 'type'},
            context=f'{path}: command',
        )
        try:
            kind = CommandKind(_string(command['kind'], path, 'command.kind'))
        except ValueError as error:
            raise ManifestError(
                f'{path}: command.kind must be ACTION or SERVICE'
            ) from error
        command_name = _normalize_ros_name(
            _string(command['name'], path, 'command.name')
        )
        if command_name == MANAGER_ACTION_NAME:
            raise ManifestError(
                f'{path}: a capability cannot recursively call '
                f'{MANAGER_ACTION_NAME}'
            )
        command_type = _string(command['type'], path, 'command.type')
        interface_type = self._resolve_type(path, kind, command_type)

        execution = _mapping(document['execution'], path, 'execution')
        _expect_keys(
            execution,
            required={'mode', 'priority', 'resources'},
            context=f'{path}: execution',
        )
        try:
            mode = ExecutionMode(
                _string(execution['mode'], path, 'execution.mode')
            )
        except ValueError as error:
            raise ManifestError(
                f'{path}: execution.mode must be FOREGROUND or BACKGROUND'
            ) from error
        try:
            priority = MissionPriority[
                _string(execution['priority'], path, 'execution.priority')
            ]
        except KeyError as error:
            raise ManifestError(
                f'{path}: execution.priority must be LOW, NORMAL, HIGH, '
                'or URGENT'
            ) from error

        resource_names = execution['resources']
        if not isinstance(resource_names, list):
            raise ManifestError(f'{path}: execution.resources must be a list')
        resources: set[ExecutionResource] = set()
        for name in resource_names:
            if not isinstance(name, str):
                raise ManifestError(
                    f'{path}: execution.resources entries must be strings'
                )
            try:
                resource = ExecutionResource(name)
            except ValueError as error:
                raise ManifestError(
                    f'{path}: execution.resources must contain only BASE, '
                    'SPEAKER, BUZZER, LED, or DISPLAY'
                ) from error
            if resource in resources:
                raise ManifestError(
                    f'{path}: duplicate execution.resources entry: {name}'
                )
            resources.add(resource)

        input_section = _mapping(document['input'], path, 'input')
        _expect_keys(
            input_section,
            required={'fields'},
            context=f'{path}: input',
        )
        fields = _mapping(input_section['fields'], path, 'input.fields')
        parsed_fields = self._parse_fields(path, fields)
        self._validate_interface_fields(
            path,
            interface_type,
            kind,
            parsed_fields,
        )

        return CapabilityManifest(
            capability_id=capability_id,
            title=_string(capability['title'], path, 'capability.title'),
            description=_string(
                capability['description'], path, 'capability.description'
            ),
            command_kind=kind,
            command_name=command_name,
            command_type=command_type,
            execution_mode=mode,
            priority=priority,
            input_fields=parsed_fields,
            interface_type=interface_type,
            resources=frozenset(resources),
            source_path=str(path),
        )

    def _resolve_type(
        self,
        path: Path,
        kind: CommandKind,
        command_type: str,
    ) -> Any:
        expected_segment = '/action/' if kind is CommandKind.ACTION else '/srv/'
        if expected_segment not in command_type:
            raise ManifestError(
                f'{path}: {command_type!r} does not match {kind.value}'
            )
        resolver = (
            self._action_resolver
            if kind is CommandKind.ACTION
            else self._service_resolver
        )
        try:
            return resolver(command_type)
        except (AttributeError, ImportError, ModuleNotFoundError, ValueError) \
                as error:
            raise ManifestError(
                f'{path}: cannot load ROS interface {command_type!r}: {error}'
            ) from error

    @staticmethod
    def _parse_fields(
        path: Path,
        fields: Mapping[str, Any],
    ) -> dict[str, InputField]:
        parsed: dict[str, InputField] = {}
        for name, value in fields.items():
            if not isinstance(name, str) or not name:
                raise ManifestError(
                    f'{path}: input field names must be non-empty strings'
                )
            field_data = _mapping(value, path, f'input.fields.{name}')
            _expect_keys(
                field_data,
                required={'type', 'description'},
                optional={'default'},
                context=f'{path}: input.fields.{name}',
            )
            parsed[name] = InputField(
                name=name,
                ros_type=_string(
                    field_data['type'], path, f'input.fields.{name}.type'
                ),
                description=_string(
                    field_data['description'],
                    path,
                    f'input.fields.{name}.description',
                ),
                has_default='default' in field_data,
                default=deepcopy(field_data.get('default')),
            )
        return parsed

    @staticmethod
    def _validate_interface_fields(
        path: Path,
        interface_type: Any,
        kind: CommandKind,
        fields: Mapping[str, InputField],
    ) -> None:
        message_class = (
            interface_type.Goal
            if kind is CommandKind.ACTION
            else interface_type.Request
        )
        actual = message_class.get_fields_and_field_types()
        missing = sorted(set(actual) - set(fields))
        extra = sorted(set(fields) - set(actual))
        if missing or extra:
            details = []
            if missing:
                details.append('missing fields: ' + ', '.join(missing))
            if extra:
                details.append('unknown fields: ' + ', '.join(extra))
            raise ManifestError(f'{path}: ' + '; '.join(details))
        for name, field_spec in fields.items():
            declared = _canonical_ros_type(field_spec.ros_type)
            generated = _canonical_ros_type(actual[name])
            if declared != generated:
                raise ManifestError(
                    f'{path}: input field {name!r} declares '
                    f'{field_spec.ros_type!r}, but ROS interface uses '
                    f'{actual[name]!r}'
                )
            if field_spec.has_default:
                message = message_class()
                try:
                    set_message_fields(message, {name: deepcopy(field_spec.default)})
                except (
                    AssertionError,
                    AttributeError,
                    KeyError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise ManifestError(
                        f'{path}: invalid default for {name!r}: {error}'
                    ) from error


def _request_class(manifest: CapabilityManifest) -> Any:
    return (
        manifest.interface_type.Goal
        if manifest.command_kind is CommandKind.ACTION
        else manifest.interface_type.Request
    )


def _canonical_ros_type(value: str) -> str:
    result = re.sub(r'\s+', '', value)
    result = result.replace('/msg/', '/')
    replacements = {
        'float32': 'float',
        'float64': 'double',
        'bool': 'boolean',
    }
    for source, target in replacements.items():
        result = re.sub(rf'\b{source}\b', target, result)
    if result.endswith('[]'):
        result = f'sequence<{result[:-2]}>'
    return result


def _normalize_ros_name(value: str) -> str:
    parts = [part for part in value.split('/') if part]
    if not parts:
        raise ManifestError('command.name must not be empty')
    return '/' + '/'.join(parts)


def _mapping(value: Any, path: Path, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f'{path}: {field_name} must be a mapping')
    return value


def _string(value: Any, path: Path, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f'{path}: {field_name} must be a non-empty string')
    return value.strip()


def _expect_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    context: str,
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    if any(not isinstance(key, str) for key in value):
        raise ManifestError(f'{context}: keys must be strings')
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing:
        raise ManifestError(
            f'{context}: missing key(s): ' + ', '.join(missing)
        )
    if unknown:
        raise ManifestError(
            f'{context}: unknown key(s): ' + ', '.join(unknown)
        )


def _check_depth(value: Any, maximum: int, context: str) -> None:
    def walk(item: Any, depth: int) -> None:
        if depth > maximum:
            raise ManifestError(
                f'{context}: YAML nesting exceeds {maximum} levels'
            )
        if isinstance(item, dict):
            for key, child in item.items():
                walk(key, depth + 1)
                walk(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child, depth + 1)

    try:
        walk(value, 0)
    except ManifestError:
        raise
