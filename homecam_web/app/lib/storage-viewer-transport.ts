export type StorageViewerTransport = "webrtc" | "hls";

/**
 * AWS documents Chrome as the supported browser for WebRTC media storage.
 * Other browsers consume the same archived stream through KVS HLS instead of
 * attempting a storage WebRTC negotiation that can connect without media.
 */
export function storageViewerTransport(userAgent: string): StorageViewerTransport {
  const isDesktopChrome =
    /\bChrome\/\d+/i.test(userAgent) &&
    !/\b(?:Edg|OPR|CriOS)\/\d+/i.test(userAgent);
  return isDesktopChrome ? "webrtc" : "hls";
}
