import { OPINION_LABELS, setFallOpinion, type OpinionLabel } from "../../../../../../../db/fall-review";
import { noStore } from "../../../../../../api-response";
import { deliverPendingFallNotice } from "../../../../../../fall-event-push";
import { fallMember, fallReviewFailure } from "../../../../../../fall-review-route";
import { sameOriginJsonRequest } from "../../../../../../same-origin-request";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string; incidentId: string }> };

function parseOpinion(value: unknown): { label: OpinionLabel | null; memo: string | null } | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  if (!Object.keys(v).every((k) => k === "label" || k === "memo")) return null;
  const label = v.label ?? null;
  if (label !== null && !(OPINION_LABELS as readonly unknown[]).includes(label)) return null;
  let memo = v.memo ?? null;
  if (memo !== null) {
    if (typeof memo !== "string") return null;
    memo = memo.trim() || null;
    if (memo !== null && (memo as string).length > 500) return null;
  }
  // Clearing the label (toggle off) clears the memo too.
  return { label: label as OpinionLabel | null, memo: label === null ? null : memo as string | null };
}

/** Set, change or clear (label: null) this user's opinion. Never changes the automatic judgment. */
export async function PUT(request: Request, context: Context) {
  const { deviceId, incidentId } = await context.params;
  const member = await fallMember(request, deviceId, incidentId);
  if (member.response) return member.response;
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const opinion = parseOpinion(await request.json().catch(() => null));
  if (!opinion) return noStore({ error: "의견 형식을 확인해 주세요." }, 400);
  try {
    const result = await setFallOpinion(deviceId, incidentId, member.userId, opinion.label, opinion.memo);
    if (result.noticeId) {
      // Saved first; delivery failures are retried by the maintenance worker.
      await deliverPendingFallNotice({ deviceId, noticeId: result.noticeId }).catch(() => undefined);
    }
    return noStore({ saved: true, reopened: result.reopened });
  } catch (error) { return fallReviewFailure(error); }
}
