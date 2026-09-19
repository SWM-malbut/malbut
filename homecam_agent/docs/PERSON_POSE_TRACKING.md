# 사람별 Pose 추적 — 2단계

## 이번에 바꾼 범위

일반 사람 검출과 무관하게 실행하는 homecam YOLO26n-pose ONNX 경로에서,
최상위 한 명만 남기던 처리를 여러 사람의 관측 목록으로 바꿨다.
다른 사람이 더 높은 점수를 받아도 기존 사람의 ID와 자세 이력을 유지한다.
이 ID는 영상 안에서 잠시 이어지는 추적 번호이지, 얼굴 인식이나 영구적인 사용자 ID가 아니다.
공유 `malbut_yolo`의 `.pt` ROS 파이프라인을 바꾼 작업은 아니다.

```text
RGB 1프레임 → Pose 추론 1회 → 여러 후보 → 사람별 ID 연결
                                          ├ 목록: /homecam/person_poses
                                          └ 호환용 1명: /homecam/person_pose
```

박스 위치·겹침·관절 위치로 이전 관측과 연결한다. 양쪽에서 가장 잘 맞고 다른
연결보다 충분히 구별되는 경우만 ID를 이어 간다. 연결이 애매한 후보는
`unassigned`에도 남긴다. 후보를 없애거나 다른 사람의 이력에 강제로 붙이지 않는다.
거의 같은 박스(IoU 0.85 이상)는 중복으로 보고 높은 점수 하나만 남긴다.
실제 두 사람이 거의 완전히 겹친 경우까지 완벽히 구분하는 방식은 아니다.

## 설정

| ROS 파라미터 | 기본값 | 의미 |
| --- | --- | --- |
| `pose_confidence_threshold` | 0.45 | 기존 검출 기준. 단일 출력은 이 기준 유지 |
| `pose_candidate_confidence_threshold` | 0.10 | 새 목록에 남길 후보 하한. 실험값이며 오검출도 늘어남 |
| `pose_inference_fps` | 5.0 | 기존 추론 빈도 상한 |
| `pose_track_max_gap_sec` | 1.0 | 마지막 관측 이후 ID를 유지할 최대 간격 |
| `pose_track_min_observations` | 3 | 약한 후보의 연속 관측 횟수. 신뢰도 자체를 올리지는 않음 |
| `pose_track_max_people` | 32 | 유지할 ID 수 상한. 초과 후보는 `unassigned`에 이유와 함께 남김 |

파라미터 변경에는 노드 재시작이 필요하다. 기본값은 실제 로봇에서 확정한 수치가 아니다.
추적 이력은 ID당 실제 관측 30개까지만 메모리에 보관한다. 간격 계산은 로컬
monotonic 수신 시각을 사용한다. 촬영 시각은 출력의 `captureStamp`로 전달한다.
카메라 촬영 시각 중복은 새 관측으로 세지 않으며, 시각 역행·카메라 frame ID·해상도
변경 때 이전 추적 이력을 지운다. 촬영 시각이 정상적으로 증가하는 카메라가 필요하다.

## 목록 출력

`/homecam/person_poses`: `std_msgs/String` JSON, 기존 센서 데이터 QoS 사용.
추론 대상으로 선택한 프레임마다 내보낸다. 이미지 자체나 원본 depth는 포함하지 않는다.

| 항목 | 타입 | 의미 |
| --- | --- | --- |
| `schemaVersion` | integer | 현재 `1` |
| `status` | string | `ok`, `duplicate_frame`, `model_unavailable`, `inference_error`, `disabled`, `waiting_frame` |
| `captureStamp` | object 또는 null | 원본 ROS 촬영 시각 `{sec, nanosec}`. 상태 초기화 시 null |
| `frameId` | string 또는 null | 원본 이미지 frame ID |
| `persons` | array | 유지 중인 ID 목록. 현재 관측이 없는 ID도 포함 |
| `unassigned` | array | `{pose, reason}`. 연결 보류 또는 수용 한도 초과 후보 |
| `expiredTrackIds` | string array | 유지 시간 초과·초기화로 제거한 ID |

`persons` 원소:

| 항목 | 타입 | 의미 |
| --- | --- | --- |
| `trackId` | string | 실행별 접두사와 증가 번호. OFF/만료 후에도 같은 ID 재사용 안 함 |
| `state` | string | `tentative`, `tracked`, `missing`, `ambiguous` |
| `observed` | boolean | 이번 프레임에서 ID와 연결된 실제 관측이 있는지 |
| `confidenceLevel` | string 또는 null | 현재 관측이 기준 이상이면 `strong`, 미만이면 `weak`, 없으면 null |
| `observationCount` | integer | 해당 ID에 연결한 총 실제 관측 수 |
| `consecutiveObservations` | integer | 최근 연속 관측 수. 놓친 프레임에서는 0 |
| `lastSeenAgeSec` | number | 마지막 실제 관측 후 경과 시간 |
| `pose` | object 또는 null | 기존 정규화 박스·17개 관절 형식. 관측이 없으면 null |
| `depthEvidence` | object | 해당 사람 박스로 계산한 기존 depth 요약. 현재 관측이 없으면 usable=false |

`tentative`는 아직 짧게만 관측한 약한 후보다. `tracked`는 강한 관측이 있었거나
약한 후보가 연속 관측된 상태다. **둘 다 사람이 확실하다는 뜻이나 낙상 판정이 아니다.**
`unassigned.reason`은 `association_ambiguous` 또는 `track_capacity`다.
관측이 사라지거나 ID가 만료됐다는 이유로 낙상 사건을 정상 종료해서는 안 된다.
도움을 주러 온 사람의 답변·자세를 누운 사람의 정보와 합쳐서도 안 된다.

모니터링 OFF 시 목록·이력을 비우고 `disabled`를 발행한다. 첫 유효 privacy 상태
수신 전에는 추론하지 않는다. 이미지가 아예 안 들어오면 새 목록도 안 나오므로,
소비자는 마지막 메시지를 영구적인 현재 상태로 쓰지 말고 수신 중단을 별도로 감시해야 한다.

## 확인한 범위와 남은 문제

2026-09-09 로컬 합성 영상 41개, 기존과 같은 1,106개 표본 프레임으로 확인했다.
실제 ONNX 모델과 수정된 이미지 콜백을 실행했고, ROS publisher·depth·일반 검출은
테스트 대역을 사용했다. 모델/영상/소스 SHA256과 프레임별 출력을 별도 결과에 남겼다.

- 414: ID 2개를 유지했고 27프레임 중 18프레임에서 두 후보를 출력했다.
  3.42초에는 앉은 사람 0.70, 누운 사람 0.38을 각각 보존했다.
  관측 누락 구간이 있으며 마지막에는 누운 사람 박스가 상반신 쪽으로 축소된다.
  따라서 `18/27`은 올바른 다인원 추적률이 아니라 두 후보가 출력된 횟수다.
- 431: 후보 하한을 0.10으로 낮춰도 관측이 없었다. 추적기는 모델이 못 찾은
  사람을 복원하지 못한다.
- 107·411: 배경 물체·화면 가장자리에도 약한 후보가 붙는다. 계속 보인다는
  이유만으로 올바른 사람 검출이라고 볼 수 없다.
- 연결 보류·짧은 검출 누락·ID 변경도 발생한다. 저상 시점의 가림, 급격한 자세
  변화, 사람 교차, 로봇 회전에 대한 실제 대상 ID 정확도는 아직 검증하지 않았다.

별도 JPG는 실제 출력의 박스·관절·ID를 겹쳐 표시한 것이다. 이 데이터에는
대상자별 프레임 정답과 낙상 시작 시각이 없으므로 ID-switch 비율이나 낙상
미탐·오탐·지연 수치로 해석할 수 없다. 고정된 합성 시점 결과를 Jetson의
움직이는 Aurora 카메라 성능으로 일반화하지 않는다.

후속 3단계에서 사람별 이력을 사용하는 [낙상 의심 판단](FALL_CANDIDATES.md)을
추가했다. 기존 단일 자세 토픽 소비자는 그대로 한 명만 받는다. VLM 호출·질문·
알림·로봇 제어는 아직 연결하지 않았다.

3단계 v2 보완에서도 모델·입력 방식·후보 하한·추적기는 유지했다. 비율을 유지한
입력도 비교했지만 414의 누운 사람을 놓치는 등 퇴행이 있어 적용하지 않았다.
412의 후보 추가 및 431의 미검출 잔여 사항은 위 낙상 의심 판단 문서에 기록했다.
