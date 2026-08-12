import assert from "node:assert/strict";
import test from "node:test";
import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { HomecamDevStack } from "../lib/homecam-dev-stack";

function synthesize(deviceIds = ["gazebo-homecam"]) {
  const app = new App();
  const stack = new HomecamDevStack(app, "TestHomecam", {
    stage: "dev",
    deviceIds,
    containerImageTag: "dev",
    env: { account: "111122223333", region: "ap-northeast-2" },
  });
  return Template.fromStack(stack);
}

test("creates the isolated homecam network and application platform", () => {
  const template = synthesize();

  template.resourceCountIs("AWS::EC2::VPC", 1);
  template.resourceCountIs("AWS::EC2::NatGateway", 0);
  template.resourceCountIs("AWS::ECS::Cluster", 1);
  template.resourceCountIs("AWS::ECS::Service", 1);
  template.resourceCountIs("AWS::ElasticLoadBalancingV2::LoadBalancer", 1);
  template.resourceCountIs("AWS::RDS::DBInstance", 1);
  template.resourceCountIs("AWS::Cognito::UserPool", 1);
  template.resourceCountIs("AWS::Cognito::UserPoolUser", 1);
  template.resourceCountIs("AWS::ECR::Repository", 1);
  template.resourceCountIs("AWS::CodeBuild::Project", 1);
  template.resourceCountIs("AWS::Events::Rule", 1);
  template.resourceCountIs("AWS::Route53::RecordSet", 1);

  template.hasResourceProperties("AWS::RDS::DBInstance", {
    Engine: "postgres",
    EngineVersion: "16.13",
    PubliclyAccessible: false,
    StorageEncrypted: true,
  });
  template.hasResourceProperties("AWS::ECS::Service", {
    LaunchType: "FARGATE",
    DeploymentConfiguration: Match.objectLike({
      DeploymentCircuitBreaker: { Enable: true, Rollback: true },
      MinimumHealthyPercent: 100,
    }),
    NetworkConfiguration: {
      AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: "ENABLED" }),
    },
  });
  const [service] = Object.values(template.findResources("AWS::ECS::Service"));
  assert.ok(service);
  const taskSubnets = service.Properties?.NetworkConfiguration?.AwsvpcConfiguration
    ?.Subnets as Array<{ Ref?: string }>;
  assert.ok(taskSubnets.length > 0);
  assert.ok(taskSubnets.every((subnet) => subnet.Ref?.includes("publicSubnet")));

  const ingressRules = Object.values(
    template.findResources("AWS::EC2::SecurityGroupIngress"),
  );
  const appIngress = ingressRules.find(
    (rule) => rule.Properties?.FromPort === 3000 && rule.Properties?.ToPort === 3000,
  );
  assert.ok(appIngress?.Properties?.SourceSecurityGroupId);
  assert.equal(appIngress?.Properties?.CidrIp, undefined);
  assert.equal(appIngress?.Properties?.CidrIpv6, undefined);
  template.hasResourceProperties("AWS::ECS::Cluster", {
    ClusterSettings: [{ Name: "containerInsights", Value: "disabled" }],
  });
  template.hasResourceProperties("AWS::Cognito::UserPoolUser", {
    DesiredDeliveryMediums: ["EMAIL"],
    Username: { Ref: "InitialOwnerEmail" },
    UserAttributes: Match.arrayWith([
      { Name: "email", Value: { Ref: "InitialOwnerEmail" } },
    ]),
  });
});

test("builds only an exact ARM64 Git commit into the scoped ECR repository", () => {
  const template = synthesize();
  template.hasResourceProperties("AWS::CodeBuild::Project", {
    Source: Match.objectLike({ Type: "NO_SOURCE" }),
    ConcurrentBuildLimit: 1,
    Environment: Match.objectLike({
      Type: "ARM_CONTAINER",
      ComputeType: "BUILD_GENERAL1_MEDIUM",
      PrivilegedMode: true,
      EnvironmentVariables: Match.arrayWith([
        { Name: "GIT_SHA", Type: "PLAINTEXT", Value: "dev" },
      ]),
    }),
  });

  const project = Object.values(
    template.findResources("AWS::CodeBuild::Project"),
  )[0];
  assert.ok(project);
  const buildSpec = JSON.stringify(project.Properties?.Source?.BuildSpec);
  assert.match(buildSpec, /\^\[0-9a-f\]\{40\}\$/);
  assert.match(buildSpec, /SWM-malbut\/malbut\.git/);
  assert.match(buildSpec, /--platform linux\/arm64/);
  assert.match(buildSpec, /Architecture/);
  assert.doesNotMatch(buildSpec, /GIT_URL|REPOSITORY_URI/);
});

test("creates one isolated P2P/storage/archive set per device", () => {
  const template = synthesize(["gazebo-homecam", "jetson-homecam-01"]);

  template.resourceCountIs("AWS::KinesisVideo::SignalingChannel", 4);
  template.resourceCountIs("AWS::KinesisVideo::Stream", 2);
  template.resourceCountIs("Custom::AWS", 2);
  template.hasResourceProperties("AWS::KinesisVideo::Stream", {
    DataRetentionInHours: 168,
    MediaType: "video/h264,audio/aac",
  });
  template.hasResourceProperties("AWS::KinesisVideo::SignalingChannel", {
    Tags: Match.arrayWith([
      {
        Key: "WebRtcInputMediaType",
        Value: "video-h264_audio-opus",
      },
    ]),
  });

  const channels = template.findResources("AWS::KinesisVideo::SignalingChannel");
  const names = Object.values(channels).map(
    (resource) => resource.Properties?.Name as string,
  );
  assert.equal(new Set(names).size, 4);
  assert.equal(names.filter((name) => name.endsWith("-p2p")).length, 2);
  assert.equal(names.filter((name) => name.endsWith("-storage")).length, 2);
});

test("keeps device credentials scoped to KVS channels and streams", () => {
  const template = synthesize();
  const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));

  assert.match(policies, /kinesisvideo:ConnectAsMaster/);
  assert.match(policies, /kinesisvideo:ConnectAsViewer/);
  assert.match(policies, /kinesisvideo:JoinStorageSession/);
  assert.match(policies, /kinesisvideo:JoinStorageSessionAsViewer/);
  assert.match(policies, /kinesisvideo:GetHLSStreamingSessionURL/);
  assert.match(policies, /kinesisvideo:PutMedia/);
  assert.doesNotMatch(policies, /"Resource":"\*"[^}]*kinesisvideo:/);
});

test("uses HTTPS, no-echo bootstrap inputs, and server-side secrets", () => {
  const template = synthesize();

  template.hasResourceProperties("AWS::ElasticLoadBalancingV2::Listener", {
    Port: 443,
    Protocol: "HTTPS",
  });
  template.hasResourceProperties("AWS::Lambda::Url", { AuthType: "NONE" });
  template.resourceCountIs("AWS::SecretsManager::Secret", 8);

  const rendered = template.toJSON() as {
    Parameters: Record<string, { NoEcho?: boolean }>;
  };
  assert.equal(rendered.Parameters.VapidPublicKey?.NoEcho, true);
  assert.equal(rendered.Parameters.VapidPrivateKey?.NoEcho, true);
  assert.equal(
    rendered.Parameters.DeviceProvisioningManifestSha256?.NoEcho,
    true,
  );
  assert.equal(rendered.Parameters.DeviceProvisioningExpiresAt?.NoEcho, true);
});

test("uses the imported child hosted zone apex as the homecam domain", () => {
  const template = synthesize();
  const rendered = template.toJSON() as {
    Parameters: Record<string, unknown>;
  };

  assert.equal(rendered.Parameters.HomecamRecordName, undefined);
  template.hasResourceProperties("AWS::CertificateManager::Certificate", {
    DomainName: { Ref: "HomecamHostedZoneName" },
    ValidationMethod: "DNS",
  });
  template.hasResourceProperties("AWS::Route53::RecordSet", {
    HostedZoneId: { Ref: "HomecamHostedZoneId" },
    Name: {
      "Fn::Join": [
        "",
        [{ Ref: "HomecamHostedZoneName" }, "."],
      ],
    },
    Type: "A",
    AliasTarget: Match.objectLike({
      DNSName: Match.anyValue(),
      HostedZoneId: Match.anyValue(),
    }),
  });
});

test("keeps public assets nonce-free and denies unauthenticated background APIs", () => {
  const template = synthesize();
  const rules = Object.values(
    template.findResources("AWS::ElasticLoadBalancingV2::ListenerRule"),
  );
  const ruleFor = (path: string) =>
    rules.find((rule) =>
      rule.Properties?.Conditions?.some(
        (condition: { PathPatternConfig?: { Values?: string[] } }) =>
          condition.PathPatternConfig?.Values?.includes(path),
      ),
    );

  const publicPaths = [
    "/api/health",
    "/auth/logout",
    "/auth/logout/complete",
    "/api/device/v1/session",
    "/api/device/v1/heartbeat",
    "/api/device/v1/events",
    "/api/internal/maintenance",
    "/api/internal/device-provisioning",
    "/sw.js",
    "/manifest.webmanifest",
    "/favicon.ico",
    "/favicon.svg",
    "/homecam-icon.svg",
    "/_next/static/*",
    "/vendor/kvs-webrtc.min.js",
    "/og.png",
  ];
  for (const path of publicPaths) {
    assert.deepEqual(
      ruleFor(path)?.Properties?.Actions?.map((action: { Type: string }) => action.Type),
      ["forward"],
    );
    assert.ok(ruleFor(path)?.Properties?.Priority < 15);
  }
  const apiRule = ruleFor("/api/*");
  const apiActions = apiRule?.Properties?.Actions;
  assert.equal(apiRule?.Properties?.Priority, 15);
  assert.deepEqual(
    apiActions?.map((action: { Type: string }) => action.Type),
    ["authenticate-cognito", "forward"],
  );
  assert.equal(
    apiActions?.[0]?.AuthenticateCognitoConfig?.OnUnauthenticatedRequest,
    "deny",
  );
  assert.equal(
    apiActions?.[0]?.AuthenticateCognitoConfig?.SessionCookieName,
    "AWSELBAuthSessionCookie",
  );

  const documentRule = ruleFor("/*");
  const documentActions = documentRule?.Properties?.Actions;
  assert.equal(documentRule?.Properties?.Priority, 20);
  assert.deepEqual(
    documentActions?.map((action: { Type: string }) => action.Type),
    ["authenticate-cognito", "forward"],
  );
  assert.equal(
    documentActions?.[0]?.AuthenticateCognitoConfig?.OnUnauthenticatedRequest,
    "authenticate",
  );
  assert.equal(
    documentActions?.[0]?.AuthenticateCognitoConfig?.SessionCookieName,
    "AWSELBAuthSessionCookie",
  );
  const apiConfig = apiActions?.[0]?.AuthenticateCognitoConfig;
  const documentConfig = documentActions?.[0]?.AuthenticateCognitoConfig;
  assert.equal(apiConfig?.Scope, "openid email profile");
  assert.equal(apiConfig?.SessionTimeout, 43_200);
  assert.deepEqual(
    { ...apiConfig, OnUnauthenticatedRequest: "authenticate" },
    documentConfig,
  );
  assert.equal(ruleFor("/api/device/v1/*"), undefined);
  assert.equal(ruleFor("/api/internal/*"), undefined);
  assert.equal(ruleFor("/_next/*"), undefined);
  assert.equal(ruleFor("/vendor/*"), undefined);
  assert.ok(
    rules.every((rule) =>
      rule.Properties?.Conditions?.every(
        (condition: { PathPatternConfig?: { Values?: string[] } }) =>
          (condition.PathPatternConfig?.Values?.length ?? 0) <= 3,
      ),
    ),
    "each ALB path condition must stay within the three-value comparison limit",
  );

  const userPoolClient = Object.values(
    template.findResources("AWS::Cognito::UserPoolClient"),
  )[0];
  assert.ok(userPoolClient);
  assert.match(
    JSON.stringify(userPoolClient.Properties?.LogoutURLs),
    /auth\/logout\/complete/,
  );
});

test("injects the AWS authentication and PostgreSQL runtime contract", () => {
  const template = synthesize();
  const task = Object.values(template.findResources("AWS::ECS::TaskDefinition"))[0];
  assert.ok(task);
  const [container] = task.Properties?.ContainerDefinitions ?? [];
  const environmentNames = new Set(
    container.Environment.map((entry: { Name: string }) => entry.Name),
  );
  const secretNames = new Set(
    container.Secrets.map((entry: { Name: string }) => entry.Name),
  );

  for (const name of [
    "AUTH_MODE",
    "AUTH_AWS_REGION",
    "AUTH_ALB_ARN",
    "AUTH_OIDC_CLIENT_ID",
    "AUTH_OIDC_ISSUER",
    "AUTH_COGNITO_DOMAIN",
    "AUTH_COGNITO_CLIENT_ID",
    "AUTH_PUBLIC_ORIGIN",
    "DEVICE_PROVISIONING_MANIFEST_SHA256",
    "DEVICE_PROVISIONING_EXPIRES_AT",
    "DATABASE_SSL_MODE",
    "DATABASE_SSL_CA_FILE",
    "DATABASE_POOL_MAX",
  ]) {
    assert.equal(environmentNames.has(name), true, `missing ${name}`);
  }
  assert.equal(secretNames.has("DATABASE_URL"), true);
  assert.equal(secretNames.has("DATABASE_SSL_CA_BASE64"), false);
  assert.equal(secretNames.has("DEVICE_PROVISIONING_SECRET"), true);
  assert.equal(
    container.Environment.find(
      (entry: { Name: string; Value: string }) =>
        entry.Name === "DATABASE_SSL_CA_FILE",
    )?.Value,
    "/app/certs/ap-northeast-2-bundle.pem",
  );
  assert.doesNotMatch(JSON.stringify(task), /PENDING_ALB_ARN/);
});
