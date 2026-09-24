# 이상 상황 확인 대화 평가

한국어 20개 고정 사례로 최종 실제 상황 판단, 도움 필요 여부, 질문 횟수를 비교한다.
휴식·낙상 부정, 실제 낙상 후 도움 거절, 도움 요청, 무응답, 모호함 재질문 상한,
앞선 답변 철회, 연기·큰 소리 등 다른 이상 상황을 포함한다.

기본 실행은 네트워크와 실제 API 키를 사용하지 않는다.

```bash
python3 -m malbut_agent_server.situation_eval_runner
```

설치된 패키지는 `malbut-situation-eval` 명령도 제공한다. 고정 예문을 처리하는
mock의 13개 사례는 상태 흐름 확인용이다. 의미 변형·인용·다른 상황을 포함하는
7개 사례는 `skipped_live_only`로 표시하며, **mock 통과는 실제 모델 의미 검증이 아니다.**

실제 모델 평가는 운영과 같은 `Settings`, 설정 파일 로더,
`build_situation_factory`를 재사용한다. `--provider openai`를 명시해야 실행되며
설정된 `OPENAI_GENERAL_MODEL` 또는 `OPENAI_MODEL`을 사용한다.

```bash
python3 -m malbut_agent_server.situation_eval_runner \
  --provider openai --env-file /absolute/path/to/private.env \
  --output /absolute/path/to/situation-evaluation.json
```

`--case-id resting-paraphrase`로 사례를 선택하거나 `--cases /absolute/path/to/cases.json`으로
동일 형식의 데이터를 읽을 수 있다. 사례 JSON의 `providers`는 mock 적용 가능 여부를
표시하고, `answers`의 `null`은 명시적인 무응답이다. 준비한 답변이 소진된 상태를
무응답으로 바꾸지 않는다. 질문 수는 마무리 발화를 제외하며, 남은 답변이 있는데
조기 종료하거나 기대 질문 수와 다르면 실패로 집계한다.

결과에는 평가 종류·집계와 각 사례의 ID, 상태, 최종 두 필드, 질문 횟수만 남긴다.
키, 요약 원문, 질문, 답변, 모델 오류 본문은 기록하지 않는다. 모델·전송 오류는
`error`로 구분하며 사용자 무응답이나 도움 필요 판단으로 바꾸지 않는다.

이 도구는 텍스트 의미와 대화 상태를 평가한다. 음성 재생 품질, STT 정확도,
실제 10초 대기, 끼어들기, ROS 전달, 질문 문구의 자연스러움은 별도 검증 대상이다.
이 변경에서는 실제 API 평가를 실행하지 않았다.
