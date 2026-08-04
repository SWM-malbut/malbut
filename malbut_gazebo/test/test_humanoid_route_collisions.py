"""Validate the humanoid route against the Small House collision meshes."""

import ast
import math
from pathlib import Path
from xml.etree import ElementTree


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ACTOR_FILE = PACKAGE_ROOT / 'models' / 'humanoid_actor' / 'model.sdf'
WORLD_FILE = PACKAGE_ROOT / 'worlds' / 'small_house.sdf'
AWS_MODELS = PACKAGE_ROOT / 'models' / 'aws_small_house'
LAUNCH_FILE = PACKAGE_ROOT / 'launch' / 'humanoid_demo.launch.py'
NON_BLOCKING_NAMES = ('Floor', 'Carpet')


def _launch_offsets():
    tree = ast.parse(LAUNCH_FILE.read_text(encoding='utf-8'))
    defaults = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        if node.func.id != 'DeclareLaunchArgument' or not node.args:
            continue
        if not isinstance(node.args[0], ast.Constant):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == 'default_value'
                and isinstance(keyword.value, ast.Constant)
            ):
                defaults[node.args[0].value] = keyword.value.value
    return float(defaults['actor_x']), float(defaults['actor_y'])


def _route_points():
    offset_x, offset_y = _launch_offsets()
    root = ElementTree.parse(ACTOR_FILE).getroot()
    points = []
    for waypoint in root.findall('actor/script/trajectory/waypoint'):
        pose = [float(value) for value in waypoint.findtext('pose').split()]
        point = (offset_x + pose[0], offset_y + pose[1])
        if not points or point != points[-1]:
            points.append(point)
    return points


def _matrix_transform(values, point):
    """Apply a COLLADA column-major 4x4 matrix to one XYZ point."""
    x, y, z = point
    return (
        values[0] * x
        + values[4] * y
        + values[8] * z
        + values[12],
        values[1] * x
        + values[5] * y
        + values[9] * z
        + values[13],
        values[2] * x
        + values[6] * y
        + values[10] * z
        + values[14],
    )


def _dae_triangles(path):
    root = ElementTree.parse(path).getroot()
    namespace = {'c': root.tag.split('}')[0].strip('{')}
    unit_element = root.find('c:asset/c:unit', namespace)
    unit = (
        float(unit_element.get('meter', '1'))
        if unit_element is not None
        else 1.0
    )
    geometries = {
        geometry.get('id'): geometry
        for geometry in root.findall(
            'c:library_geometries/c:geometry', namespace
        )
    }
    triangles = []
    for node in root.findall(
        './/c:library_visual_scenes//c:node', namespace
    ):
        matrix_element = node.find('c:matrix', namespace)
        matrix = [
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
        ]
        if matrix_element is not None:
            matrix = [
                float(value) for value in matrix_element.text.split()
            ]
        for instance in node.findall('c:instance_geometry', namespace):
            geometry_id = instance.get('url', '').lstrip('#')
            geometry = geometries.get(geometry_id)
            if geometry is None:
                continue
            mesh = geometry.find('c:mesh', namespace)
            sources = {}
            for source in mesh.findall('c:source', namespace):
                array = source.find('c:float_array', namespace)
                accessor = source.find(
                    'c:technique_common/c:accessor', namespace
                )
                if array is None or accessor is None:
                    continue
                values = [float(value) for value in array.text.split()]
                stride = int(accessor.get('stride', '1'))
                sources[source.get('id')] = [
                    tuple(values[index:index + 3])
                    for index in range(0, len(values), stride)
                ]
            vertex_sources = {}
            for vertices in mesh.findall('c:vertices', namespace):
                position = next(
                    (
                        element
                        for element in vertices.findall('c:input', namespace)
                        if element.get('semantic') == 'POSITION'
                    ),
                    None,
                )
                if position is not None:
                    vertex_sources[vertices.get('id')] = position.get(
                        'source', ''
                    ).lstrip('#')
            for primitive_name in ('triangles', 'polylist'):
                for primitive in mesh.findall(
                    f'c:{primitive_name}', namespace
                ):
                    inputs = primitive.findall('c:input', namespace)
                    vertex_input = next(
                        (
                            element
                            for element in inputs
                            if element.get('semantic')
                            in ('VERTEX', 'POSITION')
                        ),
                        None,
                    )
                    if vertex_input is None:
                        continue
                    source_id = vertex_input.get('source', '').lstrip('#')
                    if vertex_input.get('semantic') == 'VERTEX':
                        source_id = vertex_sources.get(source_id, '')
                    positions = sources.get(source_id)
                    indices_element = primitive.find('c:p', namespace)
                    if positions is None or indices_element is None:
                        continue
                    index_stride = max(
                        int(element.get('offset', '0'))
                        for element in inputs
                    ) + 1
                    vertex_offset = int(vertex_input.get('offset', '0'))
                    indices = [
                        int(value)
                        for value in indices_element.text.split()
                    ][vertex_offset::index_stride]
                    if primitive_name == 'triangles':
                        vertex_counts = [3] * (len(indices) // 3)
                    else:
                        vertex_counts = [
                            int(value)
                            for value in primitive.findtext(
                                'c:vcount', namespaces=namespace
                            ).split()
                        ]
                    cursor = 0
                    for vertex_count in vertex_counts:
                        polygon = indices[cursor:cursor + vertex_count]
                        cursor += vertex_count
                        for index in range(1, vertex_count - 1):
                            face = (polygon[0], polygon[index], polygon[index + 1])
                            triangles.append(
                                tuple(
                                    _matrix_transform(
                                        matrix,
                                        tuple(value * unit for value in positions[i]),
                                    )
                                    for i in face
                                )
                            )
    return triangles


def _cross(origin, first, second):
    return (
        (first[0] - origin[0]) * (second[1] - origin[1])
        - (first[1] - origin[1]) * (second[0] - origin[0])
    )


def _collision_triangles():
    world = ElementTree.parse(WORLD_FILE).getroot().find('world')
    triangles = []
    for include in world.findall('include'):
        name = include.findtext('name', '')
        if any(token in name for token in NON_BLOCKING_NAMES):
            continue
        uri = include.findtext('uri', '')
        if not uri.startswith('model://aws_'):
            continue
        model_directory = AWS_MODELS / uri.removeprefix('model://')
        model_file = model_directory / 'model.sdf'
        if not model_file.is_file():
            continue
        include_pose = [
            float(value) for value in include.findtext('pose').split()
        ]
        cosine = math.cos(include_pose[5])
        sine = math.sin(include_pose[5])
        model = ElementTree.parse(model_file).getroot().find('model')
        for collision in model.findall('.//collision'):
            mesh = collision.find('geometry/mesh')
            if mesh is None:
                continue
            mesh_path = (
                model_directory
                / 'meshes'
                / Path(mesh.findtext('uri')).name
            )
            if not mesh_path.is_file():
                continue
            scale = [
                float(value)
                for value in mesh.findtext('scale', '1 1 1').split()
            ]
            for triangle in _dae_triangles(mesh_path):
                world_triangle = []
                for x, y, z in triangle:
                    x *= scale[0]
                    y *= scale[1]
                    z *= scale[2]
                    world_triangle.append(
                        (
                            cosine * x - sine * y + include_pose[0],
                            sine * x + cosine * y + include_pose[1],
                            z + include_pose[2],
                        )
                    )
                heights = [point[2] for point in world_triangle]
                if max(heights) < 0.10 or min(heights) > 1.90:
                    continue
                triangles.append(
                    (name, [(point[0], point[1]) for point in world_triangle])
                )
    return triangles


def _point_segment_distance(point, start, end):
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    fraction = (
        (point[0] - start[0]) * dx
        + (point[1] - start[1]) * dy
    ) / length_squared
    fraction = max(0.0, min(1.0, fraction))
    nearest = (start[0] + fraction * dx, start[1] + fraction * dy)
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])


def _segments_intersect(first_start, first_end, second_start, second_end):
    epsilon = 1e-9

    def on_segment(start, end, point):
        return (
            min(start[0], end[0]) - epsilon
            <= point[0]
            <= max(start[0], end[0]) + epsilon
            and min(start[1], end[1]) - epsilon
            <= point[1]
            <= max(start[1], end[1]) + epsilon
        )

    first_a = _cross(first_start, first_end, second_start)
    first_b = _cross(first_start, first_end, second_end)
    second_a = _cross(second_start, second_end, first_start)
    second_b = _cross(second_start, second_end, first_end)
    if first_a * first_b < 0 and second_a * second_b < 0:
        return True
    return any(
        abs(value) <= epsilon and on_segment(start, end, point)
        for value, start, end, point in (
            (first_a, first_start, first_end, second_start),
            (first_b, first_start, first_end, second_end),
            (second_a, second_start, second_end, first_start),
            (second_b, second_start, second_end, first_end),
        )
    )


def _segment_distance(first_start, first_end, second_start, second_end):
    if _segments_intersect(
        first_start, first_end, second_start, second_end
    ):
        return 0.0
    return min(
        _point_segment_distance(first_start, second_start, second_end),
        _point_segment_distance(first_end, second_start, second_end),
        _point_segment_distance(second_start, first_start, first_end),
        _point_segment_distance(second_end, first_start, first_end),
    )


def _point_in_triangle(point, triangle):
    first, second, third = triangle
    area = _cross(first, second, third)
    if abs(area) <= 1e-9:
        return False
    signs = (
        _cross(first, second, point),
        _cross(second, third, point),
        _cross(third, first, point),
    )
    return all(value >= -1e-9 for value in signs) or all(
        value <= 1e-9 for value in signs
    )


def _route_to_triangle_distance(start, end, triangle):
    if _point_in_triangle(start, triangle) or _point_in_triangle(
        end, triangle
    ):
        return 0.0
    edges = zip(triangle, triangle[1:] + [triangle[0]])
    return min(
        _segment_distance(start, end, edge_start, edge_end)
        for edge_start, edge_end in edges
    )


def test_route_clears_actual_small_house_collision_meshes():
    route = _route_points()
    triangles = _collision_triangles()
    names = {name for name, _ in triangles}
    assert len(triangles) >= 1000
    assert any('HouseWall' in name for name in names)
    assert any('Door' in name for name in names)
    for name, triangle in triangles:
        clearance = min(
            _route_to_triangle_distance(start, end, triangle)
            for start, end in zip(route, route[1:])
        )
        assert clearance >= 0.35, (name, clearance)
