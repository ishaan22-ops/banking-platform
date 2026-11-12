#!/bin/bash
set -euo pipefail

for service in gateway auth accounts transfers assistant; do
  password_var="$(printf '%s_DB_PASSWORD' "$service" | tr '[:lower:]' '[:upper:]')"
  service_password="${!password_var:?${password_var} must be set}"
  psql \
    --set ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set service_name="$service" \
    --set service_password="$service_password" <<'SQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L',
  'harbor_' || :'service_name',
  :'service_password'
) WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'harbor_' || :'service_name'
) \gexec
SELECT format(
  'ALTER ROLE %I WITH LOGIN PASSWORD %L',
  'harbor_' || :'service_name',
  :'service_password'
) \gexec
SELECT format(
  'CREATE DATABASE %I OWNER %I',
  'harbor_' || :'service_name',
  'harbor_' || :'service_name'
) WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = 'harbor_' || :'service_name'
) \gexec
SELECT format(
  'ALTER DATABASE %I OWNER TO %I',
  'harbor_' || :'service_name',
  'harbor_' || :'service_name'
) \gexec
SQL
done
