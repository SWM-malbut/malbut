import { AsyncLocalStorage } from "node:async_hooks";
import {
  Pool as PostgresPool,
  types,
  type Pool,
  type PoolClient,
  type QueryResultRow,
} from "pg";
import { postgresPlaceholders } from "./sql-compat";

const GLOBAL_POOL = Symbol.for("malbut.homecam.postgres.pool");
const testPoolContext = new AsyncLocalStorage<PostgresPoolTestAdapter>();

type HomecamGlobal = typeof globalThis & {
  [GLOBAL_POOL]?: Pool;
};

/**
 * Small pool surface used only by repository integration tests. Async-local
 * scoping prevents concurrent tests from leaking an in-memory database into
 * another request or into the production singleton path.
 */
export type PostgresPoolTestAdapter = {
  query: Pool["query"];
  connect(): Promise<Pick<PoolClient, "query" | "release">>;
};

// Keep the values consumed by the application identical between raw pg and
// Drizzle queries. Homecam timestamps are exposed as ISO-8601 strings and
// aggregate counts are small enough to be represented safely as numbers.
types.setTypeParser(20, (value) => Number(value));
types.setTypeParser(1114, (value) => new Date(`${value}Z`).toISOString());
types.setTypeParser(1184, (value) => new Date(value).toISOString());

export function getPostgresPool(): Pool {
  const testPool = testPoolContext.getStore();
  if (testPool) return testPool as unknown as Pool;

  const globalState = globalThis as HomecamGlobal;
  if (globalState[GLOBAL_POOL]) return globalState[GLOBAL_POOL];

  const databaseUrl = resolveDatabaseUrl(process.env);
  if (!databaseUrl) {
    throw new Error(
      "DATABASE_URL is required. Run `npm run db:up` for local development or inject the RDS connection secret in AWS.",
    );
  }

  // Constructing a pool does not open a connection. Next.js can therefore
  // discover routes during the build without requiring a live database.
  const pool = new PostgresPool({
    connectionString: databaseUrl,
    max: positiveInteger(process.env.DATABASE_POOL_MAX, 10),
    idleTimeoutMillis: positiveInteger(process.env.DATABASE_IDLE_TIMEOUT_MS, 30_000),
    connectionTimeoutMillis: positiveInteger(
      process.env.DATABASE_CONNECT_TIMEOUT_MS,
      10_000,
    ),
    ssl: postgresSsl(),
  });

  globalState[GLOBAL_POOL] = pool;
  return pool;
}

export function withPostgresPoolForTest<T>(
  pool: PostgresPoolTestAdapter,
  operation: () => Promise<T>,
): Promise<T> {
  if (process.env.NODE_ENV === "production") {
    throw new Error("POSTGRES_TEST_POOL_DISABLED_IN_PRODUCTION");
  }
  return testPoolContext.run(pool, operation);
}

export function resolveDatabaseUrl(runtime: NodeJS.ProcessEnv): string | undefined {
  const configured = runtime.DATABASE_URL?.trim();
  if (configured) return configured;

  const host = runtime.DATABASE_HOST?.trim();
  const database = runtime.DATABASE_NAME?.trim();
  const username = runtime.DATABASE_USERNAME?.trim();
  const password = runtime.DATABASE_PASSWORD;
  if (!host || !database || !username || !password) return undefined;

  const port = runtime.DATABASE_PORT?.trim() || "5432";
  if (!/^\d{1,5}$/.test(port) || Number(port) > 65_535) {
    throw new Error("DATABASE_PORT is invalid.");
  }
  return (
    `postgresql://${encodeURIComponent(username)}:${encodeURIComponent(password)}` +
    `@${host}:${port}/${encodeURIComponent(database)}`
  );
}

export type SqlExecutor = Pick<Pool | PoolClient, "query">;

export async function queryRows<T extends QueryResultRow>(
  executor: SqlExecutor,
  sql: string,
  values: readonly unknown[] = [],
) {
  try {
    return await executor.query<T>(postgresPlaceholders(sql), [...values]);
  } catch (error) {
    throw normalizePostgresError(error);
  }
}

function postgresSsl(): false | { rejectUnauthorized: boolean; ca?: string } {
  const mode = (process.env.DATABASE_SSL_MODE ?? "disable").toLowerCase();
  if (mode === "disable") return false;
  if (mode === "require") return { rejectUnauthorized: false };
  if (mode !== "verify-full") {
    throw new Error(
      "DATABASE_SSL_MODE must be one of disable, require, or verify-full.",
    );
  }

  const encodedCa = process.env.DATABASE_SSL_CA_BASE64?.trim();
  if (!encodedCa) {
    throw new Error(
      "DATABASE_SSL_CA_BASE64 is required when DATABASE_SSL_MODE=verify-full.",
    );
  }
  return {
    rejectUnauthorized: true,
    ca: Buffer.from(encodedCa, "base64").toString("utf8"),
  };
}

function positiveInteger(value: string | undefined, fallback: number) {
  if (!value) return fallback;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function normalizePostgresError(error: unknown) {
  if (
    error &&
    typeof error === "object" &&
    "code" in error &&
    error.code === "23505"
  ) {
    const message = "message" in error ? String(error.message) : "duplicate key";
    return new Error(`UNIQUE_CONSTRAINT: ${message}`, { cause: error });
  }
  return error;
}
