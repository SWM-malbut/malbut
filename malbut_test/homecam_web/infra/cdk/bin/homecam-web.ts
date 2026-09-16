#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { HomecamDevStack } from "../lib/homecam-dev-stack";

const app = new cdk.App();
const stage = String(app.node.tryGetContext("stage") ?? "dev");
const configuredDeviceIds = app.node.tryGetContext("deviceIds");

function parseDeviceIds(value: unknown): string[] {
  if (value === undefined) {
    return ["gazebo-homecam"];
  }

  const parsed = typeof value === "string" ? JSON.parse(value) : value;
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error("CDK context deviceIds must be a non-empty JSON array");
  }

  return parsed.map(String);
}

const deviceIds = parseDeviceIds(configuredDeviceIds);
const containerImageTag = String(
  app.node.tryGetContext("containerImageTag") ?? stage,
);
const region = String(app.node.tryGetContext("region") ?? "ap-northeast-2");
const configuredAuthMigrationPhase = app.node.tryGetContext("authMigrationPhase");
if (configuredAuthMigrationPhase === undefined) {
  throw new Error(
    "CDK context authMigrationPhase is required: prepare, dual, cutover, or cleanup",
  );
}
const authMigrationPhase = String(configuredAuthMigrationPhase);
const ingressMode = String(app.node.tryGetContext("ingressMode") ?? "custom-domain");
if (ingressMode !== "custom-domain" && ingressMode !== "cloudfront") {
  throw new Error("CDK context ingressMode must be custom-domain or cloudfront");
}
const deploymentSourceDirectory = app.node.tryGetContext("deploymentSourceDirectory");
if (deploymentSourceDirectory !== undefined && typeof deploymentSourceDirectory !== "string") {
  throw new Error("CDK context deploymentSourceDirectory must be a string");
}

if (!["prepare", "dual", "cutover", "cleanup"].includes(authMigrationPhase)) {
  throw new Error(
    "CDK context authMigrationPhase must be prepare, dual, cutover, or cleanup",
  );
}

new HomecamDevStack(app, `MalbutHomecam-${stage}`, {
  stage,
  deviceIds,
  containerImageTag,
  ingressMode,
  deploymentSourceDirectory,
  authMigrationPhase: authMigrationPhase as
    | "prepare"
    | "dual"
    | "cutover"
    | "cleanup",
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region,
  },
  description: `MALBUT homecam ${stage} infrastructure`,
});
