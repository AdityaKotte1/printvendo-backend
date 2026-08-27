#!/usr/bin/env bash
# Nightly backup: Postgres dump + the storage tree. Keeps 14 days.
#
# Install:
#   sudo cp backup.sh /usr/local/bin/printvendo-backup
#   sudo chmod +x /usr/local/bin/printvendo-backup
#   ( sudo crontab -l 2>/dev/null; echo '0 2 * * * /usr/local/bin/printvendo-backup >> /var/log/printvendo-backup.log 2>&1' ) | sudo crontab -
#
# A backup that has never been restored is a folder of files. Restore one into
# a scratch database before you believe any of this.
set -euo pipefail

COMPOSE_DIR=/opt/printvendo/printvendo-backend/deploy
BACKUP_DIR=/opt/printvendo/backups
STAMP=$(date +%Y%m%d-%H%M%S)
RETENTION_DAYS=14

mkdir -p "$BACKUP_DIR"

# shellcheck disable=SC1091
source "$COMPOSE_DIR/.env"

docker compose -f "$COMPOSE_DIR/docker-compose.yml" exec -T db \
	pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
	> "$BACKUP_DIR/db-$STAMP.dump"

tar -czf "$BACKUP_DIR/storage-$STAMP.tar.gz" -C /opt/printvendo storage

find "$BACKUP_DIR" -name 'db-*.dump' -mtime +$RETENTION_DAYS -delete
find "$BACKUP_DIR" -name 'storage-*.tar.gz' -mtime +$RETENTION_DAYS -delete

echo "[$(date -Is)] backup ok: db-$STAMP.dump ($(du -h "$BACKUP_DIR/db-$STAMP.dump" | cut -f1))"
