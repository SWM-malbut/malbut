import { getPostgresPool } from "../../../db/postgres";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    await getPostgresPool().query("SELECT 1");
    return Response.json(
      { status: "ok" },
      { headers: { "cache-control": "no-store" } },
    );
  } catch {
    return Response.json(
      { status: "unavailable" },
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }
}
