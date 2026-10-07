# 최신 main 위 PR 검증

기준은 `origin/main`의 `723c901d82fc4b38f9caefc08e938fcec71307f5`이다.
보존용 snapshot 전체가 아니라 통합 커밋 `43b2b5d`의 변경만 이식했다.
최신 main의 독립 모듈 Bringup, 기본 미확인 지도, AutoSLAM의 SLAM 소유권,
웹 userId 인증·지도 라벨·벽 편집·연속 녹화 화면은 유지한다.

## 현재 코드 검증

- Mac Agent 전체 오프라인 + DeviceOperations 단위: 3372 passed, 35 skipped,
  71 subtests passed. 실제 사용자 음성 또는 LLM/API 호출 시험은 아니다.
- Ubuntu ARM64 / ROS 2 Humble: 현재 PR 소스로 인터페이스·Manager 2개 패키지를
  새 workspace에서 빌드했다. Manager 전체 ROS·lint 219/219 통과.
  Bringup도 별도로 새 소스에서 빌드했다. Agent ROS 계약 38/38 통과.
  Bringup 전체 614/614 통과(ament metadata deprecation warning 2건).
- 웹 전체 255/255, 전체 TypeScript와 변경 파일 ESLint 통과.
  미디어 설정·heartbeat 네이티브 C++ 16/16 통과. SQL은 PGlite에서
  전체 0001→0024 migration, 재실행 무변경 및 userId 권한·위임 철회·재시도를 확인했다.
  이번 코드의 별도 PostgreSQL 서버 다중 연결 동시성 및 전체 ROS 미디어 빌드는 미실행이다.
- 과거 실패 10개와 관련된 Agent 테스트 3개 파일을 기준 main에서 별도 검증:
  34/34 통과. 과거 snapshot의 실패를 현재 PR의 알려진 실패로 취급하지 않는다.

정지 회귀에는 대기 중인 중립·이동 입력 폐기, 지도 전환 전 좌표의 원래 지도 바인딩,
Manager 정지 서비스 미확인 시 통합 종료 거부를 추가했다.

## 현재 배포 상태와 범위

사용자 요청으로 이전 로봇 통합 검증용 프로세스·overlay·workspace는 제거했으며,
통합 소스는 Mac에 보존했다. PR 준비에서는 로봇에 재접속하거나 배포하지 않았다.
`README.md`와 JSON의 실제 로봇·LLM 기록은 이전 코드에 대한 역사적 증거다.
현재 PR의 실제 주행·마이크·스피커 검증이나 프로덕션 웹 배포는 수행하지 않았다.

향후 운영 배포는 `0024_voice_agent.sql`, 웹 API/UI, 생성 ROS 인터페이스,
Manager·Bringup·Agent·미디어 agent를 호환 버전으로 함께 적용해야 한다.
