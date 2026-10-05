import { getD1 } from ".";
import { ensureDatabaseSchema } from "./migration-state";

/** Login methods that identify a person. Social providers are added with social login. */
export type IdentityProvider = "email";

export type UserLabel = { userId: string; name: string };

const UNNAMED_USER = "이름 없는 사용자";

/**
 * The user behind a login identity, created on first sight. Email identities are
 * also created ahead of login when an owner invites a guardian by email.
 */
export async function ensureUserForIdentity(provider: IdentityProvider, subject: string) {
  await ensureDatabaseSchema();
  const normalized = provider === "email" ? subject.trim().toLowerCase() : subject;
  if (!normalized) throw new Error("USER_IDENTITY_INVALID");
  const existing = await findUserIdForIdentity(provider, normalized);
  if (existing) return existing;
  const userId = crypto.randomUUID();
  // A concurrent first request may insert the same identity; the loser keeps the winner's user.
  const created = await getD1()
    .prepare(
      `WITH new_user AS (INSERT INTO users (id) VALUES (?) RETURNING id)
       INSERT INTO user_identities (provider, subject, user_id)
       SELECT ?, ?, id FROM new_user
       ON CONFLICT (provider, subject) DO NOTHING
       RETURNING user_id`,
    )
    .bind(userId, provider, normalized)
    .first<{ user_id: string }>();
  if (created) return created.user_id;
  await getD1().prepare("DELETE FROM users WHERE id = ?").bind(userId).run();
  const winner = await findUserIdForIdentity(provider, normalized);
  if (!winner) throw new Error("USER_IDENTITY_CONFLICT");
  return winner;
}

export async function findUserIdForIdentity(provider: IdentityProvider, subject: string) {
  await ensureDatabaseSchema();
  const row = await getD1()
    .prepare("SELECT user_id FROM user_identities WHERE provider = ? AND subject = ?")
    .bind(provider, provider === "email" ? subject.trim().toLowerCase() : subject)
    .first<{ user_id: string }>();
  return row?.user_id ?? null;
}

/**
 * Names shown to other people: the chosen display name, or the login email while
 * a person has not chosen one yet.
 */
export async function userLabels(userIds: Iterable<string | null | undefined>) {
  const ids = [...new Set([...userIds].filter((id): id is string => Boolean(id)))];
  const labels = new Map<string, string>();
  if (ids.length === 0) return labels;
  await ensureDatabaseSchema();
  const { results } = await getD1()
    .prepare(
      `SELECT u.id, u.display_name, MIN(i.subject) AS email
       FROM users u
       LEFT JOIN user_identities i ON i.user_id = u.id AND i.provider = 'email'
       WHERE u.id = ANY(?)
       GROUP BY u.id, u.display_name`,
    )
    .bind(ids)
    .all<{ id: string; display_name: string | null; email: string | null }>();
  for (const row of results) labels.set(row.id, row.display_name ?? row.email ?? UNNAMED_USER);
  return labels;
}

export function labelFor(labels: Map<string, string>, userId: string | null | undefined) {
  if (!userId) return null;
  return labels.get(userId) ?? UNNAMED_USER;
}
