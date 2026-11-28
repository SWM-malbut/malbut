import type { DemoBadge, DemoLine } from "../demo-feed-contract";

const script = [
  [
    10,
    "robot",
    "괜찮으세요? 넘어지신 것 같아요. 도움이 필요하면 말씀해 주세요.",
  ],
  [15, "user", "괜찮아요."],
  [
    17,
    "robot",
    "다행이에요. 천천히 일어나세요. 도움이 필요하면 언제든 불러 주세요.",
  ],
  [30, "user", "긴장 풀 방법 하나만 알려줘."],
  [33, "robot", "어깨를 편하게 내리고 천천히 숨을 내쉬어 보세요."],
] as const;

export function rehearsal(start: number, now: number) {
  const elapsed = Math.max(0, now - start);
  const cycle = Math.floor(elapsed / 48000);
  const seconds = (elapsed % 48000) / 1000;
  const lines: DemoLine[] = [];
  for (let i = Math.max(0, cycle - 75); i <= cycle; i++) {
    for (const [sec, speaker, text] of script) {
      const at = start + i * 48000 + sec * 1000;
      if (at <= now && now - at < 3600000)
        lines.push({
          id: String(at),
          at,
          speaker,
          text,
          confirmation: sec < 20,
        });
    }
  }
  const badge: DemoBadge | null =
    seconds < 7 || seconds >= 27
      ? null
      : seconds < 10
        ? "낙상 의심"
        : seconds < 17
          ? "음성 확인 중"
          : "도움 요청 없음";
  return { lines, badge, seconds };
}

// Normalized rehearsal scene only. The live scene comes from the robot's video track.
export function RehearsalCamera({ seconds }: { seconds: number }) {
  const tilt = Math.min(1, Math.max(0, (seconds - 6) / 1.05));
  const upright = seconds > 27 ? 1 - Math.min(1, (seconds - 27) / 1.2) : 1;
  const fall = tilt * upright;
  const color = fall > 0.8 ? "#ff5d50" : "#f7f5ec";
  const limbs = [
    "0,-130 0,-68 0,2",
    "-18,-125 -16,-66 -17,5",
    "0,-130 -38,-114 -48,-89",
    "-18,-125 10,-110 20,-82",
    "0,-68 2,3 0,65",
    "-16,-66 -14,5 -16,68",
  ];
  return (
    <svg
      viewBox="0 0 640 400"
      role="img"
      aria-label="로컬 낙상 시연 애니메이션"
    >
      <rect width="640" height="400" fill="#d2d6cd" />
      <rect x="0" y="245" width="640" height="155" fill="#a18e6e" />
      {[0, 80, 160, 240, 320, 400, 480, 560, 640].map((x) => (
        <path
          key={x}
          d={`M ${x} 400 L ${320 + (x - 320) * 0.45} 245`}
          stroke="#b3a188"
          strokeWidth="2"
        />
      ))}
      <rect
        x="44"
        y="40"
        width="152"
        height="128"
        fill="#eef2ed"
        stroke="#a4afa6"
        strokeWidth="5"
      />
      <rect x="460" y="150" width="151" height="85" fill="#728078" />
      <rect x="454" y="135" width="165" height="31" fill="#5c685f" />
      <g
        transform={`translate(${318 + 48 * fall} ${258 + 61 * fall}) rotate(${-89 * fall})`}
      >
        {fall > 0.8 && (
          <rect
            x="-57"
            y="-178"
            width="94"
            height="257"
            rx="8"
            fill="none"
            stroke={color}
            strokeWidth="3"
          />
        )}
        <circle cy="-151" cx="-10" r="13" fill="#e5b892" />
        {limbs.map((points) => (
          <g key={points}>
            <polyline
              points={points}
              fill="none"
              stroke="#354354"
              strokeWidth="12"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <polyline
              points={points}
              fill="none"
              stroke={color}
              strokeWidth="2.4"
            />
            {points.split(" ").map((p) => (
              <circle
                key={p}
                cx={p.split(",")[0]}
                cy={p.split(",")[1]}
                r="3.6"
                fill={color}
                stroke="white"
              />
            ))}
          </g>
        ))}
      </g>
      <rect x="521" y="367" width="113" height="27" fill="#39372c" />
      <text
        x="578"
        y="385"
        textAnchor="middle"
        fill="white"
        fontSize="12"
        fontWeight="bold"
      >
        LOCAL DEMO
      </text>
    </svg>
  );
}
