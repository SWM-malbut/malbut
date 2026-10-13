"""Optional browser regression tests; synthetic logs, no ROS or robot control."""

import os
import threading
from urllib.parse import urljoin

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
    server = LogServer(('127.0.0.1', 0), tmp_path, tmp_path / 'incidents')
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


def test_incident_trace_logs_pagination_download_and_selection_restore(viewer):
    from test_viewer_incidents import write_incident

    page, store, _ = viewer
    evidence = write_incident(store.path.parent / 'incidents', extra_events=205,
                              text='<img src=x onerror="window.injected=true">')
    page.get_by_role('link', name='STT 실행 추적', exact=True).click()
    playwright.expect(page.locator('#traceStages')).to_contain_text('추론 작업 큐에 저장')
    playwright.expect(page.locator('#traceStages')).to_contain_text('stale_generation')
    assert page.locator('#traceWarning').text_content().startswith('합성 테스트 데이터')
    assert '음성 저장 안 함' in page.locator('#traceWindow').text_content()
    assert 'Agent 답변 · 미관측' in page.locator('#traceCoverage').text_content()
    assert page.locator('#tracePanel img').count() == 0
    assert page.evaluate('window.injected') is None
    assert page.locator('#sessionField').is_hidden()
    assert page.locator('#dataPanel').is_hidden()
    page.locator('#traceSource').select_option('program')
    playwright.expect(page.locator('#traceLogs')).to_contain_text('Agent received u1')
    page.locator('#traceLogs summary').click()
    assert 'source_stamp_ns' in page.locator('#traceLogs pre').text_content()
    assert '1791728505899334199' in page.locator('#traceLogs pre').text_content()
    page.locator('#traceSource').select_option('vad')
    playwright.expect(page.locator('#traceLogSummary')).to_contain_text('205개 중 1–200')
    page.get_by_role('button', name='다음 200개', exact=True).click()
    playwright.expect(page.locator('#traceLogSummary')).to_contain_text('205개 중 201–205')
    assert page.locator('#traceLogs tr').count() == 5
    page.reload()
    playwright.expect(page.locator('#traceSource')).to_have_value('vad')
    playwright.expect(page.locator('#traceLogSummary')).to_contain_text('205개 중 1–200')
    link = page.get_by_role('link', name='events.jsonl 저장', exact=True).get_attribute('href')
    assert page.request.get(urljoin(page.url, link)).body() == (evidence / 'events.jsonl').read_bytes()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


def test_shadow_vad_scores_show_observation_scope_and_missing_frames(viewer):
    from test_viewer_incidents import write_incident

    page, store, _ = viewer
    write_incident(store.path.parent / 'incidents', shadow_trace=[
        dict(event='shadow_vad', observation_only=True, silero_probability=.0123,
             silero_speech=False, webrtc_frames=[dict(speech=True), dict(speech=False)],
             source=dict(blocked=True, captured_at=100.), queue_wait_s=.001,
             sample_index=512, dropped_chunks_total=2),
        dict(event='shadow_vad_gap', reason='queue_gap', missing_samples=1024),
    ])
    page.get_by_role('link', name='STT 실행 추적', exact=True).click()
    page.locator('#traceSource').select_option('vad')
    logs = page.locator('#traceLogs')
    playwright.expect(logs).to_contain_text('기존 STT 판정 유지')
    playwright.expect(logs).to_contain_text('Silero 음성 확률 0.0123 · 비음성')
    playwright.expect(logs).to_contain_text('관찰용 WebRTC 음성 1/2프레임')
    playwright.expect(logs).to_contain_text('입력 차단 중 · 대기 1.0ms · 누적 누락 2청크')
    playwright.expect(logs).to_contain_text('VAD 관찰 누락')
    playwright.expect(logs).to_contain_text('queue_gap')
    logs.locator('summary').first.click()
    assert 'captured_at' in logs.locator('pre').first.text_content()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


def test_silero_gate_is_distinct_from_observation_and_explains_blocking(viewer):
    from test_viewer_incidents import write_incident

    page, store, _ = viewer
    write_incident(store.path.parent / 'incidents', shadow_trace=[
        dict(event='silero_gate', allowed=False, peak_probability=.0123, longest_speech_s=0.),
        dict(event='silero_gate', allowed=True, peak_probability=.91, longest_speech_s=.096),
    ])
    page.get_by_role('link', name='STT 실행 추적', exact=True).click()
    page.locator('#traceSource').select_option('vad')
    logs = page.locator('#traceLogs')
    playwright.expect(logs).to_contain_text('비음성 차단 · Whisper 호출 안 함')
    playwright.expect(logs).to_contain_text('Silero 통과 · Whisper 호출 허용')
    playwright.expect(logs).to_contain_text('최고 음성 점수 0.0123 · 최장 연속 음성 0ms')
    playwright.expect(logs).to_contain_text('통과하면 검사 조기 종료')


def test_native_decode_scores_and_missing_measurements_are_distinct(viewer):
    from test_viewer_incidents import write_incident

    page, store, _ = viewer
    trace = dict(status='observed', return_code=0, windows_truncated=False, windows=[
        dict(start_s=0, end_s=1.08, next_seek_s=1.08,
             no_speech_prob=.95, avg_logprobs=-1.2,
             no_speech_threshold=.6, logprob_threshold=-1,
             skipped_no_speech=True, single_timestamp_ending=True),
        dict(start_s=1.08, end_s=2.08, next_seek_s=2.08,
             no_speech_prob=.95, avg_logprobs=-.2,
             no_speech_threshold=.6, logprob_threshold=-1,
             skipped_no_speech=False, single_timestamp_ending=False),
    ])
    write_incident(store.path.parent / 'incidents', native_trace=trace)
    page.get_by_role('link', name='STT 실행 추적', exact=True).click()
    stages = page.locator('#traceStages')
    playwright.expect(stages).to_contain_text('무음 확률 0.9500')
    playwright.expect(stages).to_contain_text('평균 로그확률 -1.2000')
    playwright.expect(stages).to_contain_text('무음으로 제외')
    playwright.expect(stages).to_contain_text('무음 제외 조건 미충족')
    playwright.expect(stages).to_contain_text('종료 타임스탬프로 창 건너뜀')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    write_incident(store.path.parent / 'incidents', 'incident-300-old-bridge',
                   native_trace=dict(status='unavailable', reason='bridge_abi_3', windows=[]))
    page.reload()
    page.locator('#incidentSelect').select_option('incident-300-old-bridge')
    playwright.expect(stages).to_contain_text('unavailable · bridge_abi_3')
    assert '무음 확률' not in stages.text_content()


def test_speech_row_opens_exact_utterance_and_missing_evidence_stays_empty(viewer):
    from test_viewer_incidents import write_incident

    page, store, _ = viewer
    write_incident(store.path.parent / 'incidents', uid='linked-turn')
    store.register('speech', kind='speech', label='speech')
    store.write('speech', dict(t=1, event='stt_transcript', text='기록 있는 발화',
                               utterance_id='linked-turn'))
    store.write('speech', dict(t=2, event='stt_transcript', text='과거 미수집 발화',
                               utterance_id='missing-turn'))
    page.get_by_role('link', name='음성 대화', exact=True).click()
    page.locator('#speechRows tr').filter(has_text='기록 있는 발화').get_by_role('button').click()
    playwright.expect(page.locator('#traceUtterance')).to_have_value('linked-turn')
    assert page.locator('#incidentSearch').input_value() == 'linked-turn'
    page.go_back()
    playwright.expect(page.locator('#pageTitle')).to_have_text('음성 대화')
    page.locator('#speechRows tr').filter(has_text='과거 미수집 발화').get_by_role('button').click()
    playwright.expect(page.locator('#incidentSummary')).to_contain_text('연결된 실행 기록이 없습니다')
    assert page.locator('#traceDetail').is_hidden()
    assert page.locator('#traceStages tr').count() == 0
    page.get_by_role('button', name='모든 저장 사건 보기', exact=True).click()
    playwright.expect(page.locator('#traceDetail')).to_be_visible()


def test_trace_works_without_any_resource_session_and_updates_new_bundles(viewer):
    from test_viewer_incidents import write_incident

    page, store, _ = viewer
    (store.path / 'metadata.json').unlink()
    page.goto(page.url.split('#')[0] + '#stt-trace')
    playwright.expect(page.locator('#incidentSummary')).to_contain_text('저장된 STT 실행 기록이 없습니다')
    write_incident(store.path.parent / 'incidents')
    playwright.expect(page.locator('#traceStages')).to_contain_text('최종 STT 발행')
    page.locator('#traceQuery').fill('stale_generation')
    playwright.expect(page.locator('#traceLogSummary')).to_contain_text('1개 중 1–1')
    assert page.locator('#traceLogs tr').count() == 1
    write_incident(store.path.parent / 'incidents', 'incident-200-later', uid='next-turn')
    playwright.expect(page.locator('#incidentSummary')).to_contain_text('2개 저장 사건')
    assert page.locator('#traceUtterance').input_value() == 'u1', 'live refresh must retain selection'
