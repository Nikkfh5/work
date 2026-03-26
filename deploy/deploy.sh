#!/bin/bash
# deploy.sh — деплой на VPS через rsync + pip + systemctl
#
# Использование:
#   ./deploy/deploy.sh user@host
#   ./deploy/deploy.sh deploy@123.45.67.89

set -euo pipefail

HOST="${1:?Usage: ./deploy/deploy.sh user@host}"
REMOTE_DIR="/home/deploy/work"

echo "==> Syncing files to $HOST:$REMOTE_DIR"
rsync -avz --delete \
    --exclude '.env' \
    --exclude 'data/' \
    --exclude 'logs/' \
    --exclude 'repos_cache/' \
    --exclude 'worktrees/' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude 'venv/' \
    --exclude '.claude/settings.local.json' \
    ./ "$HOST:$REMOTE_DIR/"

echo "==> Installing dependencies"
ssh "$HOST" "cd $REMOTE_DIR && python3 -m venv venv 2>/dev/null || true && venv/bin/pip install -q -r requirements.txt"

echo "==> Setting up systemd service"
ssh "$HOST" "sudo cp $REMOTE_DIR/deploy/supervisor.service /etc/systemd/system/ai-supervisor.service && sudo systemctl daemon-reload"

echo "==> Restarting service"
ssh "$HOST" "sudo systemctl restart ai-supervisor"

echo "==> Status"
ssh "$HOST" "sudo systemctl status ai-supervisor --no-pager -l" || true

echo "==> Done!"
