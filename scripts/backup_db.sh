#!/usr/bin/env bash
#
# backup_db.sh — PostgreSQL backup with 7-day rolling retention.
#
# Dumps the `nepse_analytics` database from the dockerized `db` service
# (postgres:16-alpine) through gzip into data/backups/, then prunes any
# snapshot older than 7 days so disk usage never grows unbounded.
#
# Requirements:
#   - The `db` service must be up (docker compose up -d db).
#   - Run from the repository root (or a subdirectory; paths are resolved
#     relative to this script's location).

set -euo pipefail

# Resolve the repository root as the directory that contains this script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
BACKUP_DIR="${REPO_ROOT}/data/backups"

# ---------------------------------------------------------------------------
# 1. Ensure the target directory exists.
# ---------------------------------------------------------------------------
mkdir -p "${BACKUP_DIR}"

# ---------------------------------------------------------------------------
# 2. Dump the database inside the `db` container, gzip on the host.
#    Filename: nepse_backup_YYYYMMDD_HHMMSS.sql.gz
# ---------------------------------------------------------------------------
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="${BACKUP_DIR}/nepse_backup_${TIMESTAMP}.sql.gz"

echo "[1/3] Creating backup: ${OUT_FILE}"
echo "      Dumping from container 'db' (quant@nepse_analytics) ..."

# `-T` disables TTY allocation so output can be piped to gzip cleanly.
(cd "${REPO_ROOT}" \
  && docker compose exec -T db pg_dump -U quant -d nepse_analytics) \
  | gzip > "${OUT_FILE}"

BACKUP_SIZE="$(du -h "${OUT_FILE}" | cut -f1)"
echo "      Backup complete (${BACKUP_SIZE})."

# ---------------------------------------------------------------------------
# 3. Rolling retention: delete snapshots older than 7 days.
# ---------------------------------------------------------------------------
echo "[2/3] Pruning stale backups older than 7 days ..."
PRUNED="$(find "${BACKUP_DIR}" -type f -name 'nepse_backup_*.sql.gz' -mtime +7 -delete -print)"
if [[ -n "${PRUNED}" ]]; then
  echo "      Removed ${#PRUNED} snapshot(s) for deletion:"
  while IFS= read -r f; do
    echo "        - $(basename "${f}")"
  done <<< "${PRUNED}"
else
  echo "      No stale backups to prune."
fi

# ---------------------------------------------------------------------------
# 4. Status summary.
# ---------------------------------------------------------------------------
echo "[3/3] Done."
echo "      File    : ${OUT_FILE}"
echo "      Size    : ${BACKUP_SIZE}"
echo "      Retention: kept = $(find "${BACKUP_DIR}" -type f -name 'nepse_backup_*.sql.gz' -mtime -7 | wc -l | tr -d ' '), pruned = $(if [[ -n "${PRUNED}" ]]; then echo "${#PRUNED}"; else echo 0; fi)"
echo "      Next    : none"