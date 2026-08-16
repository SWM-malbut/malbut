import { createHash, randomBytes, randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const DEVICE_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const SOURCE_PROFILES = new Set(["sim", "aurora", "unknown"]);
const DAY_MS = 24 * 60 * 60 * 1000;

const options = parseArguments(process.argv.slice(2));
if (options.help) {
  process.stdout.write(helpText());
  process.exit(0);
}

const input = validateOptions(options);
const now = new Date();
const credentialId = randomUUID();
const deviceToken = `hc1.${credentialId}.${randomBytes(32).toString("hex")}`;
const tokenDigest = sha256(deviceToken);
const credentialExpiresAt = new Date(
  now.getTime() + input.credentialDays * DAY_MS,
).toISOString();
const provisioningExpiresAt = new Date(
  now.getTime() + input.windowMinutes * 60_000,
).toISOString();
const manifest = {
  deviceId: input.deviceId,
  displayName: input.displayName,
  ownerEmail: input.ownerEmail,
  sourceProfile: input.sourceProfile,
  credential: {
    id: credentialId,
    label: input.label,
    tokenDigest,
    expiresAt: credentialExpiresAt,
  },
};
const manifestSha256 = sha256(JSON.stringify(manifest));
const outputDirectory = path.resolve(input.outputDirectory);

await mkdir(outputDirectory, { recursive: true, mode: 0o700 });
const manifestPath = path.join(outputDirectory, "manifest.json");
const tokenPath = path.join(outputDirectory, "device-token");
const runtimeValuesPath = path.join(outputDirectory, "runtime-values.json");

await writePrivateFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
await writePrivateFile(tokenPath, `${deviceToken}\n`);
await writePrivateFile(
  runtimeValuesPath,
  `${JSON.stringify(
    {
      DEVICE_PROVISIONING_MANIFEST_SHA256: manifestSha256,
      DEVICE_PROVISIONING_EXPIRES_AT: provisioningExpiresAt,
    },
    null,
    2,
  )}\n`,
);

process.stdout.write(
  [
    `Provisioning bundle created: ${outputDirectory}`,
    `Manifest: ${manifestPath}`,
    `Device token: ${tokenPath}`,
    `CDK/runtime values: ${runtimeValuesPath}`,
    `Provisioning expires at: ${provisioningExpiresAt}`,
    "The device token was not printed. Keep the output directory private.",
  ].join("\n") + "\n",
);

async function writePrivateFile(filePath, contents) {
  await writeFile(filePath, contents, { encoding: "utf8", mode: 0o600, flag: "wx" });
}

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function parseArguments(arguments_) {
  const result = {};
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (argument === "--help" || argument === "-h") {
      result.help = true;
      continue;
    }
    if (!argument.startsWith("--")) fail(`Unexpected argument: ${argument}`);
    const key = argument.slice(2);
    const value = arguments_[index + 1];
    if (!value || value.startsWith("--")) fail(`Missing value for --${key}`);
    if (key in result) fail(`Duplicate argument: --${key}`);
    result[key] = value;
    index += 1;
  }
  return result;
}

function validateOptions(options_) {
  const allowed = new Set([
    "device-id",
    "display-name",
    "owner-email",
    "source-profile",
    "label",
    "credential-days",
    "window-minutes",
    "output",
    "help",
  ]);
  for (const key of Object.keys(options_)) {
    if (!allowed.has(key)) fail(`Unknown argument: --${key}`);
  }

  const deviceId = required(options_, "device-id");
  const displayName = required(options_, "display-name");
  const ownerEmail = required(options_, "owner-email").trim().toLowerCase();
  const sourceProfile = options_["source-profile"] ?? "sim";
  const label = options_.label ?? `${sourceProfile}-device`;
  const credentialDays = positiveInteger(
    options_["credential-days"] ?? "365",
    "credential-days",
    366,
  );
  const windowMinutes = positiveInteger(
    options_["window-minutes"] ?? "60",
    "window-minutes",
    24 * 60,
  );
  const outputDirectory =
    options_.output ?? path.join(".local", "provisioning", deviceId);

  if (!DEVICE_ID_PATTERN.test(deviceId)) {
    fail("--device-id must be lowercase letters, digits, or internal hyphens (1-64 characters)");
  }
  if (
    displayName !== displayName.trim() ||
    displayName.length < 1 ||
    displayName.length > 80 ||
    /[\u0000-\u001f\u007f]/.test(displayName)
  ) {
    fail("--display-name must be trimmed, printable, and at most 80 characters");
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(ownerEmail) || ownerEmail.length > 254) {
    fail("--owner-email is invalid");
  }
  if (!SOURCE_PROFILES.has(sourceProfile)) {
    fail("--source-profile must be sim, aurora, or unknown");
  }
  if (
    label !== label.trim() ||
    label.length < 1 ||
    label.length > 100 ||
    /[\u0000-\u001f\u007f]/.test(label)
  ) {
    fail("--label must be trimmed, printable, and at most 100 characters");
  }
  if (!outputDirectory.trim()) fail("--output cannot be empty");

  return {
    deviceId,
    displayName,
    ownerEmail,
    sourceProfile,
    label,
    credentialDays,
    windowMinutes,
    outputDirectory,
  };
}

function required(options_, key) {
  const value = options_[key];
  if (!value) fail(`--${key} is required`);
  return value;
}

function positiveInteger(value, name, maximum) {
  if (!/^\d+$/.test(value)) fail(`--${name} must be a positive integer`);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > maximum) {
    fail(`--${name} must be between 1 and ${maximum}`);
  }
  return parsed;
}

function fail(message) {
  process.stderr.write(`${message}\n\n${helpText()}`);
  process.exit(2);
}

function helpText() {
  return `Usage:
  node scripts/create-provisioning-bundle.mjs \\
    --device-id malbut-sim-01 \\
    --display-name "MALBUT simulator" \\
    --owner-email owner@example.com \\
    [--source-profile sim|aurora|unknown] \\
    [--credential-days 365] [--window-minutes 60] \\
    [--output .local/provisioning/malbut-sim-01]

Creates mode-0600 manifest, device-token, and runtime-values files without
printing the device bearer token.
`;
}
