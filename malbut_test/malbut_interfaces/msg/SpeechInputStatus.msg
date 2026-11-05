string STARTED=started
string FAILED=failed
string FILTERED=filtered
string ENDED=ended
# 빈 값은 일반 대화, 비어 있지 않은 값은 Agent가 먼저 연 확인 대화 세션
string session_id
# 일반 대화의 모든 상태에는 비어 있지 않은 발화 ID가 필요하다.
# 확인 세션 전체의 FAILED에만 빈 발화 ID를 허용한다.
string utterance_id
# STARTED는 VAD 발화 시작, FAILED는 해당 발화 또는 확인 세션의 청취/인식 실패
# FILTERED는 일반 대화의 문구 차단으로, 고정 재발화 안내 후 호출어 없이 다시 듣는다.
# ENDED는 사용자의 명시적 대화 종료로, 이 발화에 대한 답변 없이 청취를 마친다.
string state
