import { notFound } from "next/navigation";
import { requireChatGPTUser } from "../../../../chatgpt-auth";
import { userCanViewDevice } from "../../../../../db/homecam";
import { getPostgresPool } from "../../../../../db/postgres";
import { VoiceRecordingResult } from "../../../../components/voice-recording-result";

export const dynamic = "force-dynamic";

export default async function VoiceReferencePage({ params }: {
  params: Promise<{ deviceId: string; kind: string; referenceId: string }>;
}) {
  const { deviceId, kind, referenceId } = await params;
  if (!["recording", "fall", "event"].includes(kind) || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(referenceId)) notFound();
  const href = `/voice-results/${encodeURIComponent(deviceId)}/${kind}/${encodeURIComponent(referenceId)}`;
  const user = await requireChatGPTUser(href);
  if (!(await userCanViewDevice(deviceId, user.userId))) notFound();
  const back = `/?device=${encodeURIComponent(deviceId)}&view=robot`;
  if (kind === "recording") {
    const recording = (await getPostgresPool().query(`SELECT GREATEST(r.started_at,
      LEAST(COALESCE(r.ended_at,clock_timestamp()),clock_timestamp())-INTERVAL '1 minute',
      clock_timestamp()-INTERVAL '7 days'+INTERVAL '1 second') AS moment_at
      FROM recording_sessions r JOIN stream_sessions s ON s.id=r.session_id
      WHERE s.device_id=$1 AND r.session_id=$2 AND r.started_at IS NOT NULL
        AND COALESCE(r.ended_at,clock_timestamp())>clock_timestamp()-INTERVAL '7 days'`,
    [deviceId, referenceId])).rows[0];
    if (!recording) notFound();
    const momentAt = new Date(recording.moment_at).toISOString();
    return <main className="homecam-settings-card"><h1>음성으로 찾은 저장 영상</h1>
      <a href={back}>음성 요청·결과로 돌아가기</a>
      <VoiceRecordingResult deviceId={deviceId} momentAt={momentAt} />
    </main>;
  }
  if (kind === "event") {
    const event = (await getPostgresPool().query(`SELECT event_type,occurred_at,recording_session_id
      FROM homecam_events WHERE device_id=$1 AND id=$2`, [deviceId, referenceId])).rows[0];
    if (!event) notFound();
    const types: Record<string, string> = { motion: "움직임", person: "사람", dog: "강아지", cat: "고양이" };
    return <main className="homecam-settings-card"><h1>음성으로 찾은 감지 기록</h1>
      <p>{types[event.event_type] ?? "감지"} · {new Date(event.occurred_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}</p>
      {event.recording_session_id && <p><a href={`/voice-results/${encodeURIComponent(deviceId)}/recording/${encodeURIComponent(event.recording_session_id)}`}>
        연결된 저장 영상 열기
      </a></p>}
      <a href={back}>음성 요청·결과로 돌아가기</a>
    </main>;
  }
  const incident = (await getPostgresPool().query(`SELECT state,assessment,answer,occurred_at,updated_at
    FROM fall_incidents WHERE device_id=$1 AND incident_id=$2`, [deviceId, referenceId])).rows[0];
  if (!incident) notFound();
  const states: Record<string, string> = { verifying: "확인 중", recheck_required: "추가 확인 필요",
    help_required: "도움 필요", resolved: "확인 완료" };
  const assessments: Record<string, string> = { observed_fall: "낙상 관찰", suspected_fall: "낙상 의심",
    normal_activity: "일상 활동", unobservable: "관찰 불가" };
  const answers: Record<string, string> = { help_request: "도움 요청", okay: "괜찮다고 응답", unclear: "응답 불명확",
    no_response: "응답 없음", failed: "음성 확인 실패" };
  return <main className="homecam-settings-card"><h1>음성으로 찾은 낙상 기록</h1>
    <p>상태: {states[incident.state] ?? "확인 필요"}</p>
    <p>관찰 결과: {assessments[incident.assessment] ?? "미확인"} · 음성 응답: {answers[incident.answer] ?? "미확인"}</p>
    <p>발생 시각: {new Date(incident.occurred_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}</p>
    <p>최근 갱신: {new Date(incident.updated_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}</p>
    <a href={back}>음성 요청·결과로 돌아가기</a>
  </main>;
}
