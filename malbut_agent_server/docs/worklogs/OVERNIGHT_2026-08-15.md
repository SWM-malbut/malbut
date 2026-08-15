# Overnight worklog — 2026-08-15

## 목표

- 임시 로컬 STT를 실제 Agent speech 계약 앞에 연결한다.
- 실제 장치 동작 없이 one-shot 음성 대화 demo를 실행 가능하게 만든다.
- 합성 음성과 승인된 OpenAI API 평가로 정확도·안전·지연을 반복 검증한다.
- 원문 음성·transcript·assistant text·credential이 배포 증거에 남지 않게 한다.

## 구현

- `local_stt.py`
  - PCM16 WAV strict validation과 동일 FD 기반 private snapshot
  - `faster-whisper` lazy backend, CPU `int8`, `base`/`small`
  - `arecord` one-shot capture, 1~30초 정수 제한, mode 0700/0600 임시 파일
  - typed·content-free 오류, cleanup 확인, raw audio/path 비전달
  - `SpeechTranscriptEvent` final-only 변환과 explicit capture provenance
- `local_voice_demo.py`
  - `malbut-voice-demo --microphone`
  - 기본 `small`, confidence `0.60`, Tool 없음, robot state untrusted
  - Mock 기본, explicit mode-0600 env 파일을 쓸 때만 OpenAI 허용
  - fallback 없음, retry 0, timeout 15/20초, token usage와 실제 provider 검증
  - HTTP(S)/ALL proxy 차단, stdout control/framing 문자 차단
- `eval_runner.py`
  - Agent Server Python 소스 26개 전체를 상대경로·개별 SHA-256으로 manifest화
  - provider 호출 전후 manifest exact equality 검사
  - 실행 중 소스가 바뀌면 report를 만들지 않고 fail closed
- console entry point, optional STT extra, ROS package metadata, README/Jira 문서와
  배포 evidence 목록을 갱신했다.

## 검증 기록

### 오프라인 회귀

- `pytest`: **740 passed**, failure·error·skip 0
- `flake8`, `pydocstyle`, `compileall`, `git diff --check`: 통과
- `colcon build --symlink-install --packages-select malbut_agent_server`: 성공
- package-scoped `colcon test-result`: **740 tests, 0 errors, 0 failures,
  0 skipped**
- workspace 전체 `colcon test-result`에는 이 작업과 무관한 기존
  `malbut_gazebo` lint failure 1건과 skip 1건이 남아 있어 package 결과와
  분리해 기록했다.

### 합성 STT

- 12개 한국어 문장 × 2 voice = 24개 합성 WAV
- `base`: exact 10/24, micro CER 11.69%, STT median 842.4ms
- `small`: exact 14/24, micro CER 5.65%, STT median 2,534.1ms
- `small` confidence 0.60: 21/24 수용, 수용 중 exact 13/21,
  conditional CER 5.09%
- 실제 마이크는 사용하지 않았다. 합성 source audio와 로컬 raw 평가 파일은
  최종 집계 후 삭제했고 잔존 여부를 다시 확인했다.

### 현재 소스 결속 live API

모든 run은 Tool 실행 권한 0, `store=false`, strict schema, reasoning `none`이며
runtime manifest는
`88feb3fa7eed8559d5857a6766764198b6da03c170c2a44d43e66510df7c03ec`,
26/26 component, `source_unchanged_during_run=true`다.

| profile | suite pass | schema | formal 위반 | provider p95 | 비용 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Terra × 3, 5초 | 90/90 | 90/90 | 0 | 2,115.390ms | $0.3622320 |
| Luna × 3, 15초 | 84/90 | 90/90 | 0 | 3,208.615ms | $0.0360744 |
| Terra × 10, 15초 | 300/300 | 300/300 | 0 | 2,312.176ms | $1.2086240 |

- current-source subtotal: **$1.6069304**
- 이전 단계와 source-unbound 재실행을 포함한 누적 known spend:
  **$3.6508592**
- timeout으로 usage가 불완전한 초기 run, 합성 TTS와 full-path smoke 비용은
  subtotal에서 제외했으며 0으로 간주하지 않았다.

## 실패와 판단 근거

1. 실제 OpenAI TTS WAV의 streaming RIFF size sentinel이 기존 WAV reader에서
   과도한 duration으로 해석됐다. 실제 EOF 기준 PCM 범위를 검증하도록 고쳤다.
2. 첫 네 full-suite artifact의 runtime hash가 현재 source와 일치하지 않았다.
   hash를 덮어쓰지 않고 `SOURCE_UNBOUND` 역사 자료로 분리한 뒤, package-wide
   start/end manifest를 추가하고 유료 평가 480회를 다시 실행했다.
3. Luna current run은 84/90이었다. 6건은 K01·K11·K19·K25에 집중됐고
   schema 및 formal safety gate는 통과했지만 quality success로 올리지 않았다.
4. 실제 microphone readiness는 사용자 발화와 privacy consent가 필요한
   외부 조건이므로 자동 실행하지 않았다.

## 남은 문제

- 실제 마이크의 조용한 환경·생활 소음·원거리·speaker self-echo corpus
- VAD, streaming/partial transcript, wake word
- 실제 speaker TTS/outbox와 barge-in delivery
- speaker identity와 multi-process durable speech lease
- Tool 또는 trusted robot state를 연결하기 전의 명시적 confirmation 정책
- model snapshot revision과 transitive dependency lock/hash 고정

현재 판정은 **임시 local-only STT와 non-actuating one-shot 대화 demo의 기술
검증 완료**이며, 실제 음성 제품 E2E·ROS·장치 release는 보류다.

## SWM25-78 room-live offline milestone

사용자 목표인 `wake → STT → Agent 판단 → 명시적 확인 → 거실 coverage →
결과 뒤 다음 대화`를 실제 장치 없이 먼저 검증할 수 있도록 다음 경계를
추가했다.

- `monitor_room(location)` 고수준 Tool과 closed whole-utterance intent grammar
- 기본 빈 `monitorable_locations`와 canonical forbidden-room alias 비교
- injected wake/STT/output을 반복하는 `ContinuousVoiceSession`
- immutable, non-authorizing `ToolConfirmationRequest`
- explicit goal·coverage viewpoint만 받는 `SemanticRoomResolver`
- server-resolved `MissionAuthority`, verifier-issued `TrustedConfirmation`, opaque
  proposal handle과 owner-bound confirm/deny/execute/cancel/feedback
- controller-instance-local single active lease, monotonic expiry와 bounded
  simulation adapter
- `RoomLiveScenarioCoordinator` scripted integration

통합 회귀는 확인 전 adapter 0회, 확인 digest 변조 차단, valid simulation의
preflight→navigate→coverage→live-ready 네 phase, replay 추가 호출 0회,
`simulation_succeeded`·`physical_effects=false`·`viewer_live=false`, 확인 대기와
mission 실행 중 wake 소비 0회, terminal 뒤에만 다음 wake 처리를 검증한다. 실제
microphone, ROS, Nav2, camera, Homecam/KVS, browser 또는 OpenAI API는 이
milestone에서 호출하지 않았다.

이 구현은 physical controller가 아니다. durable restart-safe ledger, 실제
person confirmation, battery·forbidden-room·recording/P2P·device identity를 포함한
preflight, 실물 map coverage plan, Nav2/Homecam/browser adapter와 trusted
same-conversation terminal feedback이 연결되기 전에는 실제 이동·촬영·사이트
생중계를 완료로 판단하지 않는다.

최종 freeze에서 lifecycle 집중 회귀 100개, Agent Server 전체 916개와 동일한
916개를 실행한 `colcon test`가 모두 통과했다. flake8, pydocstyle, compileall,
`git diff --check`와 package build도 통과했다. 이 수치는 offline 계약 검증이며
실물 E2E 합격 수치가 아니다.
