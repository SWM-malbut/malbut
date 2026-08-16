"""Tests for the robot-profile loader and its Xacro handoff."""

from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

from malbut_description.variant_config import (
    VariantConfigError,
    load_variant_arguments,
    resolve_variant_config,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROFILE = PACKAGE_ROOT / 'config' / 'rosorin_ultimate_mecanum.yaml'
XACRO_MODEL = PACKAGE_ROOT / 'urdf' / 'rosorin_model.xacro'
XACRO_NAMESPACE = 'http://www.ros.org/wiki/xacro'


def _profile_data():
    return yaml.safe_load(PROFILE.read_text(encoding='utf-8'))


def _write_profile(tmp_path, data):
    path = tmp_path / 'profile.yaml'
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')
    return path


def test_profile_basename_and_absolute_path_resolve_to_the_same_file():
    """Launch callers may use either the installed basename or a full path."""
    assert resolve_variant_config(PROFILE.name, PACKAGE_ROOT) == PROFILE
    assert resolve_variant_config(PROFILE.stem, PACKAGE_ROOT) == PROFILE
    assert resolve_variant_config(str(PROFILE), PACKAGE_ROOT) == PROFILE


def test_xacro_defaults_match_the_selected_profile():
    """Direct Xacro rendering and launch rendering must not silently drift."""
    arguments = load_variant_arguments(PROFILE)
    root = ElementTree.parse(XACRO_MODEL).getroot()
    defaults = {
        element.get('name'): float(element.get('default'))
        for element in root.findall(f'{{{XACRO_NAMESPACE}}}arg')
    }

    assert set(defaults) == set(arguments)
    for name, value in arguments.items():
        assert defaults[name] == pytest.approx(value), name


@pytest.mark.parametrize(
    ('mutation', 'message'),
    [
        (
            lambda data: data['xacro']['arguments'].pop('camera_far'),
            'missing Xacro arguments',
        ),
        (
            lambda data: data['xacro']['arguments'].__setitem__(
                'made_up_parameter', 1.0
            ),
            'unknown Xacro arguments',
        ),
        (
            lambda data: data['xacro']['arguments'].__setitem__(
                'wheel_radius', '0.04'
            ),
            'wheel_radius must be numeric',
        ),
        (
            lambda data: data['xacro']['arguments'].__setitem__(
                'wheel_radius', 0.0
            ),
            'wheel_radius must be positive',
        ),
        (
            lambda data: data['xacro']['arguments'].__setitem__(
                'camera_near', 31.0
            ),
            'camera_near must be below camera_far',
        ),
        (
            lambda data: data['xacro']['arguments'].__setitem__(
                'total_mass', 3.0
            ),
            'component masses total',
        ),
    ],
)
def test_invalid_profile_values_are_rejected(tmp_path, mutation, message):
    """A malformed or physically inconsistent profile must fail before Xacro."""
    data = deepcopy(_profile_data())
    mutation(data)
    path = _write_profile(tmp_path, data)

    with pytest.raises(VariantConfigError, match=message):
        load_variant_arguments(path)


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    """Duplicate keys may not silently replace a robot parameter."""
    path = tmp_path / 'duplicate.yaml'
    path.write_text(
        'schema_version: 1\nschema_version: 1\n',
        encoding='utf-8',
    )

    with pytest.raises(VariantConfigError, match='duplicate key'):
        load_variant_arguments(path)
