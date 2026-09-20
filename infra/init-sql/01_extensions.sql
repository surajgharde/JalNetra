-- Runs once on first database creation (docker-entrypoint-initdb.d).
-- Alembic migration 0001 repeats these idempotently for non-Docker databases.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
