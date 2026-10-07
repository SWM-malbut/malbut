"use client";

import { useRouter } from "next/navigation";
import { FallTimelinePanel } from "./fall-timeline-panel";

export function VoiceRecordingResult({ deviceId, momentAt }: { deviceId: string; momentAt: string }) {
  const router = useRouter();
  const back = `/?device=${encodeURIComponent(deviceId)}&view=robot`;
  return <FallTimelinePanel deviceId={deviceId} mode={{ kind: "report", momentAt }}
    onBack={() => router.push(back)}
    onOpenIncident={(incidentId) => router.push(
      `/?device=${encodeURIComponent(deviceId)}&view=events&incident=${encodeURIComponent(incidentId)}`)} />;
}
