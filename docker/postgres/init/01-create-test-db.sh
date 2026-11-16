#!/bin/sh
# Runs ONCE, only when the postgres_data volume is empty (first start).
# Creates a second database for automated tests, owned by the same user,
# so tests never touch development data.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
CREATE DATABASE "${POSTGRES_DB}_test" OWNER "${POSTGRES_USER}";
EOSQL