"use client";

import { GearSix, House, MapTrifold, VideoCamera, Warning } from "@phosphor-icons/react";
import type { HomecamTab } from "./homecam-header";

// 새 디자인 아래 탭 (앱 UI 개편 목업). 개발자 화면은 설정 › 개발자 메뉴에서 연다.
const TABS: Array<{ tab: HomecamTab; label: string; Icon: typeof House }> = [
  { tab: "home", label: "홈", Icon: House },
  { tab: "live", label: "홈캠", Icon: VideoCamera },
  { tab: "events", label: "사건", Icon: Warning },
  { tab: "map", label: "지도", Icon: MapTrifold },
  { tab: "settings", label: "설정", Icon: GearSix },
];

export function UiTabBar({ activeTab, onNavigate }: {
  activeTab: HomecamTab;
  onNavigate: (tab: HomecamTab) => void;
}) {
  return (
    <nav className="ui-tabbar" aria-label="주요 화면">
      {TABS.map(({ tab, label, Icon }) => {
        const active = activeTab === tab;
        return (
          <button type="button" key={tab} className={active ? "is-active" : ""}
            aria-current={active ? "page" : undefined} onClick={() => onNavigate(tab)}>
            <Icon size={22} weight={active ? "bold" : "regular"} aria-hidden="true" />
            <span>{label}</span>
          </button>
        );
      })}
    </nav>
  );
}
