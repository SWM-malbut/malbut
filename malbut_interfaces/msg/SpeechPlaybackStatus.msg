string PLAYING=playing
string PAUSED=paused
string FINISHED=finished
string FAILED=failed
string STOPPED=stopped
# TTS 재생 한 건을 구분하는 고유 ID
string playback_id

# 위 상수 중 하나. FINISHED만 정상 재생 완료를 뜻한다.
string state

# 해당 SpeechRequest의 interim 값. 한 재생 ID의 모든 상태에서 동일하다.
bool interim

# 원본 SpeechRequest의 request_id. CONFIRMATION은 기존 정책대로 빈 값이다.
# PLAYING 이전에 실패하거나 취소한 경우에도 동일하게 전달한다.
string request_id
