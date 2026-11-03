string STARTED=started
string FAILED=failed
string FILTERED=filtered
# 빈 값은 일반 대화, 비어 있지 않은 값은 Agent가 먼저 연 확인 대화 세션
string session_id
# 일반 대화는 STARTED/FAILED/FILTERED 모두 비어 있지 않은 발화 ID가 필요하다.
# 확인 세션 전체의 FAILED에만 빈 발화 ID를 허용한다.
string utterance_id
# STARTED는 VAD 발화 시작, FAILED는 해당 발화 또는 확인 세션의 청취/인식 실패
# FILTERED는 일반 대화의 문구 차단으로, 고정 재발화 안내 후 호출어 없이 다시 듣는다.
string state
