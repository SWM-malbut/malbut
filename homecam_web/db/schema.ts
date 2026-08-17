import {
  bigint,
  index,
  integer,
  pgTable,
  primaryKey,
  real,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";
import { sql } from "drizzle-orm";

const timestampText = (name: string) =>
  timestamp(name, { mode: "string", withTimezone: true });

export const devices = pgTable(
  "devices",
  {
    id: text("id").primaryKey(),
    displayName: text("display_name").notNull(),
    kvsChannelArn: text("kvs_channel_arn").notNull(),
    createdAt: timestampText("created_at").notNull().defaultNow(),
  },
  (table) => [uniqueIndex("devices_kvs_channel_arn_idx").on(table.kvsChannelArn)],
);

export const deviceMemberships = pgTable(
  "device_memberships",
  {
    deviceId: text("device_id")
      .notNull()
      .references(() => devices.id, { onDelete: "cascade" }),
    userEmail: text("user_email").notNull(),
    role: text("role").notNull().default("owner"),
    bindingGeneration: bigint("binding_generation", { mode: "bigint" })
      .notNull(),
    createdAt: timestampText("created_at").notNull().defaultNow(),
  },
  (table) => [
    primaryKey({ columns: [table.deviceId, table.userEmail] }),
    index("device_memberships_user_email_idx").on(table.userEmail),
  ],
);

export const streamSessions = pgTable(
  "stream_sessions",
  {
    id: text("id").primaryKey(),
    roomCode: text("room_code").notNull(),
    deviceId: text("device_id")
      .notNull()
      .references(() => devices.id, { onDelete: "cascade" }),
    startedBy: text("started_by").notNull(),
    status: text("status").notNull().default("active"),
    startedAt: timestampText("started_at").notNull(),
    expiresAt: timestampText("expires_at").notNull(),
    endedAt: timestampText("ended_at"),
  },
  (table) => [
    uniqueIndex("stream_sessions_room_code_idx").on(table.roomCode),
    index("stream_sessions_device_status_idx").on(table.deviceId, table.status),
  ],
);

export const streamSessionAccess = pgTable("stream_session_access", {
  sessionId: text("session_id")
    .primaryKey()
    .references(() => streamSessions.id, { onDelete: "cascade" }),
  secretDigest: text("secret_digest").notNull(),
  authVersion: text("auth_version").notNull(),
  createdAt: timestampText("created_at").notNull().defaultNow(),
});

export const recordingSessions = pgTable(
  "recording_sessions",
  {
    sessionId: text("session_id")
      .primaryKey()
      .references(() => streamSessions.id, { onDelete: "cascade" }),
    kvsStreamArn: text("kvs_stream_arn").notNull(),
    kvsChannelArn: text("kvs_channel_arn").notNull(),
    startedAt: timestampText("started_at"),
    endedAt: timestampText("ended_at"),
    createdAt: timestampText("created_at").notNull().defaultNow(),
  },
  (table) => [index("recording_sessions_started_at_idx").on(table.startedAt)],
);

export const requestRateLimits = pgTable("request_rate_limits", {
  rateKey: text("rate_key").primaryKey(),
  windowStartedAt: bigint("window_started_at", { mode: "number" }).notNull(),
  requestCount: integer("request_count").notNull(),
});

export const deviceCredentials = pgTable(
  "device_credentials",
  {
    id: text("id").primaryKey(),
    deviceId: text("device_id")
      .notNull()
      .references(() => devices.id, { onDelete: "cascade" }),
    label: text("label").notNull(),
    tokenDigest: text("token_digest").notNull(),
    createdAt: timestampText("created_at").notNull().defaultNow(),
    lastUsedAt: timestampText("last_used_at"),
    expiresAt: timestampText("expires_at"),
    revokedAt: timestampText("revoked_at"),
  },
  (table) => [
    uniqueIndex("device_credentials_token_digest_idx").on(table.tokenDigest),
    index("device_credentials_device_id_idx").on(table.deviceId),
  ],
);

export const deviceState = pgTable("device_state", {
  deviceId: text("device_id")
    .primaryKey()
    .references(() => devices.id, { onDelete: "cascade" }),
  monitoringEnabled: integer("monitoring_enabled").notNull().default(0),
  cameraEnabled: integer("camera_enabled").notNull().default(1),
  microphoneEnabled: integer("microphone_enabled").notNull().default(1),
  sourceProfile: text("source_profile").notNull().default("unknown"),
  imageTopic: text("image_topic"),
  activeStreamMode: text("active_stream_mode").notNull().default("idle"),
  activeSessionId: text("active_session_id").references(() => streamSessions.id, {
    onDelete: "set null",
  }),
  mediaHealthy: integer("media_healthy").notNull().default(0),
  detectorHealthy: integer("detector_healthy").notNull().default(0),
  lastSeenAt: timestampText("last_seen_at"),
  updatedAt: timestampText("updated_at").notNull().defaultNow(),
});

export const homecamEvents = pgTable(
  "homecam_events",
  {
    id: text("id").primaryKey(),
    deviceId: text("device_id")
      .notNull()
      .references(() => devices.id, { onDelete: "cascade" }),
    eventType: text("event_type").notNull(),
    confidence: real("confidence"),
    occurredAt: timestampText("occurred_at").notNull(),
    receivedAt: timestampText("received_at").notNull().defaultNow(),
    idempotencyKey: text("idempotency_key").notNull(),
    requestFingerprint: text("request_fingerprint").notNull(),
    recordingSessionId: text("recording_session_id").references(
      () => streamSessions.id,
      { onDelete: "set null" },
    ),
    recordingOffsetMs: integer("recording_offset_ms"),
  },
  (table) => [
    uniqueIndex("homecam_events_device_idempotency_idx").on(
      table.deviceId,
      table.idempotencyKey,
    ),
    index("homecam_events_device_occurred_idx").on(
      table.deviceId,
      table.occurredAt,
    ),
  ],
);

export const homecamPushOutbox = pgTable(
  "homecam_push_outbox",
  {
    eventId: text("event_id")
      .primaryKey()
      .references(() => homecamEvents.id, { onDelete: "cascade" }),
    deviceId: text("device_id")
      .notNull()
      .references(() => devices.id, { onDelete: "cascade" }),
    attemptCount: integer("attempt_count").notNull().default(0),
    nextAttemptAt: timestampText("next_attempt_at").notNull().defaultNow(),
    deliveredAt: timestampText("delivered_at"),
    lastError: text("last_error"),
    createdAt: timestampText("created_at").notNull().defaultNow(),
  },
  (table) => [
    index("homecam_push_outbox_due_idx").on(
      table.deviceId,
      table.deliveredAt,
      table.nextAttemptAt,
    ),
  ],
);

export const pushSubscriptions = pgTable(
  "push_subscriptions",
  {
    id: text("id").primaryKey(),
    deviceId: text("device_id")
      .notNull()
      .references(() => devices.id, { onDelete: "cascade" }),
    userEmail: text("user_email").notNull(),
    endpoint: text("endpoint").notNull(),
    p256dh: text("p256dh").notNull(),
    auth: text("auth").notNull(),
    createdAt: timestampText("created_at").notNull().defaultNow(),
    updatedAt: timestampText("updated_at").notNull().defaultNow(),
    revokedAt: timestampText("revoked_at"),
  },
  (table) => [
    uniqueIndex("push_subscriptions_user_device_endpoint_idx").on(
      table.userEmail,
      table.deviceId,
      table.endpoint,
    ),
    index("push_subscriptions_device_id_idx").on(table.deviceId),
  ],
);

export const accessAuditLog = pgTable(
  "access_audit_log",
  {
    id: text("id").primaryKey(),
    deviceId: text("device_id")
      .notNull()
      .references(() => devices.id, { onDelete: "cascade" }),
    actorType: text("actor_type").notNull(),
    actorId: text("actor_id").notNull(),
    action: text("action").notNull(),
    metadataJson: text("metadata_json"),
    createdAt: timestampText("created_at").notNull().defaultNow(),
  },
  (table) => [
    index("access_audit_log_device_created_idx").on(table.deviceId, table.createdAt),
  ],
);

export const talkLeases = pgTable("talk_leases", {
  deviceId: text("device_id")
    .primaryKey()
    .references(() => devices.id, { onDelete: "cascade" }),
  leaseId: text("lease_id").notNull(),
  userEmail: text("user_email").notNull(),
  clientId: text("client_id").notNull(),
  expiresAt: timestampText("expires_at").notNull(),
  createdAt: timestampText("created_at").notNull().defaultNow(),
  updatedAt: timestampText("updated_at").notNull().defaultNow(),
});

export const robotMaps = pgTable("robot_maps", {
  deviceId: text("device_id")
    .primaryKey()
    .references(() => devices.id, { onDelete: "cascade" }),
  revision: text("revision").notNull(),
  mapId: text("map_id").notNull(),
  mapRevision: text("map_revision").notNull(),
  width: integer("width").notNull(),
  height: integer("height").notNull(),
  resolution: real("resolution").notNull(),
  originX: real("origin_x").notNull(),
  originY: real("origin_y").notNull(),
  originYaw: real("origin_yaw").notNull(),
  previewBase64: text("preview_base64").notNull(),
  userMapJson: text("user_map_json"),
  semanticZonesJson: text("semantic_zones_json"),
  serverGeneration: bigint("server_generation", { mode: "bigint" })
    .notNull(),
  sourceCreatedAt: timestampText("source_created_at"),
  updatedAt: timestampText("updated_at").notNull().defaultNow(),
});

export const robotMapDrafts = pgTable("robot_map_drafts", {
  deviceId: text("device_id")
    .primaryKey()
    .references(() => devices.id, { onDelete: "cascade" }),
  revision: text("revision").notNull(),
  mapId: text("map_id").notNull(),
  mapRevision: text("map_revision").notNull(),
  width: integer("width").notNull(),
  height: integer("height").notNull(),
  resolution: real("resolution").notNull(),
  originX: real("origin_x").notNull(),
  originY: real("origin_y").notNull(),
  originYaw: real("origin_yaw").notNull(),
  previewBase64: text("preview_base64").notNull(),
  userMapJson: text("user_map_json"),
  semanticZonesJson: text("semantic_zones_json"),
  sourceCreatedAt: timestampText("source_created_at"),
  updatedAt: timestampText("updated_at").notNull().defaultNow(),
});

export const robotRuntimeState = pgTable("robot_runtime_state", {
  deviceId: text("device_id")
    .primaryKey()
    .references(() => devices.id, { onDelete: "cascade" }),
  state: text("state").notNull(),
  message: text("message").notNull(),
  poseX: real("pose_x"),
  poseY: real("pose_y"),
  poseYaw: real("pose_yaw"),
  localizationState: text("localization_state").notNull(),
  tfAgeS: real("tf_age_s"),
  nav2Json: text("nav2_json").notNull(),
  targetJson: text("target_json"),
  mapRevisionCounter: integer("map_revision_counter").notNull(),
  observedAt: timestampText("observed_at").notNull(),
  updatedAt: timestampText("updated_at").notNull().defaultNow(),
});

export const robotCommands = pgTable(
  "robot_commands",
  {
    id: text("id").primaryKey(),
    deviceId: text("device_id")
      .notNull()
      .references(() => devices.id, { onDelete: "cascade" }),
    operation: text("operation").notNull(),
    payloadJson: text("payload_json").notNull().default("{}"),
    requestedBy: text("requested_by").notNull(),
    status: text("status").notNull().default("queued"),
    requestedAt: timestampText("requested_at").notNull(),
    claimedAt: timestampText("claimed_at"),
    completedAt: timestampText("completed_at"),
    resultJson: text("result_json"),
  },
  (table) => [
    index("robot_commands_device_status_idx").on(table.deviceId, table.status),
    uniqueIndex("robot_commands_one_active_idx")
      .on(table.deviceId)
      .where(sql`${table.status} IN ('queued', 'claimed')`),
  ],
);
