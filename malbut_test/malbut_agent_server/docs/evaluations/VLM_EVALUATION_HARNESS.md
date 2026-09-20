# 홈캠 VLM 평가 하네스

낙상 기능의 두 감지 경로, 능동 시야 재확인, 안전한 실패 정책과 첫 모델 후보는
[`FALL_DETECTION_VLM_REQUIREMENTS.md`](FALL_DETECTION_VLM_REQUIREMENTS.md)를
기준으로 한다.

이 하네스는 모델 API 호출과 채점을 분리한다. 각 공급자 어댑터는 같은
프롬프트·JSON Schema로 모델을 호출해 prediction JSONL을 만들고,
`malbut-vlm-eval`은 네트워크나 API 자격 증명 없이 고정 manifest와
prediction을 채점한다. 이 구조는 같은 테스트셋을 다시 업로드하지 않고도
채점 코드를 검증하고, 공급자 장애와 채점 오류를 구분하기 위한 것이다.

현재 구현 범위는 다음과 같다.

- 버전 1·2 ground-truth manifest와 prediction envelope 검증
- 고정 한국어 프롬프트와 strict JSON Schema, 프롬프트 SHA-256
- `confirmed_fall`, `found_down`, `normal_activity`, `unobservable` 분리
- Nova 2 Lite, Gemini, Qwen, OpenAI-compatible 로컬 서버 어댑터
- C0/C1/C2 컨텍스트 격리와 후보 prediction 생성 명령
- Aurora 930 정렬 depth의 로컬 물리 근거 요약
- 낙상 Recall/FPR과 Wilson 95% 신뢰구간
- 대상 존재 P/R/F1, 사람·반려동물 개수 정확도
- 사건 유형별 tIoU 0.3 기반 1:1 매칭과 P/R/F1
- 시작·종료 오차, 자세·위험도 혼동행렬, quadratic weighted kappa
- 낙상 confidence Brier score와 ECE
- 로봇 이동 오탐, 화면·반사 오탐, 층별 낙상 Recall
- 요청 성공·스키마 준수·지연 p50/p90/p95/p99·반복 flip rate
- 실제 usage와 날짜가 고정된 가격표를 사용한 비용 계산
- 비가중 hard gate와 동일 프로토콜 내부 Pareto frontier
- 원문 영상 경로와 모델 설명을 제외한 `0600` 비공개 JSON 보고서

KVS에서 MP4를 내보내는 작업, 주석 도구, 실물 영상 수집은 이 하네스와
별도 단계다. 평가 결과를 운영 이벤트 DB에 쓰는 VLM worker도 아직 이
명령의 책임이 아니다.

## 평가 파일

manifest의 한 줄은 한 클립이다. 필수 판정 정보는 `case_id`, `clip`,
`robot_motion`, `subjects_present`, `counts`, `events`, `posture_end`,
`risk_gt`다. `traffic_class`를 생략하면 사건과 이동 상태에서 유도하지만,
필드 파일럿에서는 명시하는 것을 권장한다.

`traffic_class`는 운영 오경보 추정을 위한 서로 배타적인 값이다.

- `fall`
- `found_down` (운영 오경보 분포에는 포함하지 않는 별도 안전 사건)
- `hard_negative`
- `no_event`
- `robot_motion_only`
- `screen_or_reflection`
- `other_non_fall`

prediction의 한 줄은 모델 호출 한 번이다. 동일한 모델 설정의 레코드는
`model`, `input.track`, `input.context_variant`, `input.prompt_version`,
`input.prompt_sha256`가 모두 같아야 한다. `repetition`은 1부터 연속이어야
한다. `telemetry`에는 지연, 토큰, 캐시 토큰, 재시도 횟수만 기록하며 API
키를 넣지 않는다.

공통 출력 형식은
[`vlm_eval_prompt.py`](../../malbut_agent_server/vlm_eval_prompt.py)의
`PREDICTION_JSON_SCHEMA`다. 구조적으로 맞는 JSON이라도 사건 시간이 클립
밖에 있거나, confidence가 0~1 범위를 벗어나거나,
`fall.assessment=confirmed_fall`과 fall event가 서로 다르면 semantic
invalid로 처리한다. 현재 계약에서는
`fall.assessment=confirmed_fall`만 `fall` event를 동반한다. 하강 과정이
없고 쓰러진 사람만 발견한 경우는 `found_down`, 판정 불가능한 경우는
`unobservable`로 보존한다.

## 후보 호출

모델 입력에 정답이 섞이지 않도록 ground-truth manifest와 로컬 관측
sidecar를 분리한다. sidecar에는 `candidate_kind`(`event_candidate`,
`observed_fall_candidate`, `found_down_candidate`), 하강·바닥 근접 점수,
가시성, 로봇 이동, YOLO/Pose 요약과 선택적인 Aurora depth 요약만 둔다.
원본 depth 프레임은 공급자에게 전송하거나 prediction 파일에 저장하지 않는다.
sidecar v2의 `source`에는 감지기 이름·버전, 설정 SHA-256, 원본 rosbag 또는
캡처 artifact SHA-256을 반드시 기록한다. 관측 sidecar는 정답 라벨을 보기
전에 감지기에서 생성·봉인해야 하며, 사람이 GT를 보고 값을 작성한 결과는
C1/C2 평가에 사용할 수 없다. 각 prediction은 실제 관측 계약의 SHA-256에
바인딩된다.

샘플 sidecar 형식은
`malbut_agent_server/data/vlm_eval_observations_sample.jsonl`에서 확인한다.
실행 전 입력만 검증하려면 다음 명령을 사용한다.

```bash
MALBUT_VLM_PROVIDER=nova \
PYTHONPATH=. python3 -m malbut_agent_server.vlm_inference_runner \
  --manifest malbut_agent_server/data/vlm_eval_pilot_sample.jsonl \
  --observations malbut_agent_server/data/vlm_eval_observations_sample.jsonl
```

실제 영상이 준비되면 `--execute`와 비공개 출력 경로를 명시한다. 이 옵션이
있을 때만 네트워크·과금 호출을 수행하고, 영상 파일 존재와 SHA-256을
검증한다.

```bash
MALBUT_VLM_PROVIDER=nova \
PYTHONPATH=. python3 -m malbut_agent_server.vlm_inference_runner \
  --manifest /secure/eval/manifest.jsonl \
  --observations /secure/eval/observations.jsonl \
  --output /secure/eval/nova-2-lite.jsonl \
  --context-variant C2 \
  --repetitions 3 \
  --execute
```

후보 교체는 코드를 고치지 않고 환경 설정으로 수행한다.

- `nova`: `MALBUT_VLM_MODEL`, `MALBUT_VLM_REGION`, AWS 자격 증명
- `gemini`: `MALBUT_VLM_MODEL`, `MALBUT_VLM_API_KEY`
- `qwen`: `MALBUT_VLM_MODEL`, `MALBUT_VLM_API_KEY`
- `openai_compatible`: `MALBUT_VLM_MODEL`, `MALBUT_VLM_BASE_URL`; 로컬
  vLLM 등 인증 없는 loopback endpoint는 API 키를 비워 둔다.

Nova 어댑터의 SDK는 선택 의존성으로 설치한다.

```bash
python3 -m pip install -e '.[vlm-nova]'
```

서로 다른 후보의 공정한 비교를 위해 `--sampling`, `--effective-fps`,
`--resolution`, `--audio-included`, `--preprocessing-sha256`를 실제 전처리와
일치시켜 기록한다.

## 실행

패키지 루트에서 자격 증명 없이 예제를 검증한다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.vlm_eval_runner \
  --manifest malbut_agent_server/data/vlm_eval_pilot_sample.jsonl \
  --predictions malbut_agent_server/data/vlm_eval_predictions_sample.jsonl \
  --prices malbut_agent_server/data/vlm_eval_prices_sample.json \
  --gates malbut_agent_server/data/vlm_eval_gates_sample.json \
  --traffic-profile malbut_agent_server/data/vlm_eval_traffic_sample.json \
  --output /tmp/malbut-vlm-eval.json
```

실제 평가에서는 `--require-media`를 추가한다. 그러면 manifest 기준 상대
경로에 영상이 실제로 있는지 확인한다. 이 모드에서는 `clip.sha256`이
필수이며 파일 해시도 항상 검증한다.

```bash
malbut-vlm-eval \
  --manifest /secure/eval/manifest.jsonl \
  --predictions /secure/eval/nova-2-lite.jsonl \
  --prices /secure/eval/prices-2026-09-02.json \
  --output /secure/eval/reports/nova-2-lite.json \
  --require-media
```

종료 코드는 다음 의미다.

- `0`: 평가 입력이 완전하고 설정한 gate 통과
- `2`: 평가 실행 자체가 불완전함(예: 기대한 prediction 행 누락)
- `3`: hard gate 실패
- `4`: gate 계산에 필요한 지표가 없어 보류

요청 실패율과 schema/semantic invalid 비율은 모델 품질 지표다. 이 값이
제품 허용치를 넘으면 gate 파일로 실패시켜야 하며, 하네스 오류 코드 2와
혼동하지 않는다.

## 비용과 선택 규칙

단가는 반드시 `as_of`, 공식 `source`, `currency`를 함께 기록한다. USD가
아닌 단가를 USD로 비교하려면 그 날짜의 `usd_per_currency_unit`을 가격표에
명시해야 한다. 환율이 없으면 해당 통화 비용만 계산하고 USD Pareto 축에서
제외한다.

기본 Pareto 축은 전체 낙상 Recall의 Wilson 하한, 필드 분포로 환산한
카메라-일당 예상 오경보, p95 지연, 일 50클립 기준 카메라당 월 USD
비용이다. 트랙(V/F/A), 컨텍스트
(C0/C1/C2), 프롬프트 버전·해시가 같은 결과끼리만 비교한다. 가중 합산
점수는 만들지 않는다.

현재 VLM 하네스는 공급자 호출을 자동 재시도하지 않으므로 `retry_count=0`이다.
향후 재시도를 추가한다면 실패 호출에서 청구된 토큰까지 합산해 `telemetry`에
넣어야 한다. 샘플 gate 수치는 하네스 동작 확인용이지 제품 승인 기준이
아니다. 실제
게이트는 파일럿 데이터와 오경보 예산을 정한 뒤 별도 파일로 승인해야 한다.

실제 호출 prediction JSONL은 각 호출 직후 `fsync`하는 행 단위 저널이다.
중단되면 이미 완료된 과금 호출은 파일에 남는다. 출력 경로가 이미 존재하면
덮어쓰지 않으므로, 부분 파일을 감사·보관한 뒤 미완료 case만 별도 실행한다.

## 개인정보와 재현성

보고서는 로컬 경로, 영상, 한국어 설명·근거, 원시 요청·응답을 저장하지
않는다. case ID별 성공 여부만 남긴다. 출력은 원자적으로 교체되고 파일
권한은 `0600`이다. 보고서의 `dataset_contract_sha256`은 경로를 제외한
정답 계약에 바인딩되며, 실제 영상은 manifest의 `clip.sha256`으로
별도 봉인한다.

## Aurora 930 실기 적용 경계

`homecam_detector`는 RGB에 정렬된 depth image와 그 depth CameraInfo가
동시에 설정된 경우에만 포즈 키포인트별 거리를 계산한다. RGB/depth timestamp
차가 설정 한계를 넘거나 정렬 여부가 확인되지 않으면 해당 근거는 실패 폐쇄로
버린다. 장착 높이 기본값은 URDF 기준 약 0.091864 m이지만 실물 장착 높이와
pitch를 재측정해 덮어써야 한다. 실제 ROS 토픽 이름도 Aurora 드라이버에서
확인하기 전에는 기본값으로 고정하지 않는다.
