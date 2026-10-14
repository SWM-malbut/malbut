/* SQLite memory evidence. All database text is rendered as text, never HTML. */
class MemoryView {
  constructor(get, refresh) {
    this.get = get; this.refresh = refresh; this.selected = ''; this.signatures = new Map();
    const $ = id => document.getElementById(id); this.$ = $;
    for (const id of ['memoryDatabase', 'memoryUser', 'memoryConversation']) {
      $(id).onchange = () => {
        this.selected = '';
        if (id === 'memoryDatabase') {
          $('memoryUser').replaceChildren(); this.signatures.delete('memoryUser');
          $('memoryQuery').value = '';
        }
        if (id !== 'memoryConversation') {
          $('memoryConversation').replaceChildren(); this.signatures.delete('memoryConversation');
        }
        refresh();
      };
    }
    $('memorySearchForm').onsubmit = e => {e.preventDefault(); this.selected = ''; refresh();};
    $('memoryTurn').onchange = () => {this.selected = $('memoryTurn').value; $('memoryFollow').checked = false; refresh();};
    $('memoryFollow').onchange = () => {this.selected = ''; refresh();};
    $('memoryReload').onclick = refresh;
  }
  node(tag, text, className) {
    const node = document.createElement(tag); if (text !== undefined) node.textContent = text;
    if (className) node.className = className; return node;
  }
  changed(key, value, render) {
    const signature = JSON.stringify(value);
    if (this.signatures.get(key) !== signature) {render(); this.signatures.set(key, signature);}
  }
  options(id, values, selected) {
    const element = this.$(id);
    this.changed(id, values, () => element.replaceChildren(...values.map(([value, text]) => {
      const option = this.node('option', text); option.value = value; return option;
    })));
    if (values.some(([value]) => value === selected)) element.value = selected;
  }
  url(route, extra = {}) {
    return route + '?' + new URLSearchParams({database: this.$('memoryDatabase').value,
      user: this.$('memoryUser').value, ...extra});
  }
  time(value) {return typeof value === 'number' ? new Date(value * 1000).toLocaleString() : '—';}
  jobSummary(activity) {
    const states = {queued:'대기', running:'정리 중', done:'완료', failed:'실패', cancelled:'취소'};
    const errors = {actor_source_mismatch:'사용자·AI 근거 혼합', invalid_extraction:'정리 결과 검증 실패',
      invalid_previous_evidence:'이전 근거 변경 오류', invalid_recall_coverage:'회상 원문 연결 오류',
      evidence_mismatch:'원문 인용 불일치', provider_error:'정리 모델 호출 실패',
      source_too_large:'정리 원문 길이 초과', processing_failed:'정리 실패 · 상세 원인 미기록'};
    const jobs = activity?.jobs || [], failed = jobs.some(j => j.state === 'failed');
    const text = jobs.map(j => (states[j.state] || j.state) +
      (j.attempts ? ' · ' + j.attempts + '회 시도' : '') +
      (j.error_code || j.reason ? ' · ' + (errors[j.error_code || j.reason] || j.error_code || j.reason) : '')).join('\n');
    return {failed, text:(text || '작업 기록 없음') +
      (activity?.failure_attempts ? '\n실패 이력 ' + activity.failure_attempts + '회' : '')};
  }
  selectRequest(request, narrow = false) {
    this.selected = request; this.$('memoryFollow').checked = false;
    if (narrow) {this.$('memoryConversation').value = ''; this.$('memoryQuery').value = request;}
    this.refresh();
  }
  async load(signal) {
    const $ = this.$;
    const databases = await this.get('/api/memory/databases', signal);
    this.options('memoryDatabase', databases.map(d => [d.id, d.path.split('/').pop() + ' · ' + d.path + (d.exists ? '' : ' · 아직 없음')]), $('memoryDatabase').value);
    const database = databases.find(d => d.id === $('memoryDatabase').value);
    if (!database?.exists) {
      $('memoryStatus').textContent = '대화 DB가 아직 없습니다. 음성 대화를 실행하거나 --memory-db로 실험 DB를 지정하세요.';
      $('memoryContent').hidden = true; return;
    }
    const data = await this.get(this.url('/api/memory', {conversation: $('memoryConversation').value,
      q: $('memoryQuery').value.trim()}), signal);
    $('memoryContent').hidden = false;
    this.options('memoryUser', data.users.map(id => [id, id]), data.user);
    this.options('memoryConversation', [['', '모든 대화 · 재시작 전후 비교'], ...data.conversations.map(c =>
      [c.conversation_id, c.conversation_id + ' · 세대 ' + c.generation + ' · ' + this.time(c.updated_at)])], $('memoryConversation').value);
    $('memoryStatus').textContent = 'SQLite 읽기 전용 · ' + new Date().toLocaleTimeString() + ' 확인 · ' +
      (data.trace_available ? '단계별 추적 기록 사용 가능' : '기존 DB: 단계별 추적은 업데이트한 Agent의 다음 대화부터 기록됩니다.') +
      (data.turns_truncated ? ' · 최근 100개만 표시, 대화/검색으로 범위를 좁히세요.' : '');
    this.changed('cards', [data.user, data.policies, data.jobs, data.stories.length, data.facts.length], () => {
      const story = data.policies.story_runtime_policy?.enabled, fact = data.policies.memory_policy_state?.enabled;
      const jobs = Object.values(data.jobs), pending = jobs.reduce((n, j) => n + (j.queued || 0) + (j.running || 0), 0);
      const failed = jobs.reduce((n, j) => n + (j.failed || 0), 0);
      $('memoryCards').replaceChildren(...[
        ['이야기 기억', story ? '켜짐' : '꺼짐', '현재 저장 ' + data.stories.length + '개'],
        ['개인 사실 기억', fact ? '켜짐' : '꺼짐', '현재 저장 ' + data.facts.length + '개'],
        ['정리 대기 / 실행 중', pending + '건', '답변 완료와 기억 저장 완료는 별도입니다.'],
        ['실패한 작업', failed + '건', '아래 실패·재시도 이력에서 원인을 확인하세요.']
      ].map(([label, value, note]) => {const card = this.node('div', undefined, 'summary-card');
        card.append(this.node('span', label), this.node('strong', value), this.node('small', note)); return card;}));
    });
    const turns = data.turns;
    if ($('memoryFollow').checked || !turns.some(t => t.request_id === this.selected)) this.selected = turns[0]?.request_id || '';
    this.changed('attention', [database.id, data.user, data.attention, data.attention_truncated], () => {
      $('memoryAttention').replaceChildren();
      for (const item of data.attention || []) {
        const state = this.jobSummary(item.activity), block = this.node('div', undefined, 'memory-entry');
        const button = this.node('button', (item.user_content || item.request_id).slice(0,140)); button.type = 'button';
        button.onclick = () => this.selectRequest(item.request_id, true);
        block.append(button, this.node('p', state.text, state.failed ? 'memory-failed memory-lines' : 'memory-ok memory-lines'),
          this.node('small', item.request_id, 'memory-id')); $('memoryAttention').append(block);
      }
      if (!data.attention?.length) $('memoryAttention').append(this.node('p', '현재 실패 작업이나 관측된 정리 실패 이력이 없습니다.', 'muted'));
      if (data.attention_truncated) $('memoryAttention').append(this.node('p', '최근 50개 실패 발화까지 표시합니다.', 'muted'));
    });
    this.changed('activity', [database.id, data.user, turns, this.selected], () => {
      $('memoryActivity').replaceChildren();
      for (const item of turns) {
        const a = item.activity || {}, state = this.jobSummary(a), tr = this.node('tr');
        tr.setAttribute('aria-selected', String(item.request_id === this.selected));
        const cell = this.node('td'), button = this.node('button', (item.user_content || item.request_id).slice(0,100));
        button.type = 'button'; button.onclick = () => this.selectRequest(item.request_id);
        cell.append(this.node('small', this.time(item.created_at)), this.node('br'), button);
        tr.append(cell, this.node('td', a.saved ? '커밋 확인 ' + a.saved + '회' : a.no_change ? '정리 완료 · 변경 없음' : '저장 커밋 미관측'),
          this.node('td', a.model_input ? '이야기 ' + a.model_input.stories + '개 / 사실 ' + a.model_input.facts + '개' : '입력 기록 없음'),
          this.node('td', state.text, state.failed ? 'memory-failed memory-lines' : 'memory-lines'));
        $('memoryActivity').append(tr);
      }
    });
    this.options('memoryTurn', turns.map(t => [t.request_id, this.time(t.created_at) + ' · ' + (t.user_content || '(본문 삭제됨)').slice(0,80)]), this.selected);
    const turn = turns.find(t => t.request_id === this.selected);
    this.changed('dialogue', turn || null, () => {
      $('memoryDialogue').replaceChildren();
      if (!turn) {$('memoryDialogue').append(this.node('p', '선택 조건에 해당하는 대화가 없습니다.', 'muted')); return;}
      $('memoryDialogue').append(this.node('p', '요청 ' + turn.request_id + '\n대화 ' + turn.conversation_id + ' · 세대 ' + turn.generation + ' · 턴 ' + turn.ordinal + ' · ' + turn.status, 'memory-id'));
      for (const [label, content] of [['사용자', turn.user_content], ['Agent 답변', turn.assistant_content]]) {
        const block = this.node('div', undefined, 'memory-message');
        block.append(this.node('strong', label), this.node('p', content || '(본문 없음 / 삭제됨)')); $('memoryDialogue').append(block);
      }
    });
    if (turn) {
      const detail = await this.get(this.url('/api/memory/turn', {request: turn.request_id}), signal);
      this.renderTrace(turn, detail);
    } else {this.renderTrace(null, {events: [], jobs: []});}
    this.changed('stored', [database.id, data.user, data.stories, data.facts, data.stories_truncated, data.facts_truncated], () => {
      $('memoryStored').replaceChildren();
      if (!data.stories.length && !data.facts.length) $('memoryStored').append(this.node('p', '현재 저장된 장기기억이 없습니다.', 'muted'));
      for (const story of data.stories) {
        const entry = this.node('details', undefined, 'memory-entry');
        entry.append(this.node('summary', story.title + ' · v' + story.version + (story.active ? '' : ' · 비활성')),
          this.node('p', story.story_id + ' · 갱신 ' + this.time(story.updated_at), 'memory-id'));
        for (const item of story.current) entry.append(this.node('p', '[' + [item.actor, item.kind, item.status].filter(Boolean).join(' / ') + '] ' + item.text));
        const button = this.node('button', 'SQLite 원문 근거 보기'), output = this.node('div');
        button.type = 'button';
        button.onclick = async () => {
          button.disabled = true;
          try {
            const result = await this.get(this.url('/api/memory/story', {story: story.story_id}), AbortSignal.timeout(10000));
            output.replaceChildren(this.node('p', '현재 버전 v' + result.version + (result.truncated ? ' · 근거 100개까지만 표시' : ''), 'muted'));
            for (const source of result.sources) {
              const block = this.node('div', undefined, 'memory-message');
              block.append(this.node('small', this.time(source.completed_at) + ' · ' + source.ref.role + ' · 요청 ' + (source.request_id || '없음')),
                this.node('p', source.available ? source.text : '(삭제되었거나 현재 원문과 일치하지 않는 근거)')); output.append(block);
            }
          } catch (error) {output.replaceChildren(this.node('p', error.message));}
          finally {button.disabled = false;}
        };
        entry.append(button, output); $('memoryStored').append(entry);
      }
      for (const fact of data.facts) {
        const block = this.node('details', undefined, 'memory-entry');
        block.append(this.node('summary', '사실 · ' + fact.kind + ' · ' + fact.content),
          this.node('p', fact.id + ' · 저장 ' + this.time(fact.created_at) + ' · 만료 ' + this.time(fact.expires_at), 'memory-id'));
        $('memoryStored').append(block);
      }
      if (data.stories_truncated || data.facts_truncated) $('memoryStored').append(this.node('p', '각 종류의 최신 100개까지 표시합니다.', 'muted'));
    });
    this.exportValue = {database: database.path, user: data.user, turn, snapshot: data, detail: this.detail};
    $('memoryExport').onclick = () => {
      const blob = new Blob([JSON.stringify(this.exportValue, null, 2)], {type: 'application/json'});
      const url = URL.createObjectURL(blob), link = this.node('a'); link.href = url; link.download = 'memory-evidence.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
  }
  renderTrace(turn, detail) {
    this.detail = detail;
    this.changed('trace', [turn?.request_id, detail], () => {
      const $ = this.$, events = detail.events;
      $('memoryStages').replaceChildren(); $('memoryJobs').replaceChildren();
      const labels = {memory_retrieved: 'SQLite 기억 조회', model_input: '모델 요청 입력 확정',
        provider_completed: '모델 응답 수신', provider_failed: '모델 호출 실패', turn_committed: '대화·답변 DB 커밋',
        fact_committed: '사실 기억 DB 커밋', story_committed: '이야기 기억 DB 커밋', story_no_change: '이야기 정리 완료 · 저장 후보 없음',
        story_queued:'이야기 정리 대기 등록', story_extraction_started:'이야기 정리 시작',
        story_extraction_failed:'이야기 정리 실패', story_retry_requested:'이야기 정리 재시도 요청'};
      $('memoryTraceNote').textContent = !events.length ? '이 발화의 단계별 추적은 기록되지 않았습니다. 아래 DB 상태만으로 모델 입력이나 회상 성공을 추정하지 않습니다.' :
        '요청 ID가 일치하는 이벤트만 표시합니다. 모델 입력은 최종 길이 제한 적용 후의 기록이며, 답변이 기억을 정확히 활용했는지는 원문·답변을 비교해 판단하세요.' +
        (detail.events_truncated ? ' · 첫 500개 이벤트만 표시' : '');
      for (const event of events) {
        const tr = this.node('tr'), data = event.data, lines = [];
        if (data.status) lines.push('상태: ' + data.status + ' · 대기 ' + (data.pending || 0) + ' · 실패 ' + (data.failed || 0));
        if (Array.isArray(data.fact_ids)) lines.push('사실: ' + (data.fact_ids.join(', ') || '0개'));
        if (Array.isArray(data.stories)) lines.push('이야기: ' + (data.stories.map(s => s.story_id + ' v' + s.version).join(', ') || '0개'));
        if ('input_chars' in data) lines.push('입력 ' + data.input_chars + '자 · 이전 메시지 ' + data.history_messages + '개 · 요약 ' + (data.summary_present ? '있음' : '없음') + ' · 원문 근거 ' + data.evidence_count + '개');
        if (data.provider) lines.push(data.provider + ' / ' + data.model);
        if ('latency_ms' in data) lines.push(Math.round(data.latency_ms) + ' ms');
        if (data.error_type) lines.push(data.error_type);
        if (data.error_code) lines.push(this.jobSummary({jobs:[{state:data.state || 'failed', error_code:data.error_code}]}).text);
        if ('attempt' in data) lines.push('시도 ' + data.attempt + '회');
        if ('retry_scheduled' in data) lines.push(data.retry_scheduled ? data.retry_after_seconds + '초 뒤 자동 재시도' : (data.attempt >= 3 ? '자동 재시도 소진' : '자동 재시도 없음') + ' · 실패 상태 유지');
        if (data.source_request_ids) lines.push('정리 대상 요청: ' + data.source_request_ids.join(', '));
        const result = this.node('td', undefined, 'memory-lines'); result.append(this.node('div', lines.join('\n')));
        const raw = this.node('details'); raw.append(this.node('summary', '식별자 / 원본 필드'), this.node('pre', JSON.stringify(data, null, 2))); result.append(raw);
        tr.append(this.node('td', this.time(event.wall_time)), this.node('td', labels[event.event] || event.event), result); $('memoryStages').append(tr);
      }
      for (const job of detail.jobs) $('memoryJobs').append(this.node('p', (job.type === 'story_runtime_jobs' ? '이야기 정리' : '사실 추출') + ' · ' + job.state + (job.error_code || job.reason ? ' · ' + (job.error_code || job.reason) : '')));
      if (!detail.jobs.length) $('memoryJobs').append(this.node('p', '이 발화에 연결된 기억 작업 없음', 'muted'));
      if (!events.length && detail.legacy_fact_ids?.length) $('memoryJobs').append(this.node('p', '기존 응답에 기록된 사실 ID: ' + detail.legacy_fact_ids.join(', '), 'memory-id'));
    });
  }
}
