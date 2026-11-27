#!/bin/bash
# Выполняется Patroni один раз — на участнике, который создаёт кластер (initdb).
# Реплики получают всё это потоковой репликацией.
set -euo pipefail
SOCK=/var/run/postgresql
psql -h "$SOCK" -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE feed LOGIN PASSWORD 'feed'" \
  -c "CREATE DATABASE feed OWNER feed"
psql -h "$SOCK" -U feed -d feed -v ON_ERROR_STOP=1 -f /schema/schema.sql
echo "post_bootstrap: база feed и схема созданы"
