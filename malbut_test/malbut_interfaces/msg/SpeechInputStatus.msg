string STARTED=started
string FAILED=failed
# 호출어 인식 직후의 사건. utterance_id는 중복 방지용 사건 ID이며 발화 ID가 아니다.
string WAKE=wake
# 빈 값은 일반 대화, 비어 있지 않은 값은 Agent가 먼저 연 확인 대화 세션
string session_id
# 일반 대화는 STARTED/FAILED 모두 비어 있지 않은 발화 ID가 필요하다.
# 확인 세션 전체의 FAILED에만 빈 발화 ID를 허용한다.
string utterance_id
# STARTED는 VAD 발화 시작, FAILED는 청취/인식 실패, WAKE는 일반 호출어 인식
string state
