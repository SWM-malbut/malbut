import * as path from "node:path";
import {
  Aws,
  CfnOutput,
  CfnParameter,
  Duration,
  Fn,
  RemovalPolicy,
  SecretValue,
  Stack,
  StackProps,
  Tags,
} from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as cr from "aws-cdk-lib/custom-resources";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecsPatterns from "aws-cdk-lib/aws-ecs-patterns";
import * as events from "aws-cdk-lib/aws-events";
import * as eventTargets from "aws-cdk-lib/aws-events-targets";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as elbv2Actions from "aws-cdk-lib/aws-elasticloadbalancingv2-actions";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kinesisvideo from "aws-cdk-lib/aws-kinesisvideo";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as nodejs from "aws-cdk-lib/aws-lambda-nodejs";
import * as logs from "aws-cdk-lib/aws-logs";
import * as rds from "aws-cdk-lib/aws-rds";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as sns from "aws-cdk-lib/aws-sns";
import * as sqs from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";

export interface HomecamDevStackProps extends StackProps {
  readonly stage: string;
  readonly deviceIds: string[];
  readonly containerImageTag: string;
  readonly authMigrationPhase: AuthMigrationPhase;
}

export type AuthMigrationPhase = "prepare" | "dual" | "cutover" | "cleanup";

type DeviceResources = {
  readonly deviceId: string;
  readonly p2pChannel: kinesisvideo.CfnSignalingChannel;
  readonly storageChannel: kinesisvideo.CfnSignalingChannel;
  readonly archiveStream: kinesisvideo.CfnStream;
};

const DEVICE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const WEBRTC_INPUT_MEDIA_TAG = "video-h264_audio-opus";
// KVS WebRTC Storage Sessions ingest Opus but persist the archive audio as AAC.
const KVS_ARCHIVE_MEDIA_TYPE = "video/h264,audio/aac";

export class HomecamDevStack extends Stack {
  constructor(scope: Construct, id: string, props: HomecamDevStackProps) {
    super(scope, id, props);

    if (!/^[a-z][a-z0-9-]{1,14}$/.test(props.stage)) {
      throw new Error("stage must be 2-15 lowercase letters, numbers, or hyphens");
    }
    if (
      props.deviceIds.length === 0 ||
      new Set(props.deviceIds).size !== props.deviceIds.length ||
      props.deviceIds.some((deviceId) => !DEVICE_ID_PATTERN.test(deviceId))
    ) {
      throw new Error("deviceIds must be unique valid homecam device identifiers");
    }
    if (!/^[A-Za-z0-9._-]{1,128}$/.test(props.containerImageTag)) {
      throw new Error("containerImageTag is not a valid ECR image tag");
    }
    if (!["prepare", "dual", "cutover", "cleanup"].includes(props.authMigrationPhase)) {
      throw new Error("authMigrationPhase is invalid");
    }

    const prefix = `malbut-homecam-${props.stage}`;
    const keepsLegacyAlbAuth = props.authMigrationPhase !== "cleanup";
    const usesApplicationSession = props.authMigrationPhase !== "prepare";
    Tags.of(this).add("Project", "malbut-homecam");
    Tags.of(this).add("Environment", props.stage);
    Tags.of(this).add("ManagedBy", "aws-cdk");

    const parameters = deploymentParameters(this);
    const homecamDomainName = parameters.hostedZoneName.valueAsString;
    const hostedZone = route53.HostedZone.fromHostedZoneAttributes(
      this,
      "HomecamHostedZone",
      {
        hostedZoneId: parameters.hostedZoneId.valueAsString,
        zoneName: parameters.hostedZoneName.valueAsString,
      },
    );
    const certificate = new acm.Certificate(this, "HomecamCertificate", {
      domainName: homecamDomainName,
      validation: acm.CertificateValidation.fromDns(hostedZone),
    });

    const flowLogGroup = new logs.LogGroup(this, "VpcFlowLogs", {
      logGroupName: `/malbut/homecam/${props.stage}/vpc-flow`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const vpc = new ec2.Vpc(this, "HomecamVpc", {
      ipAddresses: ec2.IpAddresses.cidr("10.42.0.0/16"),
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: "public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: "database",
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });
    vpc.addFlowLog("CloudWatchFlowLog", {
      destination: ec2.FlowLogDestination.toCloudWatchLogs(flowLogGroup),
      trafficType: ec2.FlowLogTrafficType.REJECT,
    });

    const databaseCredentialsSecret = new secretsmanager.Secret(
      this,
      "DatabaseCredentials",
      {
        secretName: `${prefix}/database`,
        generateSecretString: {
          secretStringTemplate: JSON.stringify({ username: "homecam_admin" }),
          generateStringKey: "password",
          passwordLength: 40,
          excludePunctuation: true,
        },
      },
    );
    databaseCredentialsSecret.applyRemovalPolicy(RemovalPolicy.DESTROY);
    const database = new rds.DatabaseInstance(this, "HomecamDatabase", {
      databaseName: "homecam",
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16_13,
      }),
      credentials: rds.Credentials.fromSecret(
        databaseCredentialsSecret,
        "homecam_admin",
      ),
      instanceType: ec2.InstanceType.of(
        ec2.InstanceClass.T4G,
        ec2.InstanceSize.MICRO,
      ),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      allocatedStorage: 20,
      maxAllocatedStorage: 100,
      storageType: rds.StorageType.GP3,
      storageEncrypted: true,
      multiAz: false,
      publiclyAccessible: false,
      deletionProtection: false,
      backupRetention: Duration.days(1),
      deleteAutomatedBackups: true,
      cloudwatchLogsExports: ["postgresql"],
      cloudwatchLogsRetention: logs.RetentionDays.ONE_MONTH,
      autoMinorVersionUpgrade: true,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const databaseUrlSecret = new secretsmanager.Secret(this, "DatabaseUrl", {
      secretName: `${prefix}/database-url`,
      description: "PostgreSQL connection URL consumed by the homecam ECS task",
      secretStringValue: SecretValue.unsafePlainText(
        Fn.join("", [
          "postgresql://homecam_admin:",
          databaseCredentialsSecret
            .secretValueFromJson("password")
            .unsafeUnwrap(),
          "@",
          database.dbInstanceEndpointAddress,
          ":",
          database.dbInstanceEndpointPort,
          "/homecam",
        ]),
      ),
    });
    databaseUrlSecret.applyRemovalPolicy(RemovalPolicy.DESTROY);
    databaseUrlSecret.node.addDependency(database);
    const userPool = new cognito.UserPool(this, "HomecamUsers", {
      userPoolName: `${prefix}-users`,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { otp: true, sms: false },
      passwordPolicy: {
        minLength: 12,
        requireDigits: true,
        requireLowercase: true,
        requireSymbols: true,
        requireUppercase: true,
        tempPasswordValidity: Duration.days(3),
      },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const legacyUserPoolClient = keepsLegacyAlbAuth
      ? userPool.addClient("HomecamWebClient", {
          userPoolClientName: `${prefix}-web`,
          generateSecret: true,
          authFlows: { userSrp: true },
          preventUserExistenceErrors: true,
          enableTokenRevocation: true,
          accessTokenValidity: Duration.minutes(30),
          idTokenValidity: Duration.minutes(30),
          refreshTokenValidity: Duration.days(7),
          oAuth: {
            flows: { authorizationCodeGrant: true },
            scopes: [
              cognito.OAuthScope.OPENID,
              cognito.OAuthScope.EMAIL,
              cognito.OAuthScope.PROFILE,
            ],
            callbackUrls: [
              Fn.join("", [
                "https://",
                homecamDomainName,
                "/oauth2/idpresponse",
              ]),
            ],
            logoutUrls: [
              Fn.join("", [
                "https://",
                homecamDomainName,
                "/auth/logout/complete",
              ]),
            ],
          },
        })
      : undefined;
    const serverAuthUserPoolClient = userPool.addClient(
      "HomecamServerAuthClient",
      {
        userPoolClientName: `${prefix}-server-auth`,
        generateSecret: false,
        authFlows: { adminUserPassword: true },
        preventUserExistenceErrors: true,
        enableTokenRevocation: true,
        accessTokenValidity: Duration.minutes(30),
        idTokenValidity: Duration.minutes(30),
        refreshTokenValidity: Duration.days(7),
        disableOAuth: true,
      },
    );
    const userPoolDomain = keepsLegacyAlbAuth
      ? userPool.addDomain("HomecamCognitoDomain", {
          cognitoDomain: {
            domainPrefix: Fn.join("-", [prefix, Aws.ACCOUNT_ID]),
          },
        })
      : undefined;
    const initialOwnerUser = new cognito.CfnUserPoolUser(this, "InitialOwnerUser", {
      userPoolId: userPool.userPoolId,
      username: parameters.initialOwnerEmail.valueAsString,
      desiredDeliveryMediums: ["EMAIL"],
      userAttributes: [
        {
          name: "email",
          value: parameters.initialOwnerEmail.valueAsString,
        },
        { name: "email_verified", value: "true" },
      ],
    });
    initialOwnerUser.applyRemovalPolicy(RemovalPolicy.RETAIN);

    const appShareSecret = generatedSecret(
      this,
      "PetcamShareSecret",
      `${prefix}/petcam-share-secret`,
    );
    const kvsBrokerSecret = generatedSecret(
      this,
      "KvsBrokerSecret",
      `${prefix}/kvs-broker-secret`,
    );
    const pushBrokerSecret = generatedSecret(
      this,
      "PushBrokerSecret",
      `${prefix}/push-broker-secret`,
    );
    const maintenanceSecret = generatedSecret(
      this,
      "MaintenanceSecret",
      `${prefix}/maintenance-secret`,
    );
    const provisioningSecret = generatedSecret(
      this,
      "ProvisioningSecret",
      `${prefix}/device-provisioning-secret`,
    );
    const authSessionSecret = new secretsmanager.Secret(this, "AuthSessionSecret", {
      secretName: `${prefix}/auth-session-secret`,
      description: "256-bit base64url key for encrypted homecam web sessions",
      generateSecretString: {
        // 43 unpadded base64url characters decode to 32 bytes. Restricting the
        // alphabet to alphanumerics keeps every generated value base64url-safe.
        passwordLength: 43,
        excludePunctuation: true,
        includeSpace: false,
      },
    });
    authSessionSecret.applyRemovalPolicy(RemovalPolicy.DESTROY);
    const vapidSecret = new secretsmanager.Secret(this, "VapidSecret", {
      secretName: `${prefix}/vapid`,
      description: "VAPID key pair supplied by the homecam administrator",
      secretObjectValue: {
        subject: SecretValue.cfnParameter(parameters.vapidSubject),
        publicKey: SecretValue.cfnParameter(parameters.vapidPublicKey),
        privateKey: SecretValue.cfnParameter(parameters.vapidPrivateKey),
      },
    });
    vapidSecret.applyRemovalPolicy(RemovalPolicy.DESTROY);

    const deviceResources = props.deviceIds.map((deviceId) =>
      createDeviceKvsResources(this, prefix, deviceId),
    );
    const kvsDeviceMapping = deviceResourceMapping(deviceResources);
    const channelArns = deviceResources.flatMap(({ p2pChannel, storageChannel }) => [
      p2pChannel.attrArn,
      storageChannel.attrArn,
    ]);
    const storageChannelArns = deviceResources.map(
      ({ storageChannel }) => storageChannel.attrArn,
    );
    const streamArns = deviceResources.map(({ archiveStream }) => archiveStream.attrArn);

    const kvsBrokerRole = new iam.Role(this, "KvsBrokerExecutionRole", {
      roleName: `${prefix}-kvs-broker`,
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });
    const deviceSessionRole = new iam.Role(this, "DeviceSessionRole", {
      roleName: `${prefix}-device-session`,
      assumedBy: new iam.ArnPrincipal(kvsBrokerRole.roleArn),
      maxSessionDuration: Duration.hours(1),
    });
    deviceSessionRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "kinesisvideo:DescribeSignalingChannel",
          "kinesisvideo:GetSignalingChannelEndpoint",
          "kinesisvideo:GetIceServerConfig",
          "kinesisvideo:ConnectAsMaster",
        ],
        resources: channelArns,
      }),
    );
    deviceSessionRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "kinesisvideo:DescribeMediaStorageConfiguration",
          "kinesisvideo:JoinStorageSession",
        ],
        resources: storageChannelArns,
      }),
    );
    deviceSessionRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "kinesisvideo:GetDataEndpoint",
          "kinesisvideo:DescribeStream",
          "kinesisvideo:PutMedia",
        ],
        resources: streamArns,
      }),
    );
    kvsBrokerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "kinesisvideo:DescribeSignalingChannel",
          "kinesisvideo:GetSignalingChannelEndpoint",
          "kinesisvideo:GetIceServerConfig",
          "kinesisvideo:ConnectAsMaster",
          "kinesisvideo:ConnectAsViewer",
        ],
        resources: channelArns,
      }),
    );
    kvsBrokerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "kinesisvideo:DescribeMediaStorageConfiguration",
          "kinesisvideo:JoinStorageSession",
          "kinesisvideo:JoinStorageSessionAsViewer",
        ],
        resources: storageChannelArns,
      }),
    );
    kvsBrokerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "kinesisvideo:GetDataEndpoint",
          "kinesisvideo:DescribeStream",
          "kinesisvideo:GetHLSStreamingSessionURL",
        ],
        resources: streamArns,
      }),
    );
    deviceSessionRole.grantAssumeRole(kvsBrokerRole);

    const kvsBrokerLogGroup = new logs.LogGroup(this, "KvsBrokerLogs", {
      logGroupName: `/aws/lambda/${prefix}-kvs-broker`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const kvsBroker = new nodejs.NodejsFunction(this, "KvsBroker", {
      functionName: `${prefix}-kvs-broker`,
      description: "Issues scoped KVS sessions and HLS playback URLs",
      entry: path.join(
        __dirname,
        "..",
        "..",
        "aws",
        "kvs-broker",
        "index.mjs",
      ),
      depsLockFilePath: path.join(
        __dirname,
        "..",
        "..",
        "aws",
        "kvs-broker",
        "package-lock.json",
      ),
      handler: "handler",
      runtime: lambda.Runtime.NODEJS_22_X,
      architecture: lambda.Architecture.ARM_64,
      memorySize: 512,
      timeout: Duration.seconds(15),
      reservedConcurrentExecutions: 10,
      role: kvsBrokerRole,
      logGroup: kvsBrokerLogGroup,
      bundling: {
        target: "node22",
        minify: true,
        sourceMap: true,
        nodeModules: [
          "@aws-sdk/client-kinesis-video",
          "@aws-sdk/client-kinesis-video-archived-media",
          "@aws-sdk/client-kinesis-video-signaling",
          "@aws-sdk/client-kinesis-video-webrtc-storage",
          "@aws-sdk/client-sts",
        ],
      },
      environment: {
        BROKER_SHARED_SECRET: kvsBrokerSecret.secretValue.unsafeUnwrap(),
        KVS_DEVICE_CHANNELS_JSON: kvsDeviceMapping,
        KVS_DEVICE_ROLE_ARN: deviceSessionRole.roleArn,
      },
    });
    const kvsBrokerUrl = kvsBroker.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      invokeMode: lambda.InvokeMode.BUFFERED,
    });

    const pushBrokerLogGroup = new logs.LogGroup(this, "PushBrokerLogs", {
      logGroupName: `/aws/lambda/${prefix}-push-broker`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const pushBroker = new nodejs.NodejsFunction(this, "PushBroker", {
      functionName: `${prefix}-push-broker`,
      description: "Encrypts and sends RFC 8291 Web Push notifications",
      entry: path.join(
        __dirname,
        "..",
        "..",
        "aws",
        "push-broker",
        "index.mjs",
      ),
      depsLockFilePath: path.join(
        __dirname,
        "..",
        "..",
        "aws",
        "push-broker",
        "package-lock.json",
      ),
      handler: "handler",
      runtime: lambda.Runtime.NODEJS_22_X,
      architecture: lambda.Architecture.ARM_64,
      memorySize: 256,
      timeout: Duration.seconds(15),
      reservedConcurrentExecutions: 10,
      logGroup: pushBrokerLogGroup,
      bundling: {
        target: "node22",
        minify: true,
        sourceMap: true,
        nodeModules: ["web-push"],
      },
      environment: {
        BROKER_SHARED_SECRET: pushBrokerSecret.secretValue.unsafeUnwrap(),
        PUSH_VAPID_SUBJECT: vapidSecret
          .secretValueFromJson("subject")
          .unsafeUnwrap(),
        PUSH_VAPID_PUBLIC_KEY: vapidSecret
          .secretValueFromJson("publicKey")
          .unsafeUnwrap(),
        PUSH_VAPID_PRIVATE_KEY: vapidSecret
          .secretValueFromJson("privateKey")
          .unsafeUnwrap(),
      },
    });
    const pushBrokerUrl = pushBroker.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      invokeMode: lambda.InvokeMode.BUFFERED,
    });

    const repository = new ecr.Repository(this, "HomecamRepository", {
      repositoryName: `${prefix}-web`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      encryption: ecr.RepositoryEncryption.AES_256,
      emptyOnDelete: true,
      removalPolicy: RemovalPolicy.DESTROY,
      lifecycleRules: [{ maxImageCount: 20, description: "Keep recent dev images" }],
    });
    const ecrRegistryHost = Fn.join("", [
      Aws.ACCOUNT_ID,
      ".dkr.ecr.",
      this.region,
      ".",
      Aws.URL_SUFFIX,
    ]);
    const imageBuilder = new codebuild.Project(this, "HomecamImageBuilder", {
      projectName: `${prefix}-image-builder`,
      description: "On-demand ARM64 builder for the MALBUT homecam web image",
      buildSpec: codebuild.BuildSpec.fromObjectToYaml({
        version: "0.2",
        phases: {
          install: {
            commands: ["git --version", "docker --version", "aws --version"],
          },
          pre_build: {
            commands: [
              "printf '%s' \"$GIT_SHA\" | grep -Eq '^[0-9a-f]{40}$'",
              "git init /tmp/malbut-source",
              "git -C /tmp/malbut-source remote add origin https://github.com/SWM-malbut/malbut.git",
              "git -C /tmp/malbut-source fetch --depth=1 origin \"$GIT_SHA\"",
              "git -C /tmp/malbut-source checkout --detach FETCH_HEAD",
              "test \"$(git -C /tmp/malbut-source rev-parse HEAD)\" = \"$GIT_SHA\"",
              `aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "${ecrRegistryHost}"`,
            ],
          },
          build: {
            commands: [
              `docker build --pull --platform linux/arm64 -t "${repository.repositoryUri}:$GIT_SHA" /tmp/malbut-source/homecam_web`,
              `docker image inspect --format '{{.Architecture}}' "${repository.repositoryUri}:$GIT_SHA" | grep -qx arm64`,
            ],
          },
          post_build: {
            commands: [`docker push "${repository.repositoryUri}:$GIT_SHA"`],
          },
        },
      }),
      environment: {
        buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
        computeType: codebuild.ComputeType.MEDIUM,
        privileged: true,
        environmentVariables: {
          GIT_SHA: { value: props.containerImageTag },
        },
      },
      concurrentBuildLimit: 1,
      timeout: Duration.minutes(30),
      queuedTimeout: Duration.minutes(15),
      grantReportGroupPermissions: false,
    });
    repository.grantPullPush(imageBuilder);
    const cluster = new ecs.Cluster(this, "HomecamCluster", {
      clusterName: `${prefix}-cluster`,
      vpc,
      containerInsightsV2: ecs.ContainerInsights.DISABLED,
    });
    const taskDefinition = new ecs.FargateTaskDefinition(this, "HomecamTask", {
      family: `${prefix}-web`,
      cpu: 1024,
      memoryLimitMiB: 2048,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    const appLogGroup = new logs.LogGroup(this, "HomecamAppLogs", {
      logGroupName: `/malbut/homecam/${props.stage}/app`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const cognitoIssuer = Fn.join("", [
      "https://cognito-idp.",
      this.region,
      ".amazonaws.com/",
      userPool.userPoolId,
    ]);
    const container = taskDefinition.addContainer("HomecamWeb", {
      containerName: "homecam-web",
      image: ecs.ContainerImage.fromEcrRepository(
        repository,
        props.containerImageTag,
      ),
      essential: true,
      logging: ecs.LogDrivers.awsLogs({
        logGroup: appLogGroup,
        streamPrefix: "web",
      }),
      environment: {
        NODE_ENV: "production",
        PORT: "3000",
        AWS_REGION: this.region,
        DATABASE_HOST: database.dbInstanceEndpointAddress,
        DATABASE_PORT: database.dbInstanceEndpointPort,
        DATABASE_NAME: "homecam",
        DATABASE_SSL_MODE: "verify-full",
        DATABASE_SSL_CA_FILE: "/app/certs/ap-northeast-2-bundle.pem",
        DATABASE_POOL_MAX: "10",
        DATABASE_IDLE_TIMEOUT_MS: "30000",
        DATABASE_CONNECT_TIMEOUT_MS: "10000",
        AUTH_MODE:
          props.authMigrationPhase === "prepare"
            ? "alb_oidc"
            : keepsLegacyAlbAuth
              ? "alb_oidc_or_cognito_session"
              : "cognito_session",
        AUTH_AWS_REGION: this.region,
        ...(keepsLegacyAlbAuth
          ? {
              AUTH_OIDC_CLIENT_ID: legacyUserPoolClient!.userPoolClientId,
              AUTH_OIDC_ISSUER: cognitoIssuer,
              AUTH_EMAIL_CLAIM: "email",
            }
          : {}),
        AUTH_SIGN_IN_PATH: "/auth/login",
        AUTH_SIGN_OUT_PATH: "/auth/logout",
        ...(keepsLegacyAlbAuth
          ? {
              AUTH_COGNITO_DOMAIN: userPoolDomain!.baseUrl(),
              AUTH_COGNITO_CLIENT_ID: legacyUserPoolClient!.userPoolClientId,
            }
          : {}),
        AUTH_PUBLIC_ORIGIN: Fn.join("", ["https://", homecamDomainName]),
        PETCAM_DEVICE_ID: props.deviceIds[0]!,
        PETCAM_BROADCASTER_EMAILS: parameters.initialOwnerEmail.valueAsString,
        DEVICE_PROVISIONING_MANIFEST_SHA256:
          parameters.deviceProvisioningManifestSha256.valueAsString,
        DEVICE_PROVISIONING_EXPIRES_AT:
          parameters.deviceProvisioningExpiresAt.valueAsString,
        KVS_DEVICE_CHANNELS_JSON: kvsDeviceMapping,
        KVS_BROKER_URL: kvsBrokerUrl.url,
        PUSH_BROKER_URL: pushBrokerUrl.url,
        COGNITO_USER_POOL_ID: userPool.userPoolId,
        COGNITO_USER_POOL_CLIENT_ID: usesApplicationSession
          ? serverAuthUserPoolClient.userPoolClientId
          : legacyUserPoolClient!.userPoolClientId,
        ...(keepsLegacyAlbAuth
          ? {
              COGNITO_ISSUER: cognitoIssuer,
            }
          : {}),
      },
      secrets: {
        DATABASE_URL: ecs.Secret.fromSecretsManager(databaseUrlSecret),
        PETCAM_SHARE_SECRET: ecs.Secret.fromSecretsManager(appShareSecret),
        KVS_BROKER_SECRET: ecs.Secret.fromSecretsManager(kvsBrokerSecret),
        PUSH_BROKER_SECRET: ecs.Secret.fromSecretsManager(pushBrokerSecret),
        PUSH_VAPID_PUBLIC_KEY: ecs.Secret.fromSecretsManager(
          vapidSecret,
          "publicKey",
        ),
        MAINTENANCE_SECRET: ecs.Secret.fromSecretsManager(maintenanceSecret),
        DEVICE_PROVISIONING_SECRET:
          ecs.Secret.fromSecretsManager(provisioningSecret),
        ...(usesApplicationSession
          ? {
              AUTH_SESSION_SECRET:
                ecs.Secret.fromSecretsManager(authSessionSecret),
            }
          : {}),
      },
      healthCheck: {
        command: [
          "CMD-SHELL",
          "node -e \"fetch('http://127.0.0.1:3000/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))\"",
        ],
        interval: Duration.seconds(30),
        retries: 3,
        startPeriod: Duration.seconds(60),
        timeout: Duration.seconds(5),
      },
    });
    container.addPortMappings({ containerPort: 3000, protocol: ecs.Protocol.TCP });
    if (usesApplicationSession) {
      taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          actions: [
            "cognito-idp:AdminInitiateAuth",
            "cognito-idp:AdminRespondToAuthChallenge",
            "cognito-idp:AdminGetUser",
          ],
          resources: [userPool.userPoolArn],
        }),
      );
    }

    const albLogBucket = new s3.Bucket(this, "AlbLogBucket", {
      bucketName: Fn.join("-", [prefix, Aws.ACCOUNT_ID, "alb-logs"]),
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      lifecycleRules: [{ expiration: Duration.days(30) }],
      autoDeleteObjects: true,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const service = new ecsPatterns.ApplicationLoadBalancedFargateService(
      this,
      "HomecamService",
      {
        serviceName: `${prefix}-web`,
        cluster,
        taskDefinition,
        desiredCount: parameters.serviceDesiredCount.valueAsNumber,
        assignPublicIp: true,
        taskSubnets: { subnetType: ec2.SubnetType.PUBLIC },
        publicLoadBalancer: true,
        circuitBreaker: { rollback: true },
        minHealthyPercent: 100,
        certificate,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        listenerPort: 443,
        redirectHTTP: true,
        healthCheckGracePeriod: Duration.seconds(90),
      },
    );
    if (keepsLegacyAlbAuth) {
      const taskContainer = taskDefinition.defaultContainer;
      if (!taskContainer) throw new Error("Homecam task container is missing");
      taskContainer.addEnvironment(
        "AUTH_ALB_ARN",
        service.loadBalancer.loadBalancerArn,
      );
      for (const [ruleId, priority, pathPatterns] of [
        ["PublicHealth", 1, ["/api/health"]],
        ["PublicLogoutLanding", 2, ["/auth/logout", "/auth/logout/complete"]],
        ["DeviceSessionApi", 3, ["/api/device/v1/session"]],
        ["DeviceHeartbeatApi", 4, ["/api/device/v1/heartbeat"]],
        [
          "DeviceEventsApi",
          5,
          [
            "/api/device/v1/events",
            "/api/device/v1/robot/state",
            "/api/device/v1/robot/map",
            "/api/device/v1/robot/commands",
            "/api/device/v1/robot/commands/*/complete",
          ],
        ],
        ["MaintenanceApi", 6, ["/api/internal/maintenance"]],
        ["DeviceProvisioningApi", 7, ["/api/internal/device-provisioning"]],
        [
          "PublicPwaRuntime",
          8,
          ["/_next/static/*", "/sw.js", "/manifest.webmanifest"],
        ],
        [
          "PublicPwaIcons",
          9,
          ["/favicon.ico", "/favicon.svg", "/homecam-icon.svg"],
        ],
        ["PublicMediaAssets", 10, ["/og.png", "/vendor/kvs-webrtc.min.js"]],
      ] as const) {
        service.listener.addAction(ruleId, {
          priority,
          conditions: [elbv2.ListenerCondition.pathPatterns([...pathPatterns])],
          action: elbv2.ListenerAction.forward([service.targetGroup]),
        });
      }
      if (usesApplicationSession) {
        service.listener.addAction("IntegratedAuthPublic", {
          priority: 11,
          conditions: [
            elbv2.ListenerCondition.pathPatterns([
              "/auth/login",
              "/api/auth/login",
              "/api/auth/me",
              "/auth/logout",
              "/api/auth/logout",
            ]),
          ],
          action: elbv2.ListenerAction.forward([service.targetGroup]),
        });
      }
      if (props.authMigrationPhase === "cutover") {
        service.listener.addAction("ApplicationAuthCutover", {
          priority: 14,
          conditions: [elbv2.ListenerCondition.pathPatterns(["/*"])],
          action: elbv2.ListenerAction.forward([service.targetGroup]),
        });
      }
      const authenticatedAction = (
        onUnauthenticatedRequest: elbv2.UnauthenticatedAction,
      ) =>
        new elbv2Actions.AuthenticateCognitoAction({
          userPool,
          userPoolClient: legacyUserPoolClient!,
          userPoolDomain: userPoolDomain!,
          scope: "openid email profile",
          sessionCookieName: "AWSELBAuthSessionCookie",
          sessionTimeout: Duration.hours(12),
          onUnauthenticatedRequest,
          next: elbv2.ListenerAction.forward([service.targetGroup]),
        });
      service.listener.addAction("CognitoApiAuthentication", {
        priority: 15,
        conditions: [elbv2.ListenerCondition.pathPatterns(["/api/*"])],
        action: authenticatedAction(elbv2.UnauthenticatedAction.DENY),
      });
      service.listener.addAction("CognitoAuthentication", {
        priority: 20,
        conditions: [elbv2.ListenerCondition.pathPatterns(["/*"])],
        action: authenticatedAction(elbv2.UnauthenticatedAction.AUTHENTICATE),
      });
    }
    new route53.ARecord(this, "HomecamDnsRecord", {
      zone: hostedZone,
      target: route53.RecordTarget.fromAlias(
        new route53Targets.LoadBalancerTarget(service.loadBalancer),
      ),
    });
    service.loadBalancer.logAccessLogs(albLogBucket, "alb");
    service.targetGroup.configureHealthCheck({
      path: "/api/health",
      healthyHttpCodes: "200",
      interval: Duration.seconds(30),
      timeout: Duration.seconds(10),
    });
    database.connections.allowDefaultPortFrom(
      service.service,
      "Homecam web tasks may connect to PostgreSQL",
    );
    const scaling = service.service.autoScaleTaskCount({
      minCapacity: parameters.serviceDesiredCount.valueAsNumber,
      maxCapacity: 4,
    });
    scaling.scaleOnCpuUtilization("CpuScaling", {
      targetUtilizationPercent: 60,
      scaleInCooldown: Duration.minutes(5),
      scaleOutCooldown: Duration.minutes(1),
    });

    const maintenanceLogGroup = new logs.LogGroup(this, "MaintenanceLogs", {
      logGroupName: `/aws/lambda/${prefix}-maintenance`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const maintenanceFunction = new lambda.Function(this, "MaintenanceFunction", {
      functionName: `${prefix}-maintenance`,
      description: "Calls the authenticated homecam retention and push outbox job",
      runtime: lambda.Runtime.NODEJS_22_X,
      architecture: lambda.Architecture.ARM_64,
      handler: "index.handler",
      code: lambda.Code.fromInline(maintenanceFunctionSource),
      timeout: Duration.seconds(30),
      memorySize: 256,
      logGroup: maintenanceLogGroup,
      environment: {
        MAINTENANCE_SECRET_ARN: maintenanceSecret.secretArn,
        MAINTENANCE_URL: Fn.join("", [
          "https://",
          homecamDomainName,
          "/api/internal/maintenance",
        ]),
      },
    });
    maintenanceSecret.grantRead(maintenanceFunction);
    const maintenanceDlq = new sqs.Queue(this, "MaintenanceDlq", {
      queueName: `${prefix}-maintenance-dlq`,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      retentionPeriod: Duration.days(14),
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const maintenanceRule = new events.Rule(this, "MaintenanceSchedule", {
      ruleName: `${prefix}-maintenance`,
      description: "Retries push outbox and removes expired homecam metadata",
      schedule: events.Schedule.rate(Duration.minutes(5)),
      enabled: true,
    });
    maintenanceRule.addTarget(
      new eventTargets.LambdaFunction(maintenanceFunction, {
        deadLetterQueue: maintenanceDlq,
        retryAttempts: 2,
        maxEventAge: Duration.hours(1),
      }),
    );

    const alarmTopic = new sns.Topic(this, "AlarmTopic", {
      topicName: `${prefix}-alarms`,
      displayName: "MALBUT homecam dev alarms",
    });
    const alarms = [
      service.loadBalancer
        .metrics.httpCodeTarget(
          elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
          { period: Duration.minutes(5), statistic: "Sum" },
        )
        .createAlarm(this, "AlbTarget5xxAlarm", {
          threshold: 5,
          evaluationPeriods: 1,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        }),
      service.service.metricCpuUtilization().createAlarm(this, "EcsCpuAlarm", {
        threshold: 80,
        evaluationPeriods: 3,
      }),
      database.metricCPUUtilization().createAlarm(this, "DatabaseCpuAlarm", {
        threshold: 80,
        evaluationPeriods: 3,
      }),
      kvsBroker.metricErrors().createAlarm(this, "KvsBrokerErrorsAlarm", {
        threshold: 1,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }),
      pushBroker.metricErrors().createAlarm(this, "PushBrokerErrorsAlarm", {
        threshold: 1,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }),
      maintenanceFunction
        .metricErrors()
        .createAlarm(this, "MaintenanceErrorsAlarm", {
          threshold: 1,
          evaluationPeriods: 1,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        }),
    ];
    for (const alarm of alarms) {
      alarm.addAlarmAction(new cloudwatchActions.SnsAction(alarmTopic));
    }

    new CfnOutput(this, "HomecamUrl", {
      value: Fn.join("", ["https://", homecamDomainName]),
    });
    new CfnOutput(this, "ContainerRepositoryUri", {
      value: repository.repositoryUri,
    });
    new CfnOutput(this, "ImageBuilderProjectName", {
      value: imageBuilder.projectName,
    });
    new CfnOutput(this, "DatabaseSecretArn", {
      value: databaseCredentialsSecret.secretArn,
    });
    new CfnOutput(this, "CognitoUserPoolId", { value: userPool.userPoolId });
    new CfnOutput(this, "CognitoUserPoolClientId", {
      value:
        legacyUserPoolClient?.userPoolClientId ??
        serverAuthUserPoolClient.userPoolClientId,
    });
    new CfnOutput(this, "CognitoServerAuthClientId", {
      value: serverAuthUserPoolClient.userPoolClientId,
    });
    if (userPoolDomain) {
      new CfnOutput(this, "CognitoHostedUiBaseUrl", {
        value: userPoolDomain.baseUrl(),
      });
    }
    new CfnOutput(this, "KvsBrokerFunctionUrl", { value: kvsBrokerUrl.url });
    new CfnOutput(this, "PushBrokerFunctionUrl", { value: pushBrokerUrl.url });
    new CfnOutput(this, "KvsDeviceChannelsJson", {
      value: kvsDeviceMapping,
      description: "Server-only device mapping; do not expose in public client config",
    });
    new CfnOutput(this, "AlarmTopicArn", { value: alarmTopic.topicArn });
  }
}

function deploymentParameters(stack: Stack) {
  return {
    hostedZoneId: new CfnParameter(stack, "HomecamHostedZoneId", {
      type: "String",
      description: "Existing Route 53 public child hosted zone ID",
      allowedPattern: "^Z[A-Z0-9]{1,31}$",
    }),
    hostedZoneName: new CfnParameter(stack, "HomecamHostedZoneName", {
      type: "String",
      description:
        "Existing Route 53 child zone name used as the homecam apex, for example malbut.hyenje29.click",
    }),
    initialOwnerEmail: new CfnParameter(stack, "InitialOwnerEmail", {
      type: "String",
      description: "Initial homecam owner and broadcaster email",
      allowedPattern: "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$",
    }),
    deviceProvisioningManifestSha256: new CfnParameter(
      stack,
      "DeviceProvisioningManifestSha256",
      {
        type: "String",
        description:
          "SHA-256 of the canonical one-time device provisioning manifest",
        allowedPattern: "^[0-9a-f]{64}$",
        noEcho: true,
      },
    ),
    deviceProvisioningExpiresAt: new CfnParameter(
      stack,
      "DeviceProvisioningExpiresAt",
      {
        type: "String",
        description:
          "Future UTC expiry for one-time provisioning, for example 2026-08-13T00:00:00.000Z",
        allowedPattern:
          "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}Z$",
        noEcho: true,
      },
    ),
    vapidSubject: new CfnParameter(stack, "VapidSubject", {
      type: "String",
      description: "VAPID contact URI, normally mailto:homecam-ops@example.com",
      allowedPattern: "^(mailto:|https://).+",
      noEcho: true,
    }),
    vapidPublicKey: new CfnParameter(stack, "VapidPublicKey", {
      type: "String",
      description: "Base64url VAPID public key",
      noEcho: true,
      minLength: 40,
    }),
    vapidPrivateKey: new CfnParameter(stack, "VapidPrivateKey", {
      type: "String",
      description: "Base64url VAPID private key",
      noEcho: true,
      minLength: 20,
    }),
    serviceDesiredCount: new CfnParameter(stack, "ServiceDesiredCount", {
      type: "Number",
      description:
        "Use 0 on the first deploy, push the image to ECR, then update to 1",
      default: 0,
      minValue: 0,
      maxValue: 4,
    }),
  };
}

function generatedSecret(
  scope: Construct,
  id: string,
  secretName: string,
): secretsmanager.Secret {
  const secret = new secretsmanager.Secret(scope, id, {
    secretName,
    generateSecretString: {
      passwordLength: 64,
      excludePunctuation: true,
      includeSpace: false,
    },
  });
  secret.applyRemovalPolicy(RemovalPolicy.DESTROY);
  return secret;
}

function createDeviceKvsResources(
  scope: Stack,
  prefix: string,
  deviceId: string,
): DeviceResources {
  const safeName = deviceId.replace(/[^A-Za-z0-9_.-]/g, "-");
  const constructId = deviceId.replace(/[^A-Za-z0-9]/g, "-");
  const p2pChannel = new kinesisvideo.CfnSignalingChannel(
    scope,
    `${constructId}P2pChannel`,
    {
      name: `${prefix}-${safeName}-p2p`,
      type: "SINGLE_MASTER",
      messageTtlSeconds: 60,
    },
  );
  const storageChannel = new kinesisvideo.CfnSignalingChannel(
    scope,
    `${constructId}StorageChannel`,
    {
      name: `${prefix}-${safeName}-storage`,
      type: "SINGLE_MASTER",
      messageTtlSeconds: 60,
    },
  );
  const archiveStream = new kinesisvideo.CfnStream(
    scope,
    `${constructId}ArchiveStream`,
    {
      name: `${prefix}-${safeName}-archive`,
      dataRetentionInHours: 168,
      mediaType: KVS_ARCHIVE_MEDIA_TYPE,
    },
  );
  Tags.of(p2pChannel).add("WebRtcInputMediaType", WEBRTC_INPUT_MEDIA_TAG);
  Tags.of(storageChannel).add("WebRtcInputMediaType", WEBRTC_INPUT_MEDIA_TAG);
  p2pChannel.applyRemovalPolicy(RemovalPolicy.DESTROY);
  storageChannel.applyRemovalPolicy(RemovalPolicy.DESTROY);
  archiveStream.applyRemovalPolicy(RemovalPolicy.DESTROY);

  const enableStorage = new cr.AwsCustomResource(
    scope,
    `${constructId}EnableStorage`,
    {
      installLatestAwsSdk: false,
      onCreate: mediaStorageCall(
        storageChannel.attrArn,
        "ENABLED",
        `${prefix}-${safeName}-storage-enabled`,
        archiveStream.attrArn,
      ),
      onUpdate: mediaStorageCall(
        storageChannel.attrArn,
        "ENABLED",
        `${prefix}-${safeName}-storage-enabled`,
        archiveStream.attrArn,
      ),
      onDelete: mediaStorageCall(
        storageChannel.attrArn,
        "DISABLED",
        `${prefix}-${safeName}-storage-disabled`,
      ),
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ["kinesisvideo:UpdateMediaStorageConfiguration"],
          resources: [storageChannel.attrArn, archiveStream.attrArn],
        }),
      ]),
    },
  );
  enableStorage.node.addDependency(storageChannel, archiveStream);

  return { deviceId, p2pChannel, storageChannel, archiveStream };
}

function mediaStorageCall(
  channelArn: string,
  status: "ENABLED" | "DISABLED",
  physicalId: string,
  streamArn?: string,
): cr.AwsSdkCall {
  return {
    service: "KinesisVideo",
    action: "updateMediaStorageConfiguration",
    parameters: {
      ChannelARN: channelArn,
      MediaStorageConfiguration: {
        Status: status,
        ...(streamArn ? { StreamARN: streamArn } : {}),
      },
    },
    physicalResourceId: cr.PhysicalResourceId.of(physicalId),
  };
}

function deviceResourceMapping(resources: DeviceResources[]): string {
  return JSON.stringify(
    Object.fromEntries(
      resources.map(({ deviceId, p2pChannel, storageChannel, archiveStream }) => [
        deviceId,
        {
          p2pChannelArn: p2pChannel.attrArn,
          storageChannelArn: storageChannel.attrArn,
          streamArn: archiveStream.attrArn,
        },
      ]),
    ),
  );
}

const maintenanceFunctionSource = `
const { SecretsManagerClient, GetSecretValueCommand } = require("@aws-sdk/client-secrets-manager");
const client = new SecretsManagerClient({});
let cachedSecret;

exports.handler = async () => {
  if (!cachedSecret) {
    const response = await client.send(new GetSecretValueCommand({
      SecretId: process.env.MAINTENANCE_SECRET_ARN,
    }));
    cachedSecret = response.SecretString;
  }
  if (!cachedSecret) throw new Error("Maintenance secret is unavailable");
  const response = await fetch(process.env.MAINTENANCE_URL, {
    method: "POST",
    headers: {
      "authorization": "Bearer " + cachedSecret,
      "content-type": "application/json",
    },
    body: "{}",
    signal: AbortSignal.timeout(20000),
  });
  if (!response.ok) throw new Error("Maintenance endpoint returned " + response.status);
  return { statusCode: response.status };
};
`;
