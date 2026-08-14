import {
  AdminGetUserCommand,
  AdminInitiateAuthCommand,
  AdminRespondToAuthChallengeCommand,
  CognitoIdentityProviderClient,
} from "@aws-sdk/client-cognito-identity-provider";
import { getRuntimeEnvironment } from "./runtime-env";
import type { WebAuthChallengeName } from "../db/web-auth";

type CognitoRuntimeEnvironment = {
  AWS_REGION?: string;
  COGNITO_USER_POOL_ID?: string;
  COGNITO_USER_POOL_CLIENT_ID?: string;
};

type CognitoAuthenticationResult = {
  IdToken?: string;
};

type CognitoResponse = {
  AuthenticationResult?: CognitoAuthenticationResult;
  ChallengeName?: string;
  ChallengeParameters?: Record<string, string>;
  Session?: string;
};

export type CognitoAuthenticatedIdentity = {
  email: string;
  fullName: string | null;
  subject: string;
  username: string;
};

export type CognitoAuthenticationOutcome =
  | { status: "authenticated"; identity: CognitoAuthenticatedIdentity }
  | {
      status: "challenge";
      challengeName: WebAuthChallengeName;
      username: string;
      cognitoSession: string;
    };

let sharedClient: CognitoIdentityProviderClient | undefined;
let sharedClientRegion = "";

export async function beginCognitoAuthentication(input: {
  username: string;
  password: string;
}): Promise<CognitoAuthenticationOutcome> {
  const config = cognitoConfig();
  const response = await cognitoClient(config.region).send(
    new AdminInitiateAuthCommand({
      UserPoolId: config.userPoolId,
      ClientId: config.clientId,
      AuthFlow: "ADMIN_USER_PASSWORD_AUTH",
      AuthParameters: {
        USERNAME: input.username,
        PASSWORD: input.password,
      },
    }),
  );
  return authenticationOutcome(response, input.username, config);
}

export async function respondToCognitoChallenge(input: {
  username: string;
  challengeName: WebAuthChallengeName;
  cognitoSession: string;
  response: string;
}): Promise<CognitoAuthenticationOutcome> {
  const config = cognitoConfig();
  const challengeResponses: Record<string, string> = {
    USERNAME: input.username,
  };
  if (input.challengeName === "NEW_PASSWORD_REQUIRED") {
    challengeResponses.NEW_PASSWORD = input.response;
  } else {
    challengeResponses.SOFTWARE_TOKEN_MFA_CODE = input.response;
  }
  const response = await cognitoClient(config.region).send(
    new AdminRespondToAuthChallengeCommand({
      UserPoolId: config.userPoolId,
      ClientId: config.clientId,
      ChallengeName: input.challengeName,
      ChallengeResponses: challengeResponses,
      Session: input.cognitoSession,
    }),
  );
  return authenticationOutcome(response, input.username, config);
}

function authenticationOutcome(
  response: CognitoResponse,
  fallbackUsername: string,
  config: ReturnType<typeof cognitoConfig>,
): Promise<CognitoAuthenticationOutcome> {
  if (response.AuthenticationResult) {
    return authenticatedIdentity(fallbackUsername, config);
  }
  if (
    (response.ChallengeName === "NEW_PASSWORD_REQUIRED" ||
      response.ChallengeName === "SOFTWARE_TOKEN_MFA") &&
    response.Session
  ) {
    return Promise.resolve({
      status: "challenge",
      challengeName: response.ChallengeName,
      username: canonicalUsername(response.ChallengeParameters, fallbackUsername),
      cognitoSession: response.Session,
    });
  }
  throw new Error("UNSUPPORTED_COGNITO_AUTH_RESPONSE");
}

async function authenticatedIdentity(
  username: string,
  config: ReturnType<typeof cognitoConfig>,
): Promise<CognitoAuthenticationOutcome> {
  const user = await cognitoClient(config.region).send(
    new AdminGetUserCommand({
      UserPoolId: config.userPoolId,
      Username: username,
    }),
  );
  const attributes = Object.fromEntries(
    (user.UserAttributes ?? []).flatMap((attribute) =>
      attribute.Name && attribute.Value ? [[attribute.Name, attribute.Value]] : [],
    ),
  );
  const canonical = stringClaim(user.Username, 256);
  const subject = stringClaim(attributes.sub, 256);
  const email = normalizeEmail(attributes.email);
  if (
    attributes.email_verified !== "true" ||
    !subject ||
    !canonical ||
    !email
  ) {
    throw new Error("INVALID_COGNITO_USER");
  }
  return {
    status: "authenticated",
    identity: {
      email,
      fullName: displayName(attributes.name),
      subject,
      username: canonical,
    },
  };
}

function canonicalUsername(
  parameters: Record<string, string> | undefined,
  fallback: string,
) {
  const value =
    parameters?.USER_ID_FOR_SRP ?? parameters?.USERNAME ?? fallback;
  return stringClaim(value, 256) ?? fallback;
}

function cognitoConfig() {
  const runtime = getRuntimeEnvironment() as CognitoRuntimeEnvironment;
  const region = runtime.AWS_REGION?.trim();
  const userPoolId = runtime.COGNITO_USER_POOL_ID?.trim();
  const clientId = runtime.COGNITO_USER_POOL_CLIENT_ID?.trim();
  if (
    !region ||
    !/^[a-z]{2}-[a-z]+-\d$/.test(region) ||
    !userPoolId ||
    !/^[\w-]{1,128}$/.test(userPoolId) ||
    !clientId ||
    !/^[A-Za-z0-9]{1,128}$/.test(clientId)
  ) {
    throw new Error("COGNITO_AUTH_NOT_CONFIGURED");
  }
  return { region, userPoolId, clientId };
}

function cognitoClient(region: string) {
  if (!sharedClient || sharedClientRegion !== region) {
    sharedClient = new CognitoIdentityProviderClient({ region });
    sharedClientRegion = region;
  }
  return sharedClient;
}

function stringClaim(value: unknown, maxLength: number) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if (
    !normalized ||
    normalized.length > maxLength ||
    /[\u0000-\u001f\u007f]/.test(normalized)
  ) {
    return null;
  }
  return normalized;
}

function normalizeEmail(value: unknown) {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  if (
    normalized.length < 3 ||
    normalized.length > 254 ||
    !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalized)
  ) {
    return null;
  }
  return normalized;
}

function displayName(value: unknown) {
  return stringClaim(value, 200);
}
