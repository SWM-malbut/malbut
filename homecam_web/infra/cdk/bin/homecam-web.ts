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

new HomecamDevStack(app, `MalbutHomecam-${stage}`, {
  stage,
  deviceIds,
  containerImageTag,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region,
  },
  description: `MALBUT homecam ${stage} infrastructure`,
});
