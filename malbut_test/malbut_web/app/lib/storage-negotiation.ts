export type StorageParticipantRole = "MASTER" | "VIEWER";
export type StorageMediaKind = "audio" | "video";
export type StorageTransceiverDirection = "sendonly" | "recvonly" | "sendrecv";

export function storageTransceiverDirection(
  role: StorageParticipantRole,
  kind: StorageMediaKind,
  hasLocalAudio: boolean,
): StorageTransceiverDirection {
  if (kind === "video") {
    return role === "MASTER" ? "sendonly" : "recvonly";
  }
  if (role === "MASTER" || hasLocalAudio) return "sendrecv";
  return "recvonly";
}

type MediaSection = {
  kind: StorageMediaKind;
  payloadTypes: string[];
  direction: string;
  codecs: Map<string, string>;
};

function parseMediaSections(sdp: string): MediaSection[] {
  const sections: MediaSection[] = [];
  let current: MediaSection | null = null;

  for (const rawLine of sdp.split(/\r?\n/)) {
    const line = rawLine.trim();
    const media = /^m=(audio|video)\s+\d+\s+\S+\s+(.+)$/i.exec(line);
    if (media) {
      current = {
        kind: media[1].toLowerCase() as StorageMediaKind,
        payloadTypes: media[2].trim().split(/\s+/),
        direction: "sendrecv",
        codecs: new Map(),
      };
      sections.push(current);
      continue;
    }
    if (!current) continue;

    const direction = /^a=(sendonly|recvonly|sendrecv|inactive)$/i.exec(line);
    if (direction) {
      current.direction = direction[1].toLowerCase();
      continue;
    }
    const codec = /^a=rtpmap:(\d+)\s+([^/\s]+)/i.exec(line);
    if (codec) current.codecs.set(codec[1], codec[2].toLowerCase());
  }
  return sections;
}

export function assertStorageAnswerSdp(
  sdp: string | undefined,
  role: StorageParticipantRole,
  hasLocalAudio: boolean,
): void {
  if (!sdp) throw new Error("AWS 저장 세션 SDP 응답이 비어 있습니다.");
  const sections = parseMediaSections(sdp);

  for (const kind of ["video", "audio"] as const) {
    const section = sections.find((entry) => entry.kind === kind);
    if (!section) {
      throw new Error(`AWS 저장 세션 SDP에 ${kind} 트랙이 없습니다.`);
    }
    const requiredDirection = storageTransceiverDirection(
      role,
      kind,
      hasLocalAudio,
    );
    if (section.direction !== requiredDirection) {
      throw new Error(
        `AWS 저장 세션 ${kind} 방향은 ${requiredDirection}이어야 하지만 ${section.direction}입니다.`,
      );
    }

    const primaryCodecs = section.payloadTypes
      .map((payloadType) => section.codecs.get(payloadType))
      .filter((codec): codec is string => Boolean(codec))
      .filter((codec) => !["rtx", "red", "ulpfec", "flexfec-03"].includes(codec));
    const requiredCodec = kind === "video" ? "h264" : "opus";
    if (
      primaryCodecs.length === 0 ||
      primaryCodecs.some((codec) => codec !== requiredCodec)
    ) {
      throw new Error(
        `AWS 저장 세션 ${kind} 코덱은 ${requiredCodec.toUpperCase()}만 사용할 수 있습니다.`,
      );
    }
  }
}
