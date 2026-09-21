import { X509Certificate } from "node:crypto";
import { readFileSync } from "node:fs";

const MAX_CA_BUNDLE_BYTES = 512 * 1024;
const CERTIFICATE_PATTERN =
  /-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/g;

/**
 * Resolve the TLS settings shared by the Next.js runtime and migration CLI.
 * The CA is intentionally loaded from an immutable image asset instead of a
 * CloudFormation parameter or secret: public CA certificates are not secret
 * material, and CloudFormation string parameters have a 4 KiB value limit.
 *
 * @param {NodeJS.ProcessEnv | Record<string, string | undefined>} runtime
 * @returns {false | { rejectUnauthorized: boolean, ca?: string }}
 */
export function postgresSsl(runtime = process.env) {
  const mode = (runtime.DATABASE_SSL_MODE ?? "disable").trim().toLowerCase();
  if (mode === "disable") return false;
  if (mode === "require") return { rejectUnauthorized: false };
  if (mode !== "verify-full") {
    throw new Error(
      "DATABASE_SSL_MODE must be one of disable, require, or verify-full.",
    );
  }

  const caFile = runtime.DATABASE_SSL_CA_FILE?.trim();
  if (!caFile) {
    throw new Error(
      "DATABASE_SSL_CA_FILE is required when DATABASE_SSL_MODE=verify-full.",
    );
  }

  let ca;
  try {
    ca = readFileSync(caFile, "utf8");
  } catch (error) {
    throw new Error("DATABASE_SSL_CA_FILE could not be read.", { cause: error });
  }
  validateCaBundle(ca);

  return { rejectUnauthorized: true, ca };
}

function validateCaBundle(ca) {
  if (Buffer.byteLength(ca, "utf8") > MAX_CA_BUNDLE_BYTES) {
    throw new Error("DATABASE_SSL_CA_FILE exceeds the allowed size.");
  }

  const certificates = ca.match(CERTIFICATE_PATTERN) ?? [];
  const remainder = ca.replace(CERTIFICATE_PATTERN, "").trim();
  if (certificates.length === 0 || remainder) {
    throw new Error("DATABASE_SSL_CA_FILE is not a valid PEM certificate bundle.");
  }

  const now = Date.now();
  for (const certificatePem of certificates) {
    let certificate;
    try {
      certificate = new X509Certificate(certificatePem);
    } catch (error) {
      throw new Error("DATABASE_SSL_CA_FILE contains an invalid certificate.", {
        cause: error,
      });
    }
    if (!certificate.ca) {
      throw new Error("DATABASE_SSL_CA_FILE contains a non-CA certificate.");
    }
    if (
      !Number.isFinite(Date.parse(certificate.validFrom)) ||
      !Number.isFinite(Date.parse(certificate.validTo)) ||
      Date.parse(certificate.validFrom) > now ||
      Date.parse(certificate.validTo) <= now
    ) {
      throw new Error("DATABASE_SSL_CA_FILE contains an inactive certificate.");
    }
  }
}
