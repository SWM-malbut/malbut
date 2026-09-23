# VLM -> Manager, 1초마다 보고. 상태 보고 때문에 Cloud를 호출하지 않는다.
string runtime_id
# 이번 VLM 실행에서 1부터 증가한다. 중복/역순 메시지는 연결을 갱신하지 않는다.
uint64 sequence
bool settings_applied
uint64 applied_revision
bool enabled
bool camera_enabled
bool cloud_consent
bool accepting_images
# 마지막 정상 RGB 처리 후 지난 초. 아직 처리하지 않았으면 -1.
float64 last_frame_age_s -1.0
# waiting_settings / ready / paused / error
string runtime_state "waiting_settings"
# none / waiting_settings / disabled / camera_off / control_unavailable / runtime_error
string pause_reason "waiting_settings"
# idle / queued / waiting_response / completed / failed / cancel_requested / canceled
string analysis_state "idle"
# 분석 이력이 없으면 빈 문자열. 실패가 아니면 last_error_code도 빈 문자열.
string request_id
# incident / crosscheck, 분석 이력이 없으면 빈 문자열.
string request_purpose
string last_error_code
