"use client";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowsOut, CheckCircle } from "@phosphor-icons/react";
import { Viewer } from "../components/homecam-app";
import type { DemoView } from "../demo-feed-contract";
import { rehearsal, RehearsalCamera } from "./rehearsal";
import styles from "./screen.module.css";

const rehearsalKey = "malbut.demo.rehearsal.start";
const clock = new Intl.DateTimeFormat("ko-KR", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const ignoreExit = () => {};
export function DemoScreen({
  device,
  local,
}: {
  device: string;
  local: boolean;
}) {
  const router = useRouter();
  const [view, setView] = useState<DemoView | null>(null);
  const [preview, setPreview] = useState<ReturnType<typeof rehearsal> | null>(
    null,
  );
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const stop = useRef(false);
  const session = useRef<string | null>(null);
  const pending = useRef<Promise<void> | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const endpoint = "/api/devices/" + encodeURIComponent(device) + "/demo";

  useEffect(() => {
    if (!local) return;
    const saved = Number(sessionStorage.getItem(rehearsalKey));
    const start = saved > 0 && saved <= Date.now() ? saved : Date.now();
    sessionStorage.setItem(rehearsalKey, String(start));
    const tick = () => setPreview(rehearsal(start, Date.now()));
    tick();
    const timer = window.setInterval(tick, 150);
    return () => clearInterval(timer);
  }, [local]);

  useEffect(() => {
    if (local) return;
    let active = true;
    let nextRenew = 0;
    let timer: ReturnType<typeof setTimeout>;
    let inFlight: AbortController | null = null;
    async function poll() {
      const controller = new AbortController();
      inFlight = controller;
      const timeout = setTimeout(() => controller.abort(), 5000);
      const action = !session.current
        ? "open"
        : Date.now() >= nextRenew
          ? "renew"
          : "read";
      try {
        const response = await fetch(endpoint, {
          method: "POST",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action, session: session.current }),
          signal: controller.signal,
        });
        const data = await response.json();
        if (!active) return;
        if (!response.ok) {
          if (response.status === 409 && data.error === "EXPIRED") {
            nextRenew = 0;
            return;
          }
          if ([401, 403, 409].includes(response.status)) {
            stop.current = true;
            setBlocked(true);
            setView(null);
          }
          throw new Error(
            response.status === 409 ? "시연이 종료되었어요." : data.error,
          );
        }
        session.current = data.session;
        if (action !== "read") nextRenew = Date.now() + 4500;
        setView(data);
        setError("");
      } catch (e) {
        if (active) {
          setView((previous) => previous && { ...previous, badge: null, count: null });
          setError(controller.signal.aborted ? "시연 응답이 지연되고 있어요."
            : e instanceof Error ? e.message : "연결을 확인해 주세요.");
        }
      } finally {
        clearTimeout(timeout);
        inFlight = null;
        if (active && !stop.current) timer = setTimeout(schedule, 1000);
      }
    }
    function schedule() {
      if (!stop.current) pending.current = poll();
    }
    schedule();
    return () => {
      active = false;
      clearTimeout(timer);
      inFlight?.abort();
    };
  }, [endpoint, local]);

  async function exit() {
    if (leaving) return;
    stop.current = true;
    setLeaving(true);
    await pending.current;
    if (local) sessionStorage.removeItem(rehearsalKey);
    else if (session.current && !blocked) {
      try {
        const res = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "exit", session: session.current }),
          signal: AbortSignal.timeout(5000),
        });
        if (!res.ok && res.status !== 409) throw new Error();
      } catch {
        setError("대화를 지우지 못했어요. 나가기를 다시 눌러 주세요.");
        setLeaving(false);
        return;
      }
    }
    router.replace("/");
  }
  const lines = (local ? preview : view)?.lines ?? [];
  const last = lines.at(-1)?.id;
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "nearest" });
  }, [last]);
  const badge = (local ? preview : view)?.badge;
  const count = local ? 1 : view?.count;
  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <h1>말벗</h1>
        <div>
          <button
            onClick={() =>
              void (
                document.fullscreenElement
                  ? document.exitFullscreen()
                  : document.documentElement.requestFullscreen()
              ).catch(() => setError("전체 화면을 사용할 수 없어요."))
            }
          >
            <ArrowsOut /> 전체 화면
          </button>
          <button disabled={leaving} onClick={() => void exit()}>
            <ArrowLeft /> 나가기
          </button>
        </div>
      </header>
      <div className={styles.layout}>
        <section className={styles.camera} aria-label="카메라">
          <div className={styles.tags}>
            <span>
              <i />
              {local || ready ? "실시간" : "연결 대기"}
            </span>
            <span>
              <i />
              Pose · {count == null ? "대기 중" : `사람 ${count}명`}
            </span>
          </div>
          <div className={styles.frame}>
            {local ? (
              <RehearsalCamera seconds={preview?.seconds ?? 0} />
            ) : (
              view &&
              !blocked && (
                <Viewer
                  cameraOnly
                  deviceId={device}
                  embedded
                  onMediaReadyChange={setReady}
                  onExit={ignoreExit}
                />
              )
            )}
            {badge && (
              <strong
                className={
                  badge === "도움 요청 없음" ? styles.ok : styles.alert
                }
                role="status"
              >
                {badge === "도움 요청 없음" && <CheckCircle weight="fill" />}
                {badge}
              </strong>
            )}
          </div>
        </section>
        <section className={styles.chat} aria-label="대화">
          <header>
            <h2>대화</h2>
            <small>사용자: AI가 받은 말 · 말벗: 소리가 나올 때</small>
          </header>
          {error && (
            <p role="alert" className={styles.error}>
              {error}
            </p>
          )}
          <div className={styles.history} role="log">
            {!lines.length && (
              <p className={styles.empty}>대화가 시작되면 여기에 보여요.</p>
            )}
            {lines.map((line) => (
              <div
                key={line.id}
                className={
                  line.speaker === "robot" ? styles.robot : styles.user
                }
              >
                {line.speaker === "robot" && (
                  <>
                    <i>말벗</i>
                    <b>말벗</b>
                  </>
                )}
                <div className={styles.message}>
                  <p>
                    {line.confirmation && line.speaker === "robot" && (
                      <em>낙상 확인</em>
                    )}
                    {line.text}
                  </p>
                  <time dateTime={new Date(line.at).toISOString()}>
                    {clock.format(line.at)}
                  </time>
                </div>
              </div>
            ))}
            <div ref={bottom} />
          </div>
        </section>
      </div>
    </main>
  );
}
