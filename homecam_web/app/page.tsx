import { requireChatGPTUser } from "./chatgpt-auth";
import { HomecamApp } from "./components/homecam-app";

export const dynamic = "force-dynamic";

type HomePageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export default async function HomePage({ searchParams }: HomePageProps) {
  const returnTo = homeReturnPath(await searchParams);
  await requireChatGPTUser(returnTo);
  return <HomecamApp />;
}

function homeReturnPath(params: Record<string, string | string[] | undefined>) {
  const query = new URLSearchParams();
  for (const [name, rawValue] of Object.entries(params)) {
    for (const value of Array.isArray(rawValue) ? rawValue : [rawValue]) {
      if (typeof value === "string") query.append(name, value);
    }
  }
  const serialized = query.toString();
  return serialized ? `/?${serialized}` : "/";
}
