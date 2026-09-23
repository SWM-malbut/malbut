import { listHomecamDevices } from '../../db/homecam';
import { requireChatGPTUser } from '../chatgpt-auth';
import { ScenarioAdminPanel } from '../components/scenario-admin-panel';

export const dynamic = 'force-dynamic';

type ScenarioAdminPageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export default async function ScenarioAdminPage({
  searchParams,
}: ScenarioAdminPageProps) {
  const user = await requireChatGPTUser('/scenario-admin');
  const requested = (await searchParams).device;
  const requestedDevice = Array.isArray(requested) ? requested[0] : requested;
  const devices = (await listHomecamDevices(user.email))
    .filter((device) => device.role === 'owner')
    .map((device) => ({
      id: device.id,
      displayName: device.displayName,
      online: device.online,
    }));
  return (
    <ScenarioAdminPanel
      devices={devices}
      initialDeviceId={requestedDevice ?? ''}
    />
  );
}
