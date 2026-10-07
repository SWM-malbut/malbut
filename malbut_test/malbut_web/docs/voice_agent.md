# 음성 홈캠 연결

`0024_voice_agent` 마이그레이션 후 설정 화면의 **음성 홈캠 사용 허용**을 소유자가
켜야 장치 Agent가 홈캠 상태·기록을 조회하고 일반 설정을 변경할 수 있다. 기본은
꺼짐이며, 화자 인증을 의미하지 않는다. 가까이에서 말하는 사람이 위임된 기능을
사용한다. 가족·접근 권한, 장치 credential, Cloud 분석 동의는 웹 전용이다.

장치 bearer API는 `POST /api/device/v1/agent/operate`이다. 본문의 `deviceId`,
사용자 이메일, URL, HTTP method는 받지 않는다. 인증된 토큰의 장치에만 적용한다.

```json
{"requestId":"voice:request-1","operation":"homecam_settings","arguments":{"cameraEnabled":false}}
```

응답은 `{success,code,result,message}`이다. 요청 ID는 장치별로 영속 저장된다.
동일 내용 재전송은 저장된 응답을 돌려주며 다른 내용은 409이다. 요청 접수 후 실행
직전에 위임·위임한 사용자의 소유권·credential 유효성을 다시 검사한다. 설정 변경과
응답 저장은 위임 잠금을 가진 한 트랜잭션에서 수행된다. 위임 해제는 아직 실행되지
않은 홈캠 요청을 실패로 끝낸다. 이미 수행한 작업을 해제 때문에 되돌리지는 않는다.

| operation | arguments |
| --- | --- |
| `homecam_status` | `{}` |
| `homecam_events` | `limit` 1~20(기본 10), 선택 `eventType`: motion/person/dog/cat |
| `homecam_recordings`, `homecam_falls` | `limit` 1~20(기본 10) |
| `homecam_settings` | 하나 이상의 boolean: cameraEnabled/microphoneEnabled/monitoringEnabled/fallEnabled |
| `result_publish` | kind, title(1~100자), summary(최대 1000자), 선택 referenceId/state |

`result_publish.kind`는 mission/status/map/homecam/event/recording/fall이다. mission/status/map은
장치 자신의 로봇 결과라 홈캠 위임 없이 게시 가능하다. 나머지는 위임과 같은 장치의
실제 referenceId가 필요하다. 단, homecam은 상태·설정 결과로 위임만 필요하고 referenceId를
받지 않는다. state는 accepted/running/succeeded/failed/canceled/unknown.
진행 상태를 추가 게시하려면 새로운 requestId를 쓰고 같은 referenceId로 연결한다.
서버는 URL을 입력받지 않으며 stable ID와 인증된 웹 화면 링크만 반환한다. KVS ARN,
credential 및 presigned playback URL은 Agent 응답이나 요청 기록에 넣지 않는다.
map의 referenceId는 해당 장치에 업로드된 `robot_maps.map_id`이며, 저장된 preview revision을
포함한 인증 이미지 링크를 반환한다. 아직 업로드되지 않거나 이미 다른 지도로 바뀌었다면
reference-not-found로 응답한다. 녹화·낙상 항목은 `/voice-results/:deviceId/:kind/:referenceId`의
인증된 상세 화면으로 연결한다. 녹화 화면은 현재 7일 보관 범위의 연속 녹화 타임라인과 장치별 recording-playback API를
재사용하고, 재생 주소는 웹에서 재생할 때만 기존 권한 검사를 거쳐 발급한다.

설정 성공은 `SETTINGS_SAVED`, `saved:true`, `receiptState:waiting`이며 적용 확인이
아니다. 카메라·마이크·모니터링의 `mediaSettingsRevision`은 값이 바뀔 때 증가하며
낙상 revision과 독립적이다. heartbeat는 기존 desiredState와 함께 이 revision을
전달한다. 미디어 노드는 설정 적용 경로를 거친 뒤 `mediaSettingsReport`에 runtimeId,
sequence, requestedRevision, 실제 로컬 설정 플래그, 적용 결과를 보고한다. 카메라 ON은
PLAYING 상태의 로컬 영상·음성 파이프라인과 최근 카메라 프레임이 필요하며, 모니터링 ON은
현재 연속 녹화의 storage health도 필요하다. 카메라 OFF는 영상 파이프라인 제거를 확인한다.
카메라·마이크만 켜진 경우의 적용 회신은 원격 시청 연결 성공을 뜻하지 않는다.

`homecam_status`의 `mediaApplyReceipt`와 `fallApplyReceipt`는 현재 저장 revision의
`waiting`/`no_response`/`reported_applied`/`reported_failed`를 구분한다. 보고 플래그,
수신 시각, reportAgeS, fresh(15초 이내), runtimeId를 제공하고, 중복 sequence는 수신
시각을 갱신하지 않는다. 회신은 해당 프로세스의 생존 확인을 대신하지 않으므로
`runtimeVerified:false`를 유지한다. 오래된 로봇은 media 회신 없이 계속 작동하며 상태는
회신 없음으로 남는다. 웹 DB와 새 미디어 노드를 함께 배포해야 일반 설정 적용 회신이 보인다.

홈캠 마이크 OFF는 보호자에게 보내는
소리만 끄며 음성 대기는 유지된다. 카메라 OFF는 모니터링도 끄고 관련 미디어 세션을
종료한다. 카메라가 꺼진 상태의 모니터링 ON은 409이다.

소유자 위임 설정과 결과 조회는 `GET/PATCH /api/devices/:deviceId/voice-agent`를
사용한다. PATCH 본문은 `{enabled:boolean}`이며 소유자 및 동일 출처를 검사한다.
로봇 기능 화면의 **음성 요청·결과**에 최근 요청과 결과를 표시한다.
