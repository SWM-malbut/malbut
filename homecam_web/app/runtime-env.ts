export type RuntimeEnvironment = Readonly<Record<string, string | undefined>>;

/**
 * Server-side environment adapter.
 *
 * Keeping environment access behind this function makes the Node runtime
 * explicit and prevents AWS-only configuration from being bundled into the
 * browser by accident.
 */
export function getRuntimeEnvironment(): RuntimeEnvironment {
  if (typeof process === "undefined" || !process.env) {
    throw new Error("NODE_RUNTIME_ENVIRONMENT_UNAVAILABLE");
  }
  return process.env;
}

export function getRuntimeValue(name: string): string | undefined {
  const value = getRuntimeEnvironment()[name]?.trim();
  return value || undefined;
}
