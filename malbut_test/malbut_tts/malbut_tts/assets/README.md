# 대화 불가 안내

`notice_no_dialogue.wav`는 **“지금은 대화를 할 수 없어요.”**를 말하는
AI 생성 음성이다. 2026-10-11에 OpenAI `gpt-4o-mini-tts`, `nova`와
`voice_style.py`의 한국어 여성 음색 지시로 생성했으며,
로봇의 안내 재생에는 API나 API 키가 필요하지 않다.
`audio/conversation.unavailable.wav`와 같은 녹음을 사용한다.

- 형식: 24,000 Hz, mono, signed 16-bit PCM WAV
- 길이: 3.00초
- SHA-256: `c206b44e41c89fd80dd329453e125a831ae80ceb1db8d4ef76ed41453367274c`

음성을 교체할 때 원본과 `malbut_test` 배포 복사본을 함께 갱신한다.
`setup.py`의 `package_data`로 설치되며, 설치 누락은 `test_notice_package.py`가 검사한다.
