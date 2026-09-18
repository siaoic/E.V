// 由 scripts/generate_drizzle_schema.py 从 src/db/schema.sql 生成；勿手改。
// 列名显式为蛇形（与真实库一致）；索引与 UNIQUE 约束以 schema.sql 为准。
import { blob, integer, real, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const behaviorActions = sqliteTable("behavior_actions", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    sessionId: text("session_id"),
    action: text("action").notNull(),
    actionHash: text("action_hash").notNull(),
    sourceCount: integer("source_count").notNull(),
    createTime: text("create_time"),
    updateTime: text("update_time"),
    // 表级约束（以 schema.sql 为准）：
    //   CONSTRAINT uq_behavior_action_scope_hash UNIQUE (session_id, action_hash)
});

export const behaviorExperiencePaths = sqliteTable("behavior_experience_paths", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    sessionId: text("session_id"),
    sceneClusterId: integer("scene_cluster_id").notNull(),
    actionId: integer("action_id").notNull(),
    outcomeId: integer("outcome_id").notNull(),
    actorType: text("actor_type").notNull(),
    learningType: text("learning_type").notNull(),
    evidenceList: text("evidence_list").notNull(),
    feedbackList: text("feedback_list").notNull(),
    count: integer("count").notNull(),
    activationCount: integer("activation_count").notNull(),
    successCount: integer("success_count").notNull(),
    failureCount: integer("failure_count").notNull(),
    score: real("score").notNull().default(0.0),
    enabled: integer("enabled", { mode: "boolean" }).notNull().default(true),
    lastActiveTime: text("last_active_time"),
    lastFeedbackTime: text("last_feedback_time"),
    createTime: text("create_time"),
    updateTime: text("update_time"),
    // 表级约束（以 schema.sql 为准）：
    //   CONSTRAINT uq_behavior_experience_path_scope_cluster_action_outcome_actor UNIQUE (session_id, scene_cluster_id, action_id, outcome_id, actor_type, learning_type)
});

export const behaviorOutcomes = sqliteTable("behavior_outcomes", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    sessionId: text("session_id"),
    outcome: text("outcome").notNull(),
    outcomeHash: text("outcome_hash").notNull(),
    sourceCount: integer("source_count").notNull(),
    createTime: text("create_time"),
    updateTime: text("update_time"),
    // 表级约束（以 schema.sql 为准）：
    //   CONSTRAINT uq_behavior_outcome_scope_hash UNIQUE (session_id, outcome_hash)
});

export const behaviorSceneClusters = sqliteTable("behavior_scene_clusters", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    sessionId: text("session_id"),
    tagDistribution: text("tag_distribution").notNull(),
    sourceCount: integer("source_count").notNull(),
    updateTime: text("update_time"),
});

export const behaviorSceneTagClusters = sqliteTable("behavior_scene_tag_clusters", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    tagKind: text("tag_kind").notNull(),
    tag: text("tag").notNull(),
    clusterKey: text("cluster_key").notNull(),
    sourceCount: integer("source_count").notNull(),
    updateTime: text("update_time"),
    // 表级约束（以 schema.sql 为准）：
    //   CONSTRAINT uq_behavior_scene_tag_cluster_kind_tag UNIQUE (tag_kind, tag)
});

export const binaryData = sqliteTable("binary_data", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    dataHash: text("data_hash").notNull(),
    fullPath: text("full_path").notNull(),
});

export const botPlatformAccounts = sqliteTable("bot_platform_accounts", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    platform: text("platform").notNull(),
    accountId: text("account_id").notNull(),
    disabled: integer("disabled", { mode: "boolean" }).notNull(),
    firstSeenAt: text("first_seen_at").notNull(),
    lastSeenAt: text("last_seen_at").notNull(),
    disabledAt: text("disabled_at"),
    lastSource: text("last_source").notNull(),
    lastAdapterId: text("last_adapter_id"),
    lastPluginId: text("last_plugin_id"),
    lastGatewayName: text("last_gateway_name"),
    // 表级约束（以 schema.sql 为准）：
    //   CONSTRAINT uq_bot_platform_accounts_platform_account UNIQUE (platform, account_id)
});

export const chatSessions = sqliteTable("chat_sessions", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    sessionId: text("session_id").notNull(),
    createdTimestamp: text("created_timestamp"),
    lastActiveTimestamp: text("last_active_timestamp"),
    userId: text("user_id"),
    userNickname: text("user_nickname"),
    userCardname: text("user_cardname"),
    groupId: text("group_id"),
    groupName: text("group_name"),
    platform: text("platform").notNull(),
    accountId: text("account_id"),
    scope: text("scope"),
});

export const expressions = sqliteTable("expressions", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    situation: text("situation").notNull(),
    style: text("style").notNull(),
    contentList: text("content_list").notNull(),
    count: integer("count").notNull(),
    lastActiveTime: text("last_active_time"),
    createTime: text("create_time"),
    sessionId: text("session_id"),
    checked: integer("checked", { mode: "boolean" }).notNull(),
    modifiedBy: text("modified_by"),
});

export const highFrequencyTerms = sqliteTable("high_frequency_terms", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    chatId: text("chat_id").notNull(),
    term: text("term").notNull(),
    rank: integer("rank").notNull(),
    occurrenceCount: integer("occurrence_count").notNull(),
    messageCount: integer("message_count").notNull(),
    frequency: real("frequency").notNull(),
    messageFrequency: real("message_frequency").notNull(),
    createdAt: text("created_at").notNull(),
    updatedAt: text("updated_at").notNull(),
    // 表级约束（以 schema.sql 为准）：
    //   CONSTRAINT uq_high_frequency_terms_chat_term UNIQUE (chat_id, term)
});

export const images = sqliteTable("images", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    imageHash: text("image_hash").notNull(),
    description: text("description").notNull(),
    fullPath: text("full_path").notNull(),
    imageType: text("image_type"),
    queryCount: integer("query_count").notNull(),
    isRegistered: integer("is_registered", { mode: "boolean" }).notNull(),
    isBanned: integer("is_banned", { mode: "boolean" }).notNull(),
    noFileFlag: integer("no_file_flag", { mode: "boolean" }).notNull(),
    recordTime: text("record_time"),
    registerTime: text("register_time"),
    lastUsedTime: text("last_used_time"),
    vlmProcessed: integer("vlm_processed", { mode: "boolean" }).notNull(),
});

export const jargons = sqliteTable("jargons", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    content: text("content").notNull(),
    evidenceMessages: text("evidence_messages"),
    meaning: text("meaning").notNull(),
    sessionIdDict: text("session_id_dict").notNull(),
    count: integer("count").notNull(),
    isJargon: integer("is_jargon"),
    isComplete: integer("is_complete", { mode: "boolean" }).notNull(),
    isGlobal: integer("is_global", { mode: "boolean" }).notNull(),
    lastInferenceCount: integer("last_inference_count").notNull(),
    createdBy: text("created_by").notNull(),
    createdTimestamp: text("created_timestamp"),
    updatedTimestamp: text("updated_timestamp"),
});

export const llmUsage = sqliteTable("llm_usage", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    modelName: text("model_name").notNull(),
    modelAssignName: text("model_assign_name"),
    modelApiProviderName: text("model_api_provider_name").notNull(),
    sessionId: text("session_id").notNull(),
    taskName: text("task_name"),
    requestType: text("request_type").notNull(),
    timeCost: real("time_cost"),
    timestamp: text("timestamp"),
    promptTokens: integer("prompt_tokens").notNull(),
    completionTokens: integer("completion_tokens").notNull(),
    totalTokens: integer("total_tokens").notNull(),
    promptCacheEnabled: integer("prompt_cache_enabled", { mode: "boolean" }).notNull().default(false),
    promptCacheHitTokens: integer("prompt_cache_hit_tokens").notNull().default(0),
    promptCacheMissTokens: integer("prompt_cache_miss_tokens").notNull().default(0),
    cost: real("cost").notNull(),
});

export const maiMessages = sqliteTable("mai_messages", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    messageId: text("message_id").notNull(),
    timestamp: text("timestamp"),
    platform: text("platform").notNull(),
    userId: text("user_id").notNull(),
    userNickname: text("user_nickname").notNull(),
    userCardname: text("user_cardname"),
    groupId: text("group_id"),
    groupName: text("group_name"),
    isMentioned: integer("is_mentioned", { mode: "boolean" }).notNull(),
    isAt: integer("is_at", { mode: "boolean" }).notNull(),
    sessionId: text("session_id").notNull(),
    replyTo: text("reply_to"),
    isEmoji: integer("is_emoji", { mode: "boolean" }).notNull(),
    isPicture: integer("is_picture", { mode: "boolean" }).notNull(),
    isCommand: integer("is_command", { mode: "boolean" }).notNull(),
    isNotify: integer("is_notify", { mode: "boolean" }).notNull(),
    rawContent: blob("raw_content"),
    processedPlainText: text("processed_plain_text"),
    additionalConfig: text("additional_config"),
    replyFrequency: real("reply_frequency"),
});

export const maisakaMonitorEvents = sqliteTable("maisaka_monitor_events", {
    eventId: integer("event_id").primaryKey({ autoIncrement: true }),
    eventType: text("event_type").notNull(),
    sessionId: text("session_id").notNull(),
    timestamp: real("timestamp").notNull(),
    schemaVersion: integer("schema_version").notNull().default(1),
    payloadJson: text("payload_json").notNull(),
    createdAt: text("created_at"),
});

export const maisakaReplyEffects = sqliteTable("maisaka_reply_effects", {
    effectId: text("effect_id").primaryKey(),
    sessionId: text("session_id").notNull(),
    sessionName: text("session_name").notNull(),
    chatType: text("chat_type").notNull(),
    status: text("status").notNull(),
    createdAt: text("created_at"),
    finalizedAt: text("finalized_at"),
    strategyPrimary: text("strategy_primary").notNull(),
    modelName: text("model_name").notNull(),
    requestFingerprint: text("request_fingerprint").notNull(),
    promptFingerprint: text("prompt_fingerprint").notNull(),
    scorerVersion: integer("scorer_version").notNull(),
    responseScore: real("response_score"),
    receptionScore: real("reception_score"),
    conversationScore: real("conversation_score"),
    rawScore: real("raw_score"),
    relativeScore: real("relative_score"),
    confidence: real("confidence").notNull().default(0.0),
    recordJson: text("record_json").notNull(),
    recordBlob: blob("record_blob"),
});

export const oneTimeMaintenanceTasks = sqliteTable("one_time_maintenance_tasks", {
    taskName: text("task_name").primaryKey(),
    phase: text("phase").notNull(),
    status: text("status").notNull(),
    cursorId: integer("cursor_id").notNull(),
    statsJson: text("stats_json").notNull(),
    lastError: text("last_error"),
    completedAt: text("completed_at"),
    updatedAt: text("updated_at"),
});

export const onlineTime = sqliteTable("online_time", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    timestamp: text("timestamp"),
    durationMinutes: integer("duration_minutes").notNull(),
    startTimestamp: text("start_timestamp"),
    endTimestamp: text("end_timestamp"),
});

export const personInfo = sqliteTable("person_info", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    isKnown: integer("is_known", { mode: "boolean" }).notNull(),
    personId: text("person_id").notNull(),
    personName: text("person_name"),
    nameReason: text("name_reason"),
    platform: text("platform").notNull(),
    userId: text("user_id").notNull(),
    userNickname: text("user_nickname").notNull(),
    groupCardname: text("group_cardname"),
    memoryPoints: text("memory_points"),
    knowCounts: integer("know_counts").notNull(),
    firstKnownTime: text("first_known_time"),
    lastKnownTime: text("last_known_time"),
});

export const statisticsAggregationCursors = sqliteTable("statistics_aggregation_cursors", {
    sourceName: text("source_name").primaryKey(),
    lastProcessedId: integer("last_processed_id").notNull(),
    updatedAt: text("updated_at"),
});

export const statisticsMessageHourly = sqliteTable("statistics_message_hourly", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    bucketTime: text("bucket_time").notNull(),
    chatId: text("chat_id").notNull(),
    chatName: text("chat_name").notNull(),
    chatType: text("chat_type").notNull(),
    messageCount: integer("message_count").notNull(),
    latestTimestamp: text("latest_timestamp").notNull(),
    // 表级约束（以 schema.sql 为准）：
    //   CONSTRAINT uq_statistics_message_hourly_bucket_chat UNIQUE (bucket_time, chat_id)
});

export const statisticsModelHourly = sqliteTable("statistics_model_hourly", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    bucketTime: text("bucket_time").notNull(),
    requestType: text("request_type").notNull(),
    moduleName: text("module_name").notNull(),
    providerName: text("provider_name").notNull(),
    modelName: text("model_name").notNull(),
    requestCount: integer("request_count").notNull(),
    promptTokens: integer("prompt_tokens").notNull(),
    completionTokens: integer("completion_tokens").notNull(),
    totalTokens: integer("total_tokens").notNull(),
    cost: real("cost").notNull(),
    timeCostSum: real("time_cost_sum").notNull(),
    timeCostSqSum: real("time_cost_sq_sum").notNull(),
    // 表级约束（以 schema.sql 为准）：
    //   CONSTRAINT uq_statistics_model_hourly_bucket_request_model_provider UNIQUE (bucket_time, request_type, model_name, provider_name)
});

export const statisticsToolHourly = sqliteTable("statistics_tool_hourly", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    bucketTime: text("bucket_time").notNull(),
    toolName: text("tool_name").notNull(),
    callCount: integer("call_count").notNull(),
    // 表级约束（以 schema.sql 为准）：
    //   CONSTRAINT uq_statistics_tool_hourly_bucket_tool UNIQUE (bucket_time, tool_name)
});

export const toolRecords = sqliteTable("tool_records", {
    id: integer("id").primaryKey({ autoIncrement: true }),
    toolId: text("tool_id").notNull(),
    timestamp: text("timestamp"),
    sessionId: text("session_id").notNull(),
    toolName: text("tool_name").notNull(),
    toolReasoning: text("tool_reasoning"),
    toolData: text("tool_data"),
});

export const allTables = {
    behaviorActions,
    behaviorExperiencePaths,
    behaviorOutcomes,
    behaviorSceneClusters,
    behaviorSceneTagClusters,
    binaryData,
    botPlatformAccounts,
    chatSessions,
    expressions,
    highFrequencyTerms,
    images,
    jargons,
    llmUsage,
    maiMessages,
    maisakaMonitorEvents,
    maisakaReplyEffects,
    oneTimeMaintenanceTasks,
    onlineTime,
    personInfo,
    statisticsAggregationCursors,
    statisticsMessageHourly,
    statisticsModelHourly,
    statisticsToolHourly,
    toolRecords,
} as const;
