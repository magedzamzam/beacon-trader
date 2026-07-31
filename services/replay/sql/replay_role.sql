-- Read-only database role for services/replay (#169 §1).
--
-- Run this ONCE as a superuser on the Postgres that holds the trading schema,
-- then put the resulting DSN in .env as REPLAY_DATABASE_URL. The harness
-- deliberately does NOT fall back to DATABASE_URL: the isolation is a grant, not
-- a convention, and a missing REPLAY_DATABASE_URL is a startup error.
--
--   REPLAY_DATABASE_URL=postgresql+asyncpg://beacon_replay:CHANGE_ME@host:5432/beacon
--
-- What this buys: `docker compose stop replay` has zero effect on trading, and
-- a bug in the harness CANNOT write a trade, a leg, a signal, a strategy or a
-- setting — the database refuses, rather than the code remembering not to.
--
-- ONE BLOCK, run top to bottom. There is deliberately no "and then, after the
-- first run, grant…" step: the earlier version of this file had one, and it was
-- unrunnable — it named tables that do not exist until a run has created them,
-- and the role had no CREATE privilege to create them with. Owning a schema
-- resolves both ends and is TIGHTER than the alternative (granting CREATE on
-- `public`), because the harness ends up with zero CREATE next to the trading
-- tables.
--
-- Run it as the DATABASE OWNER. It does NOT require a superuser — nothing here
-- needs SET ROLE, which is what a managed Postgres will not give you.
--
-- Substitute below:
--   beacon        -> your database name, if different
--   beacon_app    -> the role in DATABASE_URL, i.e. whoever OWNS the trading
--                    tables. Get it with:
--                      SELECT tableowner FROM pg_tables
--                       WHERE schemaname='public' AND tablename='trades';
--   CHANGE_ME     -> a long random password

-- 1. the role. NOSUPERUSER/NOCREATEDB/NOCREATEROLE are the defaults, but stated
--    so a reader does not have to know that.
CREATE ROLE beacon_replay LOGIN PASSWORD 'CHANGE_ME'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

-- 2. read the trading schema
GRANT CONNECT ON DATABASE beacon TO beacon_replay;
GRANT USAGE ON SCHEMA public TO beacon_replay;   -- USAGE only: NO create
GRANT SELECT ON ALL TABLES IN SCHEMA public TO beacon_replay;

-- 3. …and keep reading it as it grows. FOR ROLE is load-bearing: default
--    privileges follow the role that CREATES the object, not the role that ran
--    this statement. Omitting it (as the first cut did) silently scopes the rule
--    to tables the SUPERUSER creates — while the trading tables are created by
--    `create_all` running as the app role — so the next table added to models.py
--    would be invisible to the harness for no discoverable reason.
ALTER DEFAULT PRIVILEGES FOR ROLE beacon_app IN SCHEMA public
  GRANT SELECT ON TABLES TO beacon_replay;

-- 4. a schema of its own to write into. `create_all` in the harness then works
--    with no further grant, now or ever, and the tables it makes are owned by
--    beacon_replay because the creator owns what it creates.
--
--    NOT `CREATE SCHEMA replay AUTHORIZATION beacon_replay`. That form makes the
--    ROLE the schema owner, and creating a schema owned by someone else requires
--    being able to SET ROLE to them — which a non-superuser cannot do, so on
--    managed Postgres (RDS, Azure, DO, Supabase, Neon…) it fails with
--    `ERROR: must be able to SET ROLE "beacon_replay"` (SQLSTATE 42501). This
--    form needs only CREATE on the database, which the database owner has, and
--    it is marginally TIGHTER: the harness can create and drop its own tables
--    but cannot drop the schema.
--
--    NOT named `beacon_replay` either: the default search_path is
--    `"$user", public`, so a schema sharing the role's name would shadow
--    `public` and an unqualified read could resolve to the wrong place.
CREATE SCHEMA IF NOT EXISTS replay;
GRANT USAGE, CREATE ON SCHEMA replay TO beacon_replay;

-- 5. belt and braces. Neither should be needed — nothing above grants write, and
--    privileges held by PUBLIC apply to every role, so revoking only from
--    beacon_replay would not have helped anyway.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public
  FROM beacon_replay;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public
  FROM PUBLIC;


-- ============================ VERIFY ==========================================
-- Run these AFTER the block above. Each states what a PASS looks like, so the
-- check is a yes/no rather than a judgement call.

-- (a) Any table it can do more than SELECT on. PASS = ZERO ROWS.
SELECT table_schema, table_name,
       string_agg(privilege_type, ',' ORDER BY privilege_type) AS privs
  FROM information_schema.table_privileges
 WHERE grantee = 'beacon_replay' AND table_schema = 'public'
 GROUP BY 1, 2
HAVING string_agg(privilege_type, ',' ORDER BY privilege_type) <> 'SELECT'
 ORDER BY 1, 2;

-- (b) How many trading tables it can read. PASS = equal to the second number.
SELECT (SELECT count(DISTINCT table_name)
          FROM information_schema.table_privileges
         WHERE grantee = 'beacon_replay' AND table_schema = 'public'
           AND privilege_type = 'SELECT')            AS readable,
       (SELECT count(*) FROM pg_tables
         WHERE schemaname = 'public')                AS total_public_tables;

-- (c) Schema privileges. PASS = (f, t, t) — it CANNOT create next to the trading
--     tables, can read them, and can create in its own schema.
SELECT has_schema_privilege('beacon_replay', 'public', 'CREATE') AS create_public,
       has_schema_privilege('beacon_replay', 'public', 'USAGE')  AS usage_public,
       has_schema_privilege('beacon_replay', 'replay', 'CREATE') AS create_replay;

-- (d) Anything granted to PUBLIC reaches beacon_replay too. PASS = ZERO ROWS.
SELECT table_name, privilege_type
  FROM information_schema.table_privileges
 WHERE grantee = 'PUBLIC' AND table_schema = 'public'
   AND privilege_type <> 'SELECT';

-- (e) The role is not quietly powerful. PASS = all f.
SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls
  FROM pg_roles WHERE rolname = 'beacon_replay';

-- (f) Default privileges point at the OWNER of the trading tables. PASS = a row
--     whose granting_role is the same as `SELECT tableowner FROM pg_tables
--     WHERE tablename='trades'`.
SELECT pg_get_userbyid(defaclrole) AS granting_role,
       defaclnamespace::regnamespace AS schema, defaclacl
  FROM pg_default_acl;


-- ===================== THE DECISIVE TEST =====================================
-- The queries above check the grant table. This checks the DATABASE. Connect AS
-- beacon_replay:
--
--   psql "postgresql://beacon_replay:CHANGE_ME@HOST:5432/beacon"
--
-- and run it. The INSERT is wrapped in a transaction that always rolls back, so
-- a WRONG grant costs nothing beyond a discarded row.
--
--   SELECT count(*) FROM signals;      -- PASS: returns a number
--   BEGIN;
--   INSERT INTO settings (key, value) VALUES ('__replay_probe', '{}'::json);
--   ROLLBACK;
--   -- PASS: ERROR: permission denied for table settings
--   -- FAIL: "INSERT 0 1" — the grant is wrong, do NOT deploy
--
--   CREATE TABLE public.__replay_probe (x int);
--   -- PASS: ERROR: permission denied for schema public
--
--   CREATE TABLE replay.__replay_probe (x int); DROP TABLE replay.__replay_probe;
--   -- PASS: both succeed — this is the schema it may write in
--
-- ===================== READING THE RESULTS ===================================
-- `beacon_replay` is also the right connection for ANALYSING a run: it can read
-- every trading table AND the replay_* tables it created, and it cannot write
-- any of them. No extra grant needed:
--
--   psql "postgresql://beacon_replay:CHANGE_ME@HOST:5432/beacon"
--   SELECT variant, count(*) FROM replay.replay_results
--    WHERE run_id = 1 AND taken GROUP BY 1;
--
-- If another role (a dashboard, your admin login) also needs to read them, run
-- this AS beacon_replay after the first run — it owns the tables, so it is the
-- role that can grant on them:
--
--   GRANT USAGE ON SCHEMA replay TO beacon_app;
--   GRANT SELECT ON ALL TABLES IN SCHEMA replay TO beacon_app;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA replay GRANT SELECT ON TABLES TO beacon_app;
