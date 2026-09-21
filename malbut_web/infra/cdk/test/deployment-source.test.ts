import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import test from "node:test";
import { App, IgnoreMode, Stack, SymlinkFollowMode } from "aws-cdk-lib";
import { Asset } from "aws-cdk-lib/aws-s3-assets";
import { DEPLOYMENT_SOURCE_EXCLUDES, resolveDeploymentSource } from "../lib/deployment-source";

test("deployment sources stay inside the repository and contain the build entrypoints", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "malbut-source-test-"));
  try {
    const source = path.join(root, "malbut_test/malbut_web");
    fs.mkdirSync(source, { recursive: true });
    for (const file of ["Dockerfile", "package.json", "package-lock.json", "next.config.ts"]) {
      fs.writeFileSync(path.join(source, file), "fixture");
    }
    assert.equal(resolveDeploymentSource("malbut_test/malbut_web", root), source);
    for (const candidate of ["", "/tmp", "../outside", "malbut_test/../malbut_web", "."]) {
      assert.throws(() => resolveDeploymentSource(candidate, root), /repository-relative/);
    }
    fs.symlinkSync(os.tmpdir(), path.join(root, "escape"));
    assert.throws(() => resolveDeploymentSource("escape", root), /inside the repository/);
    fs.unlinkSync(path.join(source, "Dockerfile"));
    fs.symlinkSync(path.join(source, "package.json"), path.join(source, "Dockerfile"));
    assert.throws(() => resolveDeploymentSource("malbut_test/malbut_web", root), /regular Dockerfile/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("source assets omit environment files, local credentials, dependencies and CDK", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "malbut-source-excludes-"));
  try {
    for (const file of ["app/page.tsx", ".env.local", ".local/device-token", "app/.env.production",
      "node_modules/dependency/index.js", "infra/cdk/cdk.out/template.json",
      "infra/aws/push-broker/fall-notification.mjs", "infra/aws/push-broker/.env.example",
      "infra/aws/push-broker/node_modules/web-push/index.js"]) {
      fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
      fs.writeFileSync(path.join(root, file), "fixture");
    }
    const app = new App();
    const asset = new Asset(new Stack(app, "SourceAssetTest"), "Source", {
      path: root,
      exclude: DEPLOYMENT_SOURCE_EXCLUDES,
      ignoreMode: IgnoreMode.GLOB,
      followSymlinks: SymlinkFollowMode.NEVER,
    });
    const stagedPath = path.resolve(app.outdir, asset.assetPath);
    assert.ok(fs.existsSync(path.join(stagedPath, "app/page.tsx")));
    // The web build imports the push broker's notification module from infra/aws.
    assert.ok(fs.existsSync(path.join(stagedPath, "infra/aws/push-broker/fall-notification.mjs")));
    for (const name of [".env.local", ".local", "app/.env.production", "node_modules", "infra/cdk",
      "infra/aws/push-broker/.env.example", "infra/aws/push-broker/node_modules"]) {
      assert.equal(fs.existsSync(path.join(stagedPath, name)), false, `${name} entered the deploy asset`);
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
