-- Ceph AI customer federation database (plan readiness-gap-review R8, 12/10/2026).
--
-- Customers run Ceph AI in their own networks. One that may reach out connects to this
-- database over the WAN (45.124.93.83:1207 -> PostgreSQL) with the shipped, low-privilege
-- `fed_enroll` login, which can only call enroll(code). A valid activation code (issued by us,
-- single use, expiring, stored only as a SHA-256 hash) creates that customer's own login
-- `cust_<id>` and own table `inbox.c_<id>`, INSERT-only. Learned knowledge is shared per tier
-- (`knowledge.basic`, `knowledge.vip`). Nothing here is readable by a customer except the
-- knowledge of its tier.
--
-- Apply as the PostgreSQL superuser, connected to ceph_ai_federation. Re-runnable.

\set ON_ERROR_STOP on

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fed_owner') THEN
    CREATE ROLE fed_owner NOLOGIN CREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fed_enroll') THEN
    CREATE ROLE fed_enroll LOGIN CONNECTION LIMIT 20;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fed_customers') THEN
    CREATE ROLE fed_customers NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fed_tier_basic') THEN
    CREATE ROLE fed_tier_basic NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fed_tier_vip') THEN
    CREATE ROLE fed_tier_vip NOLOGIN IN ROLE fed_tier_basic;  -- VIP also sees basic knowledge
  END IF;
END $$;

-- The owner must be able to grant these to the roles it creates.
GRANT fed_customers TO fed_owner WITH ADMIN OPTION;
GRANT fed_tier_basic TO fed_owner WITH ADMIN OPTION;
GRANT fed_tier_vip TO fed_owner WITH ADMIN OPTION;

REVOKE ALL ON DATABASE ceph_ai_federation FROM PUBLIC;
GRANT CONNECT ON DATABASE ceph_ai_federation TO fed_enroll, fed_customers;
GRANT CREATE ON DATABASE ceph_ai_federation TO fed_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA IF NOT EXISTS registry AUTHORIZATION fed_owner;
CREATE SCHEMA IF NOT EXISTS inbox AUTHORIZATION fed_owner;
CREATE SCHEMA IF NOT EXISTS knowledge AUTHORIZATION fed_owner;
CREATE SCHEMA IF NOT EXISTS enroll_api AUTHORIZATION fed_owner;
REVOKE ALL ON SCHEMA registry, inbox, knowledge, enroll_api FROM PUBLIC;

SET ROLE fed_owner;

CREATE TABLE IF NOT EXISTS registry.customers (
    id            text PRIMARY KEY,                 -- short random id, never the customer's name
    name          text NOT NULL,                    -- for us only
    tier          text NOT NULL DEFAULT 'basic' CHECK (tier IN ('basic', 'vip')),
    role_name     text NOT NULL UNIQUE,
    inbox_table   text NOT NULL UNIQUE,
    instance      text,                             -- what the customer's install reported at enrollment
    enrolled_at   timestamptz NOT NULL DEFAULT now(),
    enrolled_from inet,
    disabled_at   timestamptz
);

CREATE TABLE IF NOT EXISTS registry.enrollment_codes (
    code_sha256   text PRIMARY KEY,
    customer_name text NOT NULL,
    tier          text NOT NULL DEFAULT 'basic' CHECK (tier IN ('basic', 'vip')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    used_at       timestamptz,
    customer_id   text REFERENCES registry.customers(id),
    revoked_at    timestamptz
);

CREATE TABLE IF NOT EXISTS registry.enroll_attempts (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    client_addr inet,
    ok          boolean NOT NULL,
    reason      text
);

CREATE TABLE IF NOT EXISTS knowledge.basic (
    version    text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    pack       jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge.vip (LIKE knowledge.basic INCLUDING ALL);

RESET ROLE;

GRANT USAGE ON SCHEMA knowledge TO fed_tier_basic;
GRANT SELECT ON knowledge.basic TO fed_tier_basic;
GRANT SELECT ON knowledge.vip TO fed_tier_vip;
GRANT USAGE ON SCHEMA inbox TO fed_customers;
GRANT USAGE ON SCHEMA enroll_api TO fed_enroll;

CREATE OR REPLACE FUNCTION enroll_api.enroll(activation_code text, instance text DEFAULT NULL)
RETURNS TABLE (customer_id text, login text, password text, inbox_table text, tier text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
#variable_conflict use_column
DECLARE
    code_hash text;
    code_row  registry.enrollment_codes%ROWTYPE;
    new_id    text;
    new_role  text;
    new_table text;
    new_pw    text;
BEGIN
    -- A failure raises, which rolls the transaction back: failed attempts are kept in the PostgreSQL log
    -- (with the client address, read by fail2ban), not in registry.enroll_attempts.
    IF activation_code IS NULL OR length(activation_code) NOT BETWEEN 16 AND 64 THEN
        RAISE EXCEPTION 'activation code rejected' USING ERRCODE = '28000';
    END IF;
    code_hash := encode(sha256(convert_to(upper(btrim(activation_code)), 'UTF8')), 'hex');
    SELECT * INTO code_row FROM registry.enrollment_codes c WHERE c.code_sha256 = code_hash FOR UPDATE;
    IF NOT FOUND OR code_row.used_at IS NOT NULL OR code_row.revoked_at IS NOT NULL OR code_row.expires_at < now() THEN
        -- One message for every failure: an attacker learns nothing about which codes exist.
        RAISE EXCEPTION 'activation code rejected' USING ERRCODE = '28000';
    END IF;

    new_id    := substr(replace(gen_random_uuid()::text, '-', ''), 1, 12);
    new_role  := 'cust_' || new_id;
    new_table := 'c_' || new_id;
    new_pw    := replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '');

    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L CONNECTION LIMIT 3 IN ROLE fed_customers, %I',
                   new_role, new_pw, 'fed_tier_' || code_row.tier);
    EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', new_role, '30s');
    EXECUTE format('CREATE TABLE inbox.%I (
                        id              bigserial PRIMARY KEY,
                        received_at     timestamptz NOT NULL DEFAULT now(),
                        payload_version integer NOT NULL,
                        payload         jsonb NOT NULL CHECK (octet_length(payload::text) <= 1048576))', new_table);
    EXECUTE format('GRANT INSERT ON inbox.%I TO %I', new_table, new_role);
    EXECUTE format('GRANT USAGE ON SEQUENCE inbox.%I TO %I', new_table || '_id_seq', new_role);

    INSERT INTO registry.customers (id, name, tier, role_name, inbox_table, instance, enrolled_from)
    VALUES (new_id, code_row.customer_name, code_row.tier, new_role, new_table, left(instance, 200), inet_client_addr());
    UPDATE registry.enrollment_codes SET used_at = now(), customer_id = new_id WHERE code_sha256 = code_hash;
    INSERT INTO registry.enroll_attempts (client_addr, ok, reason) VALUES (inet_client_addr(), true, new_id);

    RETURN QUERY SELECT new_id, new_role, new_pw, 'inbox.' || new_table, code_row.tier;
END;
$fn$;

ALTER FUNCTION enroll_api.enroll(text, text) OWNER TO fed_owner;
REVOKE ALL ON FUNCTION enroll_api.enroll(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION enroll_api.enroll(text, text) TO fed_enroll;
