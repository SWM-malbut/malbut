# Publication validation — 2026-10-31

이 변경은 `wip/local-code-handoff-20261028`에 병합된 #342 이후의 후속 작업이다.
아래 로컬 검증은 새 로봇 시험이나 ROS 빌드가 아니다.

|범위|결과|
|---|---:|
|현재 저장소 STT 전체|1,290 passed / 7 skipped / 0 failed, 64.89s|
|신규 checkpoint 실패 경계 검사|6 passed|
|보존한 guard 채택 도구의 가짜 서비스 검사|3 passed|
|이전 runtime19 fake-device 검사|56 passed|
|이전 runtime19 조합 코드|135개 SHA 유지; 17 overlay + 118 repository|
|C39 공개 소스 재구성|143개 기준 트리 → 패치 → 179개 결과 전체 SHA·파일집합 일치|

```sh
PYTHONPATH=malbut_stt:malbut_tts PYTHONDONTWRITEBYTECODE=1 \
  python3 -m pytest -p no:cacheprovider malbut_stt/test
python3 tools/speech_overlap_20261029/check_checkpoint.py --test
python3 tools/speech_overlap_20261030/check_checkpoint.py
python3 -m pytest -q tools/speech_overlap_20261030/tests
git diff --check
```

기존 runtime19의 fake-device 검사와 새 후보 소스 재구성 검사는 다른 범위다.
신규 6개 검사는 정상 재구성, 패치 오염, 원본 누락, 추가 파일, 무효 시험 집계 제외,
미완료 후보의 채택 주장 거부를 확인한다. 모델·오디오 장치·네트워크를 열지 않는다.

현재 소스의 전체 테스트 stdout은 별도 로컬 보존본의
`reports/pr-20261031-stt-tests.log`에 남겼다. 원문 해시는
`817712529626a090fb03ca67c5ce2ca40c9e8b95c03722fe2d99dda89b756697`이다.
개인 경로가 포함된 전체 로그는 공개하지 않았다. 같은 검사를 여러 번 더해
고유 테스트 수로 표현하지 않는다. 이후 GitHub CI 결과는 PR의 현재 commit 기준으로
따로 확인해야 하며, STT를 수집하지 않은 CI 성공은 STT 전체 통과 근거가 아니다.

실기기 결과는 `evidence/summary.json`에 버전·조건별로 분리했다.
C38 준비 실패와 로봇 단독 대조 실패는 사람 음원 재생이 없으므로 유효 겹침 분모에서
제외했다. C37의 4/5 문장 일치는 같은 문장의 소표본이며 전체 목표 완료가 아니다.
선택 음성 프로세스 CPU +1.91%p, 전체 시스템 busy CPU +3.49%p의 미달과
브리지 순수 계산 27.63% 절감을 각각 기록했다.

원음·DB·배열·모델·환경 파일·인증 자료는 제외하며 새 공개 텍스트의 비밀키 패턴과
개인 작업공간·내부 네트워크 주소를 점검한다. 보존 소스의 일반 로봇 설치·장치 경로는
그대로 유지한다. 실제 마이크 수음, 스피커 출력, 대화 정확도, 지연·자원 합격은
이 로컬 검증만으로 확인되지 않는다.
