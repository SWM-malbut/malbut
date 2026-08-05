# Malbut Agent Boundary

`malbut_agent_server`는 LLM과 로봇 실행 계층 사이의 계약을 제공하는 ROS 2
Python 패키지다. 이번 SWM25-69 범위에는 다음 구성요소만 포함한다.

- 검증된 요청·응답·로봇 상태 스키마
- LLM에 공개할 고수준 Tool allowlist
- 신뢰된 로컬 상태와 현재 사용자 발화를 다시 확인하는 안전 게이트
- 연관 Jira 스토리와의 책임·오류·timeout·확인 계약

LLM의 `tool_call`은 실행 명령이 아니라 제안이다. 실제 실행 계층은 최신
ROS 상태를 별도로 읽고, 안전 정책과 사용자 확인을 다시 검증해야 한다.
`/cmd_vel`, 모터 PWM, 속도값과 비상 정지 해제는 LLM Tool에 포함하지 않는다.

## 안전 계약 테스트

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server
PYTHONPATH=. python3 -m pytest -q test
```

전체 계약은
[`docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md`](docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md)에
정리되어 있다. 문서에서 `제안` 또는 `대기`로 표시된 연관 스토리
인터페이스는 담당자 승인 전까지 확정 계약이나 실행 가능한 기능으로
취급하지 않는다.

연관 담당자가 확인할 항목과 Jira 댓글 양식은
[`SWM25-69 인터페이스 승인 가이드`](docs/jira/SWM25-69_INTERFACE_APPROVAL_GUIDE.md)에
정리되어 있다. CI 통과나 PR 병합은 구현 근거이며 사람의 인터페이스
승인을 대신하지 않는다.
