-- Reviewed projection used by the portable SWM25-69~74 audit report.
-- Source evidence:
--   SWM25-69_74_300X_OFFLINE_2026-08-12.json
-- The first result set backs the comparison chart. The second backs the
-- story-verdict audit table. Values are frozen to the 2026-08-12 snapshot.

WITH repeated_checks(
    story,
    iterations_attempted,
    iterations_passed,
    subchecks_per_iteration,
    subcheck_invocations,
    failed_invocations,
    evidence_type
) AS (
    VALUES
        ('SWM25-69', 300, 300, 6, 1800, 0, 'implementation boundary'),
        ('SWM25-70', 300, 300, 6, 1800, 0, 'implementation boundary'),
        ('SWM25-71', 300, 300, 6, 1800, 0, 'implementation boundary'),
        ('SWM25-72', 300, 300, 8, 2400, 0, 'implementation boundary'),
        ('SWM25-73', 300, 300, 6, 1800, 0, 'implementation boundary'),
        ('SWM25-74', 300, 300, 3,  900, 0, 'negative evidence only')
)
SELECT
    story,
    iterations_attempted,
    iterations_passed,
    subchecks_per_iteration,
    subcheck_invocations,
    failed_invocations,
    evidence_type
FROM repeated_checks
ORDER BY story;

WITH story_verdicts(
    story,
    verified_scope,
    product_verdict,
    jira_recommendation,
    evidence_type
) AS (
    VALUES
        (
            'SWM25-69',
            '책임·인터페이스 계약',
            '계약 범위 완료',
            '완료 유지 + 계약 전용 표기',
            'implementation boundary'
        ),
        (
            'SWM25-70',
            '단일 프로세스 세션 MVP',
            '제한 범위 완료',
            '완료 유지 + 운영 후속 분리',
            'implementation boundary'
        ),
        (
            'SWM25-71',
            '내부 context builder',
            '내부 범위 완료',
            '완료 유지 + 제품 연동 후속 분리',
            'implementation boundary'
        ),
        (
            'SWM25-72',
            'provider 코드·오프라인 계약',
            '실 API 배포 보류',
            '부분 완료 또는 배포 검증 중',
            'implementation boundary'
        ),
        (
            'SWM25-73',
            '비부작용 Tool Gateway',
            '제한 범위 완료',
            '완료 유지 + ROS 실행 아님 표기',
            'implementation boundary'
        ),
        (
            'SWM25-74',
            '실행 부재·차단 경계',
            '구현 근거 없음',
            '할 일로 되돌림',
            'negative evidence only'
        )
)
SELECT
    story,
    verified_scope,
    product_verdict,
    jira_recommendation,
    evidence_type
FROM story_verdicts
ORDER BY story;
