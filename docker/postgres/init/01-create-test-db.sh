#!/bin/sh
# Runs ONCE, only when the postgres_data volume is empty (first start).
# Creates a second database for automated tests, owned by the same user,
# so tests never touch development data.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
CREATE DATABASE "${POSTGRES_DB}_test" OWNER "${POSTGRES_USER}";
EOSQL

# 01-create-test-db.sh is a PostgreSQL
# initialization script. When PostgreSQL starts with a
# new, empty data volume, the official image runs scripts mounted
# in /docker-entrypoint-initdb.d. This one creates a separate
# database named <your POSTGRES_DB>_test—for example, task_management_test—for automated tests.
