"""Static launch configuration contracts, independent of the ROS launch runtime."""

import ast
from pathlib import Path


ROOT = Path(__file__).parents[2]


def tree(path):
    return ast.parse((ROOT / path).read_text())


def dictionary_value(path, key):
    values = [value for node in ast.walk(tree(path)) if isinstance(node, ast.Dict)
              for name, value in zip(node.keys, node.values)
              if isinstance(name, ast.Constant) and name.value == key]
    assert len(values) == 1
    return values[0]


def test_robot_profiles_enable_aec_but_standalone_default_remains_explicit():
    assert ast.literal_eval(dictionary_value(
        'malbut_bringup/malbut_bringup/launch_support.py', 'speech_input_has_aec')) == 'true'
    assert ast.literal_eval(dictionary_value(
        'malbut_bringup/launch/cloud.launch.py', 'input_has_aec')) == 'true'
    values = [node.value for node in ast.walk(tree('malbut_bringup/launch/speech.launch.py'))
              if isinstance(node, ast.Assign) and any(
                  isinstance(target, ast.Name) and target.id == 'defaults' for target in node.targets)]
    assert len(values) == 1
    assert ast.literal_eval(next(value for name, value in zip(values[0].keys, values[0].values)
                                 if name.value == 'input_has_aec')) == 'false'


def test_bringup_aec_passes_the_user_value_to_speech_without_forcing_true():
    value = dictionary_value('malbut_bringup/launch/bringup.launch.py', 'input_has_aec')
    assert ast.unparse(value) == "value('speech_input_has_aec')"
    root = tree('malbut_bringup/launch/speech.launch.py')
    assignment = next(node.value for node in ast.walk(root) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'input_has_aec'
                              for target in node.targets))
    assert ast.unparse(assignment) == "value('input_has_aec') == 'true'"


def test_only_stt_node_opts_into_heartbeat_supervision_and_respawn():
    nodes = [node for node in ast.walk(tree('malbut_bringup/launch/speech.launch.py'))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == 'Node']
    supervised = []
    for node in nodes:
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        if '--heartbeat-timeout-s' not in ast.unparse(keywords.get('prefix', ast.Constant(''))):
            assert 'respawn' not in keywords
            continue
        supervised.append(node)
        assert ast.literal_eval(keywords['package']) == 'malbut_stt'
        assert ast.literal_eval(keywords['respawn']) is True
        assert ast.literal_eval(keywords['respawn_delay']) == 5.0
        args = keywords['prefix'].args[0].elts
        assert [ast.literal_eval(item) for item in args[1:-1]] == [
            '--wait-for-ready', '--heartbeat-timeout-s', '10.0', '--']
    assert len(supervised) == 1
