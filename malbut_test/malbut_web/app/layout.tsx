import type { Metadata, Viewport } from "next";
import "pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css";
import "@fontsource/source-sans-3/400.css";
import "@fontsource/source-sans-3/500.css";
import "@fontsource/source-sans-3/600.css";
import "@fontsource/source-sans-3/700.css";
import { PwaBootstrap } from "./components/pwa-bootstrap";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(process.env.AUTH_PUBLIC_ORIGIN ?? "http://localhost:3000"),
  applicationName: "말벗 홈캠",
  title: "말벗 홈캠",
  description: "우리 집 말벗의 실시간 영상과 낙상 알림을 가족과 함께 봐요.",
  keywords: ["말벗", "말벗 홈캠", "홈캠", "낙상 알림"],
  referrer: "no-referrer",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "말벗 홈캠",
  },
  formatDetection: {
    telephone: false,
  },
  icons: {
    icon: "/favicon.svg",
    apple: "/homecam-icon.svg",
  },
  openGraph: {
    title: "말벗 홈캠",
    description: "우리 집 말벗의 실시간 영상과 낙상 알림을 가족과 함께 봐요.",
    images: [
      {
        url: "/og.png",
        width: 1732,
        height: 908,
        alt: "클라우드와 보호자 휴대전화에 연결된 바퀴 달린 홈캠 로봇",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    images: ["/og.png"],
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#f4efe7",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko">
      <body>
        {children}
        <PwaBootstrap />
      </body>
    </html>
  );
}
