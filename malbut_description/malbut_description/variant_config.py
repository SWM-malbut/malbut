"""Load and validate a ROSOrin hardware-variant parameter profile."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver


EXPECTED_ARGUMENTS = frozenset(
    {
        "overall_length",
        "overall_width",
        "overall_height",
        "total_mass",
        "wheelbase",
        "wheel_separation",
        "wheel_radius",
        "wheel_width",
        "wheel_joint_damping",
        "wheel_joint_friction",
        "wheel_surface_mu1",
        "wheel_surface_mu2",
        "drive_min_acceleration",
        "drive_max_acceleration",
        "drive_min_velocity",
        "drive_max_velocity",
        "odom_publish_frequency",
        "base_mass",
        "base_length",
        "base_width",
        "base_height",
        "base_center_z",
        "upper_body_mass",
        "upper_body_length",
        "upper_body_width",
        "upper_body_height",
        "upper_body_x",
        "upper_body_z",
        "wheel_mass",
        "camera_x",
        "camera_y",
        "camera_z",
        "camera_roll",
        "camera_pitch",
        "camera_yaw",
        "camera_width",
        "camera_height",
        "camera_rate",
        "camera_hfov",
        "camera_near",
        "camera_far",
        "depth_camera_near",
        "depth_camera_far",
        "camera_mass",
        "camera_size_x",
        "camera_size_y",
        "camera_size_z",
        "lidar_x",
        "lidar_y",
        "lidar_z",
        "lidar_roll",
        "lidar_pitch",
        "lidar_yaw",
        "lidar_samples",
        "lidar_rate",
        "lidar_min_range",
        "lidar_max_range",
        "lidar_range_resolution",
        "lidar_noise_stddev",
        "lidar_mass",
        "lidar_diameter",
        "lidar_height",
        "imu_x",
        "imu_y",
        "imu_z",
        "imu_roll",
        "imu_pitch",
        "imu_yaw",
        "imu_rate",
        "imu_mass",
        "imu_size_x",
        "imu_size_y",
        "imu_size_z",
        "microphone_x",
        "microphone_y",
        "microphone_z",
        "microphone_mass",
        "microphone_diameter",
        "microphone_height",
    }
)

POSITIVE_ARGUMENTS = frozenset(
    {
        "overall_length",
        "overall_width",
        "overall_height",
        "total_mass",
        "wheelbase",
        "wheel_separation",
        "wheel_radius",
        "wheel_width",
        "odom_publish_frequency",
        "base_mass",
        "base_length",
        "base_width",
        "base_height",
        "upper_body_mass",
        "upper_body_length",
        "upper_body_width",
        "upper_body_height",
        "upper_body_z",
        "wheel_mass",
        "camera_width",
        "camera_height",
        "camera_rate",
        "camera_hfov",
        "camera_near",
        "camera_far",
        "depth_camera_near",
        "depth_camera_far",
        "camera_mass",
        "camera_size_x",
        "camera_size_y",
        "camera_size_z",
        "lidar_samples",
        "lidar_rate",
        "lidar_min_range",
        "lidar_max_range",
        "lidar_range_resolution",
        "lidar_mass",
        "lidar_diameter",
        "lidar_height",
        "imu_rate",
        "imu_mass",
        "imu_size_x",
        "imu_size_y",
        "imu_size_z",
        "microphone_mass",
        "microphone_diameter",
        "microphone_height",
    }
)

NON_NEGATIVE_ARGUMENTS = frozenset(
    {
        "wheel_joint_damping",
        "wheel_joint_friction",
        "wheel_surface_mu1",
        "wheel_surface_mu2",
        "lidar_noise_stddev",
    }
)

INTEGER_ARGUMENTS = frozenset(
    {
        "camera_width",
        "camera_height",
        "lidar_samples",
    }
)


class VariantConfigError(RuntimeError):
    """Raised when a variant profile violates the project contract."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def resolve_variant_config(
    value: str,
    description_share: Path,
) -> Path:
    """Resolve a profile basename or absolute YAML path."""
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        if candidate.suffix == "":
            candidate = candidate.with_suffix(".yaml")
        candidate = description_share / "config" / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise VariantConfigError(
            f"Variant config does not exist: {candidate}"
        )
    return candidate


def _load_yaml(source: Path) -> dict[str, Any]:
    try:
        with source.open("r", encoding="utf-8") as stream:
            data = yaml.load(stream, Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as error:
        raise VariantConfigError(
            f"Cannot load variant config {source}: {error}"
        ) from error
    if not isinstance(data, dict):
        raise VariantConfigError(f"{source}: root must be a map")
    return data


def load_variant_arguments(source: Path) -> dict[str, int | float]:
    """Return a fully validated Xacro argument mapping."""
    data = _load_yaml(source)
    errors: list[str] = []

    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        errors.append(
            f"schema_version must be integer 1, got {schema_version!r}"
        )
    variant = data.get("variant")
    if not isinstance(variant, dict):
        errors.append("variant must be a map")
    else:
        for key in (
            "id",
            "sku",
            "kit",
            "compute",
            "chassis",
            "depth_camera",
            "lidar",
        ):
            if not isinstance(variant.get(key), str) or not variant[key]:
                errors.append(f"variant.{key} must be a non-empty string")

    xacro_section = data.get("xacro")
    arguments = (
        xacro_section.get("arguments")
        if isinstance(xacro_section, dict)
        else None
    )
    if not isinstance(arguments, dict):
        errors.append("xacro.arguments must be a map")
        arguments = {}

    argument_names = set(arguments)
    missing = sorted(EXPECTED_ARGUMENTS - argument_names)
    unknown = sorted(argument_names - EXPECTED_ARGUMENTS)
    if missing:
        errors.append(f"missing Xacro arguments: {missing}")
    if unknown:
        errors.append(f"unknown Xacro arguments: {unknown}")

    verification = data.get("verification")
    confirmed = (
        verification.get("confirmed")
        if isinstance(verification, dict)
        else None
    )
    provisional = (
        verification.get("provisional")
        if isinstance(verification, dict)
        else None
    )
    if not isinstance(confirmed, list) or not all(
        isinstance(item, str) for item in confirmed
    ):
        errors.append("verification.confirmed must be a string array")
        confirmed = []
    if not isinstance(provisional, list) or not all(
        isinstance(item, str) for item in provisional
    ):
        errors.append("verification.provisional must be a string array")
        provisional = []
    confirmed_names = set(confirmed)
    provisional_names = set(provisional)
    if len(confirmed_names) != len(confirmed):
        errors.append("verification.confirmed contains duplicates")
    if len(provisional_names) != len(provisional):
        errors.append("verification.provisional contains duplicates")
    overlap = sorted(confirmed_names & provisional_names)
    unclassified = sorted(
        EXPECTED_ARGUMENTS - confirmed_names - provisional_names
    )
    unknown_states = sorted(
        (confirmed_names | provisional_names) - EXPECTED_ARGUMENTS
    )
    if overlap:
        errors.append(f"arguments have two verification states: {overlap}")
    if unclassified:
        errors.append(
            f"arguments lack a verification state: {unclassified}"
        )
    if unknown_states:
        errors.append(
            f"verification names unknown arguments: {unknown_states}"
        )

    values: dict[str, float] = {}
    for key in sorted(EXPECTED_ARGUMENTS & argument_names):
        raw_value = arguments[key]
        if isinstance(raw_value, bool) or not isinstance(
            raw_value, (int, float)
        ):
            errors.append(f"{key} must be numeric")
            continue
        value = float(raw_value)
        if not math.isfinite(value):
            errors.append(f"{key} must be finite")
            continue
        values[key] = value

    for key in sorted(POSITIVE_ARGUMENTS & values.keys()):
        if values[key] <= 0:
            errors.append(f"{key} must be positive")
    for key in sorted(NON_NEGATIVE_ARGUMENTS & values.keys()):
        if values[key] < 0:
            errors.append(f"{key} must be non-negative")
    for key in sorted(INTEGER_ARGUMENTS & values.keys()):
        if not values[key].is_integer():
            errors.append(f"{key} must be an integer")

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    if EXPECTED_ARGUMENTS <= values.keys():
        require(
            values["drive_min_acceleration"] < 0
            < values["drive_max_acceleration"],
            "drive acceleration range must span zero",
        )
        require(
            values["drive_min_velocity"] < 0
            < values["drive_max_velocity"],
            "drive velocity range must span zero",
        )
        require(
            values["camera_near"] < values["camera_far"],
            "camera_near must be below camera_far",
        )
        require(
            values["depth_camera_near"] < values["depth_camera_far"],
            "depth_camera_near must be below depth_camera_far",
        )
        require(
            values["lidar_min_range"] < values["lidar_max_range"],
            "lidar_min_range must be below lidar_max_range",
        )
        require(
            0 < values["camera_hfov"] < math.pi,
            "camera_hfov must be between 0 and pi",
        )

        component_mass = (
            values["base_mass"]
            + values["upper_body_mass"]
            + 4.0 * values["wheel_mass"]
            + values["camera_mass"]
            + values["lidar_mass"]
            + values["imu_mass"]
            + values["microphone_mass"]
        )
        require(
            abs(component_mass - values["total_mass"]) <= 1e-6,
            (
                f"component masses total {component_mass:.6f} kg, "
                f"expected {values['total_mass']:.6f} kg"
            ),
        )

        length_half = values["overall_length"] / 2.0
        width_half = values["overall_width"] / 2.0
        wheel_height = values["wheel_radius"]
        constraints = [
            (
                values["wheelbase"] + 2.0 * values["wheel_radius"]
                <= values["overall_length"] + 1e-6,
                "wheelbase + wheel diameter exceeds overall_length",
            ),
            (
                values["wheel_separation"] + values["wheel_width"]
                <= values["overall_width"] + 1e-6,
                "wheel separation + visual width exceeds overall_width",
            ),
            (
                values["base_length"] <= values["overall_length"] + 1e-6,
                "base_length exceeds overall_length",
            ),
            (
                values["base_width"] <= values["overall_width"] + 1e-6,
                "base_width exceeds overall_width",
            ),
            (
                abs(values["upper_body_x"])
                + values["upper_body_length"] / 2.0
                <= length_half + 1e-6,
                "upper body exceeds overall length envelope",
            ),
            (
                values["upper_body_width"] / 2.0
                <= width_half + 1e-6,
                "upper body exceeds overall width envelope",
            ),
            (
                wheel_height
                + values["upper_body_z"]
                + values["upper_body_height"] / 2.0
                <= values["overall_height"] + 1e-6,
                "upper body exceeds overall height",
            ),
            (
                wheel_height
                + values["lidar_z"]
                + values["lidar_height"] / 2.0
                <= values["overall_height"] + 1e-6,
                "lidar exceeds overall height",
            ),
            (
                wheel_height
                + values["camera_z"]
                + values["camera_size_z"] / 2.0
                <= values["overall_height"] + 1e-6,
                "camera exceeds overall height",
            ),
            (
                wheel_height
                + values["microphone_z"]
                + values["microphone_height"] / 2.0
                <= values["overall_height"] + 1e-6,
                "microphone exceeds overall height",
            ),
        ]
        for condition, message in constraints:
            require(condition, message)

    if errors:
        raise VariantConfigError(f"{source}: " + "; ".join(errors))

    return dict(arguments)


def xacro_value(value: Any) -> str:
    """Convert a YAML scalar to the spelling expected by Xacro."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
