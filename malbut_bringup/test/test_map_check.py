from malbut_bringup.map_check import MapCheck, message_match, result_match


HOME = '/maps/home.yaml'
FOUND_LOW = ('saved map loaded; saved pose matched only 45% of the scan; '
             'found by global search, 58% of the scan matches the map')
FOUND_GOOD = 'saved map loaded; saved pose confirmed; 87% of the scan matches the map'


def _localization(mode='LOCALIZATION', map_path=HOME, message=FOUND_GOOD, *,
                  transition=1, pose_ready=True):
    return {'mode': mode, 'map': map_path, 'message': message, 'runtime_id': 'r1',
            'transition_id': transition, 'pose_ready': pose_ready}


def _result(mission_id, ratio, success=True, state='SUCCEEDED'):
    return {'mission_id': mission_id, 'capability_id': 'relocalize', 'state': state,
            'result_yaml': f'success: {str(success).lower()}\nmessage: x\nmatch_ratio: {ratio}\n'}


def _update(check, now=0.0, *, localization, saved=True, ready=True, system=None,
            results=()):
    return check.update(now, running=True, ready=ready, localization=localization,
                        saved=saved, system=system or {}, results=list(results))


def test_the_manager_message_and_mission_result_name_the_match():
    assert message_match(FOUND_GOOD) == 0.87
    assert message_match(FOUND_LOW) == 0.58, 'the last figure is the final pose'
    assert message_match('saved map loaded; relocalization is unavailable') is None
    assert result_match(_result('a', 0.6100000143051147)) == (True, 0.6100000143051147)
    assert result_match({'result_yaml': '', 'message': 'global search matched only 40% '
                         'of the scan'}) == (False, 0.4)


def test_a_start_with_the_last_map_shows_loading_then_a_good_match_is_ok():
    check = MapCheck()
    check.started('home.yaml')
    view, retry = _update(check, localization={})
    assert view == {'phase': 'loading', 'map': 'home.yaml', 'auto': True} and not retry
    view, _ = _update(check, localization=_localization('SWITCHING', None, 'starting'))
    assert view['phase'] == 'loading'
    view, _ = _update(check, localization=_localization('SWITCHING', message='finding'))
    assert view['phase'] == 'loading'
    view, retry = _update(check, localization=_localization())
    assert view == {'phase': 'ok', 'map': 'home.yaml', 'match': 0.87, 'auto': True,
                    'retried': False}
    assert not retry


def test_a_poor_match_searches_once_more_then_warns():
    check = MapCheck()
    check.started('home.yaml')
    localization = _localization(message=FOUND_LOW)
    view, retry = _update(check, localization=localization, ready=False)
    assert view['phase'] == 'low' and not retry, 'the manager must be ready first'
    view, retry = _update(check, localization=localization)
    assert retry and view['phase'] == 'retrying' and view['match'] == 0.58
    busy = {'active_foreground_missions': [{'capability_id': 'relocalize'}]}
    view, retry = _update(check, 5.0, localization=localization, system=busy)
    assert view['phase'] == 'retrying' and not retry
    view, retry = _update(check, 30.0, localization=localization,
                          results=[_result('m1', 0.62)])
    assert view['phase'] == 'low' and view['retried'] and view['match'] == 0.62
    assert not retry
    view, retry = _update(check, 60.0, localization=localization,
                          results=[_result('m1', 0.62)])
    assert view['phase'] == 'low' and not retry, 'one automatic retry per switch'


def test_the_retry_or_the_users_relocalization_can_clear_the_warning():
    check = MapCheck()
    old = [_result('before', 0.9)]
    localization = _localization(message=FOUND_LOW)
    _, retry = _update(check, localization=localization, results=old)
    assert retry, 'a result from before this switch does not count'
    view, _ = _update(check, 20.0, localization=localization,
                      results=[*old, _result('m1', 0.81)])
    assert view['phase'] == 'ok' and view['match'] == 0.81 and not view['auto']
    view, _ = _update(check, 40.0, localization=localization,
                      results=[*old, _result('m1', 0.81), _result('m2', 0.4, False, 'ABORTED')])
    assert view['phase'] == 'low', 'a later failed search by the user warns again'
    view, _ = _update(check, 50.0, localization=localization,
                      results=[*old, _result('m1', 0.81), _result('m2', 0.4, False, 'ABORTED'),
                               _result('m3', 0.88)])
    assert view['phase'] == 'ok'


def test_no_retry_over_the_users_mission_or_a_lost_request():
    check = MapCheck()
    patrol = {'active_foreground_missions': [{'capability_id': 'patrol'}]}
    view, retry = _update(check, localization=_localization(message=FOUND_LOW), system=patrol)
    assert not retry and view['phase'] == 'low'
    check = MapCheck()
    localization = _localization(message='saved map loaded; pose not found (global search '
                                 'matched only 40% of the scan)', pose_ready=False)
    _, retry = _update(check, localization=localization)
    assert retry
    view, _ = _update(check, 5.0, localization=localization)
    assert view['phase'] == 'retrying'
    view, retry = _update(check, 11.0, localization=localization)
    assert view['phase'] == 'low' and view['retried'] and not retry


def test_each_switch_is_judged_again_and_a_pick_is_not_automatic():
    check = MapCheck()
    check.started('home.yaml')
    _update(check, localization=_localization())
    check.chosen()
    view, _ = _update(check, localization=_localization('SWITCHING', '/maps/office.yaml'))
    assert view == {'phase': 'locating', 'map': 'office.yaml', 'auto': False}
    view, retry = _update(check, localization=_localization(
        map_path='/maps/office.yaml', message=FOUND_LOW, transition=2))
    assert retry and view['phase'] == 'retrying' and not view['auto']


def test_the_blank_map_and_mapping_are_not_judged():
    check = MapCheck()
    check.started('home.yaml')
    _update(check, localization=_localization())
    switching = _localization('SWITCHING', None, 'to mapping', transition=2)
    view, retry = _update(check, localization=switching, saved=False)
    assert view == {'phase': None} and not retry, 'only the first switch is the loading'
    view, _ = _update(check, localization=_localization('MAPPING', None, transition=2),
                      saved=False)
    assert view == {'phase': None}
    view, retry = _update(check, localization=_localization(
        map_path='/share/default_map.yaml', message='default unknown map loaded',
        transition=3), saved=False)
    assert view == {'phase': 'none'} and not retry
    view, _ = check.update(0.0, running=False, ready=False, localization={}, saved=False,
                           system={}, results=[])
    assert view == {'phase': None}
