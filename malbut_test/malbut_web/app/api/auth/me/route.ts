import { getRequestUserEmail } from "../../../server-auth";
import {
  chatGPTSignInPath,
  chatGPTSignOutPath,
} from "../../../chatgpt-auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const authenticated = Boolean(await getRequestUserEmail(request));
  return Response.json(
    {
      authenticated,
      signInPath: chatGPTSignInPath("/"),
      signOutPath: chatGPTSignOutPath("/"),
    },
    { headers: { "cache-control": "no-store" } },
  );
}
