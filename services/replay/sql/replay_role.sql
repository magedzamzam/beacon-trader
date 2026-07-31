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

-- 1. the role
CREATE ROLE beacon_replay LOGIN PASSWORD 'CHANGE_ME';

-- 2. read the schema
GRANT CONNECT ON DATABASE beacon TO beacon_replay;
GRANT USAGE ON SCHEMA public TO beacon_replay;

-- 3. SELECT-only on everything that exists today, including `candles` (which is
--    read from the live/replica DB, never from the daily dump — the dump
--    excludes the table, see pg_backup.ps1).
GRANT SELECT ON ALL TABLES IN SCHEMA public TO beacon_replay;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO beacon_replay;

-- 4. …and NOTHING else. Spelled out rather than assumed, because a future
--    `GRANT ALL ... TO PUBLIC` elsewhere would otherwise quietly re-open these.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public
  FROM beacon_replay;

-- 5. its OWN tables. `replay_runs` / `replay_results` are created by
--    `python main.py run` (create_all on the harness's own metadata, which has
--    never heard of a trading table). Grant write on just those two.
--    Run this part AFTER the first `run` has created them.
GRANT SELECT, INSERT, UPDATE, DELETE ON replay_runs, replay_results
  TO beacon_replay;
GRANT USAGE, SELECT ON SEQUENCE replay_runs_id_seq, replay_results_id_seq
  TO beacon_replay;

-- 6. verify. Every row here should be a trading table with only "SELECT".
-- SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type)
--   FROM information_schema.table_privileges
--  WHERE grantee = 'beacon_replay'
--  GROUP BY table_name ORDER BY table_name;
