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
  template.resourceCountIs("AWS::SecretsManager::Secret", 9);
  template.hasResourceProperties("AWS::SecretsManager::Secret", {
    Name: "malbut-homecam-dev/auth-session-secret",
    GenerateSecretString: {
      ExcludePunctuation: true,
      IncludeSpace: false,
      PasswordLength: 43,
    },
  });

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

test("forwards every HTTPS request to application-managed Cognito auth", () => {
  const template = synthesize();
  template.resourceCountIs("AWS::ElasticLoadBalancingV2::ListenerRule", 0);
  template.resourceCountIs("AWS::Cognito::UserPoolDomain", 0);
  const listeners = Object.values(
    template.findResources("AWS::ElasticLoadBalancingV2::Listener"),
  );
  const httpsListener = listeners.find(
    (listener) => listener.Properties?.Protocol === "HTTPS",
  );
  assert.ok(httpsListener);
  assert.deepEqual(
    httpsListener.Properties?.DefaultActions?.map(
      (action: { Type: string }) => action.Type,
    ),
    ["forward"],
  );
  assert.doesNotMatch(JSON.stringify(template.toJSON()), /authenticate-cognito/);

  const userPoolClient = Object.values(
    template.findResources("AWS::Cognito::UserPoolClient"),
  )[0];
  assert.ok(userPoolClient);
  assert.equal(userPoolClient.Properties?.GenerateSecret, false);
  assert.equal(userPoolClient.Properties?.AllowedOAuthFlowsUserPoolClient, false);
  assert.deepEqual(userPoolClient.Properties?.ExplicitAuthFlows, [
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]);
  assert.equal(userPoolClient.Properties?.CallbackURLs, undefined);
  assert.equal(userPoolClient.Properties?.LogoutURLs, undefined);
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
    "AUTH_PUBLIC_ORIGIN",
    "COGNITO_USER_POOL_ID",
    "COGNITO_USER_POOL_CLIENT_ID",
    "DEVICE_PROVISIONING_MANIFEST_SHA256",
    "DEVICE_PROVISIONING_EXPIRES_AT",
    "DATABASE_SSL_MODE",
    "DATABASE_SSL_CA_FILE",
    "DATABASE_POOL_MAX",
  ]) {
    assert.equal(environmentNames.has(name), true, `missing ${name}`);
  }
  for (const name of [
    "AUTH_ALB_ARN",
    "AUTH_OIDC_CLIENT_ID",
    "AUTH_OIDC_ISSUER",
    "AUTH_COGNITO_DOMAIN",
    "AUTH_COGNITO_CLIENT_ID",
    "COGNITO_ISSUER",
  ]) {
    assert.equal(environmentNames.has(name), false, `unexpected ${name}`);
  }
  assert.equal(
    container.Environment.find(
      (entry: { Name: string; Value: string }) => entry.Name === "AUTH_MODE",
    )?.Value,
    "cognito_session",
  );
  assert.equal(secretNames.has("DATABASE_URL"), true);
  assert.equal(secretNames.has("DATABASE_SSL_CA_BASE64"), false);
  assert.equal(secretNames.has("DEVICE_PROVISIONING_SECRET"), true);
  assert.equal(secretNames.has("AUTH_SESSION_SECRET"), true);
  assert.equal(
    container.Environment.find(
      (entry: { Name: string; Value: string }) =>
        entry.Name === "DATABASE_SSL_CA_FILE",
    )?.Value,
    "/app/certs/ap-northeast-2-bundle.pem",
  );
  assert.doesNotMatch(JSON.stringify(task), /PENDING_ALB_ARN/);
});

test("grants the web task only required Cognito admin authentication calls", () => {
  const template = synthesize();
  const [userPoolLogicalId] = Object.keys(
    template.findResources("AWS::Cognito::UserPool"),
  );
  const policies = Object.values(template.findResources("AWS::IAM::Policy"));
  const cognitoStatements = policies.flatMap(
    (policy) => policy.Properties?.PolicyDocument?.Statement ?? [],
  ).filter((statement: { Action?: string | string[] }) =>
    (Array.isArray(statement.Action) ? statement.Action : [statement.Action]).some(
      (action) => typeof action === "string" && action.startsWith("cognito-idp:"),
    ),
  );

  assert.equal(cognitoStatements.length, 1);
  assert.deepEqual(cognitoStatements[0]?.Action, [
    "cognito-idp:AdminInitiateAuth",
    "cognito-idp:AdminRespondToAuthChallenge",
    "cognito-idp:AdminGetUser",
  ]);
  assert.deepEqual(cognitoStatements[0]?.Resource, {
    "Fn::GetAtt": [userPoolLogicalId, "Arn"],
  });
});
