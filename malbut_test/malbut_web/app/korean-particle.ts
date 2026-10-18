// 한국어 조사 고르기. 화면 문구에 "(으)로"를 그대로 보이지 않게 한다(웹 용어 목업 40번).

const DIGIT_FINAL: Record<string, number> = { 0: 21, 1: 8, 2: 0, 3: 16, 4: 0, 5: 0, 6: 1, 7: 8, 8: 8, 9: 0 };

function finalOf(word: string): number | undefined {
  const last = word.trim().slice(-1);
  const code = last.charCodeAt(0);
  return code >= 0xac00 && code <= 0xd7a3 ? (code - 0xac00) % 28 : DIGIT_FINAL[last];
}

/** "거실로", "주방으로": 마지막 글자 받침으로 고름(받침 없음·ㄹ → 로). 숫자는 읽는 소리, 그 밖은 "(으)로". */
export function withRo(name: string) {
  const final = finalOf(name);
  if (final === undefined) return `${name}(으)로`;
  return final === 0 || final === 8 ? `${name}로` : `${name}으로`;
}

// [받침 없을 때, 있을 때]. "로"는 ㄹ 받침에도 "로".
const PAIRS: Array<[string, string]> = [["로", "으로"], ["가", "이"], ["는", "은"], ["를", "을"], ["와", "과"]];

/** 낱말 뒤 조사를 받침에 맞게 고름(바닥 + 로 → 바닥으로). 고를 수 없으면 받은 그대로. */
export function josa(word: string, particle: string) {
  const pair = PAIRS.find((p) => p.includes(particle));
  const final = finalOf(word);
  if (!pair || final === undefined) return `${word}${particle}`;
  return `${word}${final === 0 || (pair[0] === "로" && final === 8) ? pair[0] : pair[1]}`;
}
