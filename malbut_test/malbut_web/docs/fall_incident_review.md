# 낙상 사건 클립·검수

로봇이 만든 낙상 사건에 연속 녹화의 장면 구간을 붙이고, 사용자가 의견을 남기고 처리 완료하는 웹 API다.
로봇의 자동 판정(자세 분석·클라우드 AI)은 여기서 바뀌지 않는다. 마이그레이션: `db/migrations/0012_fall_incident_review.sql`.

## 로봇 → 웹

`POST /api/device/v1/fall-incident-clips` (기기 토큰 + `X-Malbut-Device-Id`, JSON 8 KB 이하)

```json
{"schemaVersion":1,"incidentId":"<uuid>","bootId":"boot-1","segmentIndex":0,"revision":2,
 "startAt":"2026-09-18T00:00:00.000Z","endAt":"2026-09-18T00:00:30.000Z",
 "anchorKinds":["pose_motion","cloud_window"],"foundDown":false,"clockSource":"wall","clockStepped":false}
```

- 영상이 아니라 연속 녹화의 실제 시각 구간이다. KVS 세션 ID는 받지 않고 시각이 겹치는 녹화를 찾는다.
- (기기·사건·구간 번호)당 가장 큰 `revision`만 남긴다. 더 작은 판과 같은 판은 바꾸지 않고 저장 확인을 돌려준다.
- 응답 `{"stored":true,"incidentId","segmentIndex","revision"}` (새로 저장 201, 그 밖 200).
- 사건 이벤트가 아직 없으면 `503`(로봇이 다시 보냄). 같은 판인데 내용이 다르거나 다른 실행(`bootId`)의 사건이면 `409`.
- 클립 등록은 알림을 만들지 않는다.

## 사용자 API (소유자·공유 사용자 모두)

| 경로 | 내용 |
|---|---|
| `GET /api/devices/{id}/fall-incidents?filter=all\|check\|closed\|normal\|report` | 목록 50개. "아무도 확인하지 않음"이 맨 앞 |
| `GET /api/devices/{id}/fall-incidents/{incidentId}` | 사건 + 장면 + 자동 판정 기록 + 알림 이력 + 의견 + 활동 기록 + 같은 장면의 다른 사건 |
| `PUT .../{incidentId}/opinion` `{"label":"fall\|suspected_fall\|normal\|null","memo":"..."}` | 내 의견 하나. `null`이면 해제. 메모 500자 이하 |
| `POST .../{incidentId}/close` `{}` | 처리 완료. 사건이 닫히는 유일한 방법. 의견(누구 것이든)이 하나도 없으면 `409 needs_opinion` |
| `POST /api/devices/{id}/fall-reports` `{"momentAt":"..."}` | 놓친 넘어짐 신고. 그 순간 −10 s ~ +20 s, 알림 없음 |
| `POST .../{incidentId}/clips/{segmentIndex}/playback` | 장면 HLS 재생 주소 (5분) |

변경 요청은 같은 사이트의 JSON 요청만 받는다. 권한이 없으면 사건이 있는지도 알려 주지 않고 `404`.

### 필터

| 필터 | 들어가는 사건 |
|---|---|
| 확인 필요 `check` | 처리 완료 전의 로봇 사건 중 로봇·AI 정상 판정이 아닌 것 (AI 검증 실패 포함). 다시 열린 사건은 정상 판정이어도 포함 |
| 정상으로 확인됨 `normal` | 로봇·AI가 정상으로 끝낸 사건. 처리 완료 전이면 `reviewPending`(검수 전), 알림·재발신 없음 |
| 처리 완료 `closed` | 사람이 처리 완료한 사건 |
| 사용자 신고 `report` | 놓친 넘어짐 신고 |

### 장면 상태 `playbackState`

`preparing`(구간 끝난 지 15초 안) · `available` · `partial`(녹화가 구간 일부만 덮음) · `unavailable`(녹화 없음) ·
`expired`(7일 지남). 영상이 없어도 사건 기록은 남는다.

## 의견·처리 완료·다시 열기

- 의견은 기록만 하고 사건 상태와 자동 판정을 바꾸지 않는다. 의견을 남기면 "확인함"으로 보고 모두의 재발신을 멈춘다.
- 처리 완료할 때 남아 있던 의견 라벨을 기억한다. 그 뒤 **그 라벨들에 없던 라벨**이 달리면 다시 열고,
  "확인 필요" 단계 알림을 전원에게 보낸다(3분 뒤 1회 [재발신]). 같은 라벨이나 의견 해제는 기록만 한다.

## [재발신]

| 단계 | 간격 | 총 횟수(첫 알림 포함) |
|---|---|---|
| 긴급 | 2분 | 3 |
| 확인 필요 | 3분 | 2 |
| 일반 | — | 1 |

- 첫 알림을 서버가 받은 시각부터 센다. 본문 앞에 `[재발신]`, 단계는 올리지 않는다.
- 로봇이 더 높은 단계 알림을 보내면 낮은 단계 재발신을 멈추고 높은 단계로 새로 센다.
- 마지막 재발신 뒤 한 간격이 더 지나도 의견이 없으면 `unacknowledged`("아무도 확인하지 않음").
- 처리 완료하면 남은 재발신을 모두 취소한다.
- `POST /api/internal/maintenance`가 매분 예약·전송한다(EventBridge 1분). 실제 발송 시각은 최대 1분 늦을 수 있다.

## 배포 시 확인

- push broker Lambda를 웹보다 **먼저** 배포해야 한다(`infra/aws/push-broker/fall-notification.mjs`의 [재발신]·다시 열림 형식).
  옛 broker는 `resend`와 `reopened_by_opinion`을 거부해 재발신·다시 열림 알림이 계속 실패한다.
- 알림을 누르면 아직 실시간 보기로 간다. 사건 화면이 생기면(PR-5) 사건으로 바꾼다.
