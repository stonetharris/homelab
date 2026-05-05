#!/usr/bin/env bash
set -euo pipefail

REPO="/mnt/backup/restic"
PASSWORD_FILE="/root/.config/restic/backup-password"

restic -r "$REPO" backup \
  /home/stone/homelab \
  /srv/docker \
  --password-file "$PASSWORD_FILE"

restic -r "$REPO" forget \
  --keep-daily 7 \
  --keep-weekly 4 \
  --keep-monthly 6 \
  --prune \
  --password-file "$PASSWORD_FILE"

restic -r "$REPO" check \
  --password-file "$PASSWORD_FILE"
