// Copies the robot-to-web fall event contract into the web tree. The web image
// and the push broker are built from malbut_web only, so they import this copy;
// tests/fall-contract-source.test.mjs fails while it differs from the source.
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = fileURLToPath(new URL("../", import.meta.url));
export const SOURCE = path.resolve(web, "../malbut_interfaces/contracts/fall_events.json");
export const COPY = path.join(web, "infra/aws/push-broker/fall-contract.mjs");

export function render(source) {
  return "// Generated from malbut_interfaces/contracts/fall_events.json by\n" +
    "// scripts/sync-fall-contract.mjs. Do not edit; change the source and run it again.\n" +
    `export const FALL_CONTRACT = ${JSON.stringify(JSON.parse(source), null, 2)};\n`;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  writeFileSync(COPY, render(readFileSync(SOURCE, "utf8")));
  console.log(`wrote ${path.relative(web, COPY)}`);
}
