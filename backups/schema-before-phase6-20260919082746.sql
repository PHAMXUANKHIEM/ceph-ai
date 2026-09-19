--
-- PostgreSQL database dump
--

\restrict zZjBly1dQSLJe6MKVTbX2egW1hxuZ4tsezYUTZcQ9TSo7s8fYF8ZmzQJrXL92fs

-- Dumped from database version 18.4 - Percona Server for PostgreSQL 18.4.1
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pgbouncer; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA pgbouncer;


ALTER SCHEMA pgbouncer OWNER TO postgres;

--
-- Name: pgaudit; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgaudit WITH SCHEMA public;


--
-- Name: EXTENSION pgaudit; Type: COMMENT; Schema: -; Owner:
--

COMMENT ON EXTENSION pgaudit IS 'provides auditing functionality';


--
-- Name: get_auth(text); Type: FUNCTION; Schema: pgbouncer; Owner: postgres
--

CREATE FUNCTION pgbouncer.get_auth(username text) RETURNS TABLE(username text, password text)
    LANGUAGE sql STABLE SECURITY DEFINER
    AS $_$
  SELECT rolname::TEXT, rolpassword::TEXT
  FROM pg_catalog.pg_authid
  WHERE pg_authid.rolname = $1
    AND pg_authid.rolcanlogin
    AND (NOT pg_authid.rolreplication OR pg_authid.rolname = 'postgres')
    AND pg_authid.rolname <>  E'_crunchypgbouncer'
    AND (pg_authid.rolvaliduntil IS NULL OR pg_authid.rolvaliduntil >= CURRENT_TIMESTAMP)$_$;


ALTER FUNCTION pgbouncer.get_auth(username text) OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: action_policy_override_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.action_policy_override_audit (
    id character varying(36) NOT NULL,
    action_id character varying(64) NOT NULL,
    actor character varying(64) NOT NULL,
    previous_classification character varying(16) NOT NULL,
    new_classification character varying(16) NOT NULL,
    reason text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.action_policy_override_audit OWNER TO postgres;

--
-- Name: action_policy_overrides; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.action_policy_overrides (
    action_id character varying(64) NOT NULL,
    classification character varying(16) NOT NULL,
    updated_by character varying(64) NOT NULL,
    reason text NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.action_policy_overrides OWNER TO postgres;

--
-- Name: actions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.actions (
    id character varying(36) NOT NULL,
    incident_id character varying(36) NOT NULL,
    action_id character varying(64) NOT NULL,
    classification character varying(16) NOT NULL,
    status character varying(32) NOT NULL,
    proposed_command text,
    executed_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    rationale text,
    target_nodes text,
    action_params text,
    execution_progress text,
    telegram_notified_at timestamp without time zone,
    telegram_message_ids text,
    expires_at timestamp without time zone,
    idempotency_key character varying(64),
    grace_until timestamp without time zone,
    cancelled_at timestamp without time zone,
    cancelled_by character varying(64),
    CONSTRAINT ck_actions_classification_valid CHECK (((classification)::text = ANY ((ARRAY['READ_ONLY'::character varying, 'SAFE'::character varying, 'RISKY'::character varying, 'DESTRUCTIVE'::character varying])::text[]))),
    CONSTRAINT ck_actions_status_valid CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'AUTO_EXECUTED'::character varying, 'PENDING_APPROVAL'::character varying, 'APPROVED'::character varying, 'EXECUTING'::character varying, 'GRACE_PENDING'::character varying, 'INCONCLUSIVE'::character varying, 'REJECTED'::character varying, 'EXECUTED'::character varying, 'FAILED'::character varying])::text[])))
);


ALTER TABLE public.actions OWNER TO postgres;

--
-- Name: ai_budget_locks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ai_budget_locks (
    period character varying(16) NOT NULL,
    period_start timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.ai_budget_locks OWNER TO postgres;

--
-- Name: ai_invocations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ai_invocations (
    id character varying(36) NOT NULL,
    feature character varying(64) NOT NULL,
    provider character varying(64) NOT NULL,
    model_id character varying(128) NOT NULL,
    status character varying(16) NOT NULL,
    latency_ms integer NOT NULL,
    input_chars integer NOT NULL,
    output_chars integer NOT NULL,
    error_type character varying(128),
    created_at timestamp without time zone NOT NULL,
    input_tokens integer,
    output_tokens integer
);


ALTER TABLE public.ai_invocations OWNER TO postgres;

--
-- Name: ai_runbook_feedback; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ai_runbook_feedback (
    id character varying(36) NOT NULL,
    runbook_id character varying(36) NOT NULL,
    rating character varying(16) NOT NULL,
    note text,
    submitted_by character varying(128) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.ai_runbook_feedback OWNER TO postgres;

--
-- Name: ai_runbooks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ai_runbooks (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    fault_family character varying(64) NOT NULL,
    source_fingerprint character varying(64) NOT NULL,
    prompt_version character varying(64) NOT NULL,
    source_case_count integer NOT NULL,
    report_json text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    feedback_rating character varying(16),
    feedback_note text,
    feedback_by character varying(128),
    feedback_at timestamp without time zone
);


ALTER TABLE public.ai_runbooks OWNER TO postgres;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO postgres;

--
-- Name: apscheduler_jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.apscheduler_jobs (
    id character varying(191) NOT NULL,
    next_run_time double precision,
    job_state bytea NOT NULL
);


ALTER TABLE public.apscheduler_jobs OWNER TO postgres;

--
-- Name: audit_entries; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.audit_entries (
    id character varying(36) NOT NULL,
    incident_id character varying(36) NOT NULL,
    action_id character varying(36),
    event_type character varying(64) NOT NULL,
    actor character varying(32) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.audit_entries OWNER TO postgres;

--
-- Name: autopilot_cluster_config_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.autopilot_cluster_config_audit (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    actor character varying(64) NOT NULL,
    previous_environment character varying(16) NOT NULL,
    new_environment character varying(16) NOT NULL,
    previous_enabled boolean NOT NULL,
    new_enabled boolean NOT NULL,
    reason text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.autopilot_cluster_config_audit OWNER TO postgres;

--
-- Name: autopilot_config_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.autopilot_config_audit (
    id character varying(36) NOT NULL,
    actor character varying(64) NOT NULL,
    previous_enabled boolean NOT NULL,
    new_enabled boolean NOT NULL,
    reason text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.autopilot_config_audit OWNER TO postgres;

--
-- Name: autopilot_leases; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.autopilot_leases (
    cluster_id character varying(36) NOT NULL,
    action_id character varying(36) NOT NULL,
    acquired_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone NOT NULL
);


ALTER TABLE public.autopilot_leases OWNER TO postgres;

--
-- Name: backup_anomalies; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.backup_anomalies (
    id character varying(36) NOT NULL,
    backup_job_id character varying(36) NOT NULL,
    kind character varying(16) NOT NULL,
    severity character varying(16) NOT NULL,
    ai_summary text,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.backup_anomalies OWNER TO postgres;

--
-- Name: backup_digest_logs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.backup_digest_logs (
    id character varying(36) NOT NULL,
    period_start timestamp without time zone NOT NULL,
    period_end timestamp without time zone NOT NULL,
    succeeded_count integer NOT NULL,
    failed_count integer NOT NULL,
    anomaly_count integer NOT NULL,
    summary_text text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    cluster_id character varying(36)
);


ALTER TABLE public.backup_digest_logs OWNER TO postgres;

--
-- Name: backup_jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.backup_jobs (
    id character varying(36) NOT NULL,
    run_id character varying(36) NOT NULL,
    pool character varying(64),
    image character varying(128),
    job_type character varying(16) NOT NULL,
    status character varying(16) NOT NULL,
    base_job_id character varying(36),
    backup_target_slot character varying(16),
    remote_key character varying(255),
    size_bytes bigint,
    duration_seconds double precision,
    error_message text,
    created_at timestamp without time zone NOT NULL,
    finished_at timestamp without time zone,
    cluster_id character varying(36),
    sha256 character varying(64)
);


ALTER TABLE public.backup_jobs OWNER TO postgres;

--
-- Name: backup_metadata_artifacts; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.backup_metadata_artifacts (
    id character varying(36) NOT NULL,
    backup_job_id character varying(36) NOT NULL,
    artifact_name character varying(64) NOT NULL,
    size_bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL
);


ALTER TABLE public.backup_metadata_artifacts OWNER TO postgres;

--
-- Name: bucket_inventory_snapshots; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.bucket_inventory_snapshots (
    cluster_id character varying(36) NOT NULL,
    rgw_host character varying(255) DEFAULT ''::character varying NOT NULL,
    bucket_names_json text DEFAULT '[]'::text NOT NULL,
    captured_at timestamp without time zone NOT NULL
);


ALTER TABLE public.bucket_inventory_snapshots OWNER TO postgres;

--
-- Name: bucket_logging_configs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.bucket_logging_configs (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    source_bucket character varying(255) NOT NULL,
    target_bucket character varying(255) NOT NULL,
    prefix character varying(1024) DEFAULT 'logs/'::character varying NOT NULL,
    owner character varying(255) NOT NULL,
    endpoint text NOT NULL,
    mode character varying(32) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    checkpoint text,
    last_error text,
    last_delivery_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.bucket_logging_configs OWNER TO postgres;

--
-- Name: capability_matrix_changes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.capability_matrix_changes (
    id character varying(36) NOT NULL,
    entry_id character varying(36) NOT NULL,
    actor character varying(64) NOT NULL,
    change_type character varying(16) NOT NULL,
    entry_snapshot_json text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.capability_matrix_changes OWNER TO postgres;

--
-- Name: capability_matrix_entries; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.capability_matrix_entries (
    id character varying(36) NOT NULL,
    command_id character varying(64) NOT NULL,
    inner_command text NOT NULL,
    flag character varying(128),
    module character varying(128),
    backend character varying(64),
    min_major integer NOT NULL,
    max_major integer,
    doc_url text NOT NULL,
    verified_at timestamp without time zone NOT NULL,
    verified_by character varying(64) NOT NULL,
    status character varying(16) DEFAULT 'ACTIVE'::character varying NOT NULL,
    notes text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    CONSTRAINT ck_capability_matrix_entries_status_valid CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'DEPRECATED'::character varying])::text[])))
);


ALTER TABLE public.capability_matrix_entries OWNER TO postgres;

--
-- Name: capability_matrix_proposals; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.capability_matrix_proposals (
    id character varying(36) NOT NULL,
    command_id character varying(64) NOT NULL,
    inner_command text NOT NULL,
    min_major integer NOT NULL,
    max_major integer,
    doc_url text NOT NULL,
    evidence_excerpt text NOT NULL,
    rationale text NOT NULL,
    status character varying(16) NOT NULL,
    proposed_by character varying(64) NOT NULL,
    reviewed_by character varying(64),
    reviewed_at timestamp without time zone,
    created_entry_id character varying(36),
    created_at timestamp without time zone NOT NULL,
    source_sha256 character varying(64) NOT NULL,
    CONSTRAINT ck_capability_matrix_proposals_status_valid CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'APPROVED'::character varying, 'REJECTED'::character varying])::text[])))
);


ALTER TABLE public.capability_matrix_proposals OWNER TO postgres;

--
-- Name: capacity_alert_states; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.capacity_alert_states (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    entity_type character varying(16) NOT NULL,
    entity_name character varying(128) NOT NULL,
    current_threshold integer NOT NULL,
    notified_threshold integer NOT NULL,
    last_attempt_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.capacity_alert_states OWNER TO postgres;

--
-- Name: ceph_capacity_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ceph_capacity_samples (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    entity_type character varying(16) NOT NULL,
    entity_name character varying(128) NOT NULL,
    used_bytes bigint NOT NULL,
    total_bytes bigint NOT NULL,
    used_percent double precision NOT NULL,
    captured_at timestamp without time zone NOT NULL
);


ALTER TABLE public.ceph_capacity_samples OWNER TO postgres;

--
-- Name: change_risk_assessments; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.change_risk_assessments (
    id character varying(36) NOT NULL,
    action_id character varying(36) NOT NULL,
    cluster_id character varying(36),
    risk_level character varying(24) NOT NULL,
    sample_count integer NOT NULL,
    success_count integer NOT NULL,
    failure_count integer NOT NULL,
    regression_count integer NOT NULL,
    summary text NOT NULL,
    evidence_json text NOT NULL,
    analyzed_at timestamp without time zone NOT NULL,
    assessment_hash character varying(64) NOT NULL,
    acknowledged_hash character varying(64)
);


ALTER TABLE public.change_risk_assessments OWNER TO postgres;

--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.chat_messages (
    id character varying(36) NOT NULL,
    role character varying(16) NOT NULL,
    content text NOT NULL,
    actor character varying(32),
    proposed_action_id character varying(64),
    proposed_target_nodes text,
    proposed_rationale text,
    proposed_command_preview text,
    proposed_status character varying(16),
    proposed_incident_id character varying(36),
    created_at timestamp without time zone NOT NULL,
    tools_used text,
    session_id character varying(36),
    proposed_action_params text,
    cluster_id character varying(36)
);


ALTER TABLE public.chat_messages OWNER TO postgres;

--
-- Name: chat_preferences; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.chat_preferences (
    username character varying(64) NOT NULL,
    ai_name character varying(64) DEFAULT 'AI'::character varying NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    female_address character varying(128) DEFAULT 'Mình yêu ơi, em là'::character varying NOT NULL
);


ALTER TABLE public.chat_preferences OWNER TO postgres;

--
-- Name: cluster_capability_inventory; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.cluster_capability_inventory (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    status character varying(32) NOT NULL,
    deployment_mode character varying(16),
    per_type_versions_json text,
    distinct_versions_json text,
    is_mixed_version boolean DEFAULT false NOT NULL,
    current_version character varying(32),
    current_major integer,
    error_message text,
    collected_at timestamp without time zone NOT NULL,
    CONSTRAINT ck_cluster_capability_inventory_status_valid CHECK (((status)::text = ANY ((ARRAY['SUPPORTED'::character varying, 'UNSUPPORTED_VERSION'::character varying, 'UNAVAILABLE'::character varying, 'UNKNOWN'::character varying])::text[])))
);


ALTER TABLE public.cluster_capability_inventory OWNER TO postgres;

--
-- Name: clusters; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.clusters (
    id character varying(36) NOT NULL,
    name character varying(128) NOT NULL,
    ceph_mon_nodes text NOT NULL,
    ceph_container_name character varying(128) NOT NULL,
    ssh_user character varying(64) NOT NULL,
    ssh_key_path text NOT NULL,
    ceph_exec_mode character varying(16) NOT NULL,
    is_default boolean NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    ceph_mgr_nodes text DEFAULT ''::text NOT NULL,
    ceph_osd_nodes text DEFAULT ''::text NOT NULL,
    ceph_rgw_nodes text DEFAULT ''::text NOT NULL,
    ceph_rgw_container_name character varying(128) DEFAULT ''::character varying NOT NULL,
    ceph_mon_hostnames text DEFAULT ''::text NOT NULL,
    ceph_osd_container_name character varying(128) DEFAULT ''::character varying NOT NULL,
    telegram_bot_token text DEFAULT ''::text NOT NULL,
    telegram_chat_id character varying(64) DEFAULT ''::character varying NOT NULL,
    telegram_enabled boolean DEFAULT true NOT NULL,
    backup_enabled boolean DEFAULT false NOT NULL,
    backup_tracked_images text DEFAULT ''::text NOT NULL,
    backup_full_refresh_days integer,
    backup_transport character varying(16) DEFAULT ''::character varying NOT NULL,
    backup_ssh_host character varying(255) DEFAULT ''::character varying NOT NULL,
    backup_ssh_user character varying(64) DEFAULT ''::character varying NOT NULL,
    backup_ssh_key_path text DEFAULT ''::text NOT NULL,
    backup_ssh_landing_dir text DEFAULT ''::text NOT NULL,
    backup_s3_endpoint text DEFAULT ''::text NOT NULL,
    backup_s3_access_key text DEFAULT ''::text NOT NULL,
    backup_s3_secret_key text DEFAULT ''::text NOT NULL,
    backup_s3_bucket character varying(255) DEFAULT ''::character varying NOT NULL,
    backup_immutable_enabled boolean DEFAULT false NOT NULL,
    backup_immutable_lock_days integer DEFAULT 7 NOT NULL,
    openstack_controller_nodes text DEFAULT ''::text NOT NULL,
    openstack_compute_nodes text DEFAULT ''::text NOT NULL,
    openstack_ceph_config_path text DEFAULT '/etc/ceph'::text NOT NULL,
    openstack_openrc_path text DEFAULT ''::text NOT NULL,
    backup_rpo_hours integer DEFAULT 24 NOT NULL,
    ceph_keyring_path text DEFAULT '/etc/ceph/ceph.client.admin.keyring'::text NOT NULL,
    autonomy_environment character varying(16) DEFAULT 'production'::character varying NOT NULL,
    autopilot_enabled boolean DEFAULT false NOT NULL
);


ALTER TABLE public.clusters OWNER TO postgres;

--
-- Name: crush_osd_distribution; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.crush_osd_distribution (
    osd_id integer NOT NULL,
    host character varying(64),
    bytes_used bigint,
    bytes_total bigint,
    pgs integer,
    updated_at timestamp without time zone NOT NULL,
    cluster_id character varying(36) NOT NULL
);


ALTER TABLE public.crush_osd_distribution OWNER TO postgres;

--
-- Name: crush_structure_snapshots; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.crush_structure_snapshots (
    id character varying(36) NOT NULL,
    tree_json text NOT NULL,
    diff_json text,
    created_at timestamp without time zone NOT NULL,
    cluster_id character varying(36) NOT NULL
);


ALTER TABLE public.crush_structure_snapshots OWNER TO postgres;

--
-- Name: delegated_ai_subtasks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.delegated_ai_subtasks (
    id character varying(36) NOT NULL,
    task_id character varying(36) NOT NULL,
    role character varying(48) NOT NULL,
    objective text NOT NULL,
    allowed_tools_json text NOT NULL,
    status character varying(24) NOT NULL,
    result_text text,
    tools_used_json text,
    error text,
    attempt integer DEFAULT 0 NOT NULL,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL,
    execution_owner character varying(128),
    lease_until timestamp without time zone
);


ALTER TABLE public.delegated_ai_subtasks OWNER TO postgres;

--
-- Name: delegated_ai_tasks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.delegated_ai_tasks (
    id character varying(36) NOT NULL,
    actor character varying(32) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    session_id character varying(36) NOT NULL,
    assistant_message_id character varying(36) NOT NULL,
    prompt text NOT NULL,
    status character varying(24) NOT NULL,
    result_text text,
    error text,
    created_at timestamp without time zone NOT NULL,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL,
    execution_owner character varying(128),
    lease_until timestamp without time zone,
    dispatch_claimed_at timestamp without time zone,
    provider_call_count integer DEFAULT 0 NOT NULL
);


ALTER TABLE public.delegated_ai_tasks OWNER TO postgres;

--
-- Name: forecast_model_evaluations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.forecast_model_evaluations (
    id character varying(36) NOT NULL,
    candidate_model_id character varying(36) NOT NULL,
    active_model_id character varying(36) NOT NULL,
    target_at timestamp without time zone NOT NULL,
    evaluated_at timestamp without time zone NOT NULL,
    active_evaluated integer NOT NULL,
    candidate_evaluated integer NOT NULL,
    active_mae double precision,
    candidate_mae double precision,
    active_rmse double precision,
    candidate_rmse double precision,
    active_smape double precision,
    candidate_smape double precision,
    active_bias double precision,
    candidate_bias double precision,
    active_false_positive_rate double precision,
    candidate_false_positive_rate double precision,
    status character varying(24) NOT NULL,
    reason text NOT NULL,
    evidence_json text
);


ALTER TABLE public.forecast_model_evaluations OWNER TO postgres;

--
-- Name: forecast_model_promotion_audits; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.forecast_model_promotion_audits (
    id character varying(36) NOT NULL,
    candidate_model_id character varying(36) NOT NULL,
    previous_active_model_id character varying(36),
    event_type character varying(32) NOT NULL,
    actor character varying(64) NOT NULL,
    reason text NOT NULL,
    evidence_json text,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.forecast_model_promotion_audits OWNER TO postgres;

--
-- Name: forecast_model_registry; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.forecast_model_registry (
    id character varying(36) NOT NULL,
    scope_type character varying(32) NOT NULL,
    scope_key character varying(255) NOT NULL,
    name character varying(64) NOT NULL,
    version character varying(32) NOT NULL,
    algorithm character varying(32) NOT NULL,
    feature_schema character varying(64) NOT NULL,
    training_window_hours integer NOT NULL,
    status character varying(16) NOT NULL,
    promotion_reason text,
    blocked_reason text,
    active_since timestamp without time zone,
    retired_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    CONSTRAINT ck_forecast_model_registry_status_valid CHECK (((status)::text = ANY ((ARRAY['CANDIDATE'::character varying, 'SHADOW'::character varying, 'ACTIVE'::character varying, 'RETIRED'::character varying, 'BLOCKED'::character varying])::text[])))
);


ALTER TABLE public.forecast_model_registry OWNER TO postgres;

--
-- Name: host_metric_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.host_metric_samples (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    host character varying(255) NOT NULL,
    node_name character varying(255),
    cpu_percent double precision NOT NULL,
    mem_percent double precision NOT NULL,
    disk_read_iops double precision NOT NULL,
    disk_write_iops double precision NOT NULL,
    disk_latency_ms double precision NOT NULL,
    network_rx_bytes_per_sec double precision NOT NULL,
    network_tx_bytes_per_sec double precision NOT NULL,
    collected_at timestamp without time zone NOT NULL
);


ALTER TABLE public.host_metric_samples OWNER TO postgres;

--
-- Name: incident_timeline_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.incident_timeline_events (
    id character varying(36) NOT NULL,
    incident_id character varying(36) NOT NULL,
    action_id character varying(36),
    event_type character varying(64) NOT NULL,
    actor character varying(32) NOT NULL,
    evidence_json text,
    source_type character varying(32),
    source_id character varying(128),
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.incident_timeline_events OWNER TO postgres;

--
-- Name: incidents; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.incidents (
    id character varying(36) NOT NULL,
    ceph_code character varying(64) NOT NULL,
    status character varying(32) NOT NULL,
    log_excerpt text,
    detected_at timestamp without time zone NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    diagnosis_text text,
    severity character varying(16),
    cluster_id character varying(36),
    telegram_reminded_at timestamp without time zone,
    verify_after timestamp without time zone,
    verify_attempts integer DEFAULT 0 NOT NULL,
    signal_evidence_json text,
    postmortem_json text,
    postmortem_generated_at timestamp without time zone,
    postmortem_prompt_version character varying(16),
    group_root_incident_id character varying(36),
    acknowledged_at timestamp without time zone,
    acknowledged_by character varying(128),
    muted_until timestamp without time zone,
    muted_by character varying(128),
    failed_at timestamp without time zone,
    dedupe_key character varying(256),
    CONSTRAINT ck_incidents_status_valid CHECK (((status)::text = ANY ((ARRAY['NEW'::character varying, 'DIAGNOSING'::character varying, 'AUTO_FIXED'::character varying, 'PENDING_APPROVAL'::character varying, 'APPROVED'::character varying, 'EXECUTING'::character varying, 'GRACE_PENDING'::character varying, 'VERIFYING'::character varying, 'RESOLVED'::character varying, 'REJECTED'::character varying, 'FAILED'::character varying])::text[])))
);


ALTER TABLE public.incidents OWNER TO postgres;

--
-- Name: log_fault_stats; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.log_fault_stats (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    daemon_type character varying(32) NOT NULL,
    fault_family character varying(64) NOT NULL,
    playbook_id character varying(64) DEFAULT 'observation_only'::character varying NOT NULL,
    playbook_version character varying(32) DEFAULT 'none'::character varying NOT NULL,
    sample_count integer DEFAULT 0 NOT NULL,
    verified_count integer DEFAULT 0 NOT NULL,
    success_count integer DEFAULT 0 NOT NULL,
    failure_count integer DEFAULT 0 NOT NULL,
    inconclusive_count integer DEFAULT 0 NOT NULL,
    trust_score double precision DEFAULT '0'::double precision NOT NULL,
    promotion_candidate_at timestamp without time zone,
    promotion_blocked_reason text,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.log_fault_stats OWNER TO postgres;

--
-- Name: log_findings; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.log_findings (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    ingest_run_id character varying(36) NOT NULL,
    verdict character varying(24) NOT NULL,
    severity character varying(16),
    confidence character varying(16),
    title text,
    summary text,
    root_cause_hypothesis text,
    evidence_pattern_ids_json text,
    affected_hosts_json text,
    affected_daemons_json text,
    recommended_action_id character varying(64),
    recommended_manual_steps_json text,
    dedupe_key character varying(64) NOT NULL,
    status character varying(16) DEFAULT 'OPEN'::character varying NOT NULL,
    model_name character varying(64),
    prompt_version character varying(16),
    validation_notes text,
    created_at timestamp without time zone NOT NULL,
    fault_family character varying(64),
    semantic_entities_json text,
    correlated_incident_id character varying(36),
    correlation_reason character varying(255),
    correlated_at timestamp without time zone,
    correlation_evidence_json text,
    recovery_check_code character varying(64),
    recovery_check_summary text,
    recovery_checked_at timestamp without time zone,
    recovery_notified_at timestamp without time zone,
    CONSTRAINT ck_log_findings_status_valid CHECK (((status)::text = ANY ((ARRAY['OPEN'::character varying, 'ACKNOWLEDGED'::character varying, 'RESOLVED'::character varying])::text[]))),
    CONSTRAINT ck_log_findings_verdict_valid CHECK (((verdict)::text = ANY (ARRAY[('FINDING'::character varying)::text, ('NO_FINDING'::character varying)::text, ('INSUFFICIENT_EVIDENCE'::character varying)::text])))
);


ALTER TABLE public.log_findings OWNER TO postgres;

--
-- Name: log_ingest_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.log_ingest_runs (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    source character varying(16) NOT NULL,
    window_start timestamp without time zone NOT NULL,
    window_end timestamp without time zone NOT NULL,
    status character varying(16) NOT NULL,
    hosts_scanned integer DEFAULT 0 NOT NULL,
    hosts_failed integer DEFAULT 0 NOT NULL,
    lines_scanned integer DEFAULT 0 NOT NULL,
    patterns_seen integer DEFAULT 0 NOT NULL,
    patterns_new integer DEFAULT 0 NOT NULL,
    error_message text,
    created_at timestamp without time zone NOT NULL,
    patterns_flagged integer,
    scan_scope character varying(16) DEFAULT 'FULL'::character varying NOT NULL,
    CONSTRAINT ck_log_ingest_runs_status_valid CHECK (((status)::text = ANY ((ARRAY['OK'::character varying, 'PARTIAL'::character varying, 'FAILED'::character varying])::text[])))
);


ALTER TABLE public.log_ingest_runs OWNER TO postgres;

--
-- Name: log_learning_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.log_learning_audit (
    id character varying(36) NOT NULL,
    sample_id character varying(36) NOT NULL,
    event_type character varying(64) NOT NULL,
    actor character varying(64) NOT NULL,
    previous_value_json text,
    new_value_json text,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.log_learning_audit OWNER TO postgres;

--
-- Name: log_learning_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.log_learning_samples (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    log_finding_id character varying(36) NOT NULL,
    ingest_run_id character varying(36) NOT NULL,
    incident_id character varying(36),
    remediation_case_id character varying(36),
    action_id character varying(36),
    daemon_type character varying(32) DEFAULT 'unknown'::character varying NOT NULL,
    daemon_id character varying(128),
    host character varying(255),
    fault_family character varying(64) DEFAULT 'unknown'::character varying NOT NULL,
    entity_key character varying(255) DEFAULT 'unknown'::character varying NOT NULL,
    pattern_ids_json text DEFAULT '[]'::text NOT NULL,
    evidence_fingerprint character varying(64) NOT NULL,
    source character varying(16) NOT NULL,
    window_start timestamp without time zone NOT NULL,
    window_end timestamp without time zone NOT NULL,
    ingest_status character varying(16) NOT NULL,
    parser_version character varying(32) NOT NULL,
    semantic_version character varying(32) NOT NULL,
    prompt_version character varying(32),
    model_name character varying(128),
    diagnosis_confidence character varying(16),
    recommended_playbook_id character varying(64),
    playbook_version character varying(32),
    state character varying(32) DEFAULT 'CANDIDATE'::character varying NOT NULL,
    label character varying(32) DEFAULT 'UNVERIFIED'::character varying NOT NULL,
    eligible_for_learning boolean DEFAULT false NOT NULL,
    exclusion_reason text,
    outcome_source character varying(32),
    verified_at timestamp without time zone,
    regressed boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    operator_verdict character varying(32),
    operator_note text,
    operator_verdict_by character varying(64),
    operator_verdict_at timestamp without time zone
);


ALTER TABLE public.log_learning_samples OWNER TO postgres;

--
-- Name: log_pattern_observations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.log_pattern_observations (
    id character varying(36) NOT NULL,
    pattern_id character varying(36) NOT NULL,
    bucket_hour timestamp without time zone NOT NULL,
    host character varying(64) NOT NULL,
    count bigint DEFAULT '0'::bigint NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.log_pattern_observations OWNER TO postgres;

--
-- Name: log_patterns; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.log_patterns (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    fingerprint character varying(40) NOT NULL,
    template text NOT NULL,
    daemon_type character varying(8) NOT NULL,
    severity integer,
    sample_line text,
    first_seen_at timestamp without time zone NOT NULL,
    last_seen_at timestamp without time zone NOT NULL,
    total_count bigint DEFAULT '0'::bigint NOT NULL,
    triage_label character varying(16) DEFAULT 'UNKNOWN'::character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    CONSTRAINT ck_log_patterns_triage_label_valid CHECK (((triage_label)::text = ANY ((ARRAY['UNKNOWN'::character varying, 'BENIGN'::character varying, 'NOTABLE'::character varying])::text[])))
);


ALTER TABLE public.log_patterns OWNER TO postgres;

--
-- Name: node_diagnostic_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.node_diagnostic_runs (
    id character varying(36) NOT NULL,
    host character varying(64) NOT NULL,
    command_id character varying(64) NOT NULL,
    command_label character varying(128) NOT NULL,
    actor character varying(32) NOT NULL,
    success boolean NOT NULL,
    output_excerpt text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.node_diagnostic_runs OWNER TO postgres;

--
-- Name: node_resource_forecast_alerts; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.node_resource_forecast_alerts (
    id character varying(36) NOT NULL,
    cluster_name character varying(128) NOT NULL,
    host character varying(255) NOT NULL,
    metric character varying(8) NOT NULL,
    status character varying(16) NOT NULL,
    first_detected_at timestamp without time zone NOT NULL,
    last_detected_at timestamp without time zone NOT NULL,
    last_notified_at timestamp without time zone,
    resolved_at timestamp without time zone,
    current_percent double precision NOT NULL,
    predicted_percent double precision NOT NULL,
    hours_to_90 double precision NOT NULL,
    confidence double precision NOT NULL,
    samples integer NOT NULL,
    window_hours integer NOT NULL,
    consensus_status character varying(32),
    consensus_ratio double precision,
    consensus_candidate_count integer,
    predicted_low double precision,
    predicted_high double precision,
    anomaly_score double precision,
    lifecycle_state character varying(16),
    notification_state character varying(16),
    state_changed_at timestamp without time zone,
    state_reason text,
    evidence_version character varying(64),
    consecutive_breach_count integer,
    consecutive_healthy_count integer,
    evidence_fingerprint character varying(64),
    suppressed_until timestamp without time zone,
    last_notified_evidence_fingerprint character varying(64),
    coverage_ratio double precision,
    max_gap_hours double precision,
    latest_observed_at timestamp without time zone
);


ALTER TABLE public.node_resource_forecast_alerts OWNER TO postgres;

--
-- Name: node_resource_forecast_feedback; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.node_resource_forecast_feedback (
    id character varying(36) NOT NULL,
    alert_id character varying(36) NOT NULL,
    verdict character varying(24) NOT NULL,
    note text,
    impact_percent double precision,
    incident_id character varying(36),
    remediation_case_id character varying(36),
    submitted_by character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.node_resource_forecast_feedback OWNER TO postgres;

--
-- Name: node_resource_forecast_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.node_resource_forecast_runs (
    id character varying(36) NOT NULL,
    cluster_name character varying(128) NOT NULL,
    host character varying(255) NOT NULL,
    metric character varying(8) NOT NULL,
    algorithm character varying(32) NOT NULL,
    window_hours integer NOT NULL,
    predicted_at timestamp without time zone NOT NULL,
    target_at timestamp without time zone NOT NULL,
    current_percent double precision NOT NULL,
    predicted_percent double precision NOT NULL,
    confidence double precision NOT NULL,
    actual_percent double precision,
    absolute_error double precision,
    status character varying(16) NOT NULL,
    idempotency_key character varying(255) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    evaluated_at timestamp without time zone,
    consensus_status character varying(32),
    consensus_ratio double precision,
    consensus_candidate_count integer,
    predicted_low double precision,
    predicted_high double precision,
    anomaly_score double precision,
    residual_percent double precision,
    model_votes_json text,
    coverage_ratio double precision,
    max_gap_hours double precision,
    latest_observed_at timestamp without time zone,
    drift_status character varying(24) DEFAULT 'INSUFFICIENT_DATA'::character varying NOT NULL,
    drift_score double precision DEFAULT '0'::double precision NOT NULL,
    drift_reason text
);


ALTER TABLE public.node_resource_forecast_runs OWNER TO postgres;

--
-- Name: node_resource_forecast_transitions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.node_resource_forecast_transitions (
    id character varying(36) NOT NULL,
    alert_id character varying(36) NOT NULL,
    previous_state character varying(16),
    new_state character varying(16) NOT NULL,
    reason text NOT NULL,
    evidence_version character varying(64),
    changed_at timestamp without time zone NOT NULL
);


ALTER TABLE public.node_resource_forecast_transitions OWNER TO postgres;

--
-- Name: node_resource_model_states; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.node_resource_model_states (
    id character varying(36) NOT NULL,
    cluster_name character varying(128) NOT NULL,
    host character varying(255) NOT NULL,
    metric character varying(8) NOT NULL,
    algorithm character varying(32) NOT NULL,
    window_hours integer NOT NULL,
    evaluated_count integer NOT NULL,
    mean_absolute_error double precision,
    last_absolute_error double precision,
    selected boolean NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    rolling_sample_count integer DEFAULT 0 NOT NULL,
    rolling_mae double precision,
    rolling_rmse double precision,
    rolling_smape double precision,
    rolling_bias double precision,
    rolling_metrics_json text
);


ALTER TABLE public.node_resource_model_states OWNER TO postgres;

--
-- Name: node_upgrade_gate_locks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.node_upgrade_gate_locks (
    id character varying(64) NOT NULL,
    active_gate_id character varying(36)
);


ALTER TABLE public.node_upgrade_gate_locks OWNER TO postgres;

--
-- Name: node_upgrade_gates; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.node_upgrade_gates (
    id character varying(36) NOT NULL,
    host character varying(64) NOT NULL,
    target_version character varying(32) NOT NULL,
    state character varying(32) NOT NULL,
    roles_snapshot text,
    osd_backup text,
    prepare_action_id character varying(36),
    confirm_action_id character varying(36),
    abort_action_id character varying(36),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    cluster_id character varying(36),
    maintenance_flags_added text,
    mon_removed boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_node_upgrade_gates_state_valid CHECK (((state)::text = ANY ((ARRAY['PREPARING'::character varying, 'PREPARED'::character varying, 'RECOVERING'::character varying, 'ABORTING'::character varying, 'DONE'::character varying, 'FAILED'::character varying])::text[])))
);


ALTER TABLE public.node_upgrade_gates OWNER TO postgres;

--
-- Name: object_storage_audit_entries; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.object_storage_audit_entries (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    actor character varying(64) NOT NULL,
    action character varying(32) NOT NULL,
    target_type character varying(32) NOT NULL,
    target_id character varying(255) NOT NULL,
    preview text NOT NULL,
    result character varying(32) NOT NULL,
    error_message text,
    created_at timestamp without time zone NOT NULL,
    completed_at timestamp without time zone
);


ALTER TABLE public.object_storage_audit_entries OWNER TO postgres;

--
-- Name: online_learner_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.online_learner_audit (
    id character varying(36) NOT NULL,
    cluster_key character varying(128) NOT NULL,
    host character varying(255) NOT NULL,
    metric character varying(64) NOT NULL,
    sample_id character varying(255) NOT NULL,
    observed_at timestamp without time zone NOT NULL,
    value double precision NOT NULL,
    label double precision,
    quality_status character varying(32) NOT NULL,
    quality_reason text NOT NULL,
    runtime_mode character varying(24) NOT NULL,
    runtime_reason text NOT NULL,
    update_applied boolean DEFAULT false NOT NULL,
    model_version character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.online_learner_audit OWNER TO postgres;

--
-- Name: online_learner_cycle_audit; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.online_learner_cycle_audit (
    id character varying(36) NOT NULL,
    cluster_key character varying(128) NOT NULL,
    host character varying(255) NOT NULL,
    metric character varying(64) NOT NULL,
    processed integer DEFAULT 0 NOT NULL,
    applied integer DEFAULT 0 NOT NULL,
    failed integer DEFAULT 0 NOT NULL,
    skipped integer DEFAULT 0 NOT NULL,
    elapsed_ms double precision DEFAULT 0 NOT NULL,
    cpu_time_ms double precision DEFAULT 0 NOT NULL,
    reason character varying(64) NOT NULL,
    runtime_mode character varying(24) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.online_learner_cycle_audit OWNER TO postgres;

--
-- Name: online_learner_label_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.online_learner_label_events (
    id character varying(36) NOT NULL,
    label_id character varying(36),
    source_run_id character varying(36),
    action character varying(24) NOT NULL,
    actor character varying(64) NOT NULL,
    reason text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.online_learner_label_events OWNER TO postgres;

--
-- Name: online_learner_labels; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.online_learner_labels (
    id character varying(36) NOT NULL,
    cluster_key character varying(128) NOT NULL,
    host character varying(255) NOT NULL,
    metric character varying(64) NOT NULL,
    sample_id character varying(255) NOT NULL,
    source_run_id character varying(36) NOT NULL,
    observed_at timestamp without time zone NOT NULL,
    label_value double precision NOT NULL,
    status character varying(16) DEFAULT 'READY'::character varying NOT NULL,
    reason text NOT NULL,
    verified_at timestamp without time zone NOT NULL,
    consumed_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    predicted_value double precision,
    absolute_error double precision,
    outcome character varying(24) DEFAULT 'VERIFIED_SUCCESS'::character varying NOT NULL,
    evidence_count integer DEFAULT 1 NOT NULL,
    source_actor character varying(64) DEFAULT 'forecast-evaluator'::character varying NOT NULL
);


ALTER TABLE public.online_learner_labels OWNER TO postgres;

--
-- Name: online_learner_states; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.online_learner_states (
    id character varying(36) NOT NULL,
    cluster_key character varying(128) NOT NULL,
    host character varying(255) NOT NULL,
    metric character varying(64) NOT NULL,
    model_version character varying(64) NOT NULL,
    algorithm character varying(64) NOT NULL,
    feature_schema character varying(64) NOT NULL,
    state_json text NOT NULL,
    state_checksum character varying(64) NOT NULL,
    sample_count integer DEFAULT 0 NOT NULL,
    last_learned_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.online_learner_states OWNER TO postgres;

--
-- Name: patch_documents; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.patch_documents (
    id integer NOT NULL,
    filename character varying(255) NOT NULL,
    content text NOT NULL,
    uploaded_by character varying(64) NOT NULL,
    uploaded_at timestamp without time zone NOT NULL
);


ALTER TABLE public.patch_documents OWNER TO postgres;

--
-- Name: patch_documents_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.patch_documents_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.patch_documents_id_seq OWNER TO postgres;

--
-- Name: patch_documents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.patch_documents_id_seq OWNED BY public.patch_documents.id;


--
-- Name: playbook_stats; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.playbook_stats (
    id character varying(36) NOT NULL,
    playbook_id character varying(64) NOT NULL,
    playbook_version character varying(32) NOT NULL,
    scope_key character varying(256) NOT NULL,
    proposed_count integer DEFAULT 0 NOT NULL,
    executed_count integer DEFAULT 0 NOT NULL,
    verified_count integer DEFAULT 0 NOT NULL,
    success_count integer DEFAULT 0 NOT NULL,
    failure_count integer DEFAULT 0 NOT NULL,
    inconclusive_count integer DEFAULT 0 NOT NULL,
    trust_score double precision DEFAULT '0'::double precision NOT NULL,
    maturity_level character varying(16) DEFAULT 'L0'::character varying NOT NULL,
    last_failure_at timestamp without time zone,
    auto_disabled_reason text,
    promotion_candidate_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    promotion_blocked_reason text
);


ALTER TABLE public.playbook_stats OWNER TO postgres;

--
-- Name: rbd_trash_usages; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.rbd_trash_usages (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    pool character varying(64) NOT NULL,
    trash_id character varying(128) NOT NULL,
    image character varying(128) NOT NULL,
    provisioned_size_bytes bigint NOT NULL,
    used_size_bytes bigint NOT NULL,
    used_percent double precision NOT NULL,
    observed_at timestamp without time zone NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.rbd_trash_usages OWNER TO postgres;

--
-- Name: remediation_cases; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.remediation_cases (
    id character varying(36) NOT NULL,
    incident_id character varying(36) NOT NULL,
    action_id character varying(36) NOT NULL,
    cluster_id character varying(36),
    fault_family character varying(64) NOT NULL,
    entity_keys_json text,
    evidence_fingerprint character varying(64) NOT NULL,
    ceph_version character varying(64),
    deployment_mode character varying(32),
    topology_snapshot_json text,
    diagnosis text,
    diagnosis_confidence double precision,
    prompt_version character varying(32) NOT NULL,
    model_provider character varying(32),
    classification character varying(16) NOT NULL,
    autonomy_decision character varying(32) NOT NULL,
    playbook_version character varying(32) NOT NULL,
    preflight_snapshot_json text,
    command_preview_hash character varying(64),
    pre_state_json text,
    post_state_json text,
    rollback_state_json text,
    outcome character varying(32) NOT NULL,
    side_effects_json text,
    started_at timestamp without time zone,
    executed_at timestamp without time zone,
    verified_at timestamp without time zone,
    recovery_seconds integer,
    regressed_1h boolean,
    regressed_24h boolean,
    regressed_7d boolean,
    operator_verdict character varying(32),
    operator_note text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    shadow_decision character varying(32),
    shadow_reason text,
    shadow_trust_score double precision,
    shadow_sample_count integer,
    shadow_recorded_at timestamp without time zone,
    operator_verdict_by character varying(64),
    operator_verdict_at timestamp without time zone
);


ALTER TABLE public.remediation_cases OWNER TO postgres;

--
-- Name: rgw_access_audit_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.rgw_access_audit_events (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    rgw_host character varying(255) NOT NULL,
    fingerprint character varying(64) NOT NULL,
    method character varying(16) NOT NULL,
    action character varying(64) NOT NULL,
    bucket character varying(255),
    object_key text,
    requester character varying(255),
    remote_addr character varying(255),
    http_status integer NOT NULL,
    bytes_sent bigint,
    latency_ms double precision,
    event_at timestamp without time zone NOT NULL,
    telegram_sent boolean DEFAULT false NOT NULL,
    telegram_attempts integer DEFAULT 0 NOT NULL,
    telegram_error text,
    created_at timestamp without time zone NOT NULL,
    telegram_sent_at timestamp without time zone,
    encryption character varying(64),
    transaction_id character varying(255),
    external_alert_queued boolean DEFAULT false NOT NULL,
    external_alert_queued_at timestamp without time zone
);


ALTER TABLE public.rgw_access_audit_events OWNER TO postgres;

--
-- Name: rgw_analysis_jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.rgw_analysis_jobs (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    source_event_id character varying(36) NOT NULL,
    signature character varying(64) NOT NULL,
    status character varying(24) DEFAULT 'QUEUED'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    ingest_run_id character varying(36),
    finding_id character varying(36),
    error text,
    created_at timestamp without time zone NOT NULL,
    started_at timestamp without time zone,
    finished_at timestamp without time zone
);


ALTER TABLE public.rgw_analysis_jobs OWNER TO postgres;

--
-- Name: rgw_error_notifications; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.rgw_error_notifications (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    rgw_host character varying(255) NOT NULL,
    fingerprint character varying(64) NOT NULL,
    message text NOT NULL,
    event_at timestamp without time zone NOT NULL,
    telegram_sent boolean DEFAULT false NOT NULL,
    telegram_attempts integer DEFAULT 0 NOT NULL,
    telegram_error text,
    created_at timestamp without time zone NOT NULL,
    telegram_sent_at timestamp without time zone,
    telegram_message_id integer,
    telegram_humanization_status character varying(32) DEFAULT 'not_started'::character varying NOT NULL,
    telegram_humanization_error text,
    telegram_humanized_at timestamp without time zone,
    external_alert_queued boolean DEFAULT false NOT NULL,
    external_alert_queued_at timestamp without time zone
);


ALTER TABLE public.rgw_error_notifications OWNER TO postgres;

--
-- Name: telegram_channel_config_changes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.telegram_channel_config_changes (
    id character varying(36) NOT NULL,
    channel character varying(16) NOT NULL,
    chat_id character varying(64) NOT NULL,
    bot_token_masked character varying(32) NOT NULL,
    actor character varying(32) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.telegram_channel_config_changes OWNER TO postgres;

--
-- Name: telegram_channel_layouts; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.telegram_channel_layouts (
    id character varying(32) NOT NULL,
    order_json text NOT NULL,
    hidden_json text NOT NULL,
    display_names_json text NOT NULL,
    updated_by character varying(64) NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.telegram_channel_layouts OWNER TO postgres;

--
-- Name: telegram_managed_channels; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.telegram_managed_channels (
    id character varying(36) NOT NULL,
    name character varying(128) NOT NULL,
    bot_token text NOT NULL,
    chat_id character varying(128) NOT NULL,
    enabled boolean NOT NULL,
    template character varying(64) NOT NULL,
    created_by character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.telegram_managed_channels OWNER TO postgres;

--
-- Name: upgrade_procedure_documents; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.upgrade_procedure_documents (
    id integer NOT NULL,
    filename character varying(255) NOT NULL,
    raw_text text NOT NULL,
    summary_text text,
    summary_error text,
    uploaded_by character varying(32) NOT NULL,
    uploaded_at timestamp without time zone NOT NULL
);


ALTER TABLE public.upgrade_procedure_documents OWNER TO postgres;

--
-- Name: upgrade_procedure_documents_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.upgrade_procedure_documents_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.upgrade_procedure_documents_id_seq OWNER TO postgres;

--
-- Name: upgrade_procedure_documents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.upgrade_procedure_documents_id_seq OWNED BY public.upgrade_procedure_documents.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.users (
    id character varying(36) NOT NULL,
    username character varying(64) NOT NULL,
    password_hash character varying(72) NOT NULL,
    is_admin boolean NOT NULL,
    is_active boolean NOT NULL,
    created_by character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    ceph_chat_restricted boolean DEFAULT true NOT NULL
);


ALTER TABLE public.users OWNER TO postgres;

--
-- Name: vitastor_anomaly_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_anomaly_events (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    entity_type character varying(16) NOT NULL,
    entity_name character varying(255) NOT NULL,
    metric character varying(64) NOT NULL,
    status character varying(16) NOT NULL,
    severity character varying(16) NOT NULL,
    current_value double precision NOT NULL,
    baseline_value double precision NOT NULL,
    deviation_ratio double precision NOT NULL,
    sample_count integer NOT NULL,
    explanation text NOT NULL,
    detected_at timestamp without time zone NOT NULL,
    last_seen_at timestamp without time zone NOT NULL,
    resolved_at timestamp without time zone
);


ALTER TABLE public.vitastor_anomaly_events OWNER TO postgres;

--
-- Name: vitastor_audit_entries; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_audit_entries (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    action_pk character varying(36),
    event_type character varying(64) NOT NULL,
    actor character varying(64) NOT NULL,
    detail text,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_audit_entries OWNER TO postgres;

--
-- Name: vitastor_capacity_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_capacity_samples (
    id integer NOT NULL,
    cluster_id character varying(36) NOT NULL,
    entity_type character varying(16) NOT NULL,
    entity_name character varying(128) NOT NULL,
    used_bytes bigint DEFAULT '0'::bigint NOT NULL,
    total_bytes bigint DEFAULT '0'::bigint NOT NULL,
    used_percent double precision DEFAULT '0'::double precision NOT NULL,
    captured_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_capacity_samples OWNER TO postgres;

--
-- Name: vitastor_capacity_samples_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.vitastor_capacity_samples_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vitastor_capacity_samples_id_seq OWNER TO postgres;

--
-- Name: vitastor_capacity_samples_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.vitastor_capacity_samples_id_seq OWNED BY public.vitastor_capacity_samples.id;


--
-- Name: vitastor_clusters; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_clusters (
    id character varying(36) NOT NULL,
    name character varying(128) NOT NULL,
    management_host character varying(255) NOT NULL,
    etcd_address text NOT NULL,
    etcd_prefix character varying(255) NOT NULL,
    config_path text NOT NULL,
    ssh_user character varying(64) NOT NULL,
    ssh_key_path text NOT NULL,
    exec_mode character varying(16) NOT NULL,
    container_name character varying(128) NOT NULL,
    is_active boolean NOT NULL,
    last_status_json text,
    last_checked_at timestamp without time zone,
    created_by character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_clusters OWNER TO postgres;

--
-- Name: vitastor_config_drift_scans; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_config_drift_scans (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    status character varying(16) DEFAULT 'OK'::character varying NOT NULL,
    hosts_scanned integer DEFAULT 0 NOT NULL,
    findings_json text DEFAULT '[]'::text NOT NULL,
    facts_json text DEFAULT '[]'::text NOT NULL,
    errors_json text DEFAULT '[]'::text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_config_drift_scans OWNER TO postgres;

--
-- Name: vitastor_diagnostic_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_diagnostic_runs (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    status character varying(16) NOT NULL,
    health character varying(16) NOT NULL,
    evidence_json text NOT NULL,
    diagnosis_text text,
    result_json text,
    error_message text,
    requested_by character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    finished_at timestamp without time zone
);


ALTER TABLE public.vitastor_diagnostic_runs OWNER TO postgres;

--
-- Name: vitastor_ec_safety_scans; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_ec_safety_scans (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    status character varying(24) DEFAULT 'INSUFFICIENT_EVIDENCE'::character varying NOT NULL,
    pools_scanned integer DEFAULT 0 NOT NULL,
    findings_json text DEFAULT '[]'::text NOT NULL,
    pools_json text DEFAULT '[]'::text NOT NULL,
    errors_json text DEFAULT '[]'::text NOT NULL,
    fingerprint character varying(64) DEFAULT ''::character varying NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_ec_safety_scans OWNER TO postgres;

--
-- Name: vitastor_entity_metric_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_entity_metric_samples (
    id integer NOT NULL,
    cluster_id character varying(36) NOT NULL,
    entity_type character varying(16) NOT NULL,
    entity_name character varying(255) NOT NULL,
    metrics_json text NOT NULL,
    collected_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_entity_metric_samples OWNER TO postgres;

--
-- Name: vitastor_entity_metric_samples_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.vitastor_entity_metric_samples_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vitastor_entity_metric_samples_id_seq OWNER TO postgres;

--
-- Name: vitastor_entity_metric_samples_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.vitastor_entity_metric_samples_id_seq OWNED BY public.vitastor_entity_metric_samples.id;


--
-- Name: vitastor_etcd_snapshots; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_etcd_snapshots (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    status character varying(24) DEFAULT 'FAILED'::character varying NOT NULL,
    destination text DEFAULT ''::text NOT NULL,
    revision bigint,
    size_bytes bigint,
    sha256 character varying(64) DEFAULT ''::character varying NOT NULL,
    detail_json text DEFAULT '{}'::text NOT NULL,
    error_message text,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_etcd_snapshots OWNER TO postgres;

--
-- Name: vitastor_incidents; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_incidents (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    fingerprint character varying(255) NOT NULL,
    status character varying(16) DEFAULT 'OPEN'::character varying NOT NULL,
    severity character varying(16) DEFAULT 'WARNING'::character varying NOT NULL,
    title character varying(255) NOT NULL,
    entity_type character varying(16) DEFAULT 'cluster'::character varying NOT NULL,
    entity_name character varying(255) DEFAULT 'cluster'::character varying NOT NULL,
    first_seen_at timestamp without time zone NOT NULL,
    last_seen_at timestamp without time zone NOT NULL,
    resolved_at timestamp without time zone,
    timeline_json text DEFAULT '[]'::text NOT NULL,
    postmortem_text text,
    postmortem_result_json text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_incidents OWNER TO postgres;

--
-- Name: vitastor_log_scans; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_log_scans (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    host character varying(255) NOT NULL,
    source character varying(16) NOT NULL,
    window_minutes integer DEFAULT 15 NOT NULL,
    status character varying(16) DEFAULT 'OK'::character varying NOT NULL,
    lines_scanned integer DEFAULT 0 NOT NULL,
    findings_json text DEFAULT '[]'::text NOT NULL,
    error_message text,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_log_scans OWNER TO postgres;

--
-- Name: vitastor_metric_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_metric_samples (
    id integer NOT NULL,
    cluster_id character varying(36) NOT NULL,
    health character varying(16) NOT NULL,
    osd_up integer NOT NULL,
    osd_total integer NOT NULL,
    used_bytes bigint NOT NULL,
    free_bytes bigint NOT NULL,
    used_percent double precision NOT NULL,
    etcd_up integer NOT NULL,
    etcd_total integer NOT NULL,
    read_iops double precision NOT NULL,
    write_iops double precision NOT NULL,
    read_bps double precision NOT NULL,
    write_bps double precision NOT NULL,
    read_latency_ms double precision,
    write_latency_ms double precision,
    recovery_bps double precision NOT NULL,
    degraded_bytes bigint NOT NULL,
    raw_json text NOT NULL,
    collected_at timestamp without time zone NOT NULL,
    etcd_latency_ms double precision,
    etcd_quorum boolean,
    etcd_leader_count integer DEFAULT 0 NOT NULL
);


ALTER TABLE public.vitastor_metric_samples OWNER TO postgres;

--
-- Name: vitastor_metric_samples_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.vitastor_metric_samples_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vitastor_metric_samples_id_seq OWNER TO postgres;

--
-- Name: vitastor_metric_samples_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.vitastor_metric_samples_id_seq OWNED BY public.vitastor_metric_samples.id;


--
-- Name: vitastor_network_metric_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_network_metric_samples (
    id integer NOT NULL,
    cluster_id character varying(36) NOT NULL,
    source character varying(255) NOT NULL,
    target character varying(255) NOT NULL,
    reachable boolean NOT NULL,
    rtt_ms double precision,
    jumbo_9000 boolean NOT NULL,
    interface_json text NOT NULL,
    collected_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_network_metric_samples OWNER TO postgres;

--
-- Name: vitastor_network_metric_samples_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.vitastor_network_metric_samples_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vitastor_network_metric_samples_id_seq OWNER TO postgres;

--
-- Name: vitastor_network_metric_samples_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.vitastor_network_metric_samples_id_seq OWNED BY public.vitastor_network_metric_samples.id;


--
-- Name: vitastor_node_metric_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_node_metric_samples (
    id integer NOT NULL,
    cluster_id character varying(36) NOT NULL,
    host character varying(255) NOT NULL,
    osd_processes integer NOT NULL,
    cpu_percent double precision NOT NULL,
    ram_bytes bigint NOT NULL,
    max_temperature_c double precision,
    max_wear_percent double precision,
    media_errors bigint NOT NULL,
    smart_failing boolean NOT NULL,
    raw_json text NOT NULL,
    collected_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_node_metric_samples OWNER TO postgres;

--
-- Name: vitastor_node_metric_samples_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.vitastor_node_metric_samples_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vitastor_node_metric_samples_id_seq OWNER TO postgres;

--
-- Name: vitastor_node_metric_samples_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.vitastor_node_metric_samples_id_seq OWNED BY public.vitastor_node_metric_samples.id;


--
-- Name: vitastor_operations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_operations (
    id character varying(36) NOT NULL,
    operation character varying(16) NOT NULL,
    status character varying(24) NOT NULL,
    cluster_id character varying(36),
    cluster_name character varying(128) NOT NULL,
    params_json text NOT NULL,
    plan_text text NOT NULL,
    progress_json text NOT NULL,
    error_message text,
    requested_by character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    started_at timestamp without time zone,
    finished_at timestamp without time zone
);


ALTER TABLE public.vitastor_operations OWNER TO postgres;

--
-- Name: vitastor_osd_metric_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_osd_metric_samples (
    id integer NOT NULL,
    cluster_id character varying(36) NOT NULL,
    osd_id character varying(64) NOT NULL,
    host character varying(255) NOT NULL,
    is_up boolean NOT NULL,
    size_bytes bigint NOT NULL,
    used_bytes bigint NOT NULL,
    used_percent double precision NOT NULL,
    read_iops double precision NOT NULL,
    write_iops double precision NOT NULL,
    read_bps double precision NOT NULL,
    write_bps double precision NOT NULL,
    read_latency_ms double precision,
    write_latency_ms double precision,
    raw_json text NOT NULL,
    collected_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_osd_metric_samples OWNER TO postgres;

--
-- Name: vitastor_osd_metric_samples_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.vitastor_osd_metric_samples_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vitastor_osd_metric_samples_id_seq OWNER TO postgres;

--
-- Name: vitastor_osd_metric_samples_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.vitastor_osd_metric_samples_id_seq OWNED BY public.vitastor_osd_metric_samples.id;


--
-- Name: vitastor_prometheus_samples; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_prometheus_samples (
    id integer NOT NULL,
    cluster_id character varying(36) NOT NULL,
    metric_name character varying(255) NOT NULL,
    labels_json text DEFAULT '{}'::text NOT NULL,
    value double precision NOT NULL,
    collected_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_prometheus_samples OWNER TO postgres;

--
-- Name: vitastor_prometheus_samples_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.vitastor_prometheus_samples_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vitastor_prometheus_samples_id_seq OWNER TO postgres;

--
-- Name: vitastor_prometheus_samples_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.vitastor_prometheus_samples_id_seq OWNED BY public.vitastor_prometheus_samples.id;


--
-- Name: vitastor_remediation_actions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_remediation_actions (
    id character varying(36) CONSTRAINT _alembic_tmp_vitastor_remediation_actions_id_not_null NOT NULL,
    cluster_id character varying(36) CONSTRAINT _alembic_tmp_vitastor_remediation_actions_cluster_id_not_null NOT NULL,
    source character varying(16) DEFAULT 'MONITOR'::character varying CONSTRAINT _alembic_tmp_vitastor_remediation_actions_source_not_null NOT NULL,
    source_id character varying(36),
    action_id character varying(64) CONSTRAINT _alembic_tmp_vitastor_remediation_actions_action_id_not_null NOT NULL,
    classification character varying(16) CONSTRAINT _alembic_tmp_vitastor_remediation_actio_classification_not_null NOT NULL,
    status character varying(32) DEFAULT 'PENDING_APPROVAL'::character varying CONSTRAINT _alembic_tmp_vitastor_remediation_actions_status_not_null NOT NULL,
    target_host character varying(255) DEFAULT ''::character varying CONSTRAINT _alembic_tmp_vitastor_remediation_actions_target_host_not_null NOT NULL,
    action_params text,
    proposed_command text,
    rationale text,
    result_output text,
    error_message text,
    dedup_key character varying(255) DEFAULT ''::character varying CONSTRAINT _alembic_tmp_vitastor_remediation_actions_dedup_key_not_null NOT NULL,
    requested_by character varying(64) DEFAULT 'vitastor-monitor'::character varying CONSTRAINT _alembic_tmp_vitastor_remediation_actions_requested_by_not_null NOT NULL,
    approved_by character varying(64),
    telegram_message_ids text,
    telegram_notified_at timestamp without time zone,
    executed_at timestamp without time zone,
    created_at timestamp without time zone CONSTRAINT _alembic_tmp_vitastor_remediation_actions_created_at_not_null NOT NULL,
    updated_at timestamp without time zone CONSTRAINT _alembic_tmp_vitastor_remediation_actions_updated_at_not_null NOT NULL,
    verification_attempts integer DEFAULT 0 NOT NULL,
    verification_error text,
    verified_at timestamp without time zone,
    CONSTRAINT ck_vita_remediation_classification_valid CHECK (((classification)::text = ANY (ARRAY[('SAFE'::character varying)::text, ('RISKY'::character varying)::text]))),
    CONSTRAINT ck_vita_remediation_status_valid CHECK (((status)::text = ANY ((ARRAY['PENDING_APPROVAL'::character varying, 'AUTO_EXECUTED'::character varying, 'APPROVED'::character varying, 'REJECTED'::character varying, 'EXECUTING'::character varying, 'VERIFYING'::character varying, 'EXECUTED'::character varying, 'FAILED'::character varying, 'OBSOLETE'::character varying])::text[])))
);


ALTER TABLE public.vitastor_remediation_actions OWNER TO postgres;

--
-- Name: vitastor_users; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.vitastor_users (
    id character varying(36) NOT NULL,
    username character varying(64) NOT NULL,
    password_hash character varying(72) NOT NULL,
    is_admin boolean NOT NULL,
    is_active boolean NOT NULL,
    created_by character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.vitastor_users OWNER TO postgres;

--
-- Name: volume_early_forecasts; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.volume_early_forecasts (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    pool character varying(64) NOT NULL,
    image character varying(128) NOT NULL,
    metric character varying(32) NOT NULL,
    horizon_hours integer NOT NULL,
    generated_at timestamp without time zone NOT NULL,
    target_at timestamp without time zone NOT NULL,
    source_latest_at timestamp without time zone NOT NULL,
    current_value double precision NOT NULL,
    predicted_value double precision NOT NULL,
    threshold_type character varying(32),
    threshold_value double precision,
    confidence double precision NOT NULL,
    training_samples integer NOT NULL,
    training_window_hours integer NOT NULL,
    seasonal_scope character varying(32) NOT NULL,
    model_version character varying(32) NOT NULL,
    status character varying(16) NOT NULL,
    reason text NOT NULL,
    idempotency_key character varying(255) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    telegram_sent_at timestamp without time zone,
    consensus_status character varying(32),
    consensus_ratio double precision,
    consensus_candidate_count integer,
    predicted_low double precision,
    predicted_high double precision,
    model_votes_json text
);


ALTER TABLE public.volume_early_forecasts OWNER TO postgres;

--
-- Name: volume_forecast_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.volume_forecast_runs (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    pool character varying(64) NOT NULL,
    image character varying(128) NOT NULL,
    metric character varying(32) NOT NULL,
    algorithm character varying(32) NOT NULL,
    window_hours integer NOT NULL,
    predicted_at timestamp without time zone NOT NULL,
    target_at timestamp without time zone NOT NULL,
    current_value double precision NOT NULL,
    predicted_value double precision NOT NULL,
    confidence double precision NOT NULL,
    seasonal_scope character varying(32) NOT NULL,
    training_samples integer NOT NULL,
    actual_value double precision,
    absolute_error double precision,
    percentage_error double precision,
    status character varying(16) NOT NULL,
    idempotency_key character varying(255) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    evaluated_at timestamp without time zone
);


ALTER TABLE public.volume_forecast_runs OWNER TO postgres;

--
-- Name: volume_metrics; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.volume_metrics (
    id integer NOT NULL,
    pool character varying(64) NOT NULL,
    image character varying(128) NOT NULL,
    iops double precision NOT NULL,
    read_latency_ms double precision NOT NULL,
    write_latency_ms double precision NOT NULL,
    saturated boolean NOT NULL,
    polled_at timestamp without time zone NOT NULL,
    cluster_id character varying(36)
);


ALTER TABLE public.volume_metrics OWNER TO postgres;

--
-- Name: volume_metrics_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.volume_metrics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.volume_metrics_id_seq OWNER TO postgres;

--
-- Name: volume_metrics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.volume_metrics_id_seq OWNED BY public.volume_metrics.id;


--
-- Name: volume_model_states; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.volume_model_states (
    id character varying(36) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    pool character varying(64) NOT NULL,
    image character varying(128) NOT NULL,
    metric character varying(32) NOT NULL,
    algorithm character varying(32) NOT NULL,
    window_hours integer NOT NULL,
    evaluated_count integer NOT NULL,
    mean_absolute_error double precision,
    mean_percentage_error double precision,
    last_absolute_error double precision,
    selected boolean NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    rolling_sample_count integer DEFAULT 0 NOT NULL,
    rolling_mae double precision,
    rolling_rmse double precision,
    rolling_smape double precision,
    rolling_bias double precision,
    rolling_metrics_json text
);


ALTER TABLE public.volume_model_states OWNER TO postgres;

--
-- Name: volume_osd_mappings; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.volume_osd_mappings (
    cluster_id character varying(36) NOT NULL,
    pool character varying(64) NOT NULL,
    image character varying(128) NOT NULL,
    image_id character varying(128) NOT NULL,
    object_name character varying(255) NOT NULL,
    pgid character varying(64) NOT NULL,
    acting_osds_json text NOT NULL,
    primary_osd integer,
    captured_at timestamp without time zone NOT NULL,
    pgids_json text NOT NULL,
    sampled_objects_json text NOT NULL,
    data_object_count integer NOT NULL,
    mapping_scope character varying(32) NOT NULL
);


ALTER TABLE public.volume_osd_mappings OWNER TO postgres;

--
-- Name: volume_perf_sweeps; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.volume_perf_sweeps (
    id character varying(36) NOT NULL,
    action_id character varying(36) NOT NULL,
    pool character varying(64) NOT NULL,
    scratch_image character varying(128) NOT NULL,
    requested_by character varying(32) NOT NULL,
    status character varying(16) NOT NULL,
    steps_json text NOT NULL,
    knee_iodepth integer,
    knee_iops double precision,
    knee_latency_avg_ms double precision,
    knee_latency_p99_ms double precision,
    qos_notes text,
    bottleneck_notes text,
    error_message text,
    created_at timestamp without time zone NOT NULL,
    finished_at timestamp without time zone,
    ai_conclusion text,
    ai_analyzed_at timestamp without time zone
);


ALTER TABLE public.volume_perf_sweeps OWNER TO postgres;

--
-- Name: volume_snapshot_policies; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.volume_snapshot_policies (
    id character varying(36) NOT NULL,
    cluster_id character varying(36),
    pool character varying(64) NOT NULL,
    image character varying(128) NOT NULL,
    volume_id character varying(36) NOT NULL,
    snapshot_prefix character varying(48) NOT NULL,
    cron_expression character varying(128) NOT NULL,
    timezone character varying(64) NOT NULL,
    retention_count integer NOT NULL,
    capacity_guard_percent double precision NOT NULL,
    enabled boolean NOT NULL,
    last_run_at timestamp without time zone,
    next_run_at timestamp without time zone,
    last_status character varying(24),
    last_error text,
    created_by character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    CONSTRAINT ck_volume_snapshot_policy_capacity_guard CHECK (((capacity_guard_percent > (0)::double precision) AND (capacity_guard_percent < (100)::double precision))),
    CONSTRAINT ck_volume_snapshot_policy_retention CHECK (((retention_count >= 1) AND (retention_count <= 365)))
);


ALTER TABLE public.volume_snapshot_policies OWNER TO postgres;

--
-- Name: watcher_heartbeat; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.watcher_heartbeat (
    id integer NOT NULL,
    success boolean NOT NULL,
    mon_node character varying(64),
    error_message text,
    polled_at timestamp without time zone NOT NULL,
    cluster_id character varying(36),
    consecutive_failures integer DEFAULT 0 NOT NULL,
    last_success_at timestamp without time zone
);


ALTER TABLE public.watcher_heartbeat OWNER TO postgres;

--
-- Name: watcher_heartbeat_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.watcher_heartbeat_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.watcher_heartbeat_id_seq OWNER TO postgres;

--
-- Name: watcher_heartbeat_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.watcher_heartbeat_id_seq OWNED BY public.watcher_heartbeat.id;


--
-- Name: patch_documents id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.patch_documents ALTER COLUMN id SET DEFAULT nextval('public.patch_documents_id_seq'::regclass);


--
-- Name: upgrade_procedure_documents id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.upgrade_procedure_documents ALTER COLUMN id SET DEFAULT nextval('public.upgrade_procedure_documents_id_seq'::regclass);


--
-- Name: vitastor_capacity_samples id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_capacity_samples ALTER COLUMN id SET DEFAULT nextval('public.vitastor_capacity_samples_id_seq'::regclass);


--
-- Name: vitastor_entity_metric_samples id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_entity_metric_samples ALTER COLUMN id SET DEFAULT nextval('public.vitastor_entity_metric_samples_id_seq'::regclass);


--
-- Name: vitastor_metric_samples id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_metric_samples ALTER COLUMN id SET DEFAULT nextval('public.vitastor_metric_samples_id_seq'::regclass);


--
-- Name: vitastor_network_metric_samples id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_network_metric_samples ALTER COLUMN id SET DEFAULT nextval('public.vitastor_network_metric_samples_id_seq'::regclass);


--
-- Name: vitastor_node_metric_samples id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_node_metric_samples ALTER COLUMN id SET DEFAULT nextval('public.vitastor_node_metric_samples_id_seq'::regclass);


--
-- Name: vitastor_osd_metric_samples id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_osd_metric_samples ALTER COLUMN id SET DEFAULT nextval('public.vitastor_osd_metric_samples_id_seq'::regclass);


--
-- Name: vitastor_prometheus_samples id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_prometheus_samples ALTER COLUMN id SET DEFAULT nextval('public.vitastor_prometheus_samples_id_seq'::regclass);


--
-- Name: volume_metrics id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_metrics ALTER COLUMN id SET DEFAULT nextval('public.volume_metrics_id_seq'::regclass);


--
-- Name: watcher_heartbeat id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.watcher_heartbeat ALTER COLUMN id SET DEFAULT nextval('public.watcher_heartbeat_id_seq'::regclass);


--
-- Name: action_policy_override_audit action_policy_override_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.action_policy_override_audit
    ADD CONSTRAINT action_policy_override_audit_pkey PRIMARY KEY (id);


--
-- Name: action_policy_overrides action_policy_overrides_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.action_policy_overrides
    ADD CONSTRAINT action_policy_overrides_pkey PRIMARY KEY (action_id);


--
-- Name: actions actions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.actions
    ADD CONSTRAINT actions_pkey PRIMARY KEY (id);


--
-- Name: ai_budget_locks ai_budget_locks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ai_budget_locks
    ADD CONSTRAINT ai_budget_locks_pkey PRIMARY KEY (period);


--
-- Name: ai_invocations ai_invocations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ai_invocations
    ADD CONSTRAINT ai_invocations_pkey PRIMARY KEY (id);


--
-- Name: ai_runbook_feedback ai_runbook_feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ai_runbook_feedback
    ADD CONSTRAINT ai_runbook_feedback_pkey PRIMARY KEY (id);


--
-- Name: ai_runbooks ai_runbooks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ai_runbooks
    ADD CONSTRAINT ai_runbooks_pkey PRIMARY KEY (id);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: apscheduler_jobs apscheduler_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.apscheduler_jobs
    ADD CONSTRAINT apscheduler_jobs_pkey PRIMARY KEY (id);


--
-- Name: audit_entries audit_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.audit_entries
    ADD CONSTRAINT audit_entries_pkey PRIMARY KEY (id);


--
-- Name: autopilot_cluster_config_audit autopilot_cluster_config_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.autopilot_cluster_config_audit
    ADD CONSTRAINT autopilot_cluster_config_audit_pkey PRIMARY KEY (id);


--
-- Name: autopilot_config_audit autopilot_config_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.autopilot_config_audit
    ADD CONSTRAINT autopilot_config_audit_pkey PRIMARY KEY (id);


--
-- Name: autopilot_leases autopilot_leases_action_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.autopilot_leases
    ADD CONSTRAINT autopilot_leases_action_id_key UNIQUE (action_id);


--
-- Name: autopilot_leases autopilot_leases_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.autopilot_leases
    ADD CONSTRAINT autopilot_leases_pkey PRIMARY KEY (cluster_id);


--
-- Name: backup_anomalies backup_anomalies_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_anomalies
    ADD CONSTRAINT backup_anomalies_pkey PRIMARY KEY (id);


--
-- Name: backup_digest_logs backup_digest_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_digest_logs
    ADD CONSTRAINT backup_digest_logs_pkey PRIMARY KEY (id);


--
-- Name: backup_jobs backup_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_jobs
    ADD CONSTRAINT backup_jobs_pkey PRIMARY KEY (id);


--
-- Name: backup_metadata_artifacts backup_metadata_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_metadata_artifacts
    ADD CONSTRAINT backup_metadata_artifacts_pkey PRIMARY KEY (id);


--
-- Name: bucket_inventory_snapshots bucket_inventory_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.bucket_inventory_snapshots
    ADD CONSTRAINT bucket_inventory_snapshots_pkey PRIMARY KEY (cluster_id);


--
-- Name: bucket_logging_configs bucket_logging_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.bucket_logging_configs
    ADD CONSTRAINT bucket_logging_configs_pkey PRIMARY KEY (id);


--
-- Name: capability_matrix_changes capability_matrix_changes_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.capability_matrix_changes
    ADD CONSTRAINT capability_matrix_changes_pkey PRIMARY KEY (id);


--
-- Name: capability_matrix_entries capability_matrix_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.capability_matrix_entries
    ADD CONSTRAINT capability_matrix_entries_pkey PRIMARY KEY (id);


--
-- Name: capability_matrix_proposals capability_matrix_proposals_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.capability_matrix_proposals
    ADD CONSTRAINT capability_matrix_proposals_pkey PRIMARY KEY (id);


--
-- Name: capacity_alert_states capacity_alert_states_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.capacity_alert_states
    ADD CONSTRAINT capacity_alert_states_pkey PRIMARY KEY (id);


--
-- Name: ceph_capacity_samples ceph_capacity_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ceph_capacity_samples
    ADD CONSTRAINT ceph_capacity_samples_pkey PRIMARY KEY (id);


--
-- Name: change_risk_assessments change_risk_assessments_action_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.change_risk_assessments
    ADD CONSTRAINT change_risk_assessments_action_id_key UNIQUE (action_id);


--
-- Name: change_risk_assessments change_risk_assessments_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.change_risk_assessments
    ADD CONSTRAINT change_risk_assessments_pkey PRIMARY KEY (id);


--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);


--
-- Name: chat_preferences chat_preferences_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chat_preferences
    ADD CONSTRAINT chat_preferences_pkey PRIMARY KEY (username);


--
-- Name: cluster_capability_inventory cluster_capability_inventory_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cluster_capability_inventory
    ADD CONSTRAINT cluster_capability_inventory_pkey PRIMARY KEY (id);


--
-- Name: clusters clusters_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.clusters
    ADD CONSTRAINT clusters_pkey PRIMARY KEY (id);


--
-- Name: crush_structure_snapshots crush_structure_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.crush_structure_snapshots
    ADD CONSTRAINT crush_structure_snapshots_pkey PRIMARY KEY (id);


--
-- Name: delegated_ai_subtasks delegated_ai_subtasks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.delegated_ai_subtasks
    ADD CONSTRAINT delegated_ai_subtasks_pkey PRIMARY KEY (id);


--
-- Name: delegated_ai_tasks delegated_ai_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.delegated_ai_tasks
    ADD CONSTRAINT delegated_ai_tasks_pkey PRIMARY KEY (id);


--
-- Name: forecast_model_evaluations forecast_model_evaluations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_model_evaluations
    ADD CONSTRAINT forecast_model_evaluations_pkey PRIMARY KEY (id);


--
-- Name: forecast_model_promotion_audits forecast_model_promotion_audits_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_model_promotion_audits
    ADD CONSTRAINT forecast_model_promotion_audits_pkey PRIMARY KEY (id);


--
-- Name: forecast_model_registry forecast_model_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_model_registry
    ADD CONSTRAINT forecast_model_registry_pkey PRIMARY KEY (id);


--
-- Name: host_metric_samples host_metric_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.host_metric_samples
    ADD CONSTRAINT host_metric_samples_pkey PRIMARY KEY (id);


--
-- Name: incident_timeline_events incident_timeline_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.incident_timeline_events
    ADD CONSTRAINT incident_timeline_events_pkey PRIMARY KEY (id);


--
-- Name: incidents incidents_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.incidents
    ADD CONSTRAINT incidents_pkey PRIMARY KEY (id);


--
-- Name: log_fault_stats log_fault_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_fault_stats
    ADD CONSTRAINT log_fault_stats_pkey PRIMARY KEY (id);


--
-- Name: log_findings log_findings_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_findings
    ADD CONSTRAINT log_findings_pkey PRIMARY KEY (id);


--
-- Name: log_ingest_runs log_ingest_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_ingest_runs
    ADD CONSTRAINT log_ingest_runs_pkey PRIMARY KEY (id);


--
-- Name: log_learning_audit log_learning_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_audit
    ADD CONSTRAINT log_learning_audit_pkey PRIMARY KEY (id);


--
-- Name: log_learning_samples log_learning_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_samples
    ADD CONSTRAINT log_learning_samples_pkey PRIMARY KEY (id);


--
-- Name: log_pattern_observations log_pattern_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_pattern_observations
    ADD CONSTRAINT log_pattern_observations_pkey PRIMARY KEY (id);


--
-- Name: log_patterns log_patterns_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_patterns
    ADD CONSTRAINT log_patterns_pkey PRIMARY KEY (id);


--
-- Name: node_diagnostic_runs node_diagnostic_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_diagnostic_runs
    ADD CONSTRAINT node_diagnostic_runs_pkey PRIMARY KEY (id);


--
-- Name: node_resource_forecast_alerts node_resource_forecast_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_forecast_alerts
    ADD CONSTRAINT node_resource_forecast_alerts_pkey PRIMARY KEY (id);


--
-- Name: node_resource_forecast_feedback node_resource_forecast_feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_forecast_feedback
    ADD CONSTRAINT node_resource_forecast_feedback_pkey PRIMARY KEY (id);


--
-- Name: node_resource_forecast_runs node_resource_forecast_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_forecast_runs
    ADD CONSTRAINT node_resource_forecast_runs_pkey PRIMARY KEY (id);


--
-- Name: node_resource_forecast_transitions node_resource_forecast_transitions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_forecast_transitions
    ADD CONSTRAINT node_resource_forecast_transitions_pkey PRIMARY KEY (id);


--
-- Name: node_resource_model_states node_resource_model_states_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_model_states
    ADD CONSTRAINT node_resource_model_states_pkey PRIMARY KEY (id);


--
-- Name: node_upgrade_gate_locks node_upgrade_gate_locks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_upgrade_gate_locks
    ADD CONSTRAINT node_upgrade_gate_locks_pkey PRIMARY KEY (id);


--
-- Name: node_upgrade_gates node_upgrade_gates_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_upgrade_gates
    ADD CONSTRAINT node_upgrade_gates_pkey PRIMARY KEY (id);


--
-- Name: object_storage_audit_entries object_storage_audit_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.object_storage_audit_entries
    ADD CONSTRAINT object_storage_audit_entries_pkey PRIMARY KEY (id);


--
-- Name: online_learner_audit online_learner_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_audit
    ADD CONSTRAINT online_learner_audit_pkey PRIMARY KEY (id);


--
-- Name: online_learner_cycle_audit online_learner_cycle_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_cycle_audit
    ADD CONSTRAINT online_learner_cycle_audit_pkey PRIMARY KEY (id);


--
-- Name: online_learner_label_events online_learner_label_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_label_events
    ADD CONSTRAINT online_learner_label_events_pkey PRIMARY KEY (id);


--
-- Name: online_learner_labels online_learner_labels_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_labels
    ADD CONSTRAINT online_learner_labels_pkey PRIMARY KEY (id);


--
-- Name: online_learner_states online_learner_states_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_states
    ADD CONSTRAINT online_learner_states_pkey PRIMARY KEY (id);


--
-- Name: patch_documents patch_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.patch_documents
    ADD CONSTRAINT patch_documents_pkey PRIMARY KEY (id);


--
-- Name: crush_osd_distribution pk_crush_osd_distribution; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.crush_osd_distribution
    ADD CONSTRAINT pk_crush_osd_distribution PRIMARY KEY (cluster_id, osd_id);


--
-- Name: playbook_stats playbook_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playbook_stats
    ADD CONSTRAINT playbook_stats_pkey PRIMARY KEY (id);


--
-- Name: rbd_trash_usages rbd_trash_usages_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rbd_trash_usages
    ADD CONSTRAINT rbd_trash_usages_pkey PRIMARY KEY (id);


--
-- Name: remediation_cases remediation_cases_action_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.remediation_cases
    ADD CONSTRAINT remediation_cases_action_id_key UNIQUE (action_id);


--
-- Name: remediation_cases remediation_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.remediation_cases
    ADD CONSTRAINT remediation_cases_pkey PRIMARY KEY (id);


--
-- Name: rgw_access_audit_events rgw_access_audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_access_audit_events
    ADD CONSTRAINT rgw_access_audit_events_pkey PRIMARY KEY (id);


--
-- Name: rgw_analysis_jobs rgw_analysis_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_analysis_jobs
    ADD CONSTRAINT rgw_analysis_jobs_pkey PRIMARY KEY (id);


--
-- Name: rgw_error_notifications rgw_error_notifications_fingerprint_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_error_notifications
    ADD CONSTRAINT rgw_error_notifications_fingerprint_key UNIQUE (fingerprint);


--
-- Name: rgw_error_notifications rgw_error_notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_error_notifications
    ADD CONSTRAINT rgw_error_notifications_pkey PRIMARY KEY (id);


--
-- Name: telegram_channel_config_changes telegram_channel_config_changes_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.telegram_channel_config_changes
    ADD CONSTRAINT telegram_channel_config_changes_pkey PRIMARY KEY (id);


--
-- Name: telegram_channel_layouts telegram_channel_layouts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.telegram_channel_layouts
    ADD CONSTRAINT telegram_channel_layouts_pkey PRIMARY KEY (id);


--
-- Name: telegram_managed_channels telegram_managed_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.telegram_managed_channels
    ADD CONSTRAINT telegram_managed_channels_pkey PRIMARY KEY (id);


--
-- Name: upgrade_procedure_documents upgrade_procedure_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.upgrade_procedure_documents
    ADD CONSTRAINT upgrade_procedure_documents_pkey PRIMARY KEY (id);


--
-- Name: backup_metadata_artifacts uq_backup_metadata_artifacts_job_name; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_metadata_artifacts
    ADD CONSTRAINT uq_backup_metadata_artifacts_job_name UNIQUE (backup_job_id, artifact_name);


--
-- Name: bucket_logging_configs uq_bucket_logging_source; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.bucket_logging_configs
    ADD CONSTRAINT uq_bucket_logging_source UNIQUE (cluster_id, source_bucket);


--
-- Name: capacity_alert_states uq_capacity_alert_state_entity; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.capacity_alert_states
    ADD CONSTRAINT uq_capacity_alert_state_entity UNIQUE (cluster_id, entity_type, entity_name);


--
-- Name: delegated_ai_subtasks uq_delegated_ai_subtask_task_role; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.delegated_ai_subtasks
    ADD CONSTRAINT uq_delegated_ai_subtask_task_role UNIQUE (task_id, role);


--
-- Name: forecast_model_evaluations uq_forecast_model_evaluation_target; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_model_evaluations
    ADD CONSTRAINT uq_forecast_model_evaluation_target UNIQUE (candidate_model_id, active_model_id, target_at);


--
-- Name: forecast_model_registry uq_forecast_model_registry_identity; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_model_registry
    ADD CONSTRAINT uq_forecast_model_registry_identity UNIQUE (scope_type, scope_key, name, version);


--
-- Name: incident_timeline_events uq_incident_timeline_event_source; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.incident_timeline_events
    ADD CONSTRAINT uq_incident_timeline_event_source UNIQUE (source_type, source_id);


--
-- Name: log_fault_stats uq_log_fault_stats_scope; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_fault_stats
    ADD CONSTRAINT uq_log_fault_stats_scope UNIQUE (cluster_id, daemon_type, fault_family, playbook_id, playbook_version);


--
-- Name: log_learning_samples uq_log_learning_samples_finding; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_samples
    ADD CONSTRAINT uq_log_learning_samples_finding UNIQUE (log_finding_id);


--
-- Name: log_pattern_observations uq_log_pattern_observations_bucket; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_pattern_observations
    ADD CONSTRAINT uq_log_pattern_observations_bucket UNIQUE (pattern_id, bucket_hour, host);


--
-- Name: log_patterns uq_log_patterns_cluster_fingerprint; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_patterns
    ADD CONSTRAINT uq_log_patterns_cluster_fingerprint UNIQUE (cluster_id, fingerprint);


--
-- Name: node_resource_forecast_alerts uq_node_resource_forecast_alert_identity; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_forecast_alerts
    ADD CONSTRAINT uq_node_resource_forecast_alert_identity UNIQUE (cluster_name, host, metric);


--
-- Name: node_resource_forecast_runs uq_node_resource_forecast_idempotency; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_forecast_runs
    ADD CONSTRAINT uq_node_resource_forecast_idempotency UNIQUE (idempotency_key);


--
-- Name: node_resource_model_states uq_node_resource_model_identity; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_model_states
    ADD CONSTRAINT uq_node_resource_model_identity UNIQUE (cluster_name, host, metric, algorithm, window_hours);


--
-- Name: online_learner_audit uq_online_learner_audit_sample; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_audit
    ADD CONSTRAINT uq_online_learner_audit_sample UNIQUE (cluster_key, host, metric, sample_id);


--
-- Name: online_learner_labels uq_online_learner_label_sample; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_labels
    ADD CONSTRAINT uq_online_learner_label_sample UNIQUE (cluster_key, host, metric, sample_id);


--
-- Name: online_learner_labels uq_online_learner_label_source_run; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_labels
    ADD CONSTRAINT uq_online_learner_label_source_run UNIQUE (source_run_id);


--
-- Name: online_learner_states uq_online_learner_state_identity; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_states
    ADD CONSTRAINT uq_online_learner_state_identity UNIQUE (cluster_key, host, metric, model_version);


--
-- Name: playbook_stats uq_playbook_stats_scope; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.playbook_stats
    ADD CONSTRAINT uq_playbook_stats_scope UNIQUE (playbook_id, playbook_version, scope_key);


--
-- Name: rbd_trash_usages uq_rbd_trash_usages_cluster_pool_id; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rbd_trash_usages
    ADD CONSTRAINT uq_rbd_trash_usages_cluster_pool_id UNIQUE (cluster_id, pool, trash_id);


--
-- Name: rgw_access_audit_events uq_rgw_access_audit_fingerprint; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_access_audit_events
    ADD CONSTRAINT uq_rgw_access_audit_fingerprint UNIQUE (fingerprint);


--
-- Name: users uq_users_username; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_username UNIQUE (username);


--
-- Name: vitastor_clusters uq_vitastor_clusters_name; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_clusters
    ADD CONSTRAINT uq_vitastor_clusters_name UNIQUE (name);


--
-- Name: vitastor_incidents uq_vitastor_incident_fingerprint; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_incidents
    ADD CONSTRAINT uq_vitastor_incident_fingerprint UNIQUE (cluster_id, fingerprint);


--
-- Name: vitastor_users uq_vitastor_users_username; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_users
    ADD CONSTRAINT uq_vitastor_users_username UNIQUE (username);


--
-- Name: volume_early_forecasts uq_volume_early_forecast_idempotency; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_early_forecasts
    ADD CONSTRAINT uq_volume_early_forecast_idempotency UNIQUE (idempotency_key);


--
-- Name: volume_forecast_runs uq_volume_forecast_idempotency; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_forecast_runs
    ADD CONSTRAINT uq_volume_forecast_idempotency UNIQUE (idempotency_key);


--
-- Name: volume_model_states uq_volume_model_identity; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_model_states
    ADD CONSTRAINT uq_volume_model_identity UNIQUE (cluster_id, pool, image, metric, algorithm, window_hours);


--
-- Name: watcher_heartbeat uq_watcher_heartbeat_cluster_id; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.watcher_heartbeat
    ADD CONSTRAINT uq_watcher_heartbeat_cluster_id UNIQUE (cluster_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: vitastor_anomaly_events vitastor_anomaly_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_anomaly_events
    ADD CONSTRAINT vitastor_anomaly_events_pkey PRIMARY KEY (id);


--
-- Name: vitastor_audit_entries vitastor_audit_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_audit_entries
    ADD CONSTRAINT vitastor_audit_entries_pkey PRIMARY KEY (id);


--
-- Name: vitastor_capacity_samples vitastor_capacity_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_capacity_samples
    ADD CONSTRAINT vitastor_capacity_samples_pkey PRIMARY KEY (id);


--
-- Name: vitastor_clusters vitastor_clusters_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_clusters
    ADD CONSTRAINT vitastor_clusters_pkey PRIMARY KEY (id);


--
-- Name: vitastor_config_drift_scans vitastor_config_drift_scans_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_config_drift_scans
    ADD CONSTRAINT vitastor_config_drift_scans_pkey PRIMARY KEY (id);


--
-- Name: vitastor_diagnostic_runs vitastor_diagnostic_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_diagnostic_runs
    ADD CONSTRAINT vitastor_diagnostic_runs_pkey PRIMARY KEY (id);


--
-- Name: vitastor_ec_safety_scans vitastor_ec_safety_scans_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_ec_safety_scans
    ADD CONSTRAINT vitastor_ec_safety_scans_pkey PRIMARY KEY (id);


--
-- Name: vitastor_entity_metric_samples vitastor_entity_metric_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_entity_metric_samples
    ADD CONSTRAINT vitastor_entity_metric_samples_pkey PRIMARY KEY (id);


--
-- Name: vitastor_etcd_snapshots vitastor_etcd_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_etcd_snapshots
    ADD CONSTRAINT vitastor_etcd_snapshots_pkey PRIMARY KEY (id);


--
-- Name: vitastor_incidents vitastor_incidents_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_incidents
    ADD CONSTRAINT vitastor_incidents_pkey PRIMARY KEY (id);


--
-- Name: vitastor_log_scans vitastor_log_scans_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_log_scans
    ADD CONSTRAINT vitastor_log_scans_pkey PRIMARY KEY (id);


--
-- Name: vitastor_metric_samples vitastor_metric_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_metric_samples
    ADD CONSTRAINT vitastor_metric_samples_pkey PRIMARY KEY (id);


--
-- Name: vitastor_network_metric_samples vitastor_network_metric_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_network_metric_samples
    ADD CONSTRAINT vitastor_network_metric_samples_pkey PRIMARY KEY (id);


--
-- Name: vitastor_node_metric_samples vitastor_node_metric_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_node_metric_samples
    ADD CONSTRAINT vitastor_node_metric_samples_pkey PRIMARY KEY (id);


--
-- Name: vitastor_operations vitastor_operations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_operations
    ADD CONSTRAINT vitastor_operations_pkey PRIMARY KEY (id);


--
-- Name: vitastor_osd_metric_samples vitastor_osd_metric_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_osd_metric_samples
    ADD CONSTRAINT vitastor_osd_metric_samples_pkey PRIMARY KEY (id);


--
-- Name: vitastor_prometheus_samples vitastor_prometheus_samples_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_prometheus_samples
    ADD CONSTRAINT vitastor_prometheus_samples_pkey PRIMARY KEY (id);


--
-- Name: vitastor_remediation_actions vitastor_remediation_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_remediation_actions
    ADD CONSTRAINT vitastor_remediation_actions_pkey PRIMARY KEY (id);


--
-- Name: vitastor_users vitastor_users_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.vitastor_users
    ADD CONSTRAINT vitastor_users_pkey PRIMARY KEY (id);


--
-- Name: volume_early_forecasts volume_early_forecasts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_early_forecasts
    ADD CONSTRAINT volume_early_forecasts_pkey PRIMARY KEY (id);


--
-- Name: volume_forecast_runs volume_forecast_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_forecast_runs
    ADD CONSTRAINT volume_forecast_runs_pkey PRIMARY KEY (id);


--
-- Name: volume_metrics volume_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_metrics
    ADD CONSTRAINT volume_metrics_pkey PRIMARY KEY (id);


--
-- Name: volume_model_states volume_model_states_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_model_states
    ADD CONSTRAINT volume_model_states_pkey PRIMARY KEY (id);


--
-- Name: volume_osd_mappings volume_osd_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_osd_mappings
    ADD CONSTRAINT volume_osd_mappings_pkey PRIMARY KEY (cluster_id, pool, image);


--
-- Name: volume_perf_sweeps volume_perf_sweeps_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_perf_sweeps
    ADD CONSTRAINT volume_perf_sweeps_pkey PRIMARY KEY (id);


--
-- Name: volume_snapshot_policies volume_snapshot_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_snapshot_policies
    ADD CONSTRAINT volume_snapshot_policies_pkey PRIMARY KEY (id);


--
-- Name: watcher_heartbeat watcher_heartbeat_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.watcher_heartbeat
    ADD CONSTRAINT watcher_heartbeat_pkey PRIMARY KEY (id);


--
-- Name: ix_ai_invocations_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_ai_invocations_created_at ON public.ai_invocations USING btree (created_at);


--
-- Name: ix_ai_invocations_feature_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_ai_invocations_feature_status ON public.ai_invocations USING btree (feature, status);


--
-- Name: ix_ai_runbook_feedback_runbook_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_ai_runbook_feedback_runbook_created ON public.ai_runbook_feedback USING btree (runbook_id, created_at);


--
-- Name: ix_apscheduler_jobs_next_run_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_apscheduler_jobs_next_run_time ON public.apscheduler_jobs USING btree (next_run_time);


--
-- Name: ix_backup_digest_logs_cluster_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_backup_digest_logs_cluster_created_at ON public.backup_digest_logs USING btree (cluster_id, created_at);


--
-- Name: ix_backup_jobs_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_backup_jobs_cluster_id ON public.backup_jobs USING btree (cluster_id);


--
-- Name: ix_backup_jobs_pool_image_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_backup_jobs_pool_image_created_at ON public.backup_jobs USING btree (pool, image, created_at);


--
-- Name: ix_backup_metadata_artifacts_backup_job_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_backup_metadata_artifacts_backup_job_id ON public.backup_metadata_artifacts USING btree (backup_job_id);


--
-- Name: ix_capability_matrix_changes_entry_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_capability_matrix_changes_entry_id ON public.capability_matrix_changes USING btree (entry_id);


--
-- Name: ix_capability_matrix_entries_command_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_capability_matrix_entries_command_id ON public.capability_matrix_entries USING btree (command_id);


--
-- Name: ix_capability_matrix_proposals_command_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_capability_matrix_proposals_command_id ON public.capability_matrix_proposals USING btree (command_id);


--
-- Name: ix_capability_matrix_proposals_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_capability_matrix_proposals_status ON public.capability_matrix_proposals USING btree (status);


--
-- Name: ix_ceph_capacity_series; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_ceph_capacity_series ON public.ceph_capacity_samples USING btree (cluster_id, entity_type, entity_name, captured_at);


--
-- Name: ix_chat_messages_actor_cluster_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_chat_messages_actor_cluster_time ON public.chat_messages USING btree (actor, cluster_id, created_at);


--
-- Name: ix_cluster_capability_inventory_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_cluster_capability_inventory_cluster_id ON public.cluster_capability_inventory USING btree (cluster_id);


--
-- Name: ix_cluster_capability_inventory_collected_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_cluster_capability_inventory_collected_at ON public.cluster_capability_inventory USING btree (collected_at);


--
-- Name: ix_crush_structure_snapshots_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_crush_structure_snapshots_cluster_id ON public.crush_structure_snapshots USING btree (cluster_id);


--
-- Name: ix_delegated_ai_subtasks_lease_until; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_subtasks_lease_until ON public.delegated_ai_subtasks USING btree (lease_until);


--
-- Name: ix_delegated_ai_subtasks_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_subtasks_status ON public.delegated_ai_subtasks USING btree (status);


--
-- Name: ix_delegated_ai_subtasks_task_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_subtasks_task_id ON public.delegated_ai_subtasks USING btree (task_id);


--
-- Name: ix_delegated_ai_subtasks_task_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_subtasks_task_status ON public.delegated_ai_subtasks USING btree (task_id, status);


--
-- Name: ix_delegated_ai_tasks_actor; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_tasks_actor ON public.delegated_ai_tasks USING btree (actor);


--
-- Name: ix_delegated_ai_tasks_actor_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_tasks_actor_created ON public.delegated_ai_tasks USING btree (actor, created_at);


--
-- Name: ix_delegated_ai_tasks_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_tasks_cluster_id ON public.delegated_ai_tasks USING btree (cluster_id);


--
-- Name: ix_delegated_ai_tasks_dispatch_claimed_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_tasks_dispatch_claimed_at ON public.delegated_ai_tasks USING btree (dispatch_claimed_at);


--
-- Name: ix_delegated_ai_tasks_lease_until; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_tasks_lease_until ON public.delegated_ai_tasks USING btree (lease_until);


--
-- Name: ix_delegated_ai_tasks_session_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_tasks_session_id ON public.delegated_ai_tasks USING btree (session_id);


--
-- Name: ix_delegated_ai_tasks_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_tasks_status ON public.delegated_ai_tasks USING btree (status);


--
-- Name: ix_delegated_ai_tasks_status_updated; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_delegated_ai_tasks_status_updated ON public.delegated_ai_tasks USING btree (status, updated_at);


--
-- Name: ix_forecast_model_evaluation_candidate_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_forecast_model_evaluation_candidate_time ON public.forecast_model_evaluations USING btree (candidate_model_id, target_at);


--
-- Name: ix_forecast_model_promotion_audit_candidate_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_forecast_model_promotion_audit_candidate_time ON public.forecast_model_promotion_audits USING btree (candidate_model_id, created_at);


--
-- Name: ix_forecast_model_registry_scope_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_forecast_model_registry_scope_status ON public.forecast_model_registry USING btree (scope_type, scope_key, status);


--
-- Name: ix_forecast_model_registry_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_forecast_model_registry_status ON public.forecast_model_registry USING btree (status);


--
-- Name: ix_host_metric_samples_cluster_host_collected; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_host_metric_samples_cluster_host_collected ON public.host_metric_samples USING btree (cluster_id, host, collected_at);


--
-- Name: ix_host_metric_samples_collected_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_host_metric_samples_collected_at ON public.host_metric_samples USING btree (collected_at);


--
-- Name: ix_incident_timeline_event_order; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_incident_timeline_event_order ON public.incident_timeline_events USING btree (incident_id, created_at);


--
-- Name: ix_incidents_cluster_detected_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_incidents_cluster_detected_at ON public.incidents USING btree (cluster_id, detected_at);


--
-- Name: ix_incidents_failed_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_incidents_failed_at ON public.incidents USING btree (failed_at);


--
-- Name: ix_incidents_group_root_incident_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_incidents_group_root_incident_id ON public.incidents USING btree (group_root_incident_id);


--
-- Name: ix_log_findings_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_findings_cluster_id ON public.log_findings USING btree (cluster_id);


--
-- Name: ix_log_findings_correlated_incident_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_findings_correlated_incident_id ON public.log_findings USING btree (correlated_incident_id);


--
-- Name: ix_log_findings_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_findings_created_at ON public.log_findings USING btree (created_at);


--
-- Name: ix_log_findings_dedupe_key; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_findings_dedupe_key ON public.log_findings USING btree (dedupe_key);


--
-- Name: ix_log_findings_fault_family; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_findings_fault_family ON public.log_findings USING btree (fault_family);


--
-- Name: ix_log_findings_ingest_run_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_findings_ingest_run_id ON public.log_findings USING btree (ingest_run_id);


--
-- Name: ix_log_ingest_runs_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_ingest_runs_cluster_id ON public.log_ingest_runs USING btree (cluster_id);


--
-- Name: ix_log_ingest_runs_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_ingest_runs_created_at ON public.log_ingest_runs USING btree (created_at);


--
-- Name: ix_log_learning_audit_sample_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_learning_audit_sample_id ON public.log_learning_audit USING btree (sample_id);


--
-- Name: ix_log_learning_samples_eligibility; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_learning_samples_eligibility ON public.log_learning_samples USING btree (eligible_for_learning, label);


--
-- Name: ix_log_learning_samples_evidence_fingerprint; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_learning_samples_evidence_fingerprint ON public.log_learning_samples USING btree (evidence_fingerprint);


--
-- Name: ix_log_learning_samples_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_learning_samples_scope ON public.log_learning_samples USING btree (cluster_id, daemon_type, fault_family);


--
-- Name: ix_log_pattern_observations_bucket_hour; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_pattern_observations_bucket_hour ON public.log_pattern_observations USING btree (bucket_hour);


--
-- Name: ix_log_pattern_observations_pattern_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_pattern_observations_pattern_id ON public.log_pattern_observations USING btree (pattern_id);


--
-- Name: ix_log_patterns_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_patterns_cluster_id ON public.log_patterns USING btree (cluster_id);


--
-- Name: ix_log_patterns_daemon_type; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_patterns_daemon_type ON public.log_patterns USING btree (daemon_type);


--
-- Name: ix_log_patterns_fingerprint; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_patterns_fingerprint ON public.log_patterns USING btree (fingerprint);


--
-- Name: ix_log_patterns_first_seen_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_patterns_first_seen_at ON public.log_patterns USING btree (first_seen_at);


--
-- Name: ix_log_patterns_last_seen_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_log_patterns_last_seen_at ON public.log_patterns USING btree (last_seen_at);


--
-- Name: ix_node_resource_forecast_alert_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_alert_status ON public.node_resource_forecast_alerts USING btree (status);


--
-- Name: ix_node_resource_forecast_alerts_cluster_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_alerts_cluster_name ON public.node_resource_forecast_alerts USING btree (cluster_name);


--
-- Name: ix_node_resource_forecast_alerts_evidence_fingerprint; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_alerts_evidence_fingerprint ON public.node_resource_forecast_alerts USING btree (evidence_fingerprint);


--
-- Name: ix_node_resource_forecast_alerts_host; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_alerts_host ON public.node_resource_forecast_alerts USING btree (host);


--
-- Name: ix_node_resource_forecast_due; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_due ON public.node_resource_forecast_runs USING btree (status, target_at);


--
-- Name: ix_node_resource_forecast_feedback_alert_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_feedback_alert_created ON public.node_resource_forecast_feedback USING btree (alert_id, created_at);


--
-- Name: ix_node_resource_forecast_feedback_incident_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_feedback_incident_id ON public.node_resource_forecast_feedback USING btree (incident_id);


--
-- Name: ix_node_resource_forecast_feedback_remediation_case_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_feedback_remediation_case_id ON public.node_resource_forecast_feedback USING btree (remediation_case_id);


--
-- Name: ix_node_resource_forecast_feedback_verdict; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_feedback_verdict ON public.node_resource_forecast_feedback USING btree (verdict);


--
-- Name: ix_node_resource_forecast_runs_cluster_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_runs_cluster_name ON public.node_resource_forecast_runs USING btree (cluster_name);


--
-- Name: ix_node_resource_forecast_runs_host; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_runs_host ON public.node_resource_forecast_runs USING btree (host);


--
-- Name: ix_node_resource_forecast_runs_predicted_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_runs_predicted_at ON public.node_resource_forecast_runs USING btree (predicted_at);


--
-- Name: ix_node_resource_forecast_runs_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_runs_status ON public.node_resource_forecast_runs USING btree (status);


--
-- Name: ix_node_resource_forecast_runs_target_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_runs_target_at ON public.node_resource_forecast_runs USING btree (target_at);


--
-- Name: ix_node_resource_forecast_transition_alert_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_forecast_transition_alert_time ON public.node_resource_forecast_transitions USING btree (alert_id, changed_at);


--
-- Name: ix_node_resource_model_states_cluster_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_model_states_cluster_name ON public.node_resource_model_states USING btree (cluster_name);


--
-- Name: ix_node_resource_model_states_host; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_resource_model_states_host ON public.node_resource_model_states USING btree (host);


--
-- Name: ix_node_upgrade_gates_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_node_upgrade_gates_cluster_id ON public.node_upgrade_gates USING btree (cluster_id);


--
-- Name: ix_object_storage_audit_cluster_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_object_storage_audit_cluster_time ON public.object_storage_audit_entries USING btree (cluster_id, created_at);


--
-- Name: ix_online_learner_audit_stream_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_online_learner_audit_stream_time ON public.online_learner_audit USING btree (cluster_key, host, metric, observed_at);


--
-- Name: ix_online_learner_cycle_audit_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_online_learner_cycle_audit_created_at ON public.online_learner_cycle_audit USING btree (created_at);


--
-- Name: ix_online_learner_cycle_audit_scope_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_online_learner_cycle_audit_scope_time ON public.online_learner_cycle_audit USING btree (cluster_key, host, metric, created_at);


--
-- Name: ix_online_learner_label_event_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_online_learner_label_event_created_at ON public.online_learner_label_events USING btree (created_at);


--
-- Name: ix_online_learner_label_event_source_action; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_online_learner_label_event_source_action ON public.online_learner_label_events USING btree (source_run_id, action);


--
-- Name: ix_online_learner_label_queue; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_online_learner_label_queue ON public.online_learner_labels USING btree (status, cluster_key, host, metric);


--
-- Name: ix_online_learner_state_lookup; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_online_learner_state_lookup ON public.online_learner_states USING btree (cluster_key, host, metric);


--
-- Name: ix_rbd_trash_usages_cluster_pool; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_rbd_trash_usages_cluster_pool ON public.rbd_trash_usages USING btree (cluster_id, pool);


--
-- Name: ix_remediation_cases_evidence_fingerprint; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_remediation_cases_evidence_fingerprint ON public.remediation_cases USING btree (evidence_fingerprint);


--
-- Name: ix_remediation_cases_operator_verdict_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_remediation_cases_operator_verdict_at ON public.remediation_cases USING btree (operator_verdict_at);


--
-- Name: ix_rgw_access_audit_cluster_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_rgw_access_audit_cluster_time ON public.rgw_access_audit_events USING btree (cluster_id, event_at);


--
-- Name: ix_rgw_access_audit_events_transaction_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_rgw_access_audit_events_transaction_id ON public.rgw_access_audit_events USING btree (transaction_id);


--
-- Name: ix_rgw_access_audit_pending; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_rgw_access_audit_pending ON public.rgw_access_audit_events USING btree (telegram_sent, event_at);


--
-- Name: ix_rgw_analysis_job_pending; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_rgw_analysis_job_pending ON public.rgw_analysis_jobs USING btree (status, created_at);


--
-- Name: ix_rgw_analysis_job_signature; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_rgw_analysis_job_signature ON public.rgw_analysis_jobs USING btree (signature, created_at);


--
-- Name: ix_rgw_error_notification_humanization_pending; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_rgw_error_notification_humanization_pending ON public.rgw_error_notifications USING btree (telegram_sent, telegram_humanization_status, external_alert_queued);


--
-- Name: ix_rgw_error_notification_pending; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_rgw_error_notification_pending ON public.rgw_error_notifications USING btree (telegram_sent, event_at);


--
-- Name: ix_telegram_channel_config_changes_channel; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_telegram_channel_config_changes_channel ON public.telegram_channel_config_changes USING btree (channel);


--
-- Name: ix_telegram_managed_channels_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_telegram_managed_channels_created_at ON public.telegram_managed_channels USING btree (created_at);


--
-- Name: ix_vita_anomaly_cluster_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vita_anomaly_cluster_status ON public.vitastor_anomaly_events USING btree (cluster_id, status, detected_at);


--
-- Name: ix_vita_audit_cluster_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vita_audit_cluster_created ON public.vitastor_audit_entries USING btree (cluster_id, created_at);


--
-- Name: ix_vita_entity_metric_lookup; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vita_entity_metric_lookup ON public.vitastor_entity_metric_samples USING btree (cluster_id, entity_type, entity_name, collected_at);


--
-- Name: ix_vita_remediation_cluster_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vita_remediation_cluster_status ON public.vitastor_remediation_actions USING btree (cluster_id, status, created_at);


--
-- Name: ix_vitastor_capacity_series; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_capacity_series ON public.vitastor_capacity_samples USING btree (cluster_id, entity_type, entity_name, captured_at);


--
-- Name: ix_vitastor_config_drift_cluster_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_config_drift_cluster_created ON public.vitastor_config_drift_scans USING btree (cluster_id, created_at);


--
-- Name: ix_vitastor_diagnostic_cluster_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_diagnostic_cluster_created ON public.vitastor_diagnostic_runs USING btree (cluster_id, created_at);


--
-- Name: ix_vitastor_ec_safety_cluster_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_ec_safety_cluster_created ON public.vitastor_ec_safety_scans USING btree (cluster_id, created_at);


--
-- Name: ix_vitastor_etcd_snapshot_cluster_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_etcd_snapshot_cluster_created ON public.vitastor_etcd_snapshots USING btree (cluster_id, created_at);


--
-- Name: ix_vitastor_incident_cluster_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_incident_cluster_status ON public.vitastor_incidents USING btree (cluster_id, status, last_seen_at);


--
-- Name: ix_vitastor_log_scan_cluster_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_log_scan_cluster_created ON public.vitastor_log_scans USING btree (cluster_id, created_at);


--
-- Name: ix_vitastor_log_scans_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_log_scans_cluster_id ON public.vitastor_log_scans USING btree (cluster_id);


--
-- Name: ix_vitastor_log_scans_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_log_scans_created_at ON public.vitastor_log_scans USING btree (created_at);


--
-- Name: ix_vitastor_metric_cluster_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_metric_cluster_time ON public.vitastor_metric_samples USING btree (cluster_id, collected_at);


--
-- Name: ix_vitastor_network_metric_cluster_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_network_metric_cluster_time ON public.vitastor_network_metric_samples USING btree (cluster_id, collected_at);


--
-- Name: ix_vitastor_node_metric_cluster_host_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_node_metric_cluster_host_time ON public.vitastor_node_metric_samples USING btree (cluster_id, host, collected_at);


--
-- Name: ix_vitastor_osd_metric_cluster_osd_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_osd_metric_cluster_osd_time ON public.vitastor_osd_metric_samples USING btree (cluster_id, osd_id, collected_at);


--
-- Name: ix_vitastor_prometheus_cluster_metric_time; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_vitastor_prometheus_cluster_metric_time ON public.vitastor_prometheus_samples USING btree (cluster_id, metric_name, collected_at);


--
-- Name: ix_volume_early_forecast_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_early_forecast_scope ON public.volume_early_forecasts USING btree (cluster_id, pool, image, generated_at);


--
-- Name: ix_volume_early_forecast_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_early_forecast_status ON public.volume_early_forecasts USING btree (status, target_at);


--
-- Name: ix_volume_early_forecasts_generated_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_early_forecasts_generated_at ON public.volume_early_forecasts USING btree (generated_at);


--
-- Name: ix_volume_early_forecasts_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_early_forecasts_status ON public.volume_early_forecasts USING btree (status);


--
-- Name: ix_volume_early_forecasts_telegram_sent_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_early_forecasts_telegram_sent_at ON public.volume_early_forecasts USING btree (telegram_sent_at);


--
-- Name: ix_volume_forecast_due; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_forecast_due ON public.volume_forecast_runs USING btree (status, target_at);


--
-- Name: ix_volume_forecast_runs_predicted_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_forecast_runs_predicted_at ON public.volume_forecast_runs USING btree (predicted_at);


--
-- Name: ix_volume_forecast_runs_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_forecast_runs_status ON public.volume_forecast_runs USING btree (status);


--
-- Name: ix_volume_forecast_runs_target_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_forecast_runs_target_at ON public.volume_forecast_runs USING btree (target_at);


--
-- Name: ix_volume_forecast_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_forecast_scope ON public.volume_forecast_runs USING btree (cluster_id, pool, image, metric);


--
-- Name: ix_volume_metrics_cluster_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_metrics_cluster_id ON public.volume_metrics USING btree (cluster_id);


--
-- Name: ix_volume_metrics_pool_image_polled_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_metrics_pool_image_polled_at ON public.volume_metrics USING btree (pool, image, polled_at);


--
-- Name: ix_volume_model_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_model_scope ON public.volume_model_states USING btree (cluster_id, pool, image, metric);


--
-- Name: ix_volume_osd_mappings_captured_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_osd_mappings_captured_at ON public.volume_osd_mappings USING btree (captured_at);


--
-- Name: ix_volume_osd_mappings_cluster_captured; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_osd_mappings_cluster_captured ON public.volume_osd_mappings USING btree (cluster_id, captured_at);


--
-- Name: ix_volume_perf_sweeps_pool_scratch_image_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_perf_sweeps_pool_scratch_image_created_at ON public.volume_perf_sweeps USING btree (pool, scratch_image, created_at);


--
-- Name: ix_volume_snapshot_policies_cluster_enabled; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_snapshot_policies_cluster_enabled ON public.volume_snapshot_policies USING btree (cluster_id, enabled);


--
-- Name: ix_volume_snapshot_policies_next_run; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_volume_snapshot_policies_next_run ON public.volume_snapshot_policies USING btree (next_run_at);


--
-- Name: uq_actions_idempotency_key_inflight; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_actions_idempotency_key_inflight ON public.actions USING btree (idempotency_key) WHERE ((idempotency_key IS NOT NULL) AND ((status)::text = ANY ((ARRAY['PENDING'::character varying, 'PENDING_APPROVAL'::character varying, 'APPROVED'::character varying, 'EXECUTING'::character varying, 'GRACE_PENDING'::character varying])::text[])));


--
-- Name: uq_ai_runbooks_source; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_ai_runbooks_source ON public.ai_runbooks USING btree (cluster_id, fault_family, source_fingerprint, prompt_version);


--
-- Name: uq_backup_jobs_active_rbd_run; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_backup_jobs_active_rbd_run ON public.backup_jobs USING btree (COALESCE(cluster_id, ''::character varying), pool, image) WHERE ((status)::text = 'RUNNING'::text);


--
-- Name: uq_clusters_single_default; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_clusters_single_default ON public.clusters USING btree (is_default) WHERE is_default;


--
-- Name: uq_incidents_inflight_cluster_code; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_incidents_inflight_cluster_code ON public.incidents USING btree (COALESCE(cluster_id, ''::character varying), ceph_code, COALESCE(dedupe_key, ''::character varying)) WHERE ((status)::text = ANY ((ARRAY['NEW'::character varying, 'DIAGNOSING'::character varying, 'PENDING_APPROVAL'::character varying, 'APPROVED'::character varying, 'EXECUTING'::character varying, 'GRACE_PENDING'::character varying, 'VERIFYING'::character varying])::text[]));


--
-- Name: uq_vitastor_single_inflight; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_vitastor_single_inflight ON public.vitastor_operations USING btree ((1)) WHERE ((status)::text = ANY ((ARRAY['PENDING_APPROVAL'::character varying, 'RUNNING'::character varying])::text[]));


--
-- Name: uq_volume_snapshot_policy_target; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX uq_volume_snapshot_policy_target ON public.volume_snapshot_policies USING btree (COALESCE(cluster_id, ''::character varying), pool, image);


--
-- Name: actions actions_incident_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.actions
    ADD CONSTRAINT actions_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.incidents(id);


--
-- Name: ai_runbook_feedback ai_runbook_feedback_runbook_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ai_runbook_feedback
    ADD CONSTRAINT ai_runbook_feedback_runbook_id_fkey FOREIGN KEY (runbook_id) REFERENCES public.ai_runbooks(id);


--
-- Name: ai_runbooks ai_runbooks_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ai_runbooks
    ADD CONSTRAINT ai_runbooks_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: audit_entries audit_entries_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.audit_entries
    ADD CONSTRAINT audit_entries_action_id_fkey FOREIGN KEY (action_id) REFERENCES public.actions(id);


--
-- Name: audit_entries audit_entries_incident_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.audit_entries
    ADD CONSTRAINT audit_entries_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.incidents(id);


--
-- Name: autopilot_cluster_config_audit autopilot_cluster_config_audit_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.autopilot_cluster_config_audit
    ADD CONSTRAINT autopilot_cluster_config_audit_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: autopilot_leases autopilot_leases_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.autopilot_leases
    ADD CONSTRAINT autopilot_leases_action_id_fkey FOREIGN KEY (action_id) REFERENCES public.actions(id);


--
-- Name: autopilot_leases autopilot_leases_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.autopilot_leases
    ADD CONSTRAINT autopilot_leases_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: backup_anomalies backup_anomalies_backup_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_anomalies
    ADD CONSTRAINT backup_anomalies_backup_job_id_fkey FOREIGN KEY (backup_job_id) REFERENCES public.backup_jobs(id);


--
-- Name: backup_jobs backup_jobs_base_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_jobs
    ADD CONSTRAINT backup_jobs_base_job_id_fkey FOREIGN KEY (base_job_id) REFERENCES public.backup_jobs(id);


--
-- Name: backup_metadata_artifacts backup_metadata_artifacts_backup_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_metadata_artifacts
    ADD CONSTRAINT backup_metadata_artifacts_backup_job_id_fkey FOREIGN KEY (backup_job_id) REFERENCES public.backup_jobs(id) ON DELETE CASCADE;


--
-- Name: bucket_inventory_snapshots bucket_inventory_snapshots_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.bucket_inventory_snapshots
    ADD CONSTRAINT bucket_inventory_snapshots_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: bucket_logging_configs bucket_logging_configs_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.bucket_logging_configs
    ADD CONSTRAINT bucket_logging_configs_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: capability_matrix_changes capability_matrix_changes_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.capability_matrix_changes
    ADD CONSTRAINT capability_matrix_changes_entry_id_fkey FOREIGN KEY (entry_id) REFERENCES public.capability_matrix_entries(id);


--
-- Name: capability_matrix_proposals capability_matrix_proposals_created_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.capability_matrix_proposals
    ADD CONSTRAINT capability_matrix_proposals_created_entry_id_fkey FOREIGN KEY (created_entry_id) REFERENCES public.capability_matrix_entries(id);


--
-- Name: capacity_alert_states capacity_alert_states_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.capacity_alert_states
    ADD CONSTRAINT capacity_alert_states_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: ceph_capacity_samples ceph_capacity_samples_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ceph_capacity_samples
    ADD CONSTRAINT ceph_capacity_samples_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: change_risk_assessments change_risk_assessments_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.change_risk_assessments
    ADD CONSTRAINT change_risk_assessments_action_id_fkey FOREIGN KEY (action_id) REFERENCES public.actions(id);


--
-- Name: change_risk_assessments change_risk_assessments_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.change_risk_assessments
    ADD CONSTRAINT change_risk_assessments_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: chat_messages chat_messages_proposed_incident_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_proposed_incident_id_fkey FOREIGN KEY (proposed_incident_id) REFERENCES public.incidents(id);


--
-- Name: cluster_capability_inventory cluster_capability_inventory_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.cluster_capability_inventory
    ADD CONSTRAINT cluster_capability_inventory_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: delegated_ai_subtasks delegated_ai_subtasks_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.delegated_ai_subtasks
    ADD CONSTRAINT delegated_ai_subtasks_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.delegated_ai_tasks(id) ON DELETE CASCADE;


--
-- Name: delegated_ai_tasks delegated_ai_tasks_assistant_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.delegated_ai_tasks
    ADD CONSTRAINT delegated_ai_tasks_assistant_message_id_fkey FOREIGN KEY (assistant_message_id) REFERENCES public.chat_messages(id);


--
-- Name: delegated_ai_tasks delegated_ai_tasks_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.delegated_ai_tasks
    ADD CONSTRAINT delegated_ai_tasks_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: backup_digest_logs fk_backup_digest_logs_cluster_id; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_digest_logs
    ADD CONSTRAINT fk_backup_digest_logs_cluster_id FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: backup_jobs fk_backup_jobs_cluster_id; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.backup_jobs
    ADD CONSTRAINT fk_backup_jobs_cluster_id FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: chat_messages fk_chat_messages_cluster_id_clusters; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT fk_chat_messages_cluster_id_clusters FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: crush_osd_distribution fk_crush_osd_distribution_cluster_id; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.crush_osd_distribution
    ADD CONSTRAINT fk_crush_osd_distribution_cluster_id FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: crush_structure_snapshots fk_crush_structure_snapshots_cluster_id; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.crush_structure_snapshots
    ADD CONSTRAINT fk_crush_structure_snapshots_cluster_id FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: incidents fk_incidents_cluster_id; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.incidents
    ADD CONSTRAINT fk_incidents_cluster_id FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: incidents fk_incidents_group_root_incident; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.incidents
    ADD CONSTRAINT fk_incidents_group_root_incident FOREIGN KEY (group_root_incident_id) REFERENCES public.incidents(id) ON DELETE SET NULL;


--
-- Name: log_findings fk_log_findings_correlated_incident; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_findings
    ADD CONSTRAINT fk_log_findings_correlated_incident FOREIGN KEY (correlated_incident_id) REFERENCES public.incidents(id);


--
-- Name: node_upgrade_gates fk_node_upgrade_gates_cluster_id; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_upgrade_gates
    ADD CONSTRAINT fk_node_upgrade_gates_cluster_id FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: volume_metrics fk_volume_metrics_cluster_id; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_metrics
    ADD CONSTRAINT fk_volume_metrics_cluster_id FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: watcher_heartbeat fk_watcher_heartbeat_cluster_id; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.watcher_heartbeat
    ADD CONSTRAINT fk_watcher_heartbeat_cluster_id FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: forecast_model_evaluations forecast_model_evaluations_active_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_model_evaluations
    ADD CONSTRAINT forecast_model_evaluations_active_model_id_fkey FOREIGN KEY (active_model_id) REFERENCES public.forecast_model_registry(id);


--
-- Name: forecast_model_evaluations forecast_model_evaluations_candidate_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_model_evaluations
    ADD CONSTRAINT forecast_model_evaluations_candidate_model_id_fkey FOREIGN KEY (candidate_model_id) REFERENCES public.forecast_model_registry(id);


--
-- Name: forecast_model_promotion_audits forecast_model_promotion_audits_candidate_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_model_promotion_audits
    ADD CONSTRAINT forecast_model_promotion_audits_candidate_model_id_fkey FOREIGN KEY (candidate_model_id) REFERENCES public.forecast_model_registry(id);


--
-- Name: forecast_model_promotion_audits forecast_model_promotion_audits_previous_active_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_model_promotion_audits
    ADD CONSTRAINT forecast_model_promotion_audits_previous_active_model_id_fkey FOREIGN KEY (previous_active_model_id) REFERENCES public.forecast_model_registry(id);


--
-- Name: host_metric_samples host_metric_samples_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.host_metric_samples
    ADD CONSTRAINT host_metric_samples_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: incident_timeline_events incident_timeline_events_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.incident_timeline_events
    ADD CONSTRAINT incident_timeline_events_action_id_fkey FOREIGN KEY (action_id) REFERENCES public.actions(id);


--
-- Name: incident_timeline_events incident_timeline_events_incident_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.incident_timeline_events
    ADD CONSTRAINT incident_timeline_events_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.incidents(id);


--
-- Name: log_fault_stats log_fault_stats_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_fault_stats
    ADD CONSTRAINT log_fault_stats_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: log_findings log_findings_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_findings
    ADD CONSTRAINT log_findings_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: log_findings log_findings_ingest_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_findings
    ADD CONSTRAINT log_findings_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES public.log_ingest_runs(id);


--
-- Name: log_ingest_runs log_ingest_runs_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_ingest_runs
    ADD CONSTRAINT log_ingest_runs_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: log_learning_audit log_learning_audit_sample_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_audit
    ADD CONSTRAINT log_learning_audit_sample_id_fkey FOREIGN KEY (sample_id) REFERENCES public.log_learning_samples(id);


--
-- Name: log_learning_samples log_learning_samples_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_samples
    ADD CONSTRAINT log_learning_samples_action_id_fkey FOREIGN KEY (action_id) REFERENCES public.actions(id);


--
-- Name: log_learning_samples log_learning_samples_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_samples
    ADD CONSTRAINT log_learning_samples_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: log_learning_samples log_learning_samples_incident_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_samples
    ADD CONSTRAINT log_learning_samples_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.incidents(id);


--
-- Name: log_learning_samples log_learning_samples_ingest_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_samples
    ADD CONSTRAINT log_learning_samples_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES public.log_ingest_runs(id);


--
-- Name: log_learning_samples log_learning_samples_log_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_samples
    ADD CONSTRAINT log_learning_samples_log_finding_id_fkey FOREIGN KEY (log_finding_id) REFERENCES public.log_findings(id);


--
-- Name: log_learning_samples log_learning_samples_remediation_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_learning_samples
    ADD CONSTRAINT log_learning_samples_remediation_case_id_fkey FOREIGN KEY (remediation_case_id) REFERENCES public.remediation_cases(id);


--
-- Name: log_pattern_observations log_pattern_observations_pattern_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_pattern_observations
    ADD CONSTRAINT log_pattern_observations_pattern_id_fkey FOREIGN KEY (pattern_id) REFERENCES public.log_patterns(id);


--
-- Name: log_patterns log_patterns_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.log_patterns
    ADD CONSTRAINT log_patterns_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: node_resource_forecast_feedback node_resource_forecast_feedback_alert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_forecast_feedback
    ADD CONSTRAINT node_resource_forecast_feedback_alert_id_fkey FOREIGN KEY (alert_id) REFERENCES public.node_resource_forecast_alerts(id);


--
-- Name: node_resource_forecast_transitions node_resource_forecast_transitions_alert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_resource_forecast_transitions
    ADD CONSTRAINT node_resource_forecast_transitions_alert_id_fkey FOREIGN KEY (alert_id) REFERENCES public.node_resource_forecast_alerts(id);


--
-- Name: node_upgrade_gates node_upgrade_gates_abort_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_upgrade_gates
    ADD CONSTRAINT node_upgrade_gates_abort_action_id_fkey FOREIGN KEY (abort_action_id) REFERENCES public.actions(id);


--
-- Name: node_upgrade_gates node_upgrade_gates_confirm_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_upgrade_gates
    ADD CONSTRAINT node_upgrade_gates_confirm_action_id_fkey FOREIGN KEY (confirm_action_id) REFERENCES public.actions(id);


--
-- Name: node_upgrade_gates node_upgrade_gates_prepare_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.node_upgrade_gates
    ADD CONSTRAINT node_upgrade_gates_prepare_action_id_fkey FOREIGN KEY (prepare_action_id) REFERENCES public.actions(id);


--
-- Name: object_storage_audit_entries object_storage_audit_entries_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.object_storage_audit_entries
    ADD CONSTRAINT object_storage_audit_entries_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: online_learner_label_events online_learner_label_events_label_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_label_events
    ADD CONSTRAINT online_learner_label_events_label_id_fkey FOREIGN KEY (label_id) REFERENCES public.online_learner_labels(id);


--
-- Name: online_learner_label_events online_learner_label_events_source_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_label_events
    ADD CONSTRAINT online_learner_label_events_source_run_id_fkey FOREIGN KEY (source_run_id) REFERENCES public.node_resource_forecast_runs(id);


--
-- Name: online_learner_labels online_learner_labels_source_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.online_learner_labels
    ADD CONSTRAINT online_learner_labels_source_run_id_fkey FOREIGN KEY (source_run_id) REFERENCES public.node_resource_forecast_runs(id);


--
-- Name: rbd_trash_usages rbd_trash_usages_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rbd_trash_usages
    ADD CONSTRAINT rbd_trash_usages_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: remediation_cases remediation_cases_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.remediation_cases
    ADD CONSTRAINT remediation_cases_action_id_fkey FOREIGN KEY (action_id) REFERENCES public.actions(id);


--
-- Name: remediation_cases remediation_cases_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.remediation_cases
    ADD CONSTRAINT remediation_cases_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: remediation_cases remediation_cases_incident_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.remediation_cases
    ADD CONSTRAINT remediation_cases_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.incidents(id);


--
-- Name: rgw_access_audit_events rgw_access_audit_events_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_access_audit_events
    ADD CONSTRAINT rgw_access_audit_events_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: rgw_analysis_jobs rgw_analysis_jobs_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_analysis_jobs
    ADD CONSTRAINT rgw_analysis_jobs_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: rgw_analysis_jobs rgw_analysis_jobs_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_analysis_jobs
    ADD CONSTRAINT rgw_analysis_jobs_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.log_findings(id);


--
-- Name: rgw_analysis_jobs rgw_analysis_jobs_ingest_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_analysis_jobs
    ADD CONSTRAINT rgw_analysis_jobs_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES public.log_ingest_runs(id);


--
-- Name: rgw_analysis_jobs rgw_analysis_jobs_source_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_analysis_jobs
    ADD CONSTRAINT rgw_analysis_jobs_source_event_id_fkey FOREIGN KEY (source_event_id) REFERENCES public.rgw_error_notifications(id);


--
-- Name: rgw_error_notifications rgw_error_notifications_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.rgw_error_notifications
    ADD CONSTRAINT rgw_error_notifications_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: volume_early_forecasts volume_early_forecasts_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_early_forecasts
    ADD CONSTRAINT volume_early_forecasts_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: volume_forecast_runs volume_forecast_runs_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_forecast_runs
    ADD CONSTRAINT volume_forecast_runs_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: volume_model_states volume_model_states_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_model_states
    ADD CONSTRAINT volume_model_states_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: volume_osd_mappings volume_osd_mappings_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_osd_mappings
    ADD CONSTRAINT volume_osd_mappings_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: volume_snapshot_policies volume_snapshot_policies_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.volume_snapshot_policies
    ADD CONSTRAINT volume_snapshot_policies_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id);


--
-- Name: SCHEMA pgbouncer; Type: ACL; Schema: -; Owner: postgres
--

GRANT USAGE ON SCHEMA pgbouncer TO _crunchypgbouncer;


--
-- Name: FUNCTION get_auth(username text); Type: ACL; Schema: pgbouncer; Owner: postgres
--

REVOKE ALL ON FUNCTION pgbouncer.get_auth(username text) FROM PUBLIC;
GRANT ALL ON FUNCTION pgbouncer.get_auth(username text) TO _crunchypgbouncer;


--
-- PostgreSQL database dump complete
--

\unrestrict zZjBly1dQSLJe6MKVTbX2egW1hxuZ4tsezYUTZcQ9TSo7s8fYF8ZmzQJrXL92fs
