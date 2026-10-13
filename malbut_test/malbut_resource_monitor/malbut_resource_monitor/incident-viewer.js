'use strict';

// Saved evidence only: no ROS calls, controls or time-based turn matching.
class IncidentView {
  constructor(get, refresh) {
    this.get = get; this.refresh = refresh; this.offset = 0; this.detail = null;
    this.$ = id => document.getElementById(id);
    this.key = 'malbut-stt-trace-v1';
    this.labels = {
      speech_started:'발화 시작', job_enqueued:'추론 작업 큐에 저장', job_dequeued:'추론 작업 꺼냄',
      job_rejected:'작업 큐 저장 거부', job_discarded:'추론 작업 폐기',
      decode_input:'Whisper 입력', decode_result:'Whisper 원시 결과', inference_result:'추론 결과',
      result_enqueued:'결과 큐에 저장', result_dequeued:'결과 큐에서 꺼냄',
      result_queue_wait:'결과 큐 대기', result_accepted:'결과 채택', result_rejected:'결과 폐기',
      result_held:'대상 판정 대기', preview_accepted:'부분 결과 채택',
      endpoint_cached:'종료 후보 보관', endpoint_reused:'종료 후보 재사용',
      endpoint_wait:'종료 후보 추론 대기', endpoint_not_cached:'종료 후보 보관 안 함',
      endpoint_cache_discarded:'종료 후보 폐기', transcript_published:'최종 STT 발행',
      program_log:'프로그램 로그', speech_transcript:'Agent 수신 처리',
      dialogue_response_published:'Agent 답변 발행', tts_request:'TTS 요청', tts_playback:'TTS 상태',
      wake_detected:'호출어 감지', vad_frame:'VAD 판정', microphone_chunk:'마이크 프레임',
      shadow_vad:'VAD 관찰 비교', shadow_vad_started:'VAD 관찰 시작',
      shadow_vad_gap:'VAD 관찰 누락', shadow_vad_error:'VAD 관찰 오류',
      shadow_vad_stopped:'VAD 관찰 종료',
      silero_gate:'Whisper 전 Silero 검사', silero_gate_error:'Silero 검사 오류',
      silero_gate_cancelled:'Silero 검사 취소',
    };
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(this.key)) || {}; } catch {}
    for (const id of ['incidentSearch','traceSource','traceQuery']) {
      if (typeof saved[id] === 'string') this.$(id).value = saved[id];
    }
    this.selected = saved.incident || ''; this.uid = saved.uid || '';
    this.$('incidentSelect').onchange = () => {
      this.selected = this.$('incidentSelect').value; this.uid = ''; this.offset = 0;
      this.clear(); this.save(); refresh();
    };
    this.$('traceUtterance').onchange = () => {
      this.uid = this.$('traceUtterance').value; this.renderTransaction(); this.save();
    };
    this.$('traceSource').onchange = () => { this.offset = 0; this.save(); refresh(); };
    let debounce;
    for (const id of ['incidentSearch','traceQuery']) this.$(id).oninput = () => {
      clearTimeout(debounce); this.offset = 0;
      if (id === 'incidentSearch') { this.selected = ''; this.uid = ''; this.clear(); }
      this.save(); debounce = setTimeout(refresh, 300);
    };
    this.$('traceAll').onclick = () => {
      this.$('incidentSearch').value = ''; this.selected = ''; this.uid = '';
      this.save(); refresh();
    };
    this.$('tracePrev').onclick = () => { this.offset = Math.max(0, this.offset - 200); refresh(); };
    this.$('traceNext').onclick = () => { this.offset += 200; refresh(); };
  }
  save() {
    try { localStorage.setItem(this.key, JSON.stringify({incident:this.selected,uid:this.uid,
      ...Object.fromEntries(['incidentSearch','traceSource','traceQuery'].map(id=>[id,this.$(id).value]))})); } catch {}
  }
  openUtterance(uid) {
    this.$('incidentSearch').value = uid; this.uid = uid; this.selected = ''; this.offset = 0;
    this.clear(); this.save(); location.hash = 'stt-trace';
  }
  options(element, pairs, selected) {
    const current = Array.from(element.options, option=>[option.value,option.textContent]);
    if (JSON.stringify(current) !== JSON.stringify(pairs)) {
      element.replaceChildren();
      for (const [value,label] of pairs) {
        const option = document.createElement('option'); option.value = value;
        option.textContent = label; element.append(option);
      }
    }
    if (pairs.some(([value])=>value===selected)) element.value = selected;
  }
  url(path, extra={}) { return path + '?' + new URLSearchParams({incident:this.selected,...extra}); }
  date(ns) { return Number.isFinite(ns) ? new Date(ns/1e6).toLocaleString() : '시각 미관측'; }
  clear() {
    this.detail = null; this.logs = null; this.$('traceDetail').hidden = true;
    this.$('traceStages').replaceChildren(); this.$('traceLogs').replaceChildren();
  }
  async load(signal) {
    const result = await this.get('/api/incidents?' + new URLSearchParams({q:this.$('incidentSearch').value}), signal);
    if (signal.aborted) return;
    this.$('incidentRoot').textContent = '저장 폴더: ' + result.root;
    if (result.legacy_roots?.length) this.$('incidentRoot').textContent +=
      ' · 이전 기록도 표시: ' + result.legacy_roots.join(', ');
    const pairs = result.items.map(item=>[item.id,
      (item.synthetic?'[합성 테스트] ':'') + this.date(item.triggers[0]?.wall_ns) + ' · ' +
      (item.triggers[0]?.text || item.id)]);
    this.options(this.$('incidentSelect'), pairs, this.selected);
    this.$('incidentSelect').disabled = !pairs.length;
    this.$('incidentSummary').textContent = result.total
      ? `${result.total}개 저장 사건${result.truncated?' · 최신 200개 표시, 검색으로 범위를 좁히세요.':''}`
      : this.$('incidentSearch').value ? '이 문구·발화 ID에 연결된 실행 기록이 없습니다. 과거 대화만으로 내부 처리 과정을 복원할 수는 없습니다.'
        : '저장된 STT 실행 기록이 없습니다. 업데이트한 STT를 실행하면 자동 수집하며, 대상 문구 감지 후 약 15초 뒤 표시됩니다.';
    if (result.unreadable) this.$('incidentSummary').textContent += ` · 읽을 수 없는 사건 ${result.unreadable}개`;
    if (!pairs.length) { this.selected = ''; this.clear(); return; }
    const selected = this.$('incidentSelect').value;
    if (selected !== this.selected) { this.selected = selected; this.offset = 0; this.clear(); }
    const detail = await this.get(this.url('/api/incident'), signal);
    if (signal.aborted) return;
    if (this.detail !== detail) { this.detail = detail; this.renderDetail(); }
    const logs = await this.get(this.url('/api/incident/events', {
      source:this.$('traceSource').value || 'pipeline', q:this.$('traceQuery').value, offset:this.offset,
    }), signal);
    if (signal.aborted) return;
    if (this.logs !== logs) { this.logs = logs; this.renderLogs(logs); }
    this.save();
  }
  renderDetail() {
    const manifest = this.detail.manifest, turns = this.detail.trace.transactions;
    this.$('traceDetail').hidden = false;
    this.$('traceWarning').textContent = [
      manifest.metadata?.synthetic ? '합성 테스트 데이터입니다. 로봇에서 발생한 사건이 아닙니다.' : '',
      !manifest.post_window_complete ? '종료 후 관측 구간이 끝나기 전에 저장되었습니다.' : '',
      manifest.buffer_evictions_total ? `버퍼 초과로 ${manifest.buffer_evictions_total}개 항목이 제외되었습니다.` : '',
      manifest.skipped_triggers_total ? `대기열 초과로 ${manifest.skipped_triggers_total}개 저장 트리거를 놓쳤습니다.` : '',
      manifest.capture_error ? '기록 오류: ' + manifest.capture_error : '',
    ].filter(Boolean).join(' ');
    this.$('traceWindow').textContent = `요청 구간: 감지 전 ${manifest.pre_seconds}초 / 후 ${manifest.post_seconds}초 · ` +
      `관측 이벤트 ${manifest.events}개 · ${manifest.audio_saved===false?'음성 저장 안 함':manifest.audio_saved===true?'음성도 저장됨':'음성 저장 여부 미기록'}`;
    this.$('traceManifest').textContent = JSON.stringify(manifest, null, 2);
    this.options(this.$('traceUtterance'), turns.map(turn=>[
      turn.utterance_id,(turn.triggered?'문제 문구 감지 · ':'함께 관측 · ') + turn.utterance_id,
    ]), this.uid || turns.find(turn=>turn.triggered)?.utterance_id);
    this.uid = this.$('traceUtterance').value;
    this.$('traceDownloads').replaceChildren();
    for (const name of ['incident.json','transactions.json','events.jsonl','rosout.jsonl']) {
      const link = document.createElement('a'); link.className = 'button';
      link.textContent = name + ' 저장'; link.href = this.url('/api/incident/download',{file:name});
      this.$('traceDownloads').append(link);
    }
    this.renderTransaction();
  }
  cell(row, text) {
    const cell = document.createElement('td'); cell.textContent = text; row.append(cell); return cell;
  }
  nativeSummary(trace) {
    if (!trace) return '';
    if (trace.status !== 'observed') return 'Whisper 판정: ' + trace.status + ' · ' + (trace.reason || '');
    const number = value => Number.isFinite(value) ? value.toFixed(4) : '미측정';
    const lines = (trace.windows || []).map((window, index) =>
      `창 ${index+1}: ${number(window.start_s)}–${number(window.end_s)}초 · ` +
      `무음 확률 ${number(window.no_speech_prob)} (기준 > ${number(window.no_speech_threshold)}) · ` +
      `평균 로그확률 ${number(window.avg_logprobs)} (기준 < ${number(window.logprob_threshold)})\n` +
      (window.skipped_no_speech ? '무음으로 제외' : '무음 제외 조건 미충족') +
      ` · 다음 위치 ${number(window.next_seek_s)}초` +
      (window.single_timestamp_ending ? ' · 종료 타임스탬프로 창 건너뜀' : '') +
      (window.nonfinite_fields ? ' · 비유한 점수: ' + JSON.stringify(window.nonfinite_fields) : ''));
    if (!lines.length) lines.push('Whisper 판정: 관측된 디코딩 창 없음');
    if (trace.windows_truncated) lines.push('판정 기록 상한 초과: 이후 창 미기록');
    if (trace.return_code) lines.push('추론 미완료: 반환 코드 ' + trace.return_code);
    return lines.join('\n');
  }
  renderTransaction() {
    const turn = this.detail?.trace.transactions.find(item=>item.utterance_id===this.uid);
    this.$('traceStages').replaceChildren(); this.$('traceCoverage').replaceChildren();
    if (!turn) return;
    const names = {stt_published:'STT 발행',agent_received:'Agent 수신',
      agent_response_published:'Agent 답변',tts_requested:'TTS 요청',tts_finished:'재생 완료'};
    for (const [key,label] of Object.entries(names)) {
      const badge = document.createElement('span'); badge.className = 'trace-badge';
      badge.textContent = label + (turn.observed?.[key]?' · 관측':' · 미관측');
      badge.dataset.observed = turn.observed?.[key]===true ? 'true' : 'false';
      this.$('traceCoverage').append(badge);
    }
    for (const stage of turn.stages) {
      const row = document.createElement('tr');
      const relative = Number.isFinite(stage.seconds_from_trigger)
        ? (stage.seconds_from_trigger>=0?'+':'')+stage.seconds_from_trigger.toFixed(3)+'초' : '기준 시점 없음';
      this.cell(row,relative+'\n'+this.date(stage.wall_ns));
      this.cell(row,this.labels[stage.source_event || stage.event] || stage.event);
      const fields = ['text','reason','error','status','kind','generation','revision','decode_id','state','playback_id'];
      const values = fields.filter(key=>stage[key]!=null).map(key=>key+': '+
        (typeof stage[key]==='object'?JSON.stringify(stage[key]):stage[key]));
      if (stage.native_decode) values.push(this.nativeSummary(stage.native_decode));
      this.cell(row,values.join('\n') || '—');
      this.cell(row,'events.jsonl #'+stage.event_line); this.$('traceStages').append(row);
    }
  }
  shadowSummary(event) {
    if (event.event !== 'shadow_vad') return '';
    const frames = Array.isArray(event.webrtc_frames) ? event.webrtc_frames : [];
    const probability = typeof event.silero_probability === 'number'
      ? event.silero_probability.toFixed(4) : '—';
    const wait = typeof event.queue_wait_s === 'number'
      ? (event.queue_wait_s * 1000).toFixed(1)+'ms' : '—';
    return `관찰용 · 기존 STT 판정 유지\nSilero 음성 확률 ${probability} · `+
      (event.silero_speech ? '음성' : '비음성')+
      ` / 관찰용 WebRTC 음성 ${frames.filter(f=>f.speech).length}/${frames.length}프레임\n`+
      `입력 차단 ${event.source?.blocked ? '중' : '아님'} · 대기 ${wait} · `+
      `누적 누락 ${event.dropped_chunks_total ?? 0}청크\n`+
      '차단 전 입력 비교 · Silero 32ms / WebRTC 20ms (프레임 범위는 원본 필드 참고)';
  }
  gateSummary(event) {
    if (event.event !== 'silero_gate') return '';
    const peak = typeof event.peak_probability === 'number'
      ? event.peak_probability.toFixed(4) : '—';
    const speech = typeof event.longest_speech_s === 'number'
      ? (event.longest_speech_s * 1000).toFixed(0)+'ms' : '—';
    return (event.allowed ? 'Silero 통과 · Whisper 호출 허용' : '비음성 차단 · Whisper 호출 안 함')+
      `\n검사한 구간 최고 음성 점수 ${peak} · 최장 연속 음성 ${speech}\n`+
      '기준: 점수 0.5 이상 · 연속 80ms 이상 (통과하면 검사 조기 종료)';
  }
  renderLogs(data) {
    this.$('traceLogs').replaceChildren();
    const first = data.rows.length ? data.offset+1 : 0;
    this.$('traceLogSummary').textContent = `${data.matching}개 중 ${first}–${data.offset+data.rows.length}개 표시` +
      (data.malformed?` · 손상 행 ${data.malformed}개 제외`:'') + (data.partial_line?' · 미완성 마지막 행 제외':'');
    this.$('tracePrev').disabled = this.offset===0; this.$('traceNext').disabled = !data.has_more;
    for (const item of data.rows) {
      const event = item.record, row = document.createElement('tr');
      this.cell(row,'#'+item.line+'\n'+this.date(event.wall_ns));
      this.cell(row,event.logger || this.labels[event.event] || event.event);
      const cell = this.cell(row,this.gateSummary(event) || this.shadowSummary(event) ||
        event.message || event.text || event.reason || event.error || event.event);
      const details = document.createElement('details'), summary = document.createElement('summary');
      summary.textContent = '원본 필드 보기';
      // Keep integer nanoseconds exact; JSON.parse/JSON.stringify would round them.
      const raw = document.createElement('pre'); raw.textContent = item.raw;
      details.append(summary,raw); cell.append(details); this.$('traceLogs').append(row);
    }
  }
}
