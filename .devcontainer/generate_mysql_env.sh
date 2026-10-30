#!/usr/bin/env bash
set -euo pipefail

env_file="$(dirname "$0")/.mysql.env"
if [[ ! -s "$env_file" ]]; then
  umask 077
  python3 - "$env_file" <<'PY'
from pathlib import Path
import secrets
import sys

path = Path(sys.argv[1])
path.write_text(f"MYSQL_ROOT_PASSWORD={secrets.token_hex(32)}\n", encoding="utf-8")
path.chmod(0o600)
PY
fi
