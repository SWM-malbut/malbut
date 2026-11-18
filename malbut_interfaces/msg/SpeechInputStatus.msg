string STARTED=started
string FAILED=failed
string INCOMPLETE=incomplete
string FILTERED=filtered
string ENDED=ended
# 빈 값은 일반 대화, 비어 있지 않은 값은 Agent가 먼저 연 확인 대화 세션
string session_id
# 일반 대화의 모든 상태에는 비어 있지 않은 발화 ID가 필요하다.
# 확인 세션 전체의 FAILED에만 빈 발화 ID를 허용한다.
string utterance_id
# STARTED는 VAD 발화 시작, FAILED는 해당 발화 또는 확인 세션의 청취/인식 실패
# INCOMPLETE는 확인 세션의 최종 전사가 최근 발화를 누락했다는 실패다. 같은
# session_id의 빈 발화 ID FAILED가 뒤따른다. 낙상 확인 소유자만 한 번 질문을
# 재생할 수 있고, 기존 소유자는 뒤따른 FAILED로 종료한다. 전사 성공이 아니다.
# FILTERED는 일반 대화의 문구 차단으로, 고정 재발화 안내 후 호출어 없이 다시 듣는다.
# ENDED는 사용자의 명시적 종료 요청이다. Agent는 종료 안내를 하고 STT는 호출어 대기로 돌아간다.
string state
