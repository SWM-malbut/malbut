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
| `POST /api/devices/{id}/fall-reports` `{"momentAt":"...","memo":"..."}` | 놓친 넘어짐 신고. 그 순간 −10 s ~ +20 s, 알림 없음. 메모 500자 이하(선택) |
| `GET /api/devices/{id}/fall-timeline?from=…&to=…` | 연속 녹화 화면의 하루(26시간 이하): 녹화된 구간과 사건 위치 |
| `POST /api/devices/{id}/recording-playback` `{"startAt","endAt"}` | 순간을 고르기 위한 녹화 재생(10분 이하, 그 로봇 녹화가 있는 시간만). 영상 0초 = `alignedStartAt` |
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

## 화면 (PR-5)

검토한 목업(사건 목록·사건 상세·연속 녹화·홈캠 설정) 그대로 만든다. 글꼴은 앱 글꼴을 쓴다.
- 사건 탭: 목록(가장 먼저 확인할 사건 / 오늘 / 어제 / 지난 기록) → 상세. 처리 완료는 의견이 하나 있어야 누를 수 있다.
- 연속 녹화: 사건 목록의 "연속 녹화 보기"(놓친 넘어짐 신고) 또는 사건의 "AI에게 다시 검토 받기"(의심 시점으로 채워짐).
  영상을 멈추거나 옮기면 그 시각이 "넘어지기 시작한 순간"이 된다. 원래 사건 구간 밖이면 새 신고를 제안한다.
- 로컬 데모: `NEXT_PUBLIC_HOMECAM_UI_DEMO=1 npx next dev --webpack` → `http://localhost:3000/?view=events`.

## 클라우드 AI 키와 AI 검토

마이그레이션: `db/migrations/0013_fall_ai_review.sql`.

### 로봇별 키

- 낙상 확인용 Ollama Cloud 키 하나를 로봇마다 둔다(대화 이미지 분석 키와 별개). 등록·변경·삭제는 소유자만.
- `PUT/DELETE /api/devices/{id}/fall-cloud-key` (소유자), `GET` (모든 사용자: 등록 여부·끝 4자리·로봇이 최신 키를 받았는지).
- 서버는 AES-256-GCM으로 암호화해 보관한다. 암호 키는 `FALL_KEY_ENCRYPTION_SECRET`(Secrets Manager)에서 만들고,
  로봇 ID와 키 버전을 함께 묶어 다른 로봇·버전으로 옮겨 쓸 수 없다. 키 원문은 사용자 응답·감사 기록에 남지 않는다.
- 로봇 낙상 노드가 주기적으로 `POST /api/device/v1/fall-cloud-key` `{"knownVersion":2,"model":"gemma4:31b"}`를 보낸다.
  응답 `{"keyVersion":3,"changed":true,"apiKey":"…"|null}`. 키는 로봇 사본이 오래됐을 때만 보낸다.
  `keyVersion` 0은 소유자가 키를 등록한 적 없음 → 로봇 자체 키 파일을 그대로 쓴다. `changed:true, apiKey:null`은 삭제.
  보고한 `model`은 서버 AI 검토가 같은 모델을 쓰는 데 쓴다. `robotHasCurrent`는 로봇이 다음 동기화에서
  그 버전을 가지고 있다고 알려 온 뒤에야 참이 된다(보낸 것만으로는 아님).

### AI 검토 (사진만으로 판정)

- `POST .../fall-incidents/{incidentId}/ai-reviews` `{"momentAt":"..."}`: 그 순간부터 5초 후까지 같은 간격 12장을
  연속 녹화에서 꺼내(KVS GetImages, 폭 640) 로봇과 같은 모델·판정 질문으로 1회 보낸다.
  - 순간은 사건 장면 구간 안이어야 한다. 밖이면 `409 outside_incident` → 앱이 "놓친 넘어짐으로 새로 신고할까요?"를 묻는다.
  - 사용자 신고 사건은 신고한 순간으로만 검토한다.
  - 클라우드 분석 동의가 꺼져 있거나(`consent_off`) 키가 없거나(`key_missing`) 로봇이 아직 모델을 알려 주지 않았으면
    (`model_unknown`) 받지 않는다.
  - 한 사건에 진행 중인 검토는 하나. 끝나면 횟수 제한 없이 다시 요청할 수 있다.
- `POST /api/devices/{id}/fall-reports` `{"momentAt":"...","requestAiReview":true}`: 신고하고 AI에게 검토 받기.
  검토를 시작하지 못해도 신고는 남는다.
- 결과는 사건 상세의 `aiReviews`에 "AI 검토 결과"(observed_fall/suspected_fall/normal_activity/unobservable)로 기록만 한다.
  사건 상태·자동 판정·의견·알림은 바꾸지 않는다.
- 추가 질문: `POST .../ai-reviews/{reviewId}/questions` `{"question":"...","includeContext":true}`. 같은 사진에
  (스위치가 켜져 있으면) 메모와 이전 판정을 함께 보내고, 답은 "참고 답변"으로만 남긴다. 판정과 집계에 쓰지 않는다.
- 우선순위: 그 로봇에 최근 1분 안에 "확인 중" 사건이 있으면 사용자 검토를 미룬다. 429·시간 초과·연결 오류는
  최대 6번까지 물러났다 다시 시도한다. 사진이 6장 미만이면 실패로 기록하고 추측하지 않는다.
- 녹화가 끝난 지 15초가 안 된 순간은 저장이 끝날 때까지 기다린다. 2분 안의 순간인데 사진이 모자라면 실패 대신 다시 시도한다.
- 요청은 바로 `202`로 답하고 검토는 응답 뒤에 돌린다(웹 프로세스당 동시에 1건). 결과는 사건 상세에서 다시 읽는다.
  maintenance(매분)도 1건씩 뒤에서 시작한다. 작업자가 6번 죽으면 `worker_lost`로 끝낸다.
- 판정 질문은 로봇 코드(`ollama_cloud_fall.py`)와 같아야 한다. `tests/fall-ai-review.test.mjs`가 파이썬과 비교한다.
  웹은 JSON 중복 키를 따로 거르지 않는다(로봇은 거른다). 판정은 기록용이라 이 차이는 받아들였다.

## 배포 시 확인

- push broker Lambda를 웹보다 **먼저** 배포해야 한다(`infra/aws/push-broker/fall-notification.mjs`의 [재발신]·다시 열림 형식).
  옛 broker는 `resend`와 `reopened_by_opinion`을 거부해 재발신·다시 열림 알림이 계속 실패한다.
- KVS broker Lambda도 함께 배포해야 한다(`GET_IMAGES`). broker 역할에 `kinesisvideo:GetImages` 권한이 추가된다.
- 새 비밀값 `fall-key-encryption-secret`이 생긴다. 바꾸면 저장된 키를 읽을 수 없으니 소유자가 다시 등록해야 한다.
- 웹 서버가 `https://ollama.com`으로 나갈 수 있어야 한다.
- 알림을 누르면 사건 화면(`/?view=events&device=…&incident=…`)으로 간다. broker를 먼저 배포해야 새 주소가 통과한다.
