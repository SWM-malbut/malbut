// 목업 27번: 낙상 확인을 시작해서 끝날 때까지 지도 화면 "지금 말벗은" 카드와 막힌 이동 안내.
// The robot reports the stage in driveMode.detail.fall_check (Agent 낙상 규칙 상태).

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export type FallCheckView = { title: string; detail: string; target: { x: number; y: number } | null };

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
      ? view(target ? "직접 움직이기가 끝나면 의심 장소로 다시 가서 확인해요" : "직접 움직이기가 끝나면 낙상 확인을 이어 가요",
        alertedDetail)
      : view("직접 움직이기가 끝나면 낙상 확인을 이어 가요",
        "직접 움직이기는 끊지 않아요. 1분 안에 확인하지 못하면 보호자에게 긴급 알림을 보내요.");
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

export const FALL_CHECK_LOCK_NOTE = "낙상 확인 중이라 잠시 이동할 수 없어요. 직접 움직이기는 할 수 있어요.";
