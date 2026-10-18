// 한국어 조사 고르기. 화면 문구에 "(으)로"를 그대로 보이지 않게 한다(웹 용어 목업 40번).

const DIGIT_FINAL: Record<string, number> = { 0: 21, 1: 8, 2: 0, 3: 16, 4: 0, 5: 0, 6: 1, 7: 8, 8: 8, 9: 0 };

/** "거실로", "주방으로": 마지막 글자 받침으로 고름(받침 없음·ㄹ → 로). 숫자는 읽는 소리, 그 밖은 "(으)로". */
export function withRo(name: string) {
  const last = name.trim().slice(-1);
  const code = last.charCodeAt(0);
  const final = code >= 0xac00 && code <= 0xd7a3 ? (code - 0xac00) % 28 : DIGIT_FINAL[last];
  if (final === undefined) return `${name}(으)로`;
  return final === 0 || final === 8 ? `${name}로` : `${name}으로`;
}
