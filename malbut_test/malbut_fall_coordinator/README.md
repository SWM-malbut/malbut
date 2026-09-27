# 낙상 코디네이터

`malbut_fall_coordinator`는 Bringup이 VLM·Pose와 함께 켜는 상시 노드다.
시스템 관리자는 낙상 이벤트·영상 판정·동의 설정을 해석하지 않는다.

## 책임과 연결

- 홈캠의 서버 설정 → VLM `ApplyFallSettings` → 실제 적용 결과를 홈캠으로 회신.
- VLM 사건 이벤트의 실행 ID·근거 버전·중복을 검사하고 확인 대화 필요 여부를 처리.
- 확인이 필요하면 `/malbut/mission/execute`에 `fall_confirmation`을 요청.
- 관리자는 Manifest의 `URGENT`, `[BASE, SPEAKER]`로 충돌 미션 종료를 확인한 뒤
  기존 Agent의 `/malbut/agent/confirm_situation` Action을 실행.
- 코디네이터가 최종 결과를 검증하여 해당 VLM 사건에 돌려줌. 실패·취소는 사용자
  무응답이나 도움 요청으로 바꾸지 않음. 일반 활동 종결도 기존 규칙을 유지.

VLM 분석·사건 상태·저장·업로드는 기존 `malbut_agent_server` 런타임,
질문·청취·답변 판단은 기존 Agent가 계속 담당한다. 코어를 복제하지 않는다.
취소된 이전 주행은 관리자의 기존 규칙대로 자동 재개하지 않는다.

코디네이터 재시작 시에는 `/malbut/state`에 남은 확인 미션의 종료를 기다린다.
동일 질문 재전달이 진행 중인 대화를 선점하지 않으며, Agent의 기존 결과 캐시로
복구한다. 새 근거 버전은 이전 요청을 취소하고 오래된 결과를 무시한다.

## 실행과 호환성

실로봇에서는 기존 `ros2 launch malbut_bringup robot.launch.py`를 사용한다.
낙상 설정이 있는 경우 준비 확인 후 노드가 함께 실행되며 별도 실행은 필요 없다.
설정 전달·heartbeat·VLM 이벤트의 Topic/Service와 Agent Action 필드는 그대로다.

새 노드의 읽기 전용 파라미터는 `runtime_id`, `bridge_runtime_id`, `vlm_runtime_id`다.
기존 메시지와 홈캠의 `manager_runtime_id`/`fall_manager_runtime_id` 필드는
호환성을 위해 이름을 유지하되 **낙상 코디네이터 실행 ID**를 담는다.
시스템 관리자에는 이 파라미터나 낙상 전용 구독을 등록하지 않는다.

`fall_confirmation.yaml`만 일반 Capability로 등록한다. 동의 설정 변경 Service는
Agent가 임의로 호출할 수 있도록 Manifest에 노출하지 않는다.
