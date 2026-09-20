# 검출 비교 실행 위치

## 2026-09-18 확인

84개/2,224프레임 × 4조건 평가 완료 및 산출물 해시 확인.
s + 원본 비율 유지를 다음 검증의 우선 후보로 선택했고 운영 설정은 바꾸지 않았다.
새 요청·회귀 사례의 이미지 검토와 PyTorch/ONNX 실영상 대조도 진행했다.
상세 내용과 남은 검증은 `DETECTION_COMPARISON_RESULTS_20260918.md`를 따른다.

## 시작 시 기록 (아래 진행 상태는 당시 기준)

- SSH를 끊고 이동하려는 사용자 요청에 따라 SSH/Codex와 독립된 systemd 사용자 서비스로 재시작했다.
- 서비스: `malbut-pose-compare-20260917.service` (`systemctl --user`)
- 로그: `/home/jisanggeun/.local/share/malbut-evaluations/fall84-detection-comparison-20260917-background.log`
- 결과: `/home/jisanggeun/.local/share/malbut-evaluations/fall84-detection-comparison-20260917-r2`
- 전체 완료 확인: 서비스 종료 코드와 결과 폴더 `completed.json` 모두 확인해야 한다.
- 모델 2종 × 입력 방식 2종, 84개 전체 실제 추론. 방법은 `DETECTION_COMPARISON_PROTOCOL.md` 참고.
- `r1`은 26개 완료 후 SSH 독립 실행 전환을 위해 SIGINT로 중단했다. 중간 파일은 보존하며 최종 결과로 사용하지 않는다.
- Ubuntu의 로컬 X11 로그인 세션이 살아 있는 것을 확인했다. SSH만 끊어도 서비스는 유지된다.
  PC 종료·절전·모든 로컬 세션 로그아웃까지 지원하는 설치는 아니다. Mac 원본/SSH 마운트는 사용하지 않는다.
- 비율 유지/원본 좌표 복원 단위 테스트: 14 passed. 기존 첫 영상의 네 조건 추론과 기존 결과 재현 smoke 통과.
- 전체 점수는 완료 후 확인한다. 운영 감지 설정은 변경하지 않았다.
- 후속 검토 도구: `homecam_agent/scripts/review_pose_detection_comparison.py`.
  완료 결과 해시와 최종 GT를 검증하고, 요청 유무가 바뀐 모든 영상과 주요 미탐 사례를 JPG/HTML로 만든다.
  아직 실행하지 않았다. 모델 내보내기의 추가 실영상 PyTorch/ONNX 대조도 후속 확인 항목이다.
