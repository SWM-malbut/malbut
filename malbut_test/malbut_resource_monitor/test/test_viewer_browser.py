"""Optional browser regression tests; synthetic logs, no ROS or robot control."""

import os
import threading

import pytest

from malbut_resource_monitor.store import Store
from malbut_resource_monitor.viewer import LogServer

playwright = pytest.importorskip('playwright.sync_api')


@pytest.fixture(scope='module')
def browser():
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(
            headless=True, executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
        yield browser
        browser.close()


@pytest.fixture
def viewer(browser, tmp_path):
    store = Store(tmp_path, 1, os.getpid())
    store.register('system', kind='system', label='system')
    store.register('processes/perception', kind='process', label='perception')
    store.register('topics/scan', kind='topic', label='/scan_raw')
    store.process('123-45', dict(pid=123, group='perception', executable='/test/yolo',
                                 ros_node_names=['yolo_node']))
    sample = {
        'cpu_percent': 20, 'gpu_percent': None, 'emc_percent': 12,
        'cpu0_percent': 30, 'cpu0_mhz': 1500, 'gpu0_mhz': 900,
        'ram_used_mib': 1200, 'swap_used_mib': 0, 'MemTotal_mib': 8192,
        'temp.cpu_c': 45, 'power.VDD_IN_mw': 5123,
        'collection_duration_ms': 4, 'sample_window_s': 1,
    }
    for t in range(1, 5):
        store.write('system', dict(sample, t=t))
        store.write('processes/perception', dict(t=t, identity='123-45', cpu_percent=5))
        store.write('topics/scan', dict(t=t, received_hz=10))
    server = LogServer(('127.0.0.1', 0), tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context(viewport={'width': 1280, 'height': 900})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(f'http://127.0.0.1:{server.server_port}')
        playwright.expect(page.locator('#liveStatus')).to_contain_text('확인')
        yield page, store, sample
        assert not errors
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join()
        store.close('browser test')


def visible_units(page):
    return page.locator('.unit-chart:visible').evaluate_all(
        '(plots) => plots.map(p => p.dataset.unit)')


def test_multi_metric_axes_and_selection_survive_reload(viewer):
    page, _, _ = viewer
    assert visible_units(page) == ['%', 'MiB']
    assert page.locator('[data-unit="%"] .unit-legend>span').count() == 3
    assert page.locator('#metricField').is_hidden()
    page.get_by_role('checkbox', name='클럭 (MHz) 전체', exact=True).check()
    assert visible_units(page) == ['%', 'MiB', 'MHz']
    assert page.locator('[data-unit="MHz"] .unit-legend>span').count() == 2
    page.get_by_role('checkbox', name='CPU (%)', exact=True).uncheck()
    assert page.get_by_role('checkbox', name='사용률 (%) 전체').evaluate('(el)=>el.indeterminate')
    page.reload()
    page.wait_for_selector('.unit-chart[data-unit="MHz"]')
    assert not page.get_by_role('checkbox', name='CPU (%)', exact=True).is_checked()
    assert page.locator('[data-unit="%"] .unit-legend>span').count() == 2
    page.get_by_role('button', name='모두 숨김', exact=True).click()
    assert not visible_units(page)
    page.reload()
    page.wait_for_selector('#systemEmpty')
    assert not visible_units(page)  # An intentionally empty choice is not reset.


def test_all_units_empty_ranges_and_narrow_layout(viewer):
    page, _, _ = viewer
    page.get_by_role('button', name='모든 지표 표시').click()
    assert visible_units(page) == ['%', 'MiB', 'MHz', '°C', 'mW', 'ms', 's']
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.locator('#start').fill('100')
    page.locator('#start').dispatch_event('change')
    playwright.expect(page.locator('#systemEmpty')).to_contain_text('기록이 없습니다')
    assert not visible_units(page)
    page.locator('#start').fill('0')
    page.locator('#start').dispatch_event('change')
    page.wait_for_selector('.unit-chart[data-unit="mW"]:visible')
    assert len(visible_units(page)) == 7


def test_live_updates_keep_selection_and_missing_values(viewer):
    page, store, sample = viewer
    page.get_by_role('checkbox', name='CPU (%)', exact=True).uncheck()
    before = page.locator('#metricCount').text_content()
    store.write('system', dict(sample, t=5, **{'temp.gpu_c': 48}))
    playwright.expect(page.locator('#metricCount')).not_to_have_text(before)
    assert not page.get_by_role('checkbox', name='CPU (%)', exact=True).is_checked()
    canvas = page.locator('[data-unit="%"] canvas')
    canvas.hover()
    hover = page.locator('[data-unit="%"] .hover').text_content()
    assert 'GPU 사용률' in hover and '미지원 / 누락' in hover
    assert page.evaluate('rows.at(-1).gpu_percent') is None
    assert page.evaluate('rows.at(-1)["power.VDD_IN_mw"]') == 5123


def test_other_log_views_still_use_original_single_metric(viewer):
    page, _, _ = viewer
    page.get_by_role('link', name='기능별', exact=True).click()
    page.wait_for_selector('#legend .check')
    assert 'YOLO 감지' in page.locator('#legend').text_content()
    assert page.locator('#systemMetrics').is_hidden()
    assert page.locator('#chart').is_visible()
    page.get_by_role('link', name='토픽', exact=True).click()
    playwright.expect(page.locator('#metric')).to_have_value('received_hz')
    assert '수집기 수신 Hz' in page.locator('#legend').text_content()
    page.get_by_role('link', name='전체 자원', exact=True).click()
    page.wait_for_selector('#systemCharts .unit-chart:visible')
    assert visible_units(page) == ['%', 'MiB']


def test_launch_gpu_selection_pages_and_browser_history(viewer):
    page, store, _ = viewer
    for launch, pid, name in [('tracking', 123, 'yolo_node'), ('fall', 456, 'fall_coordinator')]:
        store.register('launches/' + launch, kind='launch', label=launch)
        store.process(f'{pid}-45', dict(pid=pid, launch=launch, group='perception',
                                        executable='/test/' + name, ros_node_names=[name]))
        for t in range(1, 5):
            store.write('launches/' + launch, dict(
                t=t, identity=f'{pid}-45', cpu_percent=5,
                gpu_percent=None, gpu_memory_mib=128 if t < 4 else None,
                gpu_memory_source='jtop' if t < 4 else None))
    page.get_by_role('link', name='launch별', exact=True).click()
    playwright.expect(page.locator('#functionCount')).to_contain_text('/ 2개')
    page.get_by_role('button', name='모든 launch 표시').click()
    page.locator('#metric').select_option('gpu_memory_mib')
    playwright.expect(page.locator('#seriesCount')).to_contain_text('2 / 2')
    assert 'GPU 메모리' in page.locator('#legend').text_content()
    assert 'gpu_memory_source' not in page.locator('#metric').text_content()
    assert page.locator('#catalogPanel').is_hidden()
    assert page.locator('#actionPanel').is_hidden()
    assert page.evaluate('rows.at(-1).gpu_memory_mib') is None
    page.get_by_role('checkbox', name='fall.launch.py · 낙상', exact=True).uncheck()
    playwright.expect(page.locator('#seriesCount')).to_contain_text('1 / 1')
    page.get_by_role('link', name='프로세스 / 원본', exact=True).click()
    playwright.expect(page.locator('#catalogPanel')).to_be_visible()
    assert page.locator('#dataPanel').is_hidden()
    assert 'tracking.launch.py' in page.locator('#catalog').text_content()
    page.go_back()
    playwright.expect(page.locator('#metric')).to_have_value('gpu_memory_mib')
    page.reload()
    playwright.expect(page.locator('#pageTitle')).to_have_text('launch별 자원')
    playwright.expect(page.locator('#metric')).to_have_value('gpu_memory_mib')
    assert not page.get_by_role('checkbox', name='fall.launch.py · 낙상', exact=True).is_checked()
    assert page.locator('#pages a[aria-current="page"]').get_attribute('href') == '#launch'


def test_old_logs_do_not_invent_launch_membership(viewer):
    page, _, _ = viewer
    page.get_by_role('link', name='launch별', exact=True).click()
    playwright.expect(page.locator('#notice')).to_contain_text('launch 소속 기록이 없습니다')
    assert page.locator('#functions input').count() == 0
    assert page.locator('#legend input').count() == 0


def test_action_and_speech_live_in_separate_pages(viewer):
    page, store, _ = viewer
    store.register('actions/follow', kind='action', label='/follow_person')
    store.write('actions/follow', dict(t=1, goal_id='test-goal', state='EXECUTING'))
    store.write('actions/follow', dict(t=3, goal_id='test-goal', state='SUCCEEDED'))
    store.register('speech', kind='speech', label='speech')
    store.write('speech', dict(t=2, event='stt_transcript', text='안녕하세요'))
    page.get_by_role('link', name='실행 기록', exact=True).click()
    playwright.expect(page.locator('#events')).to_contain_text('SUCCEEDED')
    assert page.locator('#graph').is_hidden()
    assert page.locator('#speechPanel').is_hidden()
    page.locator('#events tr').click()
    playwright.expect(page.locator('#pageTitle')).to_have_text('전체 자원')
    playwright.expect(page.locator('#action')).to_have_value('actions/follow')
    assert page.locator('#end').input_value() == '5.00'
    page.get_by_role('link', name='음성 대화', exact=True).click()
    playwright.expect(page.locator('#speechRows')).to_contain_text('안녕하세요')
    assert page.locator('#actionPanel').is_hidden()
    assert page.locator('#catalogPanel').is_hidden()
