# 합성 영상의 사람 위치·동작 시각 라벨

현재 3분류 판단·채점 기준은 [합성 낙상 영상 판단·채점 기준](FALL_EVALUATION_CRITERIA_V2.md)에 있다.
판단 근거가 제한적인 사례와 공통 채점 적용은 [r1 개정 기록](FALL_EVALUATION_CRITERIA_V2_R1.md)을 함께 적용한다.
아래 2026-09-09~10 기록은 이전 평가 이력이며, ‘이미 쓰러짐’ 분리·의심 영상 제외 방식은 새 비교에 적용하지 않는다.
새 기준의 평가 코드 반영은 [v2 구현 상태](FALL_EVALUATION_V2_IMPLEMENTATION.md)를 참고한다. 모델 재평가는 아직 진행하지 않았다.
현재 사용할 영상·정답 묶음은 [77개 평가 자료 준비 완료](FALL_EVALUATION_77_READY.md)에 정리했다.

2026-09-09 작성. 기존 41개 영상의 분류는 그대로 두고, **누구를 언제 발견해야
하는지** 검토할 초안을 추가했다. 감지 모델·임계값 변경이나 VLM 호출은 하지 않았다.

## 확정된 것과 적용 범위

- 기존 사용자 검토 분류: 낙상 14개, 낙상 의심 5개, 이미 쓰러진 사람 2개,
  정상 행동 20개. 원본 `vlm_review_v1/labels.json`을 수정하지 않는다.
- 대표 프레임의 박스: `export-v6` 수정본을 사용자가 검토했다. 박스가 없는
  중간 프레임까지 라벨링된 것으로 확대하지 않는다.
- 시간 범위: 동작 시작·몸 첫 접촉·바닥 상태를 처음 확인할 수 있는 시각.
  2026-09-10 사용자의 검토 완료 확인과 평가 진행 요청에 따라 이번 개발 평가에
  사용한다. 원본 초안의 플래그는 이력 보존을 위해 유지하며, 별도 평가 사본에
  사용 근거와 해시를 남긴다. 같은 검수를 다시 요청하는 단계가 아니다.
- RGB 영상을 눈으로 검토한 초안이며 모델 검출 상자를 가져오지 않았다.
  작성자가 이전 모델 결과를 봤으므로 독립적인 블라인드 검수는 아니다.
- 이 41개는 이미 기준 조정에 사용한 개발용 영상이다. 최종 성능 평가용
  미공개 자료로 취급하지 않는다. 같은 장면의 H3/LTX도 독립 표본으로 나누지 않는다.

초안: `evaluations/synthetic_fall_v1/spatial_temporal_draft.json`.
원본 분류와 영상의 SHA-256을 확인한 뒤 검토용 파일을 생성한다.

## 기록 기준

| 항목 | 뜻 |
|---|---|
| `person_id` | 같은 영상 안에서 같은 사람을 구분하는 수동 라벨. 모델 추적 ID와 별개 |
| `target_person_id` | 안전 확인이 필요한 사람. 정상 영상은 `null` |
| `boxes` | `[frame, x1, y1, x2, y2]`, 원본 픽셀 기준, 보이는 신체 범위 |
| `reviewed_frames` | 실제 검토한 프레임 번호. `all`은 모든 프레임을 모은 사진 검토 |
| `spatial_unknown_frames` | 가림 등으로 위치를 확정하지 못한 프레임. 사람 부재가 아님 |
| `onset_frames` | 넘어지거나 의심스러운 하강이 시작된 프레임 범위 |
| `landing_frames` | 몸통·골반이 바닥이나 낮은 받침에 닿는 프레임 범위. 손·발 접촉과 구분 |
| `first_down_frames` | 확인 대상이 바닥에 내려와 있는 것을 처음 볼 수 있는 범위 |
| `first_visible_frames` | 해당 사람이 처음 보이는 프레임 범위 |
| `additional_motion` | 처음부터 누워 있던 사람이 이후 다시 움직인 구간. 새 사건으로 자동 집계하지 않음 |

시간 범위는 0부터 시작하는 프레임 번호이고 양 끝을 포함한다. 검토 화면의
초 단위 값은 `frame / fps`로 변환한다. 합성 영상의 바닥 접촉·의도가 애매하면
단일 시각으로 확정하지 않는다. `null`과 0초는 다르다.

`landing_frames`는 손·무릎을 먼저 짚은 때나 머리까지 완전히 눕는 때가 아니라,
몸통·골반의 **첫 접촉** 기준이다. `first_down_frames`는 누운 상태뿐 아니라
낙상 의심 하강 뒤 바닥에 주저앉은 상태도 포함한다. 정상적으로 앉거나 쉬는
영상에 이 시각을 새 낙상 사건으로 부여하지 않는다. 시간 범위는 해당 경계의
불확실성이지 동작 지속 시간이 아니다.

위치는 주로 0·1·2·3·4·5초에 표시했다. **박스 없는 중간 프레임은 미라벨**이며
사람이 없다는 뜻이 아니다. 자동 보간하거나 마지막 상자를 계속 가져다 쓰지 않는다.
가려진 몸통 전체를 추정한 상자도 만들지 않는다. 따라서 이 상자와 모델의 전신
상자에 단순 IoU 기준을 바로 적용하면 안 된다. 대상 연결 기준은
`evaluations/synthetic_fall_v1/BASELINE_PROTOCOL.md`에 고정했다.
전체 프레임 단위 미탐률·ID 전환율은 계산하지 않는다. 감지 지연은 동작 시작
범위를 반영한 구간으로 계산하고, 이미 누운 경우에는 발견 지연으로 구분한다.

## 먼저 확인할 영상

- **414**: P01은 바닥에 누운 사람, P02는 옆에서 확인하는 사람이다.
  P02만 검출한 것을 P01 발견 성공으로 세지 않는다. 낙상 시작은 영상에 없다.
- **431**: 기존 분류는 낙상 의심으로 유지하되, 첫 장면부터 바닥에 있는 사람은
  확인할 수 있다. 최초 발견 기준은 0초, 이후 상체를 들었다 내리는 동작은 별도 기록이다.
- **208**: 이미 누워 있는 장면부터 시작한다. 낙상 시작을 0초로 넣지 않는다.
- **413**: 이불에 가려 바닥 접촉 시각을 알 수 없다. 후반 위치도 확인 불가로 남긴다.
- **107·207·411·433·441·442**: 시작 동작의 경계나 가림이 애매하다.
  시간 범위를 검토한 뒤에 지연 비교에 사용할지 정한다.

## 검토 파일 생성

저장소 루트에서 실행한다. `DATASET`은 원본 `processed/`와 `vlm_review_v1/`이
있는 로컬 폴더, `OUT`은 아직 없는 결과 폴더다. 원본 파일은 변경하지 않는다.
Python 3.10+, OpenCV, Pillow와 DejaVu Sans 폰트가 필요하다.

```bash
SCRIPT=homecam_agent/scripts/review_fall_annotations.py
LABEL=homecam_agent/evaluations/synthetic_fall_v1/spatial_temporal_draft.json
python3 "$SCRIPT" --dataset "$DATASET" --annotations "$LABEL" --output "$OUT"
```

`--output`을 빼면 검사만 한다. 오류가 있거나 결과 폴더가 이미 있으면 실패하며
기존 검토 결과를 덮어쓰지 않는다. 네트워크·모델·유료 API를 사용하지 않는다.

- `review.html`: 사진과 시각·메모를 함께 보는 단일 파일. 사진을 내장해 오프라인 열기 가능.
- `SYNxxx-labels.jpg`: 영상당 대표 6개 프레임. 주황은 확인 대상, 파랑은 다른 사람/정상 행동.
- `review.csv`: 검토용 시간표. 수정 후 자동 반영·자동 승인 기능은 없다.
- `annotations.json`: 원본 분류, 영상 해시·FPS, 새 초안, 초 단위 시각을 묶은 파일.
- `summary.json`: 초안 건수. 모델 성능 점수가 아니다.

결과 폴더는 0700, 파일은 0600으로 생성한다. 사진이 포함되므로 공개 저장소에 올리지 않는다.
구조 검사는 다음 명령으로 실행한다. 시각 라벨의 정답 여부를 검증하는 테스트는 아니다.

```bash
python3 -m pytest -q homecam_agent/test/test_fall_video_annotations.py
```

## 동작 시각 검토 화면

2026-09-09, 확인 대상 21개 영상의 전체 프레임 사진과 일부 확대 구간을 다시
확인했다. 변경 내용은 `evaluations/synthetic_fall_v1/TIMING_REVIEW_20260909.md`에
남겼다. 상자·사람 ID·기존 영상 분류는 유지했고 모델을 실행하지 않았다.

```bash
SCRIPT=homecam_agent/scripts/review_fall_timing.py
python3 "$SCRIPT" --dataset "$DATASET" --annotations "$LABEL" --output "$OUT"
```

- `review.html`: 원본 RGB를 한 프레임씩 넘기거나 저속 재생하는 오프라인 화면.
  동작 시작·몸 접촉·최초 발견의 범위 양 끝으로 바로 이동할 수 있다.
- `SYNxxx-timing.jpg`: 세 시각의 범위 양 끝을 나란히 비교하는 사진.
  시각을 알 수 없는 칸에는 임의의 사진을 넣지 않는다.
- `timing.csv`: 프레임 범위와 초 단위 표기, 확인 상태, 메모.
- `manifest.json`: 원본 영상·분류·시간 초안의 해시와 검사 결과.

HTML에 모든 프레임을 내장하므로 약 122MB다. 네트워크나 영상 코덱 설치 없이
열 수 있지만 처음 열 때 시간이 걸릴 수 있다. 사진은 원본 해상도를 유지한
JPEG 재인코딩본이며 원본 영상을 수정하지 않는다. 재생은 검토용 프레임
미리보기로, 실제 처리 지연을 측정하는 타이머가 아니다.
기본 한글 폰트가 없으면 `--font`로 설치된 폰트 파일을 지정한다.

```bash
python3 -m pytest -q homecam_agent/test/test_fall_video_annotations.py homecam_agent/test/test_fall_timing_review.py
```

## 현재 기준 측정

2026-09-10, 대상 연결·미라벨 제외·시각 범위 처리 기준을 고정하고 41개 영상을
현재 Pose 모델과 후보 로직으로 다시 실행했다. 기준과 결과는 아래에 있다.

- `evaluations/synthetic_fall_v1/BASELINE_PROTOCOL.md`: 추론 전에 고정한 평가 기준.
- `evaluations/synthetic_fall_v1/BASELINE_20260910.md`: 결과·한계·재현 명령.
- `scripts/replay_fall_baseline.py`: 입력 사본 고정 및 정답을 넣지 않는 RGB 추론.
- `scripts/score_fall_baseline.py`: 박스 연결·사건별 출력·지연 집계와 검토 사진 생성.

감지 코드·임계값·기존 라벨은 이번 측정에서 변경하지 않았다.

## 분류별 집계로 변경 (2026-09-10)

현재 평가기는 보고서 `schema_version=2`, 요약 `grouping_version=2`를 쓴다.
원본 라벨이나 동결한 영상·박스·시각은 바꾸지 않았다.

- `summary.groups.observed_fall`: 낙상 동작이 보이는 영상의 대상 확인 후보.
- `summary.groups.found_down`: 이미 누운 사람을 발견한 영상의 확인 후보.
- `summary.groups.normal_activity`: 정상 영상의 불필요한 확인 후보.
- `summary.groups.suspected_fall`: 후보 유무만 기록. 미탐·정상 성공으로 채점하지 않음.
- `summary.groups.unobservable`: 관찰 불가 건수와 출력 유무만 기록.

기존 21개를 묶은 집계는 `summary.legacy_candidate_bookkeeping`으로 옮겼다.
이는 과거 결과와 비교하기 위한 원본 기대값 집계이며 낙상 감지율이 아니다.
예를 들어 441은 의심 라벨을 유지하고, `observed_fall.missed_cases`에는 들어가지 않는다.

관측 누락 실험에서는 현재 상자를 만들어 넣지 않는다. 마지막 실제 Pose가 있는
프레임과 요청 프레임을 따로 표시한다. 요청은 사건 시작 이후여도 실제 근거가
시작 전이면 성공에 넣지 않는다. 모델이 넘어지는 과정을 봤다는 뜻도 아니다.
조건과 추가 비교의 근거는 `evaluations/synthetic_fall_v1/POSE_GAP_PROTOCOL.md`,
`POSE_GAP_ABLATION.md`에 남겼다.
