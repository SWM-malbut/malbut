'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';
import { ArrowLeft, PersonSimpleWalk, StopCircle } from '@phosphor-icons/react';

type AdminDevice = {
  id: string;
  displayName: string;
  online: boolean;
};

type ScenarioAdminPanelProps = {
  devices: AdminDevice[];
  initialDeviceId: string;
};

type DemoOperation = 'demo_person_show' | 'demo_person_hide';

type RobotCommandSnapshot = {
  id?: string;
  status?: string;
  result?: { error?: string; message?: string } | null;
};

const delay = (milliseconds: number) => new Promise((resolve) => {
  window.setTimeout(resolve, milliseconds);
});

export function ScenarioAdminPanel({
  devices,
  initialDeviceId,
}: ScenarioAdminPanelProps) {
  const initial = devices.some((device) => device.id === initialDeviceId)
    ? initialDeviceId
    : devices[0]?.id ?? '';
  const [deviceId, setDeviceId] = useState(initial);
  const [busy, setBusy] = useState<DemoOperation | null>(null);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const device = useMemo(
    () => devices.find((candidate) => candidate.id === deviceId) ?? null,
    [deviceId, devices],
  );

  const send = async (operation: DemoOperation) => {
    if (!device || busy) return;
    setBusy(operation);
    setNotice('');
    setError('');
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(device.id)}/robot/commands`,
        {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ operation, payload: {} }),
        },
      );
      const body = await response.json().catch(() => ({})) as {
        error?: string;
        command?: RobotCommandSnapshot;
      };
      if (!response.ok) {
        throw new Error(body.error || '시연 명령을 등록하지 못했습니다.');
      }
      const commandId = body.command?.id;
      if (!commandId) throw new Error('시연 명령 번호를 받지 못했습니다.');
      const result = await waitForCommand(device.id, commandId);
      if (result.status === 'failed') {
        throw new Error(
          result.result?.error || '로봇이 시연 명령을 실행하지 못했습니다.',
        );
      }
      setNotice(
        result.result?.message || (
          operation === 'demo_person_show'
            ? '현관 진입 경로에서 사람 한 명이 등장했습니다.'
            : '사람 모델을 제거했습니다.'
        ),
      );
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : '시연 명령을 등록하지 못했습니다.',
      );
    } finally {
      setBusy(null);
    }
  };

  const waitForCommand = async (
    selectedDeviceId: string,
    commandId: string,
  ): Promise<RobotCommandSnapshot> => {
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      await delay(500);
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDeviceId)}/robot`,
        { cache: 'no-store' },
      );
      if (!response.ok) continue;
      const snapshot = await response.json() as {
        command?: RobotCommandSnapshot | null;
      };
      if (snapshot.command?.id !== commandId) continue;
      if (['completed', 'failed'].includes(snapshot.command.status ?? '')) {
        return snapshot.command;
      }
    }
    throw new Error('로봇의 시연 명령 완료 응답이 지연되고 있습니다.');
  };

  return (
    <main className="scenario-admin-page">
      <section className="scenario-admin-card">
        <Link className="scenario-admin-back" href="/">
          <ArrowLeft size={18} /> 홈캠으로 돌아가기
        </Link>
        <div className="scenario-admin-heading">
          <span>SIMULATION ADMIN</span>
          <h1>시연 관리자</h1>
          <p>
            일반 지도·주행 기능과 분리된 Gazebo 전용 도구입니다.
            사람은 버튼을 누를 때만 현관 경로에서 등장합니다.
          </p>
        </div>

        {devices.length === 0 ? (
          <p className="scenario-admin-error">
            관리할 수 있는 소유자 장치가 없습니다.
          </p>
        ) : (
          <>
            <label className="scenario-admin-device">
              <span>대상 장치</span>
              <select
                value={deviceId}
                onChange={(event) => setDeviceId(event.target.value)}
              >
                {devices.map((candidate) => (
                  <option key={candidate.id} value={candidate.id}>
                    {candidate.displayName} · {candidate.online ? '온라인' : '오프라인'}
                  </option>
                ))}
              </select>
            </label>

            <div className="scenario-admin-actions">
              <button
                type="button"
                onClick={() => void send('demo_person_show')}
                disabled={!device?.online || busy !== null}
              >
                <PersonSimpleWalk size={22} />
                <strong>사람 등장</strong>
                <small>중복 없이 현관 경로에서 사람 한 명만 유지</small>
              </button>
              <button
                type="button"
                className="is-danger"
                onClick={() => void send('demo_person_hide')}
                disabled={!device?.online || busy !== null}
              >
                <StopCircle size={22} />
                <strong>사람 퇴장</strong>
                <small>현재 시뮬레이션의 사람 모델을 확실히 제거</small>
              </button>
            </div>
            {!device?.online && (
              <p className="scenario-admin-error">
                로봇 연결이 오프라인입니다. cloud_robot_sync 연결을 확인하세요.
              </p>
            )}
            {notice && <p className="scenario-admin-notice">{notice}</p>}
            {error && <p className="scenario-admin-error">{error}</p>}
          </>
        )}
      </section>
    </main>
  );
}
