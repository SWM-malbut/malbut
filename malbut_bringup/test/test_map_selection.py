"""The last selected map is one small preference, never a new map mode."""

from unittest.mock import Mock

import pytest

from malbut_bringup.map_selection import remember_map, remembered_map


def test_remembered_map_is_shared_across_restart(tmp_path):
    selection = tmp_path / 'state/last-map.txt'
    selected = tmp_path / 'home map.yaml'
    selected.write_text('image: home.pgm\n')
    assert remembered_map(selection) == ''
    remember_map(selection, selected)
    assert selection.read_text() == str(selected) + '\n'
    assert remembered_map(selection) == str(selected)
    assert list(selection.parent.iterdir()) == [selection]
    selected.unlink()
    assert remembered_map(selection) == ''


@pytest.mark.parametrize('contents', [b'', b'\xff', b'../map.yaml', b'/missing.yaml'])
def test_missing_or_invalid_preference_uses_default_map(tmp_path, contents):
    selection = tmp_path / 'last-map.txt'
    selection.write_bytes(contents)
    assert remembered_map(selection) == ''
    assert remembered_map('') == ''


def test_failed_atomic_write_preserves_previous_choice(tmp_path, monkeypatch):
    from malbut_bringup import map_selection

    selection = tmp_path / 'state/last-map.txt'
    selected = tmp_path / 'home.yaml'
    selected.write_text('image: home.pgm\n')
    remember_map(selection, selected)
    monkeypatch.setattr(map_selection.os, 'fsync', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        remember_map(selection, tmp_path / 'other.yaml')
    assert remembered_map(selection) == str(selected)
    assert list(selection.parent.iterdir()) == [selection]
