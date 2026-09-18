-- MaiBot.db 真实 DDL（2026-09-19 从 data/MaiBot.db 的 sqlite_master 只读导出）
-- 本文件是 TS 侧 Drizzle schema 的单一事实源；勿手改，重导出请用 scripts/dump_schema.py
-- 注意：与 Python 侧的差异仅在于建表语句追加了 IF NOT EXISTS（TS 侧 create-all 兜底用）。

PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS behavior_actions (
	id INTEGER NOT NULL, 
	session_id VARCHAR(255), 
	action TEXT NOT NULL, 
	action_hash VARCHAR(64) NOT NULL, 
	source_count INTEGER NOT NULL, 
	create_time DATETIME, 
	update_time DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_behavior_action_scope_hash UNIQUE (session_id, action_hash)
);

CREATE TABLE IF NOT EXISTS behavior_experience_paths (
	id INTEGER NOT NULL, 
	session_id VARCHAR(255), 
	scene_cluster_id INTEGER NOT NULL, 
	action_id INTEGER NOT NULL, 
	outcome_id INTEGER NOT NULL, 
	actor_type VARCHAR(40) NOT NULL, 
	learning_type VARCHAR(40) NOT NULL, 
	evidence_list TEXT NOT NULL, 
	feedback_list TEXT NOT NULL, 
	count INTEGER NOT NULL, 
	activation_count INTEGER NOT NULL, 
	success_count INTEGER NOT NULL, 
	failure_count INTEGER NOT NULL, 
	score FLOAT DEFAULT '0' NOT NULL, 
	enabled BOOLEAN DEFAULT '1' NOT NULL, 
	last_active_time DATETIME, 
	last_feedback_time DATETIME, 
	create_time DATETIME, 
	update_time DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_behavior_experience_path_scope_cluster_action_outcome_actor UNIQUE (session_id, scene_cluster_id, action_id, outcome_id, actor_type, learning_type)
);

CREATE TABLE IF NOT EXISTS behavior_outcomes (
	id INTEGER NOT NULL, 
	session_id VARCHAR(255), 
	outcome TEXT NOT NULL, 
	outcome_hash VARCHAR(64) NOT NULL, 
	source_count INTEGER NOT NULL, 
	create_time DATETIME, 
	update_time DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_behavior_outcome_scope_hash UNIQUE (session_id, outcome_hash)
);

CREATE TABLE IF NOT EXISTS behavior_scene_clusters (
	id INTEGER NOT NULL, 
	session_id VARCHAR(255), 
	tag_distribution TEXT NOT NULL, 
	source_count INTEGER NOT NULL, 
	update_time DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS behavior_scene_tag_clusters (
	id INTEGER NOT NULL, 
	tag_kind VARCHAR(40) NOT NULL, 
	tag TEXT NOT NULL, 
	cluster_key TEXT NOT NULL, 
	source_count INTEGER NOT NULL, 
	update_time DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_behavior_scene_tag_cluster_kind_tag UNIQUE (tag_kind, tag)
);

CREATE TABLE IF NOT EXISTS binary_data (
	id INTEGER NOT NULL, 
	data_hash VARCHAR(255) NOT NULL, 
	full_path VARCHAR(1024) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS bot_platform_accounts (
	id INTEGER NOT NULL, 
	platform VARCHAR(100) NOT NULL, 
	account_id VARCHAR(255) NOT NULL, 
	disabled BOOLEAN NOT NULL, 
	first_seen_at DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	disabled_at DATETIME, 
	last_source VARCHAR(32) NOT NULL, 
	last_adapter_id VARCHAR(255), 
	last_plugin_id VARCHAR(255), 
	last_gateway_name VARCHAR(255), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_bot_platform_accounts_platform_account UNIQUE (platform, account_id)
);

CREATE TABLE IF NOT EXISTS chat_sessions (
	id INTEGER NOT NULL, 
	session_id VARCHAR(255) NOT NULL, 
	created_timestamp DATETIME, 
	last_active_timestamp DATETIME, 
	user_id VARCHAR(255), 
	user_nickname VARCHAR(255), 
	user_cardname VARCHAR(255), 
	group_id VARCHAR(255), 
	group_name VARCHAR(255), 
	platform VARCHAR(100) NOT NULL, 
	account_id VARCHAR(255), 
	scope VARCHAR(255), 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS expressions (
	id INTEGER NOT NULL, 
	situation VARCHAR(255) NOT NULL, 
	style VARCHAR(255) NOT NULL, 
	content_list VARCHAR NOT NULL, 
	count INTEGER NOT NULL, 
	last_active_time DATETIME, 
	create_time DATETIME, 
	session_id VARCHAR(255), 
	checked BOOLEAN NOT NULL, 
	modified_by VARCHAR(4), 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS high_frequency_terms (
	id INTEGER NOT NULL, 
	chat_id VARCHAR(255) NOT NULL, 
	term TEXT NOT NULL, 
	rank INTEGER NOT NULL, 
	occurrence_count INTEGER NOT NULL, 
	message_count INTEGER NOT NULL, 
	frequency FLOAT NOT NULL, 
	message_frequency FLOAT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_high_frequency_terms_chat_term UNIQUE (chat_id, term)
);

CREATE TABLE IF NOT EXISTS images (
	id INTEGER NOT NULL, 
	image_hash VARCHAR(255) NOT NULL, 
	description VARCHAR NOT NULL, 
	full_path VARCHAR(1024) NOT NULL, 
	image_type VARCHAR(5), 
	query_count INTEGER NOT NULL, 
	is_registered BOOLEAN NOT NULL, 
	is_banned BOOLEAN NOT NULL, 
	no_file_flag BOOLEAN NOT NULL, 
	record_time DATETIME, 
	register_time DATETIME, 
	last_used_time DATETIME, 
	vlm_processed BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS jargons (
	id INTEGER NOT NULL, 
	content VARCHAR(255) NOT NULL, 
	evidence_messages TEXT, 
	meaning TEXT NOT NULL, 
	session_id_dict TEXT NOT NULL, 
	count INTEGER NOT NULL, 
	is_jargon BOOLEAN, 
	is_complete BOOLEAN NOT NULL, 
	is_global BOOLEAN NOT NULL, 
	last_inference_count INTEGER NOT NULL, 
	created_by VARCHAR(6) NOT NULL, 
	created_timestamp DATETIME, 
	updated_timestamp DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS llm_usage (
	id INTEGER NOT NULL, 
	model_name VARCHAR(255) NOT NULL, 
	model_assign_name VARCHAR(255), 
	model_api_provider_name VARCHAR(255) NOT NULL, 
	session_id VARCHAR(255) NOT NULL, 
	task_name VARCHAR(100), 
	request_type VARCHAR(50) NOT NULL, 
	time_cost FLOAT, 
	timestamp DATETIME, 
	prompt_tokens INTEGER NOT NULL, 
	completion_tokens INTEGER NOT NULL, 
	total_tokens INTEGER NOT NULL, 
	prompt_cache_enabled BOOLEAN DEFAULT '0' NOT NULL, 
	prompt_cache_hit_tokens INTEGER DEFAULT '0' NOT NULL, 
	prompt_cache_miss_tokens INTEGER DEFAULT '0' NOT NULL, 
	cost FLOAT NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS mai_messages (
	id INTEGER NOT NULL, 
	message_id VARCHAR(255) NOT NULL, 
	timestamp DATETIME, 
	platform VARCHAR(100) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	user_nickname VARCHAR(255) NOT NULL, 
	user_cardname VARCHAR(255), 
	group_id VARCHAR(255), 
	group_name VARCHAR(255), 
	is_mentioned BOOLEAN NOT NULL, 
	is_at BOOLEAN NOT NULL, 
	session_id VARCHAR(255) NOT NULL, 
	reply_to VARCHAR(255), 
	is_emoji BOOLEAN NOT NULL, 
	is_picture BOOLEAN NOT NULL, 
	is_command BOOLEAN NOT NULL, 
	is_notify BOOLEAN NOT NULL, 
	raw_content BLOB, 
	processed_plain_text VARCHAR, 
	additional_config VARCHAR, 
	reply_frequency FLOAT, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS maisaka_monitor_events (
	event_id INTEGER NOT NULL, 
	event_type VARCHAR(100) NOT NULL, 
	session_id VARCHAR(255) NOT NULL, 
	timestamp FLOAT NOT NULL, 
	schema_version INTEGER DEFAULT '1' NOT NULL, 
	payload_json TEXT NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (event_id)
);

CREATE TABLE IF NOT EXISTS maisaka_reply_effects (
	effect_id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(255) NOT NULL, 
	session_name VARCHAR(255) NOT NULL, 
	chat_type VARCHAR(20) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	created_at DATETIME, 
	finalized_at DATETIME, 
	strategy_primary VARCHAR(40) NOT NULL, 
	model_name VARCHAR(255) NOT NULL, 
	request_fingerprint VARCHAR(64) NOT NULL, 
	prompt_fingerprint VARCHAR(64) NOT NULL, 
	scorer_version INTEGER NOT NULL, 
	response_score FLOAT, 
	reception_score FLOAT, 
	conversation_score FLOAT, 
	raw_score FLOAT, 
	relative_score FLOAT, 
	confidence FLOAT DEFAULT '0' NOT NULL, 
	record_json TEXT NOT NULL, 
	record_blob BLOB, 
	PRIMARY KEY (effect_id)
);

CREATE TABLE IF NOT EXISTS one_time_maintenance_tasks (
	task_name VARCHAR(100) NOT NULL, 
	phase VARCHAR(50) NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	cursor_id INTEGER NOT NULL, 
	stats_json TEXT NOT NULL, 
	last_error TEXT, 
	completed_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (task_name)
);

CREATE TABLE IF NOT EXISTS online_time (
	id INTEGER NOT NULL, 
	timestamp DATETIME, 
	duration_minutes INTEGER NOT NULL, 
	start_timestamp DATETIME, 
	end_timestamp DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS person_info (
	id INTEGER NOT NULL, 
	is_known BOOLEAN NOT NULL, 
	person_id VARCHAR(255) NOT NULL, 
	person_name VARCHAR(255), 
	name_reason VARCHAR, 
	platform VARCHAR(100) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	user_nickname VARCHAR(255) NOT NULL, 
	group_cardname VARCHAR, 
	memory_points VARCHAR, 
	know_counts INTEGER NOT NULL, 
	first_known_time DATETIME, 
	last_known_time DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS statistics_aggregation_cursors (
	source_name VARCHAR(100) NOT NULL, 
	last_processed_id INTEGER NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (source_name)
);

CREATE TABLE IF NOT EXISTS statistics_message_hourly (
	id INTEGER NOT NULL, 
	bucket_time DATETIME NOT NULL, 
	chat_id VARCHAR(255) NOT NULL, 
	chat_name VARCHAR(255) NOT NULL, 
	chat_type VARCHAR(20) NOT NULL, 
	message_count INTEGER NOT NULL, 
	latest_timestamp DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_statistics_message_hourly_bucket_chat UNIQUE (bucket_time, chat_id)
);

CREATE TABLE IF NOT EXISTS statistics_model_hourly (
	id INTEGER NOT NULL, 
	bucket_time DATETIME NOT NULL, 
	request_type VARCHAR(100) NOT NULL, 
	module_name VARCHAR(100) NOT NULL, 
	provider_name VARCHAR(255) NOT NULL, 
	model_name VARCHAR(255) NOT NULL, 
	request_count INTEGER NOT NULL, 
	prompt_tokens INTEGER NOT NULL, 
	completion_tokens INTEGER NOT NULL, 
	total_tokens INTEGER NOT NULL, 
	cost FLOAT NOT NULL, 
	time_cost_sum FLOAT NOT NULL, 
	time_cost_sq_sum FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_statistics_model_hourly_bucket_request_model_provider UNIQUE (bucket_time, request_type, model_name, provider_name)
);

CREATE TABLE IF NOT EXISTS statistics_tool_hourly (
	id INTEGER NOT NULL, 
	bucket_time DATETIME NOT NULL, 
	tool_name VARCHAR(255) NOT NULL, 
	call_count INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_statistics_tool_hourly_bucket_tool UNIQUE (bucket_time, tool_name)
);

CREATE TABLE IF NOT EXISTS tool_records (
	id INTEGER NOT NULL, 
	tool_id VARCHAR(255) NOT NULL, 
	timestamp DATETIME, 
	session_id VARCHAR(255) NOT NULL, 
	tool_name VARCHAR(255) NOT NULL, 
	tool_reasoning VARCHAR, 
	tool_data VARCHAR, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_behavior_actions_action_hash ON behavior_actions (action_hash);
CREATE INDEX IF NOT EXISTS ix_behavior_actions_session_id ON behavior_actions (session_id);
CREATE INDEX IF NOT EXISTS ix_behavior_actions_update_time ON behavior_actions (update_time);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_action ON behavior_experience_paths (action_id);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_action_id ON behavior_experience_paths (action_id);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_actor_type ON behavior_experience_paths (actor_type);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_cluster ON behavior_experience_paths (scene_cluster_id);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_last_active_time ON behavior_experience_paths (last_active_time);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_learning_type ON behavior_experience_paths (learning_type);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_outcome ON behavior_experience_paths (outcome_id);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_outcome_id ON behavior_experience_paths (outcome_id);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_scene_cluster_id ON behavior_experience_paths (scene_cluster_id);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_session_enabled ON behavior_experience_paths (session_id, enabled);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_session_id ON behavior_experience_paths (session_id);
CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_update_time ON behavior_experience_paths (update_time);
CREATE INDEX IF NOT EXISTS ix_behavior_outcomes_outcome_hash ON behavior_outcomes (outcome_hash);
CREATE INDEX IF NOT EXISTS ix_behavior_outcomes_session_id ON behavior_outcomes (session_id);
CREATE INDEX IF NOT EXISTS ix_behavior_outcomes_update_time ON behavior_outcomes (update_time);
CREATE INDEX IF NOT EXISTS ix_behavior_scene_clusters_session_id ON behavior_scene_clusters (session_id);
CREATE INDEX IF NOT EXISTS ix_behavior_scene_clusters_update_time ON behavior_scene_clusters (update_time);
CREATE INDEX IF NOT EXISTS ix_behavior_scene_tag_clusters_kind_cluster ON behavior_scene_tag_clusters (tag_kind, cluster_key);
CREATE INDEX IF NOT EXISTS ix_behavior_scene_tag_clusters_tag_kind ON behavior_scene_tag_clusters (tag_kind);
CREATE INDEX IF NOT EXISTS ix_behavior_scene_tag_clusters_update_time ON behavior_scene_tag_clusters (update_time);
CREATE INDEX IF NOT EXISTS ix_binary_data_data_hash ON binary_data (data_hash);
CREATE INDEX IF NOT EXISTS ix_bot_platform_accounts_account_id ON bot_platform_accounts (account_id);
CREATE INDEX IF NOT EXISTS ix_bot_platform_accounts_disabled ON bot_platform_accounts (disabled);
CREATE INDEX IF NOT EXISTS ix_bot_platform_accounts_last_seen_at ON bot_platform_accounts (last_seen_at);
CREATE INDEX IF NOT EXISTS ix_bot_platform_accounts_platform ON bot_platform_accounts (platform);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_account_id ON chat_sessions (account_id);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_created_timestamp ON chat_sessions (created_timestamp);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_group_id ON chat_sessions (group_id);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_last_active_timestamp ON chat_sessions (last_active_timestamp);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_platform ON chat_sessions (platform);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_scope ON chat_sessions (scope);
CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_sessions_session_id ON chat_sessions (session_id);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_id ON chat_sessions (user_id);
CREATE INDEX IF NOT EXISTS ix_expressions_last_active_time ON expressions (last_active_time);
CREATE INDEX IF NOT EXISTS ix_expressions_situation ON expressions (situation);
CREATE INDEX IF NOT EXISTS ix_expressions_style ON expressions (style);
CREATE INDEX IF NOT EXISTS ix_high_frequency_terms_chat_id ON high_frequency_terms (chat_id);
CREATE INDEX IF NOT EXISTS ix_high_frequency_terms_chat_rank ON high_frequency_terms (chat_id, rank);
CREATE INDEX IF NOT EXISTS ix_high_frequency_terms_updated_at ON high_frequency_terms (updated_at);
CREATE INDEX IF NOT EXISTS ix_images_image_hash ON images (image_hash);
CREATE INDEX IF NOT EXISTS ix_images_record_time ON images (record_time);
CREATE INDEX IF NOT EXISTS ix_jargons_complete_count_id ON jargons (is_complete, count DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_jargons_content ON jargons (content);
CREATE INDEX IF NOT EXISTS ix_jargons_created_timestamp ON jargons (created_timestamp);
CREATE INDEX IF NOT EXISTS ix_jargons_global_count_id ON jargons (is_global, count DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_jargons_status_count_id ON jargons (is_jargon, count DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_jargons_updated_timestamp ON jargons (updated_timestamp);
CREATE INDEX IF NOT EXISTS ix_llm_usage_model_api_provider_name ON llm_usage (model_api_provider_name);
CREATE INDEX IF NOT EXISTS ix_llm_usage_model_assign_name ON llm_usage (model_assign_name);
CREATE INDEX IF NOT EXISTS ix_llm_usage_model_name ON llm_usage (model_name);
CREATE INDEX IF NOT EXISTS ix_llm_usage_session_id ON llm_usage (session_id);
CREATE INDEX IF NOT EXISTS ix_llm_usage_task_name ON llm_usage (task_name);
CREATE INDEX IF NOT EXISTS ix_llm_usage_timestamp ON llm_usage (timestamp);
CREATE INDEX IF NOT EXISTS ix_mai_messages_group_id ON mai_messages (group_id);
CREATE INDEX IF NOT EXISTS ix_mai_messages_message_id ON mai_messages (message_id);
CREATE INDEX IF NOT EXISTS ix_mai_messages_platform ON mai_messages (platform);
CREATE INDEX IF NOT EXISTS ix_mai_messages_platform_message_id ON mai_messages (platform, message_id);
CREATE INDEX IF NOT EXISTS ix_mai_messages_session_id ON mai_messages (session_id);
CREATE INDEX IF NOT EXISTS ix_mai_messages_user_id ON mai_messages (user_id);
CREATE INDEX IF NOT EXISTS ix_mai_messages_user_nickname ON mai_messages (user_nickname);
CREATE INDEX IF NOT EXISTS ix_maisaka_monitor_events_created_at ON maisaka_monitor_events (created_at);
CREATE INDEX IF NOT EXISTS ix_maisaka_monitor_events_session_event ON maisaka_monitor_events (session_id, event_id);
CREATE INDEX IF NOT EXISTS ix_maisaka_monitor_events_timestamp ON maisaka_monitor_events (timestamp);
CREATE INDEX IF NOT EXISTS ix_maisaka_monitor_events_type_event ON maisaka_monitor_events (event_type, event_id);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_chat_type ON maisaka_reply_effects (chat_type);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_created_at ON maisaka_reply_effects (created_at);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_finalized_at ON maisaka_reply_effects (finalized_at);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_model_name ON maisaka_reply_effects (model_name);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_prompt_fingerprint ON maisaka_reply_effects (prompt_fingerprint);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_request_fingerprint ON maisaka_reply_effects (request_fingerprint);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_scorer_version ON maisaka_reply_effects (scorer_version);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_session_id ON maisaka_reply_effects (session_id);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_status ON maisaka_reply_effects (status);
CREATE INDEX IF NOT EXISTS ix_maisaka_reply_effects_strategy_primary ON maisaka_reply_effects (strategy_primary);
CREATE INDEX IF NOT EXISTS ix_one_time_maintenance_tasks_updated_at ON one_time_maintenance_tasks (updated_at);
CREATE INDEX IF NOT EXISTS ix_online_time_timestamp ON online_time (timestamp);
CREATE UNIQUE INDEX IF NOT EXISTS ix_person_info_person_id ON person_info (person_id);
CREATE INDEX IF NOT EXISTS ix_person_info_platform ON person_info (platform);
CREATE INDEX IF NOT EXISTS ix_person_info_user_id ON person_info (user_id);
CREATE INDEX IF NOT EXISTS ix_person_info_user_nickname ON person_info (user_nickname);
CREATE INDEX IF NOT EXISTS ix_reply_effect_model_prompt ON maisaka_reply_effects (model_name, prompt_fingerprint);
CREATE INDEX IF NOT EXISTS ix_reply_effect_request_fingerprint ON maisaka_reply_effects (request_fingerprint);
CREATE INDEX IF NOT EXISTS ix_reply_effect_session_finalized ON maisaka_reply_effects (session_id, finalized_at);
CREATE INDEX IF NOT EXISTS ix_reply_effect_strategy_finalized ON maisaka_reply_effects (strategy_primary, finalized_at);
CREATE INDEX IF NOT EXISTS ix_statistics_aggregation_cursors_updated_at ON statistics_aggregation_cursors (updated_at);
CREATE INDEX IF NOT EXISTS ix_statistics_message_hourly_bucket_time ON statistics_message_hourly (bucket_time);
CREATE INDEX IF NOT EXISTS ix_statistics_model_hourly_bucket_time ON statistics_model_hourly (bucket_time);
CREATE INDEX IF NOT EXISTS ix_statistics_tool_hourly_bucket_time ON statistics_tool_hourly (bucket_time);
CREATE INDEX IF NOT EXISTS ix_tool_records_session_id ON tool_records (session_id);
CREATE INDEX IF NOT EXISTS ix_tool_records_timestamp ON tool_records (timestamp);
CREATE INDEX IF NOT EXISTS ix_tool_records_tool_id ON tool_records (tool_id);
CREATE INDEX IF NOT EXISTS ix_tool_records_tool_name ON tool_records (tool_name);