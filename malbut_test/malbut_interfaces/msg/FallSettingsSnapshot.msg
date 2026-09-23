# 홈캠 서버 통신부 -> Manager. 조회 실패를 소유자의 OFF 설정으로 적용하지 않는다.
string bridge_runtime_id
uint64 sequence
# 조회 결과를 기록한 시각. 로봇의 단조 시계, 초.
float64 observed_at
# waiting / confirmed / unavailable / rejected
string check_state "waiting"
# 마지막 정상 설정. 아직 없으면 번호 0, bool false.
uint64 settings_revision
bool enabled
bool camera_enabled
bool cloud_consent
# 마지막 정상 설정 응답의 수신 시각. 확인 전 또는 무효이면 -1.
float64 server_checked_at -1.0
# waiting_server / none / server_timeout / server_transport_error /
# server_auth_failed / server_invalid_settings / server_settings_missing
string reason_code "waiting_server"
