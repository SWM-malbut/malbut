// Before a key is saved, the server asks the service once whether this key works for
// what the 말벗 will do with it (2026-10-06 결정): OpenAI and Ollama get one very short
// request to the model the robot actually uses; KMA gets one current-weather lookup.
// The key is sent only to the service it belongs to and never logged.

export type KeyCheckService = "openai" | "kma" | "fall";
export type KeyCheck = "ok" | "invalid" | "quota" | "no_model" | "unavailable";

export const DEFAULT_OPENAI_MODEL = "gpt-5.6-luna";
export const DEFAULT_FALL_MODEL = "gemma4:31b";
const KMA_ENDPOINT = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst";
const TIMEOUT_MS = 10_000;

let fetchImpl: typeof fetch | undefined;
/** Tests answer the services in-process. */
export function setKeyCheckFetchForTest(value: typeof fetch | undefined) {
  fetchImpl = value;
}

async function call(url: string, init: RequestInit) {
  return (fetchImpl ?? fetch)(url, { ...init, redirect: "error", signal: AbortSignal.timeout(TIMEOUT_MS) });
}

const errorCode = async (response: Response) => {
  const body = (await response.json().catch(() => null)) as { error?: { code?: unknown; type?: unknown } } | null;
  return [body?.error?.code, body?.error?.type].filter((value): value is string => typeof value === "string");
};

/**
 * OpenAI: one response of at most 16 tokens. Authentication comes before request
 * validation, so a 400 still means the key was accepted.
 */
async function checkOpenAi(apiKey: string, model: string): Promise<KeyCheck> {
  const response = await call("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: { authorization: `Bearer ${apiKey}`, "content-type": "application/json" },
    body: JSON.stringify({ model, input: "1", max_output_tokens: 16 }),
  });
  if (response.ok) return "ok";
  if (response.status === 401 || response.status === 403) return "invalid";
  if (response.status === 400 || response.status === 404) {
    return response.status === 404 || (await errorCode(response)).includes("model_not_found") ? "no_model" : "ok";
  }
  if (response.status === 429) return (await errorCode(response)).includes("insufficient_quota") ? "quota" : "unavailable";
  return "unavailable";
}

/** Ollama Cloud: one chat turn that may answer with a single token. */
async function checkOllama(apiKey: string, model: string): Promise<KeyCheck> {
  const response = await call("https://ollama.com/api/chat", {
    method: "POST",
    headers: { authorization: `Bearer ${apiKey}`, "content-type": "application/json" },
    body: JSON.stringify({ model, messages: [{ role: "user", content: "1" }], stream: false, options: { num_predict: 1 } }),
  });
  if (response.ok || response.status === 400) return "ok";
  if (response.status === 401 || response.status === 403) return "invalid";
  if (response.status === 404) return "no_model";
  if (response.status === 429) return "quota";
  return "unavailable";
}

/** KMA reason codes, as the robot's weather client reads them (weather_kma.py). */
function kmaCode(code: string | null | undefined): KeyCheck {
  if (code === "00" || code === "03") return "ok"; // 03: no data for that hour, but the key works
  if (code && ["20", "30", "31", "32", "33"].includes(code)) return "invalid";
  if (code === "22" || code === "23") return "quota";
  return "unavailable";
}

/** Base hour of the latest current-weather report, Seoul time, published ten minutes after the hour. */
function kmaBase(now: Date) {
  const seoul = new Date(now.getTime() + 9 * 3600_000 - 10 * 60_000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    date: `${seoul.getUTCFullYear()}${pad(seoul.getUTCMonth() + 1)}${pad(seoul.getUTCDate())}`,
    time: `${pad(seoul.getUTCHours())}00`,
  };
}

async function checkKma(apiKey: string, now: Date): Promise<KeyCheck> {
  // Owners paste either the encoded or the decoded key; the robot decodes it the same way.
  let key = apiKey.trim();
  try { key = decodeURIComponent(key); } catch { /* keep as pasted */ }
  const base = kmaBase(now);
  const query = new URLSearchParams({
    serviceKey: key, pageNo: "1", numOfRows: "10", dataType: "JSON",
    base_date: base.date, base_time: base.time, nx: "60", ny: "127",
  });
  const response = await call(`${KMA_ENDPOINT}?${query}`, { method: "GET" });
  if (response.status === 401 || response.status === 403) return "invalid";
  if (response.status === 429) return "quota";
  if (!response.ok) return "unavailable";
  const text = (await response.text()).slice(0, 65_536);
  // Key problems come back as XML whatever dataType asks for.
  if (text.trimStart().startsWith("<")) return kmaCode(/<returnReasonCode>\s*(\d+)\s*</.exec(text)?.[1]);
  try {
    const body = JSON.parse(text) as { response?: { header?: { resultCode?: unknown } } };
    const code = body.response?.header?.resultCode;
    return kmaCode(typeof code === "string" ? code : null);
  } catch {
    return "unavailable";
  }
}

export async function checkServiceKey(input: {
  service: KeyCheckService; apiKey: string; model?: string | null; now?: Date;
}): Promise<KeyCheck> {
  try {
    if (input.service === "openai") return await checkOpenAi(input.apiKey, input.model || DEFAULT_OPENAI_MODEL);
    if (input.service === "fall") return await checkOllama(input.apiKey, input.model || DEFAULT_FALL_MODEL);
    return await checkKma(input.apiKey, input.now ?? new Date());
  } catch {
    // Timeout, network or redirect: we could not tell, so nothing is saved.
    return "unavailable";
  }
}

/** What the owner reads when a key is not saved (목업 13, 저장 실패). */
export function keyCheckMessage(service: KeyCheckService, result: Exclude<KeyCheck, "ok">, model: string) {
  if (result === "unavailable") return "지금 키를 확인할 수 없어요. 저장하지 않았어요. 잠시 뒤 다시 저장해 주세요.";
  if (result === "no_model") return `이 키로는 말벗이 쓰는 모델(${model})을 쓸 수 없어요. 저장하지 않았어요.`;
  if (result === "quota") {
    return service === "kma"
      ? "오늘 사용량을 넘은 키예요. 저장하지 않았어요. 내일 다시 저장해 주세요."
      : "요금 한도가 찼거나 사용량을 넘은 키예요. 저장하지 않았어요.";
  }
  return service === "kma"
    ? "쓸 수 없는 키예요. 저장하지 않았어요. 막 받은 키라면 쓸 수 있게 되기까지 시간이 걸려요. 잠시 뒤 다시 저장해 주세요."
    : "쓸 수 없는 키예요. 저장하지 않았어요. 키를 다시 확인해 주세요.";
}
