"""Exercise the shipped speech renderer without ROS or a browser server."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_speech_renderer_links_only_unambiguous_playback_ids():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is needed to execute the viewer JavaScript')
    viewer = Path(__file__).parents[1] / 'malbut_resource_monitor' / 'viewer.html'
    subprocess.run([node, '-', str(viewer)], input=r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const start = html.indexOf('  function speech(){');
const end = html.indexOf('  async function action(', start);
assert.ok(start >= 0 && end > start);

function element() {
  return {
    children: [], textContent: '',
    append(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
    get lastChild() { return this.children.at(-1); },
  };
}
const elements = new Map();
const context = {
  metadata: {session: 'first', channels: {speech: {kind: 'speech'}}},
  document: {createElement: element},
  $: id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  changed: (_key, _value, render) => render(),
  textCell: (row, value) => {
    const cell = element(); cell.textContent = value; row.append(cell);
  },
};
vm.createContext(context);
vm.runInContext(html.slice(start, end), context);

function render(rows) {
  context.rows = rows.map((row, t) => ({t, ...row}));
  const original = JSON.stringify(context.rows);
  context.speech();
  assert.equal(JSON.stringify(context.rows), original, 'raw events must stay unchanged');
  return elements.get('speechRows').children.map(row => row.children[2].textContent);
}
const request = (playback_id, text) => ({event: 'tts_request', playback_id, text});
const status = playback_id => ({event: 'tts_playback', playback_id, state: 'playing'});
const fallback = '— (문장 없는 상태 이벤트)';

// Match exact IDs even if topics arrive out of order; proximity is irrelevant.
assert.deepEqual(render([
  status('one'), request('two', '다른 답변'), request('one', '첫 답변 <원문>'),
  status('two'), status('missing'), request('', 'ID 없는 답변'), status(''),
]), [fallback, 'ID 없는 답변', fallback, '다른 답변', '첫 답변 <원문>', '다른 답변', '첫 답변 <원문>']);

// A request outside the 300 visible rows still identifies its status.
const many = [request('earlier', '앞선 요청 문장')];
for (let i = 0; i < 300; i++) many.push(status('earlier'));
assert.equal(render(many).length, 300);
assert.ok(render(many).every(text => text === '앞선 요청 문장'));

// Duplicate observations agree, but conflicting text for one ID is ambiguous.
assert.equal(render([request('same', '문장'), request('same', '문장'), status('same')])[0], '문장');
assert.equal(render([
  request('same', '문장'), request('same', '다른 문장'), request('same', '문장'), status('same'),
])[0], fallback);

// A new range/session must not reuse a request that is no longer available.
render([request('same', '이전 회차 문장'), status('same')]);
context.metadata.session = 'second';
assert.deepEqual(render([status('same')]), [fallback]);
""", text=True, check=True, capture_output=True)
