# Manager -> 홈캠 서버 통신부. 실제 VLM 적용 응답을 대조한 뒤 보고한다.
string bridge_runtime_id
string manager_runtime_id
# 재전송 시 같은 번호를 유지한다.
uint64 sequence
# 적용 요청에 사용한 FallSettingsSnapshot의 sequence.
uint64 snapshot_sequence
# Manager가 적용 응답을 확인한 시각. 로봇의 단조 시계, 초.
float64 reported_at
# 이하 값은 ApplyFallSettings 응답에서 전달한다.
string runtime_id
uint64 requested_revision
uint64 applied_revision
bool applied
bool enabled
bool camera_enabled
bool cloud_consent
string reason_code "invalid_request"
