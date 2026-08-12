import assert from "node:assert/strict";
import test from "node:test";
import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import {
  type AuthMigrationPhase,
  HomecamDevStack,
} from "../lib/homecam-dev-stack";

function synthesize(
  deviceIds = ["gazebo-homecam"],
  authMigrationPhase: AuthMigrationPhase = "prepare",
) {
  const app = new App();
  const stack = new HomecamDevStack(app, "TestHomecam", {
    stage: "dev",
    deviceIds,
    containerImageTag: "dev",
    authMigrationPhase,
    env: { account: "111122223333", region: "ap-northeast-2" },
  });
  return Template.fromStack(stack);
}

const LEGACY_ALB_RULE_LOGICAL_IDS = [
  "HomecamServiceLBPublicListenerPublicHealthRuleCE34865B",
  "HomecamServiceLBPublicListenerPublicLogoutLandingRule4AC197BE",
  "HomecamServiceLBPublicListenerDeviceSessionApiRuleD3B410E3",
  "HomecamServiceLBPublicListenerDeviceHeartbeatApiRule7CCA4C69",
  "HomecamServiceLBPublicListenerDeviceEventsApiRuleC571CF03",
  "HomecamServiceLBPublicListenerMaintenanceApiRuleD1DB8DF5",
  "HomecamServiceLBPublicListenerDeviceProvisioningApiRuleDE59504A",
  "HomecamServiceLBPublicListenerPublicPwaRuntimeRule88A0DCA3",
  "HomecamServiceLBPublicListenerPublicPwaIconsRule340EFB93",
  "HomecamServiceLBPublicListenerPublicMediaAssetsRule5C756A00",
  "HomecamServiceLBPublicListenerCognitoApiAuthenticationRule3C75BE1A",
  "HomecamServiceLBPublicListenerCognitoAuthenticationRuleBF79C1B7",
] as const;

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

test("prepare preserves the deployed ALB auth resources and task contract", () => {
  const template = synthesize(["gazebo-homecam"], "prepare");
  assertLegacyAuthLogicalIds(template);
  const clients = template.findResources("AWS::Cognito::UserPoolClient");
  const legacyClient = clients.HomecamUsersHomecamWebClient81F860FC;
  const serverClient = clientByName(template, "malbut-homecam-dev-server-auth");

  template.resourceCountIs("AWS::Cognito::UserPool", 1);
  const userPool = template.findResources("AWS::Cognito::UserPool")
    .HomecamUsers1D372190;
  assert.ok(userPool);
  assert.equal(userPool.DeletionPolicy, "Retain");
  assert.equal(userPool.UpdateReplacePolicy, "Retain");
  const initialOwner = template.findResources("AWS::Cognito::UserPoolUser")
    .InitialOwnerUser;
  assert.equal(initialOwner?.DeletionPolicy, "Retain");
  assert.equal(initialOwner?.UpdateReplacePolicy, "Retain");
  template.resourceCountIs("AWS::Cognito::UserPoolClient", 2);
  template.resourceCountIs("AWS::Cognito::UserPoolDomain", 1);
  template.resourceCountIs("AWS::ElasticLoadBalancingV2::ListenerRule", 12);
  assert.ok(legacyClient, "the deployed HomecamWebClient logical ID must be stable");
  assert.equal(legacyClient.Properties?.GenerateSecret, true);
  assert.deepEqual(legacyClient.Properties?.ExplicitAuthFlows, [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]);
  assert.equal(legacyClient.Properties?.AllowedOAuthFlowsUserPoolClient, true);
  assert.ok(serverClient);
  assert.equal(serverClient.Properties?.GenerateSecret, false);
  assert.deepEqual(serverClient.Properties?.ExplicitAuthFlows, [
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]);

  const { environment, secrets } = taskRuntime(template);
  assert.equal(environment.get("AUTH_MODE"), "alb_oidc");
  assert.deepEqual(environment.get("COGNITO_USER_POOL_CLIENT_ID"), {
    Ref: "HomecamUsersHomecamWebClient81F860FC",
  });
  for (const name of legacyAlbEnvironmentNames()) {
    assert.equal(environment.has(name), true, `prepare is missing ${name}`);
  }
  assert.equal(secrets.has("AUTH_SESSION_SECRET"), false);
  assert.equal(cognitoAdminStatements(template).length, 0);
  assert.equal(ruleAtPriority(template, 15)?.Properties?.Actions?.[0]?.Type,
    "authenticate-cognito");
  assert.equal(ruleAtPriority(template, 20)?.Properties?.Actions?.[0]?.Type,
    "authenticate-cognito");
});

test("dual exposes only auth endpoints and enables both server auth contracts", () => {
  const template = synthesize(["gazebo-homecam"], "dual");
  assertLegacyAuthLogicalIds(template);
  template.resourceCountIs("AWS::Cognito::UserPoolClient", 2);
  template.resourceCountIs("AWS::Cognito::UserPoolDomain", 1);
  template.resourceCountIs("AWS::ElasticLoadBalancingV2::ListenerRule", 13);

  const serverClientId = clientLogicalIdByName(
    template,
    "malbut-homecam-dev-server-auth",
  );
  const { environment, secrets } = taskRuntime(template);
  assert.equal(environment.get("AUTH_MODE"), "alb_oidc_or_cognito_session");
  assert.deepEqual(environment.get("COGNITO_USER_POOL_CLIENT_ID"), {
    Ref: serverClientId,
  });
  for (const name of legacyAlbEnvironmentNames()) {
    assert.equal(environment.has(name), true, `dual is missing ${name}`);
  }
  assert.equal(secrets.has("AUTH_SESSION_SECRET"), true);
  assert.deepEqual(rulePaths(ruleAtPriority(template, 11)), [
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/auth/login",
    "/auth/logout",
  ]);
  assert.deepEqual(ruleAtPriority(template, 11)?.Properties?.Actions?.map(
    (action: { Type: string }) => action.Type,
  ), ["forward"]);
  assert.equal(ruleAtPriority(template, 14), undefined);
  assertAdminPolicyIsScoped(template);
});

test("cutover forwards the catch-all before retaining rollback auth rules", () => {
  const template = synthesize(["gazebo-homecam"], "cutover");
  assertLegacyAuthLogicalIds(template);
  template.resourceCountIs("AWS::Cognito::UserPoolClient", 2);
  template.resourceCountIs("AWS::Cognito::UserPoolDomain", 1);
  template.resourceCountIs("AWS::ElasticLoadBalancingV2::ListenerRule", 14);
  assert.deepEqual(rulePaths(ruleAtPriority(template, 14)), ["/*"]);
  assert.deepEqual(ruleAtPriority(template, 14)?.Properties?.Actions?.map(
    (action: { Type: string }) => action.Type,
  ), ["forward"]);
  assert.equal(ruleAtPriority(template, 15)?.Properties?.Actions?.[0]?.Type,
    "authenticate-cognito");
  assert.equal(ruleAtPriority(template, 20)?.Properties?.Actions?.[0]?.Type,
    "authenticate-cognito");
  assert.equal(taskRuntime(template).environment.get("AUTH_MODE"),
    "alb_oidc_or_cognito_session");
});

test("cleanup removes Hosted UI auth and keeps only application sessions", () => {
  const template = synthesize(["gazebo-homecam"], "cleanup");
  template.resourceCountIs("AWS::Cognito::UserPoolClient", 1);
  template.resourceCountIs("AWS::Cognito::UserPoolDomain", 0);
  template.resourceCountIs("AWS::ElasticLoadBalancingV2::ListenerRule", 0);
  assert.equal(
    clientByName(template, "malbut-homecam-dev-web"),
    undefined,
  );
  assert.ok(clientByName(template, "malbut-homecam-dev-server-auth"));
  assert.doesNotMatch(JSON.stringify(template.toJSON()), /authenticate-cognito/);

  const { environment, secrets, task } = taskRuntime(template);
  assert.equal(environment.get("AUTH_MODE"), "cognito_session");
  for (const name of legacyAlbEnvironmentNames()) {
    assert.equal(environment.has(name), false, `cleanup retained ${name}`);
  }
  assert.equal(secrets.has("AUTH_SESSION_SECRET"), true);
  assert.equal(environment.get("DATABASE_SSL_CA_FILE"),
    "/app/certs/ap-northeast-2-bundle.pem");
  assert.doesNotMatch(JSON.stringify(task), /PENDING_ALB_ARN/);
  assertAdminPolicyIsScoped(template);

  const listeners = Object.values(
    template.findResources("AWS::ElasticLoadBalancingV2::Listener"),
  );
  const httpsListener = listeners.find(
    (listener) => listener.Properties?.Protocol === "HTTPS",
  );
  assert.deepEqual(httpsListener?.Properties?.DefaultActions?.map(
    (action: { Type: string }) => action.Type,
  ), ["forward"]);
});

function clientByName(template: Template, clientName: string) {
  return Object.values(template.findResources("AWS::Cognito::UserPoolClient"))
    .find((resource) => resource.Properties?.ClientName === clientName);
}

function assertLegacyAuthLogicalIds(template: Template) {
  assert.ok(template.findResources("AWS::Cognito::UserPool").HomecamUsers1D372190);
  assert.ok(
    template.findResources("AWS::Cognito::UserPoolClient")
      .HomecamUsersHomecamWebClient81F860FC,
  );
  assert.ok(
    template.findResources("AWS::Cognito::UserPoolDomain")
      .HomecamUsersHomecamCognitoDomain54674BED,
  );
  const rules = template.findResources(
    "AWS::ElasticLoadBalancingV2::ListenerRule",
  );
  for (const logicalId of LEGACY_ALB_RULE_LOGICAL_IDS) {
    assert.ok(rules[logicalId], `missing deployed listener rule ${logicalId}`);
  }
}

function clientLogicalIdByName(template: Template, clientName: string) {
  const entry = Object.entries(
    template.findResources("AWS::Cognito::UserPoolClient"),
  ).find(([, resource]) => resource.Properties?.ClientName === clientName);
  assert.ok(entry, `missing Cognito client ${clientName}`);
  return entry[0];
}

function taskRuntime(template: Template) {
  const task = Object.values(template.findResources("AWS::ECS::TaskDefinition"))[0];
  assert.ok(task);
  const [container] = task.Properties?.ContainerDefinitions ?? [];
  assert.ok(container);
  return {
    task,
    environment: new Map(
      container.Environment.map((entry: { Name: string; Value: unknown }) =>
        [entry.Name, entry.Value]),
    ),
    secrets: new Set(
      container.Secrets.map((entry: { Name: string }) => entry.Name),
    ),
  };
}

function ruleAtPriority(template: Template, priority: number) {
  return Object.values(
    template.findResources("AWS::ElasticLoadBalancingV2::ListenerRule"),
  ).find((rule) => rule.Properties?.Priority === priority);
}

function rulePaths(rule: ReturnType<typeof ruleAtPriority>) {
  return (rule?.Properties?.Conditions ?? [])
    .flatMap((condition: { PathPatternConfig?: { Values?: string[] } }) =>
      condition.PathPatternConfig?.Values ?? [])
    .sort();
}

function legacyAlbEnvironmentNames() {
  return [
    "AUTH_ALB_ARN",
    "AUTH_OIDC_CLIENT_ID",
    "AUTH_OIDC_ISSUER",
    "AUTH_COGNITO_DOMAIN",
    "AUTH_COGNITO_CLIENT_ID",
    "COGNITO_ISSUER",
  ];
}

function cognitoAdminStatements(template: Template) {
  return Object.values(template.findResources("AWS::IAM::Policy"))
    .flatMap((policy) => policy.Properties?.PolicyDocument?.Statement ?? [])
    .filter((statement: { Action?: string | string[] }) =>
      (Array.isArray(statement.Action) ? statement.Action : [statement.Action])
        .some((action) =>
          typeof action === "string" && action.startsWith("cognito-idp:")),
    );
}

function assertAdminPolicyIsScoped(template: Template) {
  const statements = cognitoAdminStatements(template);
  assert.equal(statements.length, 1);
  assert.deepEqual(statements[0]?.Action, [
    "cognito-idp:AdminInitiateAuth",
    "cognito-idp:AdminRespondToAuthChallenge",
    "cognito-idp:AdminGetUser",
  ]);
  assert.deepEqual(statements[0]?.Resource, {
    "Fn::GetAtt": ["HomecamUsers1D372190", "Arn"],
  });
}
