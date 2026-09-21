import { drizzle } from "drizzle-orm/node-postgres";
import type { QueryResultRow } from "pg";
import * as schema from "./schema";
import { getPostgresPool, queryRows, type SqlExecutor } from "./postgres";

type D1Result<T> = {
  success: true;
  results: T[];
  meta: { changes: number };
};

class PostgresPreparedStatement {
  constructor(
    private readonly sql: string,
    private readonly bindings: readonly unknown[] = [],
    private readonly executor?: SqlExecutor,
  ) {}

  bind(...bindings: unknown[]) {
    return new PostgresPreparedStatement(this.sql, bindings, this.executor);
  }

  withExecutor(executor: SqlExecutor) {
    return new PostgresPreparedStatement(this.sql, this.bindings, executor);
  }

  async first<T>(): Promise<T | null> {
    const result = await this.execute<QueryResultRow>();
    return (result.rows[0] as T | undefined) ?? null;
  }

  async all<T extends QueryResultRow>(): Promise<D1Result<T>> {
    const result = await this.execute<T>();
    return {
      success: true,
      results: result.rows,
      meta: { changes: result.rowCount ?? 0 },
    };
  }

  async run(): Promise<D1Result<QueryResultRow>> {
    const result = await this.execute<QueryResultRow>();
    return {
      success: true,
      results: result.rows,
      meta: { changes: result.rowCount ?? 0 },
    };
  }

  private execute<T extends QueryResultRow>() {
    return queryRows<T>(
      this.executor ?? getPostgresPool(),
      this.sql,
      this.bindings,
    );
  }
}

class PostgresD1Compatibility {
  prepare(sql: string) {
    return new PostgresPreparedStatement(sql);
  }

  async batch(statements: PostgresPreparedStatement[]) {
    const pool = getPostgresPool();
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      const results = [];
      for (const statement of statements) {
        results.push(await statement.withExecutor(client).run());
      }
      await client.query("COMMIT");
      return results;
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }
}

const compatibilityDatabase = new PostgresD1Compatibility();

/**
 * Compatibility surface for the existing repository queries. It deliberately
 * keeps the old name so the device/API contract can be ported without a risky
 * all-at-once rewrite; the implementation is PostgreSQL, not Cloudflare D1.
 */
export function getD1() {
  return compatibilityDatabase;
}

export function getDb() {
  return drizzle(getPostgresPool(), { schema });
}
