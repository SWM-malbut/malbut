import { getPostgresPool } from "./postgres";

const REQUIRED_MIGRATION = "0004_robot_map_semantics";
const schemaReadiness = new WeakMap<object, Promise<void>>();

export function ensureDatabaseSchema() {
  const pool = getPostgresPool();
  const existing = schemaReadiness.get(pool);
  if (existing) return existing;

  const readiness = pool
      .query(
        `SELECT 1 FROM homecam_schema_migrations
         WHERE version = $1`,
        [REQUIRED_MIGRATION],
      )
      .then((result) => {
        if (!result.rowCount) throw new Error("DATABASE_MIGRATION_REQUIRED");
      })
      .catch((error: unknown) => {
        schemaReadiness.delete(pool);
        if (
          error &&
          typeof error === "object" &&
          "code" in error &&
          error.code === "42P01"
        ) {
          throw new Error("DATABASE_MIGRATION_REQUIRED", { cause: error });
        }
        throw error;
      });
  schemaReadiness.set(pool, readiness);
  return readiness;
}
