string STARTED=started
string FAILED=failed
string DISCARDED=discarded
# 빈 값은 일반 대화, 비어 있지 않은 값은 Agent가 먼저 연 확인 대화 세션
string session_id
# 일반 대화는 STARTED/FAILED 모두 비어 있지 않은 발화 ID가 필요하다.
# 확인 세션 전체의 FAILED에만 빈 발화 ID를 허용한다.
# DISCARDED는 확인 세션의 비어 있지 않은 발화 ID에만 사용한다.
string utterance_id
# STARTED는 VAD 발화 시작, FAILED는 해당 발화 또는 확인 세션의 청취/인식 실패
# DISCARDED는 비음성·전사 품질 필터 탈락·정상 인식의 빈 문자열로 철회된 VAD 시작이다.
# Agent는 해당 입력을 답변/오류로 처리하지 않고 기존 답변 시작 기한으로 돌아간다.
string state
