// 목업 27번: 낙상 확인을 시작해서 끝날 때까지 지도 화면 "지금 말벗은" 카드와 막힌 이동 안내.
// The robot reports the stage in driveMode.detail.fall_check (Agent 낙상 규칙 상태).

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
export type FallCheckView = { title: string; detail: string; target: { x: number; y: number } | null };

// 목업 32번: 처음이 아니라 다시 가는 이유(purpose)별 문구.
const WHY: Record<string, [string, string]> = {
  revisit: ["기다리던 일이 끝나 의심 장소로 다시 가고 있어요",
    "1분 안에 확인하지 못해 보호자에게 긴급 알림을 보냈어요. 처음부터 같은 순서로 확인해요."],
  recheck: ["30분 전에 바닥에서 확인한 분을 다시 보러 가고 있어요", "바닥에 있는 동안은 30분마다 같은 순서로 다시 확인해요."],
  stood_up: ["확인을 마친 분이 일어나서 다시 보러 가고 있어요", "일어선 뒤 괜찮은지 같은 순서로 다시 확인해요."],
  again: ["사람이 없던 자리에 누워 있는 분이 보여 다시 가고 있어요", "처음 확인 때는 그 자리가 비어 있었어요. 처음부터 다시 확인해요."],
};
// 목업 32번: 확인 단계(step)별 문구.
const STEP: Record<string, [string, string]> = {
  compare: ["처음 봤을 때와 지금을 비교하고 있어요",
    "의심 장소 1 m 앞에 멈춰서, 처음 본 분이 그대로 있는지 AI가 사진 두 장으로 비교해요."],
  reverify: ["처음 본 자리로 가서 다시 보고 있어요",
    "1 m 앞에서는 그 자리가 비어 보였어요. 처음 본 자리에서 같은 방향으로 한 번 더 봐요."],
  departure: ["무엇이 그 자리를 떠났는지 보고 있어요", "가는 동안 찍힌 사진으로 AI가 걸어서 떠났는지, 기어서 떠났는지 봐요."],
  view: ["몸 전체가 보이는 자리로 옮기고 있어요", "1 m 앞에서는 다리만 보일 때가 많아요. 조금 물러나거나 옆으로 돌아가요."],
  posture: ["몸 전체를 보고 자세를 확인하고 있어요", "지금 자세와, 넘어지는 장면을 봤다면 그 장면도 AI가 같이 봐요."],
  closer: ["물어보려고 1 m 앞으로 가고 있어요", "멀면 대답이 잘 안 들려요. 1 m까지 못 가면 갈 수 있는 가장 가까운 자리에서 물어요."],
  ask: ["낙상 의심 상황을 말로 묻고 있어요", "대답을 들으면 확인이 끝나요. 끝나면 다시 이동할 수 있어요."],
};

/** 목업 27번 "지금 말벗은": 낙상 확인의 단계와 기다리는 이유. Null when no fall check is running. */
export function fallCheckView(detail: Record<string, unknown> | null | undefined): FallCheckView | null {
  if (detail?.fall_check_active !== true) return null;
  const check = isRecord(detail.fall_check) ? detail.fall_check : {};
  const target = isRecord(check.target) && typeof check.target.x === "number" && Number.isFinite(check.target.x) &&
    typeof check.target.y === "number" && Number.isFinite(check.target.y)
    ? { x: check.target.x, y: check.target.y } : null;
  const patrol = detail.last_patrol;
  // 이번 확인 때문에 순찰을 멈췄고 아직 결과가 없다(말벗은 "pending"을 보낸다).
  const patrolStopped = isRecord(patrol) && patrol.outcome === "fall_check" &&
    (!patrol.fall_result || patrol.fall_result === "pending");
  const alerted = check.alerted === true;
  const alertedDetail = target
    ? "1분 안에 확인하지 못해 보호자에게 긴급 알림을 보냈어요. 그새 움직였을 수 있어 묻기 전에 다시 봐요."
    : "1분 안에 확인하지 못해 보호자에게 긴급 알림을 보냈어요. 끝나면 바로 물어봐요.";
  const view = (title: string, detail: string) => ({ title, detail, target });
  // 목업 32번: 하나로 합친 확인은 단계(step)와 가는 이유(purpose)도 보낸다. 기다림·돌아가기는 아래 그대로.
  const step = typeof check.step === "string" ? check.step : null;
  const purpose = typeof check.purpose === "string" ? check.purpose : null;
  if (step && check.phase !== "wait" && check.phase !== "return") {
    if (step === "approach" && purpose && Object.hasOwn(WHY, purpose)) return view(...WHY[purpose]);
    if (step === "approach") {
      return view("낙상 의심 장소를 확인하러 가고 있어요",
        `처음 본 쪽에서 1 m 앞까지 가요.${patrolStopped ? " 순찰은 멈췄어요." : ""}`);
    }
    if (Object.hasOwn(STEP, step)) return view(...STEP[step]);
  }
  if (check.phase === "approach") {
    return view("낙상 의심 장소를 확인하러 가고 있어요",
      `${patrolStopped ? "순찰은 멈췄어요. " : ""}확인이 끝나면 원래 자리로 돌아와요.`);
  }
  if (check.phase === "look") {
    return view("낙상 의심 장소 앞에서 확인하고 있어요",
      "멈춰 있지만 확인하는 중이에요. 몸 전체가 안 보이면 조금 물러나서 다시 봐요.");
  }
  if (check.phase === "ask") {
    return view("낙상 의심 상황을 말로 묻고 있어요", "대답을 들으면 확인이 끝나요. 끝나면 다시 이동할 수 있어요.");
  }
  if (check.phase === "return") return view("확인을 마치고 원래 자리로 돌아가고 있어요", "도착하면 다시 이동할 수 있어요.");
  if (check.phase === "wait" && check.waiting_for === "manual_drive") {
    return alerted
      ? view(target ? "수동 조작이 끝나면 의심 장소로 다시 가서 확인해요" : "수동 조작이 끝나면 낙상 확인을 이어 가요",
        alertedDetail)
      : view("수동 조작이 끝나면 낙상 확인을 이어 가요",
        "수동 조작은 끊지 않아요. 1분 안에 확인하지 못하면 보호자에게 긴급 알림을 보내요.");
  }
  if (check.phase === "wait") {
    return alerted
      ? view(target ? "다른 일이 끝나면 의심 장소로 다시 가서 확인해요" : "다른 일이 끝나면 낙상 확인을 이어 가요",
        alertedDetail)
      : view("다른 일이 끝나면 낙상 확인을 이어 가요",
        "말벗이 켜지는 중이거나 위치를 다시 잡는 중이에요. 1분 안에 확인하지 못하면 보호자에게 긴급 알림을 보내요.");
  }
  return view("낙상 의심을 확인하고 있어요", "확인이 끝나면 다시 이동할 수 있어요.");
}
