uint8 DIALOGUE=0
uint8 NOTIFICATION=1
uint8 CONFIRMATION=2

# TTS가 음성으로 변환하여 사용자에게 말할 텍스트
string text
# 대화 답변은 DIALOGUE, 일반 작업 알림은 NOTIFICATION
uint8 request_type

# 발행자가 지정한 재생 ID. 빈 값이면 TTS가 생성한다.
string playback_id

# 같은 사용자 요청의 진행 안내와 최종 답변을 묶는 ID. 빈 값이면 묶지 않는다.
# 최종 답변 접수 시 같은 ID의 아직 재생하지 않은 진행 안내를 취소한다.
string request_id

# 최종 답변 전의 진행 안내. 완료 뒤에도 최종 답변을 기다리며 청취를 유지한다.
bool interim
