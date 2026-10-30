#!/usr/bin/env bash
set -euo pipefail

: "${MYSQL_ROOT_PASSWORD:?Codespaces did not provide MYSQL_ROOT_PASSWORD}"
umask 077
cat > .devcontainer/.mysql-client.cnf <<EOF
[client]
host=mysql
user=root
password=${MYSQL_ROOT_PASSWORD}
database=petrotwin
EOF
chmod 600 .devcontainer/.mysql-client.cnf
