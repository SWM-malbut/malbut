"""Build a local, source-linked engineering comparison; no external publishing."""
from pathlib import Path
from html import escape
import json

ROOT = Path(__file__).resolve().parent


def read(name):
    return json.loads((ROOT / name).read_text())


def cpu(result):
    if result.get('fault'):
        return {}  # A killed PID resets /proc counters; no comparable CPU value.
    start = next(t for t, s in result['statuses'] if s == 'playing')
    end = next(t for t, s in result['statuses'] if s in ('finished', 'stopped', 'failed'))
    values = {}
    for phase, lo, hi in [('idle', result['ready_s'] + 2, start - 1),
                          ('play', start + 1, end - 1)]:
        rows = [row for row in result['cpu'] if lo < row[0] < hi]
        if len(rows) >= 2:
            a, b = rows[0], rows[-1]
            values[phase] = 100 * ((b[1] + b[2]) - (a[1] + a[2])) / (b[0] - a[0]) / 6
    return values


def trial(name):
    result = read(name + '.json')
    return dict(file=name + '.json', cpu=cpu(result),
                stops=sum(c[1] == 'stop' for c in result['controls']),
                ducks=sum(c[1] == 'duck' for c in result['controls']),
                statuses=result['statuses'], failure=result['failure'],
                error=result['error'], calibration=result['calibration'])


def integration(name):
    d = read(name)
    injected = next((t for t, kind, *_ in d['events'] if kind == 'near_injected'), None)
    stopped = [s['at'] for s in d['states'] if s['state'] == 'stopped']
    return dict(file=name, injected=injected is not None,
                injection_to_stopped_s=(stopped[0] - injected if stopped and injected is not None else None),
                transcripts=d['transcripts'], responses=d['requests'], states=d['states'])


def main():
    results = {name: trial(name) for name in
               ['final_baseline', 'final_heldout', 'final_long', 'relaxed_soak_failed',
                'strict_soak', 'strict_heldout', 'strict_fault'] if (ROOT / (name + '.json')).exists()}
    mixtures = {}
    for name in ['final_dual_doubletalk', 'strict_doubletalk']:
        cases = read(name + '.json')['cases']
        mixtures[name] = {str(ser): dict(detected=sum(c['detected'] for c in cases if c['ser_db'] == ser),
                                       total=sum(c['ser_db'] == ser for c in cases)) for ser in [0, -6, -12]}
        mixtures[name]['false_before'] = sum(c['false_before'] for c in cases)
    e2e = [integration(p.name) for p in sorted(ROOT.glob('strict_e2e_*.json'))]
    service = {name: read(name + '.json') for name in
               ['final_health', 'final_service_observation', 'legacy_service_observation',
                'final_deployment_check'] if (ROOT / (name + '.json')).exists()}
    summary = dict(trials=results, mixed_recording_detection=mixtures, integration=e2e,
                   service=service, tests={'stt_passed': 1227, 'stt_skipped': 5,
                                         'tts_bringup_passed': 512, 'tts_bringup_skipped': 5},
                   cpu_definition='Parent plus AEC worker process CPU, divided by 6 logical CPUs; excludes PulseAudio and other services.',
                   limitations=['Injected recordings are not a live human acoustic test.',
                                'XFM internal microphone delay is about 0.5 seconds.',
                                'Quiet overlapping voices may be missed and ASR can misrecognize words.',
                                'Low-confidence startup calibration disables barge-in for that run.',
                                'Limited trials do not prove zero false interruptions.'])
    (ROOT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    service_text = ''
    if 'legacy_service_observation' in service and 'final_service_observation' in service:
        old = service['legacy_service_observation']['whole_device_idle_cpu_pct']
        new = service['final_service_observation']['whole_device_idle_cpu_pct']
        service_text = (f'<p>별도로 전체 음성 서비스(Agent·TTS·STT·AEC)를 각각 60초 대기 상태로 측정했다. '
                        f'기존 {old:.2f}% → 개선 {new:.2f}%로, 6코어 전체 기준 +{new-old:.2f}%p였다. '
                        '이 수치는 아래의 처리 경로 단독 측정과 범위가 다르다.</p>')
    rows = []
    for name, r in results.items():
        usage = r['cpu']
        idle = f"{usage['idle']:.2f}%" if 'idle' in usage else '—'
        play = f"{usage['play']:.2f}%" if 'play' in usage else '—'
        rows.append(f"<tr><td><a href='{r['file']}'>{escape(name)}</a></td><td>{r['stops']}</td>"
                    f"<td>{r['ducks']}</td><td>{idle}</td>"
                    f"<td>{play}</td><td>{escape(str(r['failure']))}</td></tr>")
    interactions = ''.join('<li><a href="' + e['file'] + '">' + e['file'] + '</a>: '
                           + str(round(e['injection_to_stopped_s'], 3) if e['injection_to_stopped_s'] is not None else '중단 없음')
                           + ('초 / 전사: ' if e['injected'] else ' — 보정 신뢰도 부족으로 말 끊기 비활성 / 전사: ')
                           + escape(' → '.join(t['text'] for t in e['transcripts'])) + '</li>' for e in e2e)
    body = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>말벗 AEC 비교 · 2026-10-28</title>
<style>body{{font:16px/1.7 system-ui;max-width:1050px;margin:40px auto;padding:0 24px;color:#172536}}h1,h2{{line-height:1.3}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:10px;border-bottom:1px solid #ccd5df;text-align:left}}code{{background:#edf2f6;padding:2px 5px}}a{{color:#0868ae}}.note{{background:#fff5db;padding:16px;border-left:4px solid #b98519}}pre{{white-space:pre-wrap;background:#f2f5f8;padding:16px}}</style>
<h1>AEC 재생 참조·말 끊기 비교</h1><p>2026-10-28 · Jetson Orin 6코어 / XFM USB 마이크 / GeneralPlus USB 스피커</p>
<p>기존 Silero 시작·종료 규칙을 유지하고 실제 스피커의 PulseAudio monitor를 재생 참조로 연결했다.
시작 안내로 장치 내부 지연·이득을 보정하며 AEC3 출력은 전사에, DTLN 잔여 에코 판정은 VAD에 사용한다.
Smart Turn·추가 Reachy 시작 필터·WebRTC VAD는 사용하지 않는다.</p>
<h2>선택 근거와 실패 기록</h2><p>초기 PulseAudio AEC는 이전 녹음 10개에서 모두 자기 목소리를 발화로 판정했다.
이번의 느슨한 잔여 에코 기준도 짧은 두 시험에서는 통과했으나 연속 시험에서 오중단이 재현되어 최종값에서 제외했다.
AEC3 단독과 양자화 모델도 각각 오중단·출력 손상 때문에 제외했다. 최종 후보는 원본 DTLN 128 모델과 더 보수적인 잔여 에코 기준을 사용한다.</p>
<p>기존 방식은 재생 중 입력을 막으므로 오중단이 없지만 사용자가 말해도 중단하지 않는다. 새 방식의 이득은 양방향 입력이며,
자기 목소리 시험과 사람 음성 혼합 시험을 함께 봐야 한다.</p>
<h2>실제 스피커 재생·장애 시험</h2>{service_text}<table><thead><tr><th>기록</th><th>중단</th><th>감쇠 후보</th><th>대기 CPU</th><th>재생 CPU</th><th>AEC 상태 오류</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p>CPU는 STT 시험 프로세스와 AEC 자식 프로세스의 사용량을 장치 전체 6코어 기준으로 환산했다. 전체 로봇 CPU 측정값은 아니다.
기존·개선 방식은 같은 19.5초 음원을 사용했다. 연속 시험은 두 음원을 네 번씩, 4초 무음과 함께 총 219.2초 재생한다.
fault는 재생 중 AEC 자식을 강제 종료한 시험이다. 일부 긴 재생에서 ALSA underrun 경고가 있었으므로 무오류를 보장하지 않는다.</p>
<p>strict_soak의 <code>Empty:</code>는 정상 종료 도중 큐가 닫히면서 기록된 오류다. 이 종료 판정은 후속 수정했으며,
최종 단독 재생과 장애 시험을 다시 수행했다. 초기 원음 복귀 경로의 ALSA 종료 오류도 별도 캡처 프로세스로 격리한 후 재검증했다.</p>
<h2>녹음 음성 혼합 비교</h2><pre>{escape(json.dumps(mixtures, ensure_ascii=False, indent=2))}</pre>
<p>사람 음성 20개를 실제 로봇 에코 녹음에 0 / −6 / −12 dB 비율로 혼합했다. 이는 VAD 검출률이며 전사 정확도가 아니다.
작은 목소리의 누락은 남아 있다.</p>
<h2>ASR → 실제 API → TTS → 말 끊기</h2><ul>{interactions}</ul>
<p class="note">실제 스피커 에코가 들어오는 가상 마이크에 녹음을 주입한 시험이다. 주입→stopped 시간은 실제 사람 발화 지연이 아니며,
XFM 내부 약 0.5초 지연과 사용자 거리·음량은 별도 현장 확인이 필요하다. 긴 겹침 발화에서는 단어 오인식도 관찰됐다.</p>
<p>최종 후보의 첫 대화 시험은 중단 후 새 답변까지 진행했으나 “그만하고 다른 이야기를 해 줘”를 잘못 전사했다.
두 번째 시험은 낮은 보정 신뢰도로 말 끊기 활성화를 거부했다. 따라서 말 끊기 제어의 성공과 대화 내용의 정확성을 구분하며,
Reachy 수준의 전체 대화 품질이나 항상 활성화되는 말 끊기를 입증한 결과는 아니다.</p>
<h2>안전 복귀와 배포</h2><p>모델 해시, 참조 장치, 캡처 시간축, 큐 지연을 확인하고 실패하면 원음이 말 끊기 경로에 들어가기 전에
입력을 닫는다. 이후 기존 재생 중 입력 차단과 1초 잔향 보호로 복귀한다. 보정 신뢰도 부족도 같은 방식으로 처리한다.</p>
<p>별도 설치: <code>/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028</code>.
기존 저장소·설정과 이동·모터·펌웨어를 변경하지 않았다. 자동 부팅 등록 없이 음성 서비스만 운영한다.</p>
<pre>release=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028
"$release/control.sh" status
"$release/control.sh" stop
"$release/control.sh" start   # 보정 AEC
# 기존 방식으로 실행하려면 먼저 stop 후:
"$release/control.sh" legacy</pre>
<h2>자동 검증·재현 자료</h2><p><a href="final_stt_isolated.log">STT 전체 시험</a> ·
<a href="final_tts_bringup.log">TTS·실행 구성 시험</a> · <a href="shutdown_tests.log">AEC 종료·복귀 시험</a> ·
<a href="changes.json">작업 변경 목록</a> · <a href="task_changes.patch">작업 시작 대비 패치</a> ·
<a href="summary.json">수치 JSON</a></p><p>ROS 시험은 별도 domain과 localhost로 격리했다.
초기 domain 혼선이 있었던 통합 시험은 최종 성공 근거에서 제외했다.</p></html>'''
    (ROOT / 'report.html').write_text(body)


if __name__ == '__main__':
    main()
