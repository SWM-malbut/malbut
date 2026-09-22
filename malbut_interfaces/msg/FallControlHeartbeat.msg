# Manager -> VLM, 1초마다 연결 확인. 이 메시지로 설정을 바꾸지 않는다.
string manager_runtime_id
string runtime_id
uint64 sequence
# 서버에서 확인한 설정 번호. 확인한 적 없으면 0.
uint64 settings_revision
# 같은 로봇의 단조 시계, 초. 날짜/ROS 시뮬레이션 시각이 아니다.
# 서버 확인 전 또는 확인을 무효로 처리했으면 -1.
# 같은 확인을 다시 전달해도 시각을 바꾸지 않는다.
float64 server_checked_at -1.0
float64 sent_at
