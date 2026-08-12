import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const RECORDING_ID = "123e4567-e89b-42d3-a456-426614174000";
const AWS_HOST = "b-ca770835.kinesisvideo.ap-northeast-2.amazonaws.com";
const SESSION_TOKEN = "private-session-token~";
const SECRET = "test-broker-secret-with-enough-entropy";

async function loadHelper() {
  const source = await readFile(
    new URL("../app/recording-playback-proxy.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(
    `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}#${Date.now()}`
  );
}

test("creates an opaque same-origin recording URL and resolves authorized HLS resources", async () => {
  const helper = await loadHelper();
  const upstream = `https://${AWS_HOST}/hls/v1/getHLSMasterPlaylist.m3u8?SessionToken=${encodeURIComponent(SESSION_TOKEN)}`;
  const proxy = await helper.createRecordingPlaybackProxy(
    {
      requestUrl: `https://petcam.example.com/api/recordings/${RECORDING_ID}/playback`,
      playbackUrl: upstream,
      recordingId: RECORDING_ID,
      userEmail: "Owner@Example.com",
      expiresAt: new Date(Date.now() + 300_000).toISOString(),
    },
    SECRET,
  );

  const playbackUrl = new URL(proxy.playbackUrl);
  assert.equal(playbackUrl.origin, "https://petcam.example.com");
  assert.equal(playbackUrl.search, "");
  assert.doesNotMatch(proxy.playbackUrl, /SessionToken|kinesisvideo|private-session-token/);
  assert.match(proxy.setCookie, /^petcam_hls_[0-9a-f]{32}=/);
  assert.match(proxy.setCookie, /; HttpOnly/);
  assert.match(proxy.setCookie, /; SameSite=Strict/);
  assert.match(proxy.setCookie, /; Secure/);

  const playbackId = playbackUrl.pathname.split("/").at(-2);
  assert.match(playbackId, /^[0-9a-f]{32}$/);
  const cookie = proxy.setCookie.split(";", 1)[0];
  const resolvedMaster = await helper.resolveRecordingPlaybackProxy(
    {
      requestUrl: proxy.playbackUrl,
      recordingId: RECORDING_ID,
      playbackId,
      resource: "getHLSMasterPlaylist.m3u8",
      userEmail: "owner@example.com",
      cookieHeader: cookie,
    },
    SECRET,
  );
  assert.ok(resolvedMaster);
  assert.equal(resolvedMaster.upstreamUrl.hostname, AWS_HOST);
  assert.equal(resolvedMaster.upstreamUrl.pathname, "/hls/v1/getHLSMasterPlaylist.m3u8");
  assert.equal(resolvedMaster.upstreamUrl.searchParams.get("SessionToken"), SESSION_TOKEN);
  assert.equal(resolvedMaster.rewritePlaylist, true);

  const base = proxy.playbackUrl.slice(0, proxy.playbackUrl.lastIndexOf("/") + 1);
  const resolvedFragment = await helper.resolveRecordingPlaybackProxy(
    {
      requestUrl: `${base}getMP4MediaFragment.mp4?FragmentNumber=123456789&TrackNumber=1`,
      recordingId: RECORDING_ID,
      playbackId,
      resource: "getMP4MediaFragment.mp4",
      userEmail: "owner@example.com",
      cookieHeader: cookie,
    },
    SECRET,
  );
  assert.ok(resolvedFragment);
  assert.equal(resolvedFragment.rewritePlaylist, false);
  assert.equal(resolvedFragment.upstreamUrl.searchParams.get("FragmentNumber"), "123456789");
  assert.equal(resolvedFragment.upstreamUrl.searchParams.get("TrackNumber"), "1");

  const wrongUser = await helper.resolveRecordingPlaybackProxy(
    {
      requestUrl: proxy.playbackUrl,
      recordingId: RECORDING_ID,
      playbackId,
      resource: "getHLSMasterPlaylist.m3u8",
      userEmail: "other@example.com",
      cookieHeader: cookie,
    },
    SECRET,
  );
  assert.equal(wrongUser, null);

  const injectedQuery = await helper.resolveRecordingPlaybackProxy(
    {
      requestUrl: `${proxy.playbackUrl}?SessionToken=attacker-token`,
      recordingId: RECORDING_ID,
      playbackId,
      resource: "getHLSMasterPlaylist.m3u8",
      userEmail: "owner@example.com",
      cookieHeader: cookie,
    },
    SECRET,
  );
  assert.equal(injectedQuery, null);
});

test("rewrites AWS playlists to token-free same-origin relative resources", async () => {
  const helper = await loadHelper();
  const master = [
    "#EXTM3U",
    `#EXT-X-MEDIA:TYPE=AUDIO,URI=\"getHLSMediaPlaylist.m3u8?SessionToken=${SESSION_TOKEN}&TrackNumber=2\"`,
    "#EXT-X-STREAM-INF:BANDWIDTH=928838,AUDIO=\"audio\"",
    `getHLSMediaPlaylist.m3u8?SessionToken=${SESSION_TOKEN}&TrackNumber=1`,
    "",
  ].join("\n");
  const rewrittenMaster = helper.rewriteRecordingPlaylist(
    master,
    AWS_HOST,
    SESSION_TOKEN,
  );
  assert.doesNotMatch(rewrittenMaster, /SessionToken|private-session-token|kinesisvideo/);
  assert.match(rewrittenMaster, /getHLSMediaPlaylist\.m3u8\?TrackNumber=2/);
  assert.match(rewrittenMaster, /getHLSMediaPlaylist\.m3u8\?TrackNumber=1/);

  const media = [
    "#EXTM3U",
    `#EXT-X-MAP:URI=\"getMP4InitFragment.mp4?SessionToken=${SESSION_TOKEN}&TrackNumber=1\"`,
    "#EXTINF:10.0,",
    `getMP4MediaFragment.mp4?SessionToken=${SESSION_TOKEN}&FragmentNumber=123456789&TrackNumber=1`,
    "",
  ].join("\n");
  const rewrittenMedia = helper.rewriteRecordingPlaylist(media, AWS_HOST, SESSION_TOKEN);
  assert.doesNotMatch(rewrittenMedia, /SessionToken|private-session-token|kinesisvideo/);
  assert.match(rewrittenMedia, /getMP4InitFragment\.mp4\?TrackNumber=1/);
  assert.match(
    rewrittenMedia,
    /getMP4MediaFragment\.mp4\?FragmentNumber=123456789&TrackNumber=1/,
  );

  assert.throws(
    () => helper.rewriteRecordingPlaylist(
      `https://evil.example/getHLSMediaPlaylist.m3u8?SessionToken=${SESSION_TOKEN}&TrackNumber=1`,
      AWS_HOST,
      SESSION_TOKEN,
    ),
    /PLAYBACK_PLAYLIST_INVALID/,
  );
});
