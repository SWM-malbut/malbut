from pathlib import Path
import json,re,statistics,html
r=Path(__file__).resolve().parent
read=lambda name:json.loads((r/name).read_text()) if (r/name).exists() else None
refs={x['id']:x['text'] for x in read('human_manifest.json')['cases']}
def normalize(s):return re.sub(r'[^\w]','',s).replace('_','').lower()
def dist(a,b):
 d=list(range(len(b)+1))
 for i,c in enumerate(a,1):
  z=[i]
  for j,e in enumerate(b,1):z.append(min(z[-1]+1,d[j]+1,d[j-1]+(c!=e)))
  d=z
 return d[-1]
metrics=[]
for name,label in [('capture1_local_asr.json','기존: 1초 보존·로컬 Whisper'),('capture2_local_asr.json','현재 적용: 2초 보존·로컬 Whisper'),('capture2_api_asr.json','실험: 2초 보존·AEC3 음성·API'),('human_raw_api_asr.json','실험: 정렬 원음·API'),('human_linear_api_asr.json','미채택: 선형 AEC·API')]:
 data=read(name)
 if data is None:continue
 for level in ['normal','quiet']:
  rows=[x for x in data if level in x['file'] and not x['file'].startswith('105_003_0478')]
  pairs=[(normalize(refs[x['file'].rsplit('_',1)[0]]),normalize(x.get('text',''))) for x in rows]
  metrics.append(dict(label=label,file=name,level=level,count=len(rows),cer=sum(dist(a,b) for a,b in pairs)/sum(len(a) for a,b in pairs),exact=sum(a==b for a,b in pairs),mean_wall_s=statistics.mean(x['wall_s'] for x in rows)))
evaluation=dict(scope='Frozen measured robot echo plus public human audio; not an acoustic live-human result',excluded_clip='105_003_0478: supplied transcript does not cover all speech in recording; excluded from both sides of CER',normalization='Remove whitespace/punctuation; keep number/unit spelling differences',metrics=metrics)
(r/'asr_evaluation.json').write_text(json.dumps(evaluation,ensure_ascii=False,indent=2))
escape=lambda x:html.escape(str(x))
def table(headers,rows):return '<table><thead><tr>'+''.join('<th>'+escape(x)+'</th>' for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+escape(x)+'</td>' for x in row)+'</tr>' for row in rows)+'</tbody></table>'
parts=['''<!doctype html><html lang="ko"><meta charset="utf-8"><title>음성 상호작용 비교</title><style>body{max-width:1100px;margin:48px auto;padding:0 24px;font:16px/1.65 system-ui;color:#17212b;background:#fafbfc}table{border-collapse:collapse;width:100%;margin:24px 0;background:white}td,th{border:1px solid #d8dfe5;padding:10px;text-align:left;vertical-align:top}th{background:#eaf0f5}.note{padding:18px;background:#fff4d6}code{background:#edf0f2}a{color:#125f9e}</style><h1>음성 상호작용 비교 · 2026-10-28</h1>''',
'<p class="note"><b>사람의 말 끊기 문제는 아직 해결 완료가 아닙니다.</b> 녹음 혼합 시험의 전사 개선과 실제 로봇의 사람 음성 검출 실패를 분리해 기록합니다. 합성·주입 시험을 실제 사람의 성공률로 제시하지 않습니다.</p>',
'<h2>구현과 적용 범위</h2><ul><li>기존 Silero·보수적인 AEC 판정을 유지하고, 끼어들기 확인 전 음성 보존을 1초에서 2초로 늘렸습니다. 추가 대기 시간이 아닙니다.</li><li>선택적인 API 전사, 원음 보존, 로컬 오류 복귀를 구현했습니다. 일반 호출과 발화는 로컬 인식이며 API 실험은 기본 비활성입니다.</li><li>준비 완료 후 한 번 인사하고 호출어 없이 답을 받습니다. 끊긴 답변의 재생 상태를 다음 대화에 전달하고, 화제·놀이 요청에는 첫 내용을 직접 제시합니다.</li><li>선형 AEC의 내부 출력과 잔여 신호 전사를 비교했지만 실제 혼합 음성 왜곡이 남아 네이티브 패치는 채택하지 않았습니다.</li></ul>',
'<h2>동일 녹음 조건의 글자 오류율</h2><p>20개 사람 녹음 × 보통/작은 음량. 기준 전사가 녹음 전체를 설명하지 않는 한 파일을 양쪽 CER에서 제외해 음량별 19개를 비교했습니다. 공백·문장부호를 제외하고 숫자 표기 차이는 포함합니다. 기존 실제 스피커 에코를 보존한 고정 재생이며, 실제 사람과 실시간으로 대화한 시험이 아닙니다. 원음/선형 비교는 고정 1초 여유 구간, capture 비교는 관측 시작점과 1초/2초 보존을 반영한 근사 창입니다.</p>',
 table(['방식','음량','문장 수','글자 오류율','완전 일치'],[[m['label'],m['level'],m['count'],f"{m['cer']*100:.2f}%",f"{m['exact']}/{m['count']}"] for m in metrics]),
 '<p><a href="asr_evaluation.json">집계 JSON</a> · <a href="human_manifest.json">입력과 검출 시점</a></p>',
 '<h2>실제 로봇 시험</h2><p>스피커 재생과 마이크 경로를 사용하는 폐루프 시험에서는 합성 음성을 놓치거나 첫 단어를 잘못 인식하는 실패가 있었습니다. 원음 API 시험은 “저는 이거 다른 이야기를 해줘”로 인식해 주제 전환은 됐으나 정답 문장과 다릅니다. 선형 AEC는 사람 녹음 주입에서도 첫 구절을 더 왜곡해 제외했습니다.</p>']
for phase in ['before','after']:
 data=read('live_'+phase+'.json')
 if data:
  rows=[]
  for x in data['trials']:
   played=any(s['pid']==x['playback_id'] and s['state']=='playing' for s in data['states'])
   stopped=any(s['pid']==x['playback_id'] and s['state']=='stopped' for s in data['states'])
   rows.append([x['index'],x['expected'],'재생됨' if played else '재생 전 취소·시험 무효','중단' if stopped and played else '중단 없음',' / '.join(t['text'] for t in x.get('transcripts',[])) or '전사 없음'])
  parts += ['<h3>실제 사용자: '+escape(phase)+'</h3>',table(['회차','요청 문장','재생','중단','인식 결과'],rows),'<p><a href="live_'+phase+'.json">원시 이벤트</a></p>']
retest=read('retest_1236/result.json')
if retest:
 parts+=['<h2>조건을 맞춘 재시험</h2><p>동일한 Reachy 감지·2초 보존·선택형 API 인식 설정에서 스피커 음량만 80%/50%로 바꿨습니다. 각 음량에서 무발화 대조 1회와 사람 발화 요청 2회를 실행했습니다. 최초 보정 실패는 제외했습니다. 사용자가 안내는 들었지만 발화 타이밍을 놓쳤다고 확인했습니다. 아래 human_80/human_50은 실제 발화 미검출률 계산에서 제외합니다.</p>',table(['시험','유효 재생','중단','인식 문장'],[[x['phase'],sum(t['playing'] for t in x['trials']),sum(t['stopped'] for t in x['trials']),' / '.join(a['text'] for t in x['trials'] for a in t['transcripts']) or '없음'] for x in retest['observations']]),'<p>앞선 50% 시험의 “한번 정리해봐”는 사용자가 실제 발화로 확인했습니다. 네 번의 human_80/human_50에서는 사용자가 발화 타이밍을 놓쳤으므로 미검출률에 포함하지 않습니다. 이후 단일 시험은 사용자가 발화했다고 확인했습니다. <a href="retest_1236/result.json">재시험 집계</a>. 종료 후 80%·로컬 인식으로 복원했습니다.</p>']
if retest and retest.get('confirmed_human_trial'):
 parts+=['<h3>발화 확인을 받은 50% 단일 시험</h3><p>사용자가 로봇 재생 중 준비 문장을 끝까지 말했다고 확인했습니다. 재생은 30.72초간 끝까지 진행됐고, speech_started·중단·전사 API 호출은 모두 없었습니다. 확정된 미검출 1회입니다. 원음의 별도 API 전사는 주로 로봇 안내를 반환했고, 에코 처리 음성도 정답 문장을 복원하지 못했습니다. 이 결과만으로 마이크 입력 문제와 과도한 에코 억제를 구분하지는 못합니다.</p>']
parts+=['<h2>인사와 대화</h2><p>인사 1회 → 호출어 없는 사용자 요청 → 실제 API 답변 → 후속 대기 → 무발화 종료를 로봇에서 확인했습니다. 인사·답변에 자기 목소리로 인한 중단은 없었습니다. <a href="greeting_result.json">재생 기록</a></p>']
a=read('dialogue_before.json');b=read('dialogue_after.json')
if a and b:parts+=[table(['요청','기존 응답','변경 후 응답'],[[x['input'],x.get('response',''),y.get('response','')] for x,y in zip(a,b)]),'<p>8개의 합성 요청에 대한 실제 API 예시입니다. 보편적인 품질 우위나 Reachy와 동등한 수준을 입증하지는 않습니다. 일상 사례에는 사용자가 말하지 않은 상황을 가정한 답변도 남았습니다.</p>']
duck=read('duck_probe/duckhuman_50.json')
if duck:
 parts+=['<h2>후보 감지 후 음량 감소 실험</h2><p>사용자가 1~2m에서 재생 중 요청 문장을 말했다고 확인했습니다. 재생은 멈췄지만 전사는 “입니다. 세대 저희마다 주변에.”로, 사람의 요청을 복원하지 못했습니다. 실제 발화 시작 시각이 없어 이 중단이 사람 때문인지도 확정하지 않습니다. 무발화 대조 80%·50%에서는 완전 중단이 없었지만 임시 음량 감소가 발생했습니다. 새 후보 방식은 일반 서비스에 적용하지 않았습니다.</p><p><a href="duck_probe/duckhuman_50.json">사용자 확인을 포함한 기록</a></p>']
parts+=['<h2>마이크 내부 처리 확인</h2><p>USB의 ADB 진단 경로로 마이크 내부를 읽었습니다. 8채널·16 kHz 입력을 mic6_circle 엔진이 처리하고, 로봇에는 1채널·16 kHz 오디오를 전달합니다. 내부 에코 처리와 방향성 수음 설정이 존재하지만, 이것이 사람 음성 누락의 원인인지는 아직 확인되지 않았습니다. 원음 저장 진단만 5초간 켠 뒤 기존 설정과 원음 파일을 복원했습니다. 처리 알고리즘·수음 방향·펌웨어 변경은 남기지 않았습니다. 첫 조용한 상태 녹음은 사용자가 말하지 못했다고 확인해 사람 인식 평가에서 제외했습니다.</p><p><a href="microphone_frontend/findings.json">확인 사항과 미확인 사항</a></p>']
quiet=read('retest_1236/quiet_retry90_label.json')
if quiet:
 parts+=['<h3>조용한 상태의 실제 사용자 녹음</h3><p>두 번째 90초 녹음에서는 사용자가 같은 1~2m 위치에서 두 번 말했다고 확인했습니다. 로봇은 녹음 동안 재생하지 않았습니다. API는 “겨울 이야기만 한 문장”을 두 번 반환했습니다. 로컬 Whisper는 첫 구간을 부정확하게 인식했고 두 번째 구간은 일반적인 영상 끝인사로 잘못 인식했습니다. 사용자는 두 번 모두 “잠깐, 겨울 이야기만 한 문장으로 해줘”를 끝까지 말했다고 확인했습니다. 이 녹음은 호스트의 AEC·발화 구간 수집 이전의 USB 마이크 입력이므로, 이 조용한 상태의 누락은 호스트의 TTS 차단이나 AEC만으로 설명할 수 없습니다. 마이크 내부 처리와 인식기 오류는 아직 분리하지 못했습니다.</p><p><a href="retest_1236/quiet_retry90_asr.json">API 결과</a> · <a href="retest_1236/quiet_local_asr.json">로컬 결과</a></p>']
parts+=['<h3>처리 전·후 동시 녹음과 마이크 입력 포화</h3><p>90초 동시 녹음에 사용자는 전체 문장을 한 번 말했다고 확인했습니다. 정확한 발화 시점은 독립적으로 확인하지 못했습니다. 원음 채널0 전체 구간 API와 원음에서 고른 두 후보 구간의 6방향 처리 결과 모두 요청 문장을 복원하지 못했습니다. 따라서 방향 변경으로 해결됐다고 볼 수 없습니다. 설정 파일을 직접 전송하면 펌웨어가 쓰기 도중 기본값을 읽는 문제가 발견되어, 같은 파일시스템에서 완성된 파일을 원자적으로 교체하도록 시험 도구를 고쳤습니다. 원래 circle 엔진과 설정은 실행 로그로 복원을 확인했습니다.</p><p>별도의 동일 스피커 재생 대조에서는 진단 저장을 껐을 때와 켰을 때 모두 USB 출력이 있었습니다. 진단 모드가 출력을 통째로 끈다는 가설은 이 대조에서 지지되지 않았습니다. 다만 진단 모드가 모든 음성을 동일하게 처리한다는 증거는 아닙니다.</p>',table(['마이크 증폭','6채널 중 최대 포화 비율','최대 피크'],[['30dB','1.4823%','0dBFS'],['15dB','0.00291%','약0dBFS'],['5dB','0%','−1.30dBFS']]),'<p>80% 스피커로 같은 음성을 재생한 전체 녹음 구간의 수치입니다. 5dB에서는 측정된 포화가 사라졌지만 실제 사람의 작은 목소리 인식은 아직 검증하지 않았습니다. 원래30dB를 복원했으며 기본 설정에 적용하지 않았습니다. 비교용 Piper CLI 원본 자체의 전사도 부정확했으므로, 이 제어 음원의 전사 오류를 전부 마이크 탓으로 돌리거나 일반 앱의 TTS 품질로 일반화하지 않습니다. <a href="microphone_frontend/gain5_control/comparison.json">입력 증폭 비교</a> · <a href="microphone_frontend/paired_human_2/metadata.json">사용자 확인과 녹음 조건</a></p>']
refined=read('dialogue_refined_v2.json')
if refined:
 parts+=['<h2>짧은 응답과 화제 전환 보완</h2>',table(['요청','실제 API 응답'],[[x['input'],x.get('response','')] for x in refined]),'<p>여섯 요청의 확인 사례입니다. 관계·장면을 임의로 덧붙이지 않고, 이야기를 요청하면 실제 내용을 시작하도록 보완했습니다. 일부 답변은 여전히 기본 한두 문장 지침보다 깁니다. 한 번의 예시로 전반적인 품질이나 Reachy와의 동등성을 판정하지 않습니다.</p>']
tts=read('tts_quality/comparison.json')
if tts:
 parts+=['<h2>실제 TTS 합성 경로 비교</h2><p>같은 Jetson에서 같은 다섯 문장을 앱의 Piper 및 기존 API TTS 구현으로 합성했습니다. 실제 스피커로는 재생하지 않았습니다. Piper와 한국어 전처리 파일은 실행 중인 릴리스와 해시가 같고, 이 다섯 문장의 전처리 결과는 입력과 같습니다. API TTS 구현은 작업 전부터 존재했으며 이번에는 비교만 수행했습니다.</p>',table(['방식','첫 음성 조각 평균','첫 요청 제외 CPU 시간/문장','API 재전사 완전 일치'],[[x['backend'],f"{x['mean_first_chunk_s']:.2f}초",f"{x['warm_mean_cpu_s']:.3f} CPU초",f"{x['exact']}/{x['n']}"] for x in tts['rows']]),'<p>API 방식은 로봇 CPU 사용이 작지만 첫 음성 대기는 늘었고, 이 표본의 재전사 정확도도 우세하지 않아 기본 음성은 바꾸지 않았습니다. 재전사는 발음 보존의 제한적인 대리 지표이며 사람의 자연스러움 평가가 아닙니다. API 합성과 전사는 같은 공급자이며, 전사 요청에 정답 문장을 제공하지 않았습니다. 합성 프로세스 CPU 수치를 서비스 전체 CPU나 실제 말 끊기 성능으로 일반화하지 않습니다. <a href="tts_quality/comparison.json">문장·합성·전사 결과</a></p>']
threads=read('tts_quality/threads_comparison.json')
if threads:
 parts+=['<h3>Piper 합성 CPU 조정</h3>',table(['설정','문장 수','합계 CPU 시간','첫 음성 조각 평균'],[[x['backend'],x['count'],f"{x['total_cpu_s']:.2f} CPU초",f"{x['mean_first_chunk_s']:.3f}초"] for x in threads['rows']]),'<p>동일한 난수 조건으로 다섯 문장씩 세 번 합성했습니다. 양쪽 음원 길이는 모두 같고 16비트 PCM의 최대 차이는4단위였습니다. 3스레드가 합성 CPU 시간을15.8% 줄였고 첫 음성은 평균78ms, 표본 중 최대264ms 늦었습니다. 현재 interaction 릴리스에3스레드를 적용했으며 기존 기준 릴리스의6스레드는 보존했습니다. 이 설정 차이는 아래 유휴 서비스 CPU 비교 이후 적용됐으며, 유휴 수치를 합성 중 측정으로 제시하지 않습니다. <a href="tts_quality/threads_comparison.json">동일 음원 스레드 비교</a></p>']
cpus=[read(x) for x in ['cpu_same_db/before.json','cpu_same_db/after.json']];cpus=[x for x in cpus if x]
parts+=['<h2>CPU와 검증</h2>',table(['동일 DB에서의 관측','시간','6 CPU 전체 기준 CPU','메모리'],[[('기존 음성 서비스' if i==0 else '현재 음성 서비스'),f"{x['elapsed_s']:.1f}초",f"{x['cpu_percent_total']:.2f}%",f"{x['memory_mb']:.1f} MB"] for i,x in enumerate(cpus)]),'<p>음성 서비스 전체 cgroup CPU를 6개 CPU 용량으로 나눴습니다. 같은 437턴 대화 DB를 각각 사본으로 만들어 기존 서비스와 현재 서비스를 측정했습니다. 두 서비스 모두 AEC 준비를 확인하고 10초 안정화 뒤 30초를 측정했으며, 그 구간에 사용자 발화 이벤트가 없었습니다. CPU는 기존 11.93% → 현재 7.65%로 4.28%p 감소했습니다. 주요 개선은 대화 이력 복구가 매초 전체 이력을 다시 읽는 문제를 고친 것입니다. 변경 없는 DB는 재검색하지 않으며 새 기록·외부 변경·만료 작업은 계속 복구합니다. 예전 6.57% 관측은 빈 DB를 사용해 직접 비교할 수 없습니다. 측정은 각각 30초의 유휴 구간이며 실제 발화 중 부하는 별도입니다. 같은 측정에서 메모리는 689.9MB → 795.4MB로 약 105.5MB 늘었습니다. 이 한 번의 관측만으로 증가 원인을 특정하지는 않습니다. <a href="cpu_same_db/comparison.json">동일 조건 CPU 비교 기록</a>.</p><p>Story 관련 232개 테스트·71개 하위 검사 통과. 이번 응답 지침 관련 검사 29 통과·ROS 의존 1 건너뜀 이후, 분리한 ROS 환경에서 인사 검사 3개도 통과했습니다. STT 전체 1241 통과·4 건너뜀은 앞선 단계 결과이며 네이티브 실험 제거 후 관련 26 통과. Agent 전체는 4143 통과·36 건너뜀·1 모의 객체 실패 후 관련 5개를 수정 확인했습니다. 실제 전사 API 대기와 로컬 Whisper 시간은 장비·네트워크 차이가 있어 직접 비교하지 않습니다.</p>',
'<h2>출처와 재현</h2><p>Reachy 기준 소스: <a href="https://github.com/pollen-robotics/reachy_mini_conversation_app/blob/b731ea1fd6ce53232f1b13609331271358e10bd9/src/reachy_mini_conversation_app/huggingface_realtime.py">실시간 음성</a>, <a href="https://github.com/pollen-robotics/reachy_mini_conversation_app/blob/b731ea1fd6ce53232f1b13609331271358e10bd9/src/reachy_mini_conversation_app/prompts.py">인사 지침</a>. 현지 Silero로 재생을 줄이고 서버 발화 이벤트에서 중단하는 구성은 현재 로봇의 마이크·AEC와 동일하지 않습니다. Reachy의 idle 동작은 곧바로 능동 발화 기능을 뜻하지 않습니다.</p><p><a href="changes.patch">이번 변경 diff</a> · <a href="changes.json">파일 목록</a> · <a href="work_status.json">현재 적용 상태</a></p></html>']
(r/'report.html').write_text('\n'.join(parts))
print('report.html and asr_evaluation.json written')
