"use client";

import { useEffect, useState } from "react";
import {
  ClockCounterClockwise,
  DownloadSimple,
  GearSix,
  House,
  User,
} from "@phosphor-icons/react";

export type HomecamTab = "live" | "events" | "settings";

type AuthStatus = {
  authenticated: boolean;
  signInPath: string;
  signOutPath: string;
};

type HomecamHeaderProps = {
  activeTab: HomecamTab;
  onNavigate: (tab: HomecamTab) => void;
  onInstall?: () => void;
  showInstall?: boolean;
};

export function HomecamHeader({
  activeTab,
  onNavigate,
  onInstall,
  showInstall = false,
}: HomecamHeaderProps) {
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    void fetch("/api/auth/me", {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("AUTH_STATUS_UNAVAILABLE");
        const payload = (await response.json()) as Partial<AuthStatus>;
        return {
          authenticated: payload.authenticated === true,
          signInPath: safeSameOriginPath(payload.signInPath, "/auth/login?return_to=%2F"),
          signOutPath: safeSameOriginPath(payload.signOutPath, "/auth/logout?return_to=%2F"),
        };
      })
      .then((status) => {
        if (!controller.signal.aborted) setAuthStatus(status);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setAuthStatus({
            authenticated: false,
            signInPath: "/auth/login?return_to=%2F",
            signOutPath: "/auth/logout?return_to=%2F",
          });
        }
      });

    return () => controller.abort();
  }, []);

  return (
    <header className="homecam-header">
      <button
        type="button"
        className="homecam-brand"
        aria-label="MALBUT 홈캠 홈"
        onClick={() => onNavigate("live")}
      >
        <strong>/MALBUT</strong>
        <small>HOME CAMERA</small>
      </button>
      <nav className="homecam-nav" aria-label="홈캠 메뉴">
        <button
          type="button"
          className={activeTab === "live" ? "is-active" : ""}
          onClick={() => onNavigate("live")}
          aria-current={activeTab === "live" ? "page" : undefined}
        >
          <House size={18} weight={activeTab === "live" ? "fill" : "regular"} />
          <span>홈</span>
        </button>
        <button
          type="button"
          className={activeTab === "events" ? "is-active" : ""}
          onClick={() => onNavigate("events")}
          aria-current={activeTab === "events" ? "page" : undefined}
        >
          <ClockCounterClockwise
            size={18}
            weight={activeTab === "events" ? "fill" : "regular"}
          />
          <span>이벤트</span>
        </button>
        <button
          type="button"
          className={activeTab === "settings" ? "is-active" : ""}
          onClick={() => onNavigate("settings")}
          aria-current={activeTab === "settings" ? "page" : undefined}
        >
          <GearSix
            size={18}
            weight={activeTab === "settings" ? "fill" : "regular"}
          />
          <span>설정</span>
        </button>
      </nav>
      <div className="homecam-header-actions">
        {showInstall && onInstall && (
          <button type="button" className="homecam-install-button" onClick={onInstall}>
            <DownloadSimple size={15} weight="bold" />
            홈 화면에 설치
          </button>
        )}
        <a
          className="homecam-account-link"
          href={
            authStatus?.authenticated
              ? authStatus.signOutPath
              : authStatus?.signInPath ?? "/auth/login?return_to=%2F"
          }
          aria-label={
            authStatus?.authenticated
              ? "로그아웃"
              : authStatus === null
                ? "로그인 상태 확인 중"
                : "ID 로그인"
          }
        >
          <User
            size={16}
            weight={authStatus?.authenticated ? "fill" : "regular"}
            aria-hidden="true"
          />
          <span>{authStatus?.authenticated ? "내 계정" : "로그인"}</span>
        </a>
      </div>
    </header>
  );
}

function safeSameOriginPath(value: unknown, fallback: string): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) {
    return fallback;
  }
  return value;
}
