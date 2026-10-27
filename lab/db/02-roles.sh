# Sourced by the postgres entrypoint on first start (needs $POSTGRES_PASSWORD).
# Each client gets its own least-privilege role:
#   crm_app       the CRM API servers
#   reporting     the scheduled sales export job
#   ops_readonly  the agent's diagnostics: read-only, sees all sessions,
#                 and may use the reserved slots so it can still log in
#                 when the database is out of connections
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE crm_app LOGIN PASSWORD '$POSTGRES_PASSWORD';
GRANT SELECT, INSERT, DELETE ON customers TO crm_app;
GRANT USAGE ON SEQUENCE customers_id_seq TO crm_app;

CREATE ROLE reporting LOGIN;
GRANT SELECT ON customers TO reporting;

CREATE ROLE ops_readonly LOGIN CONNECTION LIMIT 2;
GRANT pg_monitor, pg_use_reserved_connections TO ops_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ops_readonly;
ALTER ROLE ops_readonly SET default_transaction_read_only = on;
ALTER ROLE ops_readonly SET statement_timeout = '5s';
SQL
