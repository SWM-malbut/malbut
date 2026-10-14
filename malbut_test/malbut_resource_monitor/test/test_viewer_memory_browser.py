"""Exercise the integrated memory tab without a recorder, model or robot."""

import os
import sqlite3
import threading

import pytest

from malbut_resource_monitor.viewer import LogServer
from test_viewer_memory import database, add_failure  # shared synthetic SQLite fixture

playwright = pytest.importorskip('playwright.sync_api')


def test_memory_page_reads_sqlite_live_and_keeps_database_text_inert(database, tmp_path):
    add_failure(database)
    other_database = tmp_path / 'same-users.sqlite3'
    with sqlite3.connect(database) as source, sqlite3.connect(other_database) as target:
        source.backup(target)
    server = LogServer(('127.0.0.1', 0), tmp_path / 'no-recordings', memory_databases=[database, other_database])
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True,
                executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#memory')
            playwright.expect(page.locator('#memoryStatus')).to_contain_text('SQLite 읽기 전용')
            playwright.expect(page.locator('#memoryStages')).to_contain_text('모델 호출 실패')
            playwright.expect(page.locator('#memoryActivity')).to_contain_text('사용자·AI 근거 혼합')
            playwright.expect(page.locator('#memoryAttention')).to_contain_text('실패 이력 1회')
            page.locator('#memoryAttention button').first.click()
            playwright.expect(page.locator('#memoryStages')).to_contain_text('이야기 정리 실패')
            page.locator('#memoryQuery').fill('')
            page.locator('#memorySearchForm').evaluate('(form) => form.requestSubmit()')
            page.locator('#memoryTurn').select_option('same-request')
            playwright.expect(page.locator('#memoryDialogue')).to_contain_text('편안했어 <script>')
            assert page.locator('#memoryDialogue script').count() == 0
            assert not page.locator('#memoryFollow').is_checked()
            playwright.expect(page.locator('#memoryStages')).to_contain_text('모델 요청 입력 확정')
            page.locator('#memoryStored summary').first.click()
            page.get_by_role('button', name='SQLite 원문 근거 보기').click()
            playwright.expect(page.locator('#memoryStored')).to_contain_text('요청 same-request')
            with sqlite3.connect(database) as conn:
                conn.execute("UPDATE conversation_turns SET assistant_content='변경된 답변' WHERE user_id='alice'")
                conn.execute("UPDATE story_runtime_jobs SET state='done',attempts=1,error_code=NULL")
            playwright.expect(page.locator('#memoryDialogue')).to_contain_text('변경된 답변')
            playwright.expect(page.locator('#memoryAttention')).to_contain_text('완료')
            playwright.expect(page.locator('#memoryAttention')).to_contain_text('실패 이력 1회')
            assert page.locator('#memoryTurn').input_value() == 'same-request'
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            with page.expect_download() as download:
                page.get_by_role('button', name='현재 확인 결과 JSON 저장').click()
            assert download.value.suggested_filename == 'memory-evidence.json'
            page.get_by_role('link', name='전체 자원', exact=True).click()
            playwright.expect(page.locator('#memoryPanel')).to_be_hidden()
            page.get_by_role('link', name='대화·장기기억', exact=True).click()
            playwright.expect(page.locator('#memoryPanel')).to_be_visible()
            page.locator('#memoryUser').select_option('bob')
            playwright.expect(page.locator('#memoryDialogue')).to_contain_text('다른 사용자 비밀')
            assert '편안했어' not in page.locator('#memoryDialogue').inner_text()
            page.locator('#memoryQuery').fill('previous-database-request')
            page.locator('#memoryDatabase').select_option(index=1)
            playwright.expect(page.locator('#memoryQuery')).to_have_value('')
            playwright.expect(page.locator('#memoryUser')).to_have_value('alice')
            playwright.expect(page.locator('#memoryConversation option')).to_have_count(2)
            playwright.expect(page.locator('#memoryStages')).to_contain_text('모델 호출 실패')
            assert not errors
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()
