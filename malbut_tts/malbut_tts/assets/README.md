# 대화 불가 안내

`notice_no_dialogue.wav`는 **“지금은 대화를 할 수 없어요.”**를 말하는
AI 생성 음성이다. 2026-10-18에 한국어 정규화 v2.1을 적용한
Piper KSS medium을 CPU 6스레드로 실행해 생성했으며,
로봇의 안내 재생에는 API나 API 키가 필요하지 않다.
`audio/conversation.unavailable.wav`와 같은 녹음을 사용한다.

- 형식: 24,000 Hz, mono, signed 16-bit PCM WAV
- 원본 22,050 Hz 음성을 생성 시 한 번 24,000 Hz로 리샘플링
- 길이: 2.21초
- SHA-256: `acf1e76ac6a6ff85a2d95d7f02825d996992ded163941d57d822cbff63d2aee9`

음성을 교체할 때 원본과 `malbut_test` 배포 복사본을 함께 갱신한다.
`setup.py`의 `package_data`로 설치되며, 설치 누락은 `test_notice_package.py`가 검사한다.
