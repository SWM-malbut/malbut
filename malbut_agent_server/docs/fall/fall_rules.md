# 낙상 규칙 (Agent)

낙상 규칙은 Agent 프로그램(`agent_communication`) 안의 노드 `malbut_fall_rules`다
(코드 `malbut_agent_server/fall_rules/`). 2026-10-11(SWM25-238)까지는 별도 노드
`malbut_fall_coordinator`였고, 하는 일·토픽·실행 ID는 그대로 옮겼다. 다른 문서의
"낙상 코디네이터"는 이 낙상 규칙을 뜻한다. 대화 노드와 같은 프로그램이지만 따로 돈다
(별도 실행 스레드, 대화·LLM 대기와 섞이지 않음). 대화가 실패해도 낙상 규칙은 계속 돈다.
시스템 관리자는 낙상 이벤트·영상 판정·동의 설정을 해석하지 않는다.

## 하나로 합친 확인 흐름 (2026-10-17, SWM25-261, Bringup `fall_flow`)

`fall_rules/fall_flow.py`(`FallFlow`)는 모든 낙상 의심을 같은 과정으로 판단한다. ① 처음 거르기(Cloud,
버릴 때는 두 번) → 사건 하나 = 한 사람의 한 번 넘어짐 ② 처음 본 쪽 1 m에서 도착 비교(사라지면 처음 본
자리에서 다시 보고, 그래도 없으면 떠난 것을 봄) ③ 몸 전체를 보고 Cloud 자세 판단(넘어지는 장면을 봤으면 같이,
바닥이면 항상 물음) ④ 1 m로 다가가 질문. 닫을 때 모습을 기억하고 일어섬·넘어짐·바닥에 누움(바닥 아닌 곳에서
닫았을 때)·30분(바닥에서 닫았을 때)에 같은 사건을 다음 회차로 다시 확인한다. 보호자가 처리 완료하면 다시 열지
않는다. 1분 막히면 긴급 알림 뒤 처음부터 다시 가 보고, 막힌 시간이 모두 10분이면 멈춘다(회차마다 따로 셈).
VLM이 본 것과 Cloud 답, 링크의 행동 결과를 받아 명령 목록만 내고 ROS·시계는 쓰지 않는다.

Bringup `fall_flow`(기본 `true`)가 켜져 있으면 `fall_rules/fall_flow_link.py`(`FallFlowLink`)가
`FallConfirmationLink` 대신 이 흐름을 돌린다. Manager 미션을 보내고 기다리고 바퀴를 잡아 두는 방식은
아래 단계 규칙과 같다. 같은 값이 VLM(`flow_enabled`)과 Agent(`--fall-flow`)에 함께 내려가고, Agent가 켤 때
`flow_hello`를 보내 VLM의 `flow_ready`로 열린 사건을 이어받는다(값이 다르면 `fall_flow_switch_mismatch`
오류만 남김). `false`로 켜면 아래의 예전 단계 규칙이 그대로 돈다(실기 확인 뒤 지움).
주고받는 메시지는 [런타임 문서](fall_runtime.md)의 "하나로 합친 확인의 VLM ↔ 낙상 규칙"을 따른다.
결정 근거는 `~/fall-refactor-design-20261017.md`, 테스트는 `test/test_fall_flow.py`(흐름 점검 사례 18개와
설계 결정), `test/test_fall_flow_link.py`, `test/test_fall_flow_sensor.py`(단위 테스트, 실기 확인 아님).

## 책임과 연결

- 홈캠의 서버 설정 → VLM `ApplyFallSettings` → 실제 적용 결과를 홈캠으로 회신.
- VLM 사건 이벤트의 실행 ID·근거 버전·중복을 검사하고 확인 대화 필요 여부를 처리.
- 확인이 필요하면 `/malbut/mission/execute`에 `fall_confirmation`을 요청.
- 관리자는 Manifest의 `HIGH`, `[BASE, SPEAKER]`로 충돌 미션 종료를 확인한 뒤
  기존 Agent의 `/malbut/agent/confirm_situation` Action을 실행. 직접 움직이기(URGENT) 중이면
  받지 않고, 낙상 규칙이 기다렸다가 다시 신청한다(아래 "직접 움직이기를 끊지 않기").
- 낙상 규칙이 최종 결과를 검증하여 해당 VLM 사건에 돌려줌. 실패·취소는 사용자
  무응답이나 도움 요청으로 바꾸지 않음. 일반 활동 종결도 기존 규칙을 유지.

VLM 분석·사건 상태·저장·업로드는 기존 `malbut_agent_server` 런타임,
질문·청취·답변 판단은 기존 Agent가 계속 담당한다. 코어를 복제하지 않는다.
취소된 이전 주행은 관리자의 기존 규칙대로 자동 재개하지 않는다.

낙상 규칙은 Agent와 같은 프로그램이라 Agent가 다시 켜질 때 함께 다시 시작한다. 이때
`/malbut/state`에 남은 낙상 미션의 종료를 기다린 뒤 다음 요청을 보내며, 동일 질문 재전달이
진행 중인 대화를 선점하지 않는다. 새 근거 버전은 이전 요청을 취소하고 오래된 결과를 무시한다.
종료 신호를 받으면 진행 중인 낙상 미션의 취소를 Manager에 보낸 뒤(최대 3초) 끝난다.

다시 켜지면 VLM이 열린 사건을 넘겨준다. 확인 도중 끊긴 사건은 처음부터 다시 확인하고, 보호자에게
맡긴 사건은 그대로 둔다. 다시 켠 뒤에는 열린 사건 자리 1 m 안에서 처음 확실히 보인 사람을 그 사건의
사람으로 본다. 단, 의심이 마지막으로 붙은 지 30분이 지난 사건(사람 추적이 사람을 잊는 시간과 같음)은
그 자리의 새 의심을 받지 않고, 새 의심은 새 사건으로 열린다(2026-10-20 사용자, SWM25-269).
이 시각을 남기기 전의 사건도 받지 않는다. 계기: 2026-10-20 15:14 아무도 닫지 않은 10-19 22:32·23:27
사건 자리에 누웠더니 새 사건 없이 그 둘에 붙었다.

## 실행과 호환성

실로봇에서는 Bringup(`bringup.launch.py`)이 낙상·음성 모듈 뒤에 Agent 항목(`agent.launch.py`)을
켜며 별도 실행은 필요 없다. 낙상 모듈이 시작됐으면 낙상 규칙을, 음성 모듈이 시작됐으면 대화를 켠다.
음성이 꺼져 있으면 Agent는 낙상 규칙만 돌리고 확인 대화 Action 서버를 열지 않는다. 그래서 Manager가
`unavailable`로 끝내고 질문은 바로 실패하며, 보호자에게 긴급 알림이 간다(`speech_unavailable`, 2026-10-12). `fall.launch.py`만 따로 실행하면
그 안에서 음성 없는 Agent(`--no-speech --fall-rules`)를 함께 켠다.
설정 전달·heartbeat·VLM 이벤트의 Topic/Service와 Agent Action 필드는 그대로다.

실행 ID는 Agent 실행 인자 `--fall-runtime-id`, `--fall-bridge-runtime-id`, `--fall-vlm-runtime-id`로
받는다. 기존 메시지와 홈캠의 `manager_runtime_id`/`fall_manager_runtime_id` 필드는
호환성을 위해 이름을 유지하되 **낙상 규칙 실행 ID**를 담는다.
시스템 관리자에는 이 파라미터나 낙상 전용 구독을 등록하지 않는다.

`fall_confirmation.yaml`만 일반 Capability로 등록한다. 동의 설정 변경 Service는
Agent가 임의로 호출할 수 있도록 Manifest에 노출하지 않는다.

## 다가가 확인하기 (2026-10-08)

사람인지 확실하지 않은 낙상 의심(Cloud만 찾았거나 Pose 박스가 약함)은 묻기 전에 먼저 가까이 가서 본다.
런타임이 `question_requested`에 `approach_target{x, y, frame: map}`을 붙였을 때만이다(런치 `fall_approach`,
2026-10-08부터 기본 켜짐. 지도 좌표가 필요해 깊이 스위치 `fall_depth_aligned_to_rgb`도 켜져 있어야 하며 이것도 기본 켜짐).

```
approach_target 있음 → fall_approach(phase=approach) 미션: 금지 구역을 피해 1 m 앞, 의심 장소를 바라봄
  ├ 못 감(no_map·no_path·timeout·failed) → approach_result → 그 자리에서 fall_confirmation
  │   (Manager가 받지 않거나 직접 움직이기 중이면 기다렸다가 다시 신청)
  └ arrived → approach_result → 런타임이 가까이서 확인(Pose 3초, 없으면 Cloud 1회) → person_check_completed
       ├ person(애매·실패 포함) → fall_confirmation(같은 질문 ID)
       ├ not_a_person → 질문 없음. 런타임이 사건을 not_a_person으로 종료 → fall_approach(phase=return) → return_result
       └ 15초 안에 답 없음 → 그 자리에서 fall_confirmation
```

- 다가가기 노드 `fall_approach`([`malbut_fall_approach`](../../../malbut_fall_approach/README.md)): `/malbut/falls/approach` `FallApproach` 액션 서버.
  1 m 원 위 자리를 처음 본 쪽(`from_x`·`from_y`, 모르면 지금 로봇 위치 쪽)부터 골라 ComputePathToPose(GridBased,
  keepout 반영) → FollowPath → Spin으로 바라봄. 갈 수 없으면 더 넓은 원, 그래도 안 되면 그 자리에서 바라봄.
  이미 1 m 안이면 움직이지 않고 그 자리에서 바라봄(2026-10-19 사용자).
  이동 60초 상한. 시작 위치를 질문 ID별로 저장해 return에서 돌아간다.
  결과는 `/malbut/falls/approach/status`(래치 JSON)에도 낸다(웹 순찰 카드의 "낙상 확인으로 멈춤").
- 능력 `fall_approach.yaml`: HIGH, `[BASE]`. 지도·위치·경로를 실제로 사용할 수 없으면
  하위 이동 기능이 실패하고 낙상 규칙은 그 자리에서 묻는다. 직접 움직이기 중이면 기다렸다가 다시 신청한다.
- 멈춘 순찰·따라가기는 자동으로 다시 시작하지 않는다(기존 규칙). 웹 순찰 카드가 멈춘 이유와 결과를 보여 준다.
- 결정(낙상 규칙 → 런타임): `approach_result`, `view_result`, `return_result` = `boot_id, incident_id,
  question_id, evidence_revision, outcome`.

## 몸 전체 보고 판단하기 (2026-10-11, 런치 `fall_full_view`, 기본 켜짐)

카메라가 12 cm 높이라 침대에 걸터앉은 사람은 1 m 앞에서 다리만 보인다(10월 10일 20:43, 11일 03:39).
근접 확인이 사람이면 몸통이 보일 때까지 옮겨 클라우드 AI에게 자세를 두 번 묻고, 두 번 다
침대·의자 위이거나 서 있으면 묻지 않는다. 12 cm에서 올려다보면 허벅지가 엉덩이를 가려 Pose 관절로 자세를
가리기 어려워 판단에는 Pose를 쓰지 않는다. 언제 멈출지(몸통이 화면에 들어왔는지)만 Pose로 본다(2026-10-13).

```
person_check_completed
  ├ partial_body(사람) → fall_approach(phase=view, standoff 최대 3 m): 몸통이 보이면 멈춤(바라본 채 후진만), 끝까지 안 보이면 그 자리(10-19)
  │    └ view_result → 런타임이 Cloud 자세 확인 2번(2.5초 간격) → posture_check_completed
  └ posture_check_completed
       ├ sitting_off_floor · standing → 질문 없음. 런타임이 off_floor_verified로 종료 → return(reason=off_floor)
       ├ active_on_floor(바닥에서 활동 중) → 질문 없음. 런타임이 floor_activity_verified로 종료 → return(reason=off_floor)
       ├ on_floor · body_unseen · posture_unconfirmed → 1 m로 다시 다가감 → fall_confirmation
       └ 60초 안에 답 없음 → 1 m로 다시 다가감 → fall_confirmation
```

- 질문 전 다시 다가가기(2026-10-13 사용자 "말 어떻게 정확하게 듣게?"): 몸 전체 보기로 2~3 m 물러난 채 물으면
  대답이 마이크에 작게 잡힌다. 그래서 묻기 전에 `fall_approach(phase=approach, standoff 1 m)`로 의심 지점 1 m 앞까지
  다시 가서 바라본다(단계 `closer`, 상태 `phase=approach`라 대화·홈캠 말하기는 계속 막힌다). 결과가 어떻든(도착·
  길 없음·실패) 그 자리에서 묻고, 런타임에는 따로 보내지 않는다(다가가기 결과를 보내면 다시 보기가 시작된다).
  다시 가서 보기(revisit) 뒤에는 그 지점(`revisit_target`)을 쓴다.

- 자세 확인(런타임): 도착 1초 뒤 Cloud `check_posture`(`furniture`/`standing`/`floor_active`/`floor`/`unclear`)를 묻고,
  `furniture`·`standing`이면 2.5초 뒤 새 장면으로 한 번 더 묻는다. 두 번 다 그럴 때만 닫는다(둘 다 `standing`이면
  `standing`). `floor`는 `on_floor`, 애매·실패·차단은 `posture_unconfirmed`, 옮기지 못하면 `body_unseen`.
  `floor_active`는 두 번 다 그렇고 넘어지는 장면을 못 봤고 revisit가 아닐 때만 `active_on_floor`(질문 없음, 2026-10-14),
  아니면 `on_floor`다.
- 옮기기는 60초 안(사용자 결정 1분). 시작 위치는 다가가기 때의 것을 그대로 써서 return이 원래 자리로 간다.
- return의 `reason`(`not_a_person`/`off_floor`/`revisit`)은 순찰 카드 결과(`fall_result`)를 가른다.
- 로그: `fall posture: case … Cloud looks furniture, furniture: sitting_off_floor`.

## 직접 움직이기를 끊지 않기 · 확인 못 하면 알리기 (2026-10-12)

낙상 확인 이동·질문(`fall_approach`, `fall_confirmation`)은 HIGH이다. 순찰(LOW)과 목적지 이동·따라가기·
위치 찾기·지도 만들기(NORMAL)는 끊고 확인하러 가지만, 직접 움직이기(`manual_drive`, URGENT)는
끊지 않는다. 기획 `fall-check-normal-alerts`(목업 27·28번).

멈춰서 보는 사이에도 HIGH를 쥔다(2026-10-13 사용자 "낙상 확인하는 동안에는 HIGH가 무조건 있어야지").
앱·음성 이동(NORMAL)이 가까이 보기·자세 확인·기다림 틈에 들어오지 않게 하려는 것이다.
- 다가가기·몸 전체 보기 이동은 도착하면 결과를 feedback(`outcome`·`detail`)으로 먼저 내고, 미션을 끝내지 않고
  `HOLDING`으로 남는다. 낙상 규칙은 그 feedback을 결과로 받아 다음 단계를 진행한다.
- 다음 단계(질문·돌아오기·다음 이동)는 같은 HIGH라 Manager에서 쥐고 있던 미션을 대신한다. 대신된 미션의 늦은
  결과는 지금 단계의 결과로 쓰지 않는다(Goal handle로 가림).
- 질문이 `Downstream Action server rejected the goal`로 잠깐 거절되면 기다리는 동안 `fall_approach(phase=hold)`를
  보낸다. 직접 조작(URGENT)이 쥔 것을 끊으면, 직접 조작이 끝나는 것을 `/malbut/state`로 보고 바로 다시 신청한다.
- 요청이 다 끝나면(사건 닫힘·처리 완료·"그만") 쥔 미션을 취소한다. 쥐기 상한은 660초.
- 남은 틈: Agent 거절 응답과 hold 신청 사이 수 ms, 직접 조작이 끝난 뒤 다시 신청이 받아들여질 때까지.

확인 중 들은 말(2026-10-13, `MANAGER_VOICE_COMMANDS.md` "낙상 확인 중 말과 이동 명령"): Agent가
`/malbut/falls/check/voice`(`std_msgs/String` JSON `{"kind": "help"|"stop", "utterance_id": …}`)로 알린다.
지금 진행 중인 요청(없으면 줄 맨 앞)에 적용한다.
- `help`: 도움 필요로 끝낸다(`confirmation_result`, `situation_assessment=unknown`, `help_needed=true` → 긴급 알림).
- `stop`: "그만/멈춰"는 직접 조작과 같다. 진행 중 미션을 취소하고, 묻지 않고, `check_stopped`(`reason=user_stop`)를
  보낸다(→ 확인 필요 알림 `check_stopped_by_user`). 돌아오기도 하지 않는다.

Manager 결과는 셋으로 나눈다. 문구는 `fall_confirmation_link.py`의 상수이며, Manager 원문과 같은지
`test_fall_rules_waiting.py`가 확인한다.

| Manager 결과 | 처리 |
|---|---|
| 목표를 받지 않음, 5초 응답 없음, Manager 없음 | 기다림(바쁨): 1초 뒤 같은 단계를 다시 신청 |
| `conflicting mission … higher priority URGENT` | 기다림(실제 수동 조작 중이면 직접 움직이기, 아니면 바쁨) |
| `mission preempted …`, `superseded before execution …` | 기다림(그때 직접 움직이기가 있으면 직접 움직이기, 아니면 바쁨) |
| 켜지는 중·비상 정지·충전 중·복구 중 | 기다림(바쁨) |
| 질문의 `Downstream Action server rejected the goal` | 기다림(바쁨), 그동안 `hold`로 HIGH를 쥠 |
| 질문: `Action server /malbut/agent/confirm_situation is unavailable` | 바로 실패 `speech_unavailable` → 긴급 알림 |
| 질문: 그 밖의 실패, 610초 초과, 질문 중 Agent가 5초 넘게 사라짐 | 바로 실패 `question_error` → 긴급 알림 |
| 이동: `select a saved map first …` | 갈 수 없음(`no_map`): 그 자리에서 물음 |
| 이동: 그 밖의 실패(길 없음·시간 초과·이동 서버 거부 등) | 갈 수 없음: 그 자리에서 물음 |

- `/malbut/state`에 직접 움직이기가 실행·대기 중이면 신청하지 않고 기다림으로 센다.
- **막힌 시간**만 센다: 기다림으로 분류된 때부터 Manager가 다음 단계를 `RUNNING`으로 알릴 때까지.
  이동·보기·질문이 진행 중인 시간은 세지 않는다. 기다리는 동안 줄에 있는 모든 사건의 시간이 함께 늘어난다.
- 3초 넘게 막히면 `check_waiting`(`manual_drive`|`busy`)을 한 번 보낸다(이유마다 한 번). Agent가 앞 대화를
  마무리하느라 잠깐 거절하는 것처럼 곧 풀리는 막힘은 기록하지 않는다(막힌 시간에는 들어간다).
- 막힌 시간 **60초** → `check_not_confirmed`(`waited_manual_drive`|`waited_busy`) → 런타임이 긴급 알림
  `robot_could_not_confirm`(사건당 한 번). 다시 갈 지점(`revisit_target`)이 있으면 그 사건의 다음 단계를
  **다시 보기**로 바꾼다(지금 단계가 질문이어도). 사람이 그새 움직였을 수 있어 묻기 전에 다시 본다.
- 다시 보기 = `fall_approach(phase=approach, 1 m)`를 `revisit_target`으로. `RUNNING`이면 `revisit_started`,
  끝나면 `revisit_result`(`approach_result`와 같은 결과). 그 뒤 가까이 보기·몸 전체 보기·자세는 처음과 같고,
  런타임은 사건을 스스로 닫지 않는다(사람 없음 `nobody_there`, 바닥 아님 → 알림만 보내고 돌아감 `reason=revisit`).
- 다시 본 뒤 또 막혀 60초 → 다시 보기를 한 번 더(알림은 다시 보내지 않음).
- 막힌 시간 합계 **10분** → 진행 중 단계는 끊지 않고, 다음 신청 대신 `confirmation_failed(time_limit)`로 멈춘다.
- 보호자가 앱에서 처리 완료하면 런타임이 `incident_resolved(guardian_closed)`를 내고, 낙상 규칙은 기존처럼
  그 사건 요청을 지우고 진행 중 미션을 취소한다.
- 결정(낙상 규칙 → 런타임): `check_waiting`, `check_not_confirmed`, `revisit_started`, `check_stopped` =
  `boot_id, incident_id, question_id, evidence_revision, reason`. `revisit_result`는 `approach_result`와 같은 필드.
  `confirmation_failed`에 `reason`(`speech_unavailable`|`question_error`|`time_limit`)이 붙는다.

### "낙상 확인 중" 상태

`/malbut/falls/check/status`(`std_msgs/String` JSON), 1초마다와 바뀔 때:

```json
{"active": true, "phase": "approach|look|ask|return|wait", "waiting_for": "manual_drive|busy|null",
 "alerted": false, "target": {"x": 1.2, "y": 3.4}}
```

`active`는 첫 이동부터 돌아오기 끝까지 참이다(멈춰서 보는 사이 틈 포함). `phase`: 이동 → `approach`,
가까이 보기·몸 전체 보러 옮기기·자세 → `look`, 질문 → `ask`, 돌아가기 → `return`, 막힘 → `wait`.
`alerted`는 맨 앞 사건이 1분 알림을 보냈는지, `target`은 의심 지점. Bringup 웹 패널은 3초 넘게 안 오면 끝난
것으로 보고(Agent가 멈춰도 표시가 남지 않게) 상태 표시와 지도 화면 카드에만 쓴다. 이동 버튼은 웹 패널에서 막지
않고 Manager가 HIGH 미션으로 거절한다(2026-10-12 `89f6407`, 위 "멈춰서 보는 사이에도 HIGH를 쥔다").
`approach`·`look`·`ask`·`wait`는 낙상 우선 구간이다(2026-10-13): Agent는 대화를 받지 않고, STT는 보호자 홈캠 말하기
허가를 거절하고, 웹은 말하기 시작을 `409 fall_check`로 거절한다(`MANAGER_VOICE_COMMANDS.md` "낙상 확인 중 말과 이동 명령").
