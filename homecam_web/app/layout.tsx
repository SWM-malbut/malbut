import type { Metadata, Viewport } from "next";
import "@fontsource/source-sans-3/400.css";
import "@fontsource/source-sans-3/500.css";
import "@fontsource/source-sans-3/600.css";
import "@fontsource/source-sans-3/700.css";
import { PwaBootstrap } from "./components/pwa-bootstrap";
import "./globals.css";

const legacyProductMetadata = {
  title: "PETCAM | 보호자 실시간 펫 카메라",
} as const;

export const metadata: Metadata = {
  metadataBase: new URL(process.env.AUTH_PUBLIC_ORIGIN ?? "http://localhost:3000"),
  applicationName: "MALBUT 홈캠",
  title: "MALBUT 홈캠 | 우리 집을 가까이",
  description: "이동형 홈캠의 실시간 영상, PTT, AI 이벤트와 최근 7일 녹화를 가족과 안전하게 확인합니다.",
  keywords: [legacyProductMetadata.title, "MALBUT", "홈캠", "AWS KVS WebRTC"],
  referrer: "no-referrer",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "MALBUT 홈캠",
  },
  formatDetection: {
    telephone: false,
  },
  icons: {
    icon: "/favicon.svg",
    apple: "/homecam-icon.svg",
  },
  openGraph: {
    title: "MALBUT 홈캠 | 우리 집을 가까이",
    description: "실시간 영상과 양방향 음성, AI 이벤트와 최근 7일 녹화를 한곳에서 확인합니다.",
    images: [
      {
        url: "/og.png",
        width: 1732,
        height: 908,
        alt: "클라우드와 보호자 휴대전화에 연결된 PETCAM 펫 로봇",
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
  themeColor: "#a3a799",
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
