"""Build the auditable local comparison report; does not invoke a model or device."""
import ast
import difflib
import hashlib
import html
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / 'src/malbut'
BEFORE = ROOT / 'before'


def read(name):
    return json.loads((ROOT / name).read_text())


def write(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


old, new = read('before.json'), read('after.json')
assert len(old['cases']) == len(new['cases']) == 69
leads, restores, table = [], 0, []
for a, b in zip(old['cases'], new['cases']):
    assert a['id'] == b['id'] and a['speech'] == b['speech']
    for field in ('onsets', 'turns'):
        assert a['result'][field] == b['result'][field], (b['id'], field)
    duck, case_leads = None, []
    for stamp, command in b['result']['controls']:
        if command == 'duck':
            duck = stamp
        elif command == 'unduck':
            duck = None
            restores += 1
        elif command == 'pause' and duck is not None:
            case_leads.append(round((stamp - duck) * 1000))
            duck = None
    leads.extend(case_leads)
    if not b['speech']:
        assert not b['result']['onsets']
        assert not any(c == 'duck' for _, c in b['result']['controls'])
    table.append({'id': b['id'], 'speech': b['speech'], 'onsets': b['result']['onsets'],
                  'identical_capture': True, 'duck_lead_ms': case_leads})
assert len(leads) == 60 and set(leads) == {140}

tests, passed_keys = [], set()
for name, label, expected in (
    ('malbut_stt', 'STT 전체', 1196), ('malbut_tts', 'TTS 전체', 424),
    ('malbut_agent_server', 'Agent 전체', 3962), ('bringup', '음성 배포·launch', 80),
    ('ros_speech', 'ROS Manager·대화·수신 대상', 30),
    ('ros_camera', 'ROS 합성 이미지 → JPEG → 모의 응답', 1),
    ('tts_timing', '추가 TTS 지연 로그', 1),
):
    root = ET.parse(ROOT / (name + '.xml')).getroot()
    assert not list(root.iter('failure')) and not list(root.iter('error')), name
    cases = list(root.iter('testcase'))
    passed = [c for c in cases if c.find('skipped') is None]
    keys = {(c.get('classname'), c.get('name')) for c in passed}
    assert len(passed) == len(keys) == expected and not keys & passed_keys, name
    passed_keys.update(keys)
    tests.append({'name': label, 'passed': len(passed), 'skipped': len(cases)-len(passed),
                  'artifact': name + '.xml'})

manifest = read('before_manifest.json')
for name, digest in manifest.items():
    assert sha(BEFORE / name) == digest, name
changed = []
for file in BEFORE.rglob('*'):
    if not file.is_file() or any(p.startswith('.') for p in file.relative_to(BEFORE).parts):
        continue
    rel = file.relative_to(BEFORE).as_posix()
    assert (SOURCE / rel).exists(), rel
    if file.read_bytes() != (SOURCE / rel).read_bytes():
        changed.append(rel)
new_files = [
    'malbut_agent_server/malbut_agent_server/camera_view.py',
    'malbut_agent_server/malbut_agent_server/information_queries.py',
    'malbut_agent_server/malbut_agent_server/speech_metrics.py',
    'malbut_agent_server/malbut_agent_server/data/robot_guide.json',
    'malbut_agent_server/test/test_reachy_features.py',
    'malbut_agent_server/test/test_camera_view_ros.py',
    'malbut_stt/test/test_barge_ducking.py',
]
assert all(not (BEFORE / name).exists() for name in new_files)
changed = sorted(changed + new_files)
runtime = [name for name in changed if '/test/' not in name]
patch, integrity = [], []
for name in changed:
    path, before = SOURCE / name, BEFORE / name
    current = path.read_text()
    previous = before.read_text() if before.exists() else ''
    patch.extend(difflib.unified_diff(previous.splitlines(True), current.splitlines(True),
        fromfile='a/' + name if before.exists() else '/dev/null', tofile='b/' + name))
    if path.suffix == '.py':
        ast.parse(current, filename=str(path))
    row = {'path': name, 'before_sha256': sha(before) if before.exists() else None,
           'after_sha256': sha(path)}
    if name in runtime:
        mirror = SOURCE / 'malbut_test' / name
        expected = current
        if name == 'malbut_stt/malbut_stt/node.py':
            expected = expected.replace("vad_backend='silero', input_has_aec=input_has_aec,\n",
                "vad_backend='silero', input_has_aec=False,\n", 1).replace(
                '            input_has_aec=input_has_aec,\n', '            input_has_aec=False,\n', 1)
        assert mirror.read_text() == expected, name
        row['mirror_sha256'] = sha(mirror)
    integrity.append(row)
write('changed_files.json', changed)
write('changed_runtime_files.json', runtime)
write('implementation_manifest.json', integrity)
(ROOT / 'implementation.patch').write_text(''.join(patch))
check = subprocess.run(['git', '-C', str(SOURCE), 'diff', '--check', '--',
    *changed, *['malbut_test/' + name for name in runtime]], text=True, capture_output=True)
baseline_whitespace = []
if check.returncode:
    # git HEAD also includes the user's earlier uncommitted changes. Verify
    # these exact existing lines instead of silently rewriting unrelated work.
    baseline_stt = (BEFORE / 'malbut_stt/malbut_stt/dialogue_pipeline.py').read_text()
    for line in ('        self.recorder_factory = recorder_factory  ',
                 '        self._raw_playback_gate = False  '):
        assert line + '\n' in baseline_stt
        baseline_whitespace.append(line)
    added = [line[1:] for line in check.stdout.splitlines() if line.startswith('+')]
    assert len(added) == 4 and set(added) == set(baseline_whitespace), check.stdout
patch_check = subprocess.run(['git', '-C', str(BEFORE), 'apply', '--check',
    '--whitespace=error-all', str(ROOT / 'implementation.patch')], text=True, capture_output=True)
assert patch_check.returncode == 0, patch_check.stdout + patch_check.stderr
write('source_validation.json', {'ast_parse': 'passed', 'incremental_patch_check': 'passed',
    'git_head_diff_existing_whitespace': check.stdout.splitlines() if check.returncode else [],
    'baseline_snapshot_hashes_verified': len(manifest), 'changed_main_files': len(changed),
    'runtime_mirrors_verified': len(runtime), 'deployment_aec_override_preserved': True,
    'patch_base': 'before/ snapshot of the already-dirty workspace; not git HEAD',
    'mirror_patch_policy': 'same runtime changes; retain the existing STT input_has_aec=False override'})
for name in ('stt', 'agent'):
    (ROOT / (name + '.after.yaml')).write_bytes(
        (Path.home() / '.config/malbut' / (name + '.yaml')).read_bytes())

decisions = [
    ('후보 발화 음량 감쇠', '추가', '기존 Silero 후보만 사용. 진폭 25%, 거짓 후보·pause 시 복원, 300 ms 자동 해제. AEC 입력에서만 동작.'),
    ('응답 지연 계측', '추가', '같은 request ID로 STT·Agent·TTS 시각 연결. 중간 안내와 중복 재생 제외, 누락 값은 null.'),
    ('현재 카메라 질의', '추가', '1초 이내 프레임 한 장, 기존 managed key, 모의 모델 검증. 실시간 영상·장면 인식 품질은 미검증.'),
    ('로컬 설명서 검색', '추가', '검토된 8개 사용 안내 검색. 개인 파일·웹 검색이나 로봇 동작 실행 없음.'),
    ('Silero VAD / 발화 시작·종료', '기존 유지', 'turn_detection=legacy. 160 ms 시작 확인, 기존 문장 끝 조기 종료와 2초 무음 종료 유지.'),
    ('Smart Turn / Reachy 추가 시작 필터 / WebRTC', '비활성 유지', '앞선 비교 후 복원한 설정 유지. 이번 변경은 추가 모델을 로드하지 않음.'),
    ('개인 기억·말투·답변 길이·듣기 중심 설정', '기존 유지', '현재 개인 기억 동의·조회·정정·삭제와 대화 설정을 활용. 중복 시스템 추가 없음.'),
    ('날씨·로봇 도구와 기능 확장', '기존 유지·확장', '기존 도구 계약에 카메라·설명서 조회 추가. 물리 동작은 기존 Manager 경로 유지.'),
    ('발화 수정·취소·중복 안내 억제', '기존 유지', '기존 revision/취소 검증과 재생 중복 억제 활용.'),
    ('needs_response=False 일괄 적용', '보류', '현재 STT는 최종 답변 완료를 기다림. 무조건 생략하면 대화가 재개되지 않을 수 있음.'),
    ('최종 전사 전 답변 선계산', '보류', '현재 실행 경로가 대화·기억을 기록하고 동작을 요청할 수 있음. 부작용 없는 임시 실행 경로와 실제 API 비교가 선행되어야 함.'),
    ('웹 검색', '보류', '로컬 문서 검색부터 적용. 현재 음성 흐름에 모델 지원·출처 전달 정책을 추가한 뒤 별도 검증 필요.'),
    ('한국어 입력·음성 출력 변경', '기존 유지', '현재 한국어 STT·프롬프트·TTS 유지. 새 다국어 엔진을 도입하지 않음.'),
    ('머리·안테나·표정·춤·대기 모션', '미적용', '말벗의 하드웨어·제어 경로와 맞지 않아 동작 코드를 이식하지 않음.'),
]
summary = {'date': '2026-10-28', 'scope': new['scope'], 'audio': {
    'conditions': 69, 'speech_conditions': 60, 'non_speech_conditions': 9,
    'identical_onsets_endpoints_pcm_hashes': 69, 'qualified_candidate_duck_lead_ms': 140,
    'qualified_candidate_lead_samples': 60, 'candidate_restore_commands': restores,
    'non_speech_onsets': 0, 'non_speech_ducks': 0},
    'queries_before': old['queries'], 'queries_after': new['queries'],
    'tests': tests, 'distinct_passed_tests': len(passed_keys),
    'decisions': [{'feature': f, 'decision': d, 'reason': r} for f, d, r in decisions],
    'deployment': {'source_and_mirror_updated': True, 'live_robot_restarted': False,
        'remote_deployment': False, 'api_calls': 0, 'keys_created': 0,
        'turn_detection': 'legacy', 'barge_in_ducking_config': True,
        'ducking_requires_aec': True, 'deployment_mirror_forces_aec_false': True,
        'camera_view_enabled_local_config': True},
    'camera_codec': read('camera_codec.json'), 'cases': table}
write('summary.json', summary)


def rows(items):
    return ''.join('<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in item)
                   + '</tr>' for item in items)


report = '''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reachy 기법 → 말벗 로컬 비교 · 2026-10-28</title>
<style>body{font:16px/1.7 system-ui,sans-serif;color:#18303d;background:#f2f6f7;margin:0}
main{max-width:1120px;margin:40px auto;background:white;padding:36px;border-radius:16px}
h1{font-size:30px;line-height:1.3}h2{font-size:21px;margin-top:36px}a{color:#05637f}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:10px;border-bottom:1px solid #dbe4e8;text-align:left;vertical-align:top}
th{background:#eef4f6}code,pre{font:13px/1.6 monospace;background:#eef4f6;padding:3px 5px}
pre{white-space:pre-wrap;padding:16px}.lead{font-size:18px}.note{border-left:4px solid #168199;padding:12px 18px;background:#eef7f9}
.stats{display:flex;flex-wrap:wrap;gap:16px}.stat{padding:18px;background:#eef4f6;flex:1;min-width:170px}.stat b{display:block;font-size:27px}
@media(max-width:700px){main{margin:0;padding:20px;border-radius:0}table{font-size:12px}td,th{padding:7px}}</style>
<main><p>말벗 · 로컬 비교 및 구현 기록 · 2026-10-28</p>
<h1>Silero는 유지하고 대화 보조 기능 4개를 추가</h1>
<p class="lead">후보 음량 감쇠, 응답 지연 로그, 현재 카메라 질의, 로컬 사용 설명서 검색을 소스와 배포용 미러에 반영했습니다.</p>
<div class="stats"><div class="stat"><b>69 / 69</b>발화 시작·종료·수집 PCM 동일</div>
<div class="stat"><b>140 ms</b>시작 확정 전 감쇠 요청 · 음성 60조건</div>
<div class="stat"><b>5,694</b>중복 제외 테스트 통과</div>
<div class="stat"><b>0</b>실제 API 호출 · 신규 키 생성</div></div>
<p class="note">140 ms는 녹음 재생에서 측정한 <strong>감쇠 제어 요청의 선행 시간</strong>입니다. 실제 스피커 반응, ASR 정확도, 전체 응답 속도가 개선됐다는 측정값은 아닙니다. 현재 배포 미러는 기존 정책대로 AEC를 false로 고정하므로 감쇠가 작동하지 않습니다.</p>
<h2>기존과 적용 후</h2><table><thead><tr><th>항목</th><th>기존</th><th>적용 후 / 검증</th></tr></thead><tbody>
<tr><td>끼어들기 후보</td><td>160 ms 시작 확정에서 pause</td><td>동일한 확정·pause. 앞선 후보에서 진폭 0.25 요청, 후보 해제 시 복원. 추가 VAD 없음.</td></tr>
<tr><td>음성·비음성 재생</td><td>20 원본 + 800/1600 ms 멈춤 변형 + 9 비음성</td><td>69조건 모두 onset·endpoint·PCM SHA 일치. 비음성 9조건에서 onset/duck 모두 0. 후보 복원 13회.</td></tr>
<tr><td>카메라 질문</td><td>현재 화면 조회 도구 미제공</td><td>실제 orchestration과 합성 ROS Image→JPEG 경로 통과. 모델 응답만 모의 처리.</td></tr>
<tr><td>사용법 질문</td><td>설치된 안내 검색 도구 미제공</td><td>검토한 8개 안내에서 검색. 한국어 질문 6종과 실제 orchestration 검증.</td></tr>
<tr><td>지연 관측</td><td>단계별 로그를 수동 해석</td><td>동일 request ID로 종료 대기·전사 대기·Agent·첫 오디오·장치 시작 구분.</td></tr>
</tbody></table>
<h2>후보별 결정</h2><table><thead><tr><th>기능</th><th>결정</th><th>근거·적용 범위</th></tr></thead><tbody>DECISIONS</tbody></table>
<h2>검증 결과</h2><table><thead><tr><th>범위</th><th>통과</th><th>해당 실행에서 건너뜀</th></tr></thead><tbody>TESTS</tbody></table>
<p>하위 subtest 횟수와 이미 포함된 AEC 미러 재검사 2건은 총합에서 제외했습니다. ROS 31건은 별도 격리 도메인에서 가상 상대 노드·합성 영상으로 실행했습니다. 건너뛴 항목은 해당 환경의 ROS·장치 의존 항목이며, 저장소 전체 또는 실제 로봇 검증을 의미하지 않습니다.</p>
<p>초기 통합 수집은 테스트의 형제 모듈 import 방식과 cv2 의존성 때문에 실패했습니다. 환경을 갖추고 패키지별 기본 import 방식으로 재실행한 최종 결과만 집계했습니다. 기존 도구 allowlist fixture, mock provider의 불필요한 patrol import, mock ROS node 설정 경로도 수정 후 통과했습니다. 초기 실행 기록은 regression.log/xml에 남아 있습니다.</p>
<h2>설정과 사용</h2>
<p><code>~/.config/malbut/stt.yaml</code>은 <code>turn_detection: legacy</code>, <code>barge_in_ducking: true</code>입니다. Smart Turn·Reachy 추가 시작 필터·WebRTC는 켜지 않았습니다. 감쇠에는 별도로 실제 AEC 입력이 필요하며 현재 배포 미러의 기존 비활성 정책을 유지합니다.</p>
<p><code>~/.config/malbut/agent.yaml</code>에는 <code>camera_view_enabled: true</code>와 <code>/depth_cam/rgb0/image_raw</code>를 지정했습니다. 두 launch 경로의 설정 로딩을 확인했습니다. 실제 카메라 답변은 다음 정상 실행에서 기존 관리 키·이미지 입력·지원 모델이 연결되어야 동작합니다. 이번에는 로봇을 재시작하거나 원격 배포하지 않았습니다.</p>
<p>사용 예: “지금 앞에 뭐가 있어?”, “순찰은 어떻게 사용해?” 카메라는 최신 한 장만 메모리에 유지하고 JPEG를 디스크에 저장하지 않습니다. 카메라 분석에는 현재 질문·사진만 보내며 도구 호출과 기억 저장 제안을 받지 않습니다. 질문과 텍스트 답변에는 기존 대화 저장·기억 정책이 적용됩니다.</p>
<pre>python -m malbut_agent_server.speech_metrics /absolute/path/to/combined.log</pre>
<p>같은 호스트의 monotonic 시계만 비교합니다. playback은 실제 소리가 들린 시점이 아닌 장치 스트림 시작입니다. endpoint는 VAD 무음 길이로 역산한 발화 끝을 사용하며, predecode가 겹치므로 단계 시간을 합산하지 않습니다.</p>
<h2>비교 방법과 한계</h2>
<p>이전 비교의 manifest와 SHA를 검증한 29개 원본 파일로 69조건을 재생했습니다. 16 kHz 변환, 20 ms 프레임, 512 ms 앞 무음, 6초 뒤 무음, 발화 60% 지점에 0.8/1.6초 멈춤을 삽입했습니다. 고정 Silero v6.2.3 ONNX를 실제 CPU로 실행했습니다. ASR와 문장 끝 조기 종료를 실행하지 않은 VAD/capture 회귀 비교입니다. AEC와 재생 중 상태는 가정했고 제어 응답은 녹음 재생에서 모의화했습니다. 실제 상태 변경은 별도 PCM callback·ROS 서비스 시험으로 검증했습니다.</p>
<p>카메라·설명서 도구 선택은 고정된 provider로 강제한 계약 검증입니다. 실제 언어 모델의 도구 선택률·장면 인식 정확도·유료 API 지연·마이크 에코 성능은 측정하지 않았습니다. 답변 선계산의 효과도 주장하지 않습니다.</p>
<h2>원자료와 재현</h2>
<p><a href="summary.json">요약 JSON</a> · <a href="before.json">기존 비교 결과</a> · <a href="after.json">적용 후 비교 결과</a> · <a href="compare.py">재생 비교 코드</a> · <a href="implementation.patch">이번 작업 패치</a> · <a href="implementation_manifest.json">변경 파일 SHA</a> · <a href="source_validation.json">소스·미러 검증</a></p>
<p>패치는 작업 시작 시 이미 수정되어 있던 소스의 <code>before/</code> 스냅샷을 기준으로 합니다. git HEAD와의 전체 차이가 아니며, 기존 사용자 변경을 포함하도록 되돌리지 않습니다.</p>
<pre>cd /home/shin/ros2_ws
PYTHONDONTWRITEBYTECODE=1 reachy_fixed_vs_malbut_20261027/venv/bin/python reachy_features_20261028/compare.py before
PYTHONDONTWRITEBYTECODE=1 reachy_fixed_vs_malbut_20261027/venv/bin/python reachy_features_20261028/compare.py after
python3 reachy_features_20261028/build_report.py</pre>
<details><summary>69조건별 결과</summary><table><thead><tr><th>조건</th><th>음성</th><th>시작/종료/PCM</th><th>감쇠 선행 ms</th></tr></thead><tbody>CASES</tbody></table></details>
<h2>참고 구현</h2><p><a href="https://github.com/pollen-robotics/reachy_mini_conversation_app">Pollen Robotics Reachy Mini Conversation App</a>의 로컬 Silero 감쇠·카메라·도구 구조,
<a href="https://github.com/huggingface/speech-to-speech/blob/main/docs/response-latency.md">Hugging Face speech-to-speech 지연 측정</a>과
<a href="https://github.com/huggingface/speech-to-speech/blob/main/src/speech_to_speech/pipeline/speculative_turns.py">선계산 구현</a>을 비교했습니다. 그대로 복사한 것이 아니라 말벗의 기존 전사·Manager·키 관리 구조에 맞춘 구현입니다.</p></main></html>'''
report = report.replace('DECISIONS', rows(decisions)).replace('TESTS', rows(
    [(t['name'], t['passed'], t['skipped']) for t in tests])).replace('CASES', rows(
    [(c['id'], '예' if c['speech'] else '아니오', '동일', c['duck_lead_ms']) for c in table]))
(ROOT / 'report.html').write_text(report)
print(json.dumps({'report': str(ROOT / 'report.html'), 'passed_tests': len(passed_keys),
                  'changed_files': len(changed), 'runtime_mirrors': len(runtime)}))
