import { redirect } from "next/navigation";
import { requireChatGPTUser } from "./chatgpt-auth";
import { HomecamApp } from "./components/homecam-app";
import { userHasHomecam } from "../db/homecam";

export const dynamic = "force-dynamic";

type HomePageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export default async function HomePage({ searchParams }: HomePageProps) {
  const returnTo = homeReturnPath(await searchParams);
  const localUiDemo =
    process.env.NODE_ENV !== "production" &&
    process.env.NEXT_PUBLIC_HOMECAM_UI_DEMO === "1";
  if (!localUiDemo) {
    const user = await requireChatGPTUser(returnTo);
    // First sign-in: "어떻게 불러 드릴까요?" before anything else.
    if (!user.chosenName) redirect(`/auth/name?${new URLSearchParams({ return_to: returnTo })}`);
    // No 말벗 yet: the next step is a registration code (or a guardian's invite link).
    if (!(await userHasHomecam(user.userId))) redirect("/register");
  }
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
