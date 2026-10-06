/**
 * 지도 탭 › 새 지도 만들기 (SWM25-237): the name people see and the robot's file name.
 * The robot stores maps under ASCII file names only; the shown name may be Korean.
 */
export function twoDigits(value: number) {
  return String(value).padStart(2, "0");
}

/** 자동 이름 "지도 10월 7일 14:30"과 말벗 파일 이름 map-20261007-1430(겹치면 -2…). */
export function newMapNames(now: Date, taken: Set<string>) {
  const label = `지도 ${now.getMonth() + 1}월 ${now.getDate()}일 ${twoDigits(now.getHours())}:${twoDigits(now.getMinutes())}`;
  const base = `map-${now.getFullYear()}${twoDigits(now.getMonth() + 1)}${twoDigits(now.getDate())}-${twoDigits(now.getHours())}${twoDigits(now.getMinutes())}`;
  let stem = base;
  for (let suffix = 2; taken.has(`${stem}.yaml`) || taken.has(`${stem}.yml`); suffix += 1) stem = `${base}-${suffix}`;
  return { label, stem };
}
