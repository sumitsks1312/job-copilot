#!/usr/bin/env bash
# deploy.sh — pull latest code and restart the container on EC2
# Run this every time you want to update the app.
# Usage: cd /opt/job-copilot && bash deploy.sh
set -euo pipefail

APP_DIR="/opt/job-copilot"
cd "$APP_DIR"

echo "==> Pulling latest code..."
git pull origin main

echo "==> Rebuilding image..."
docker compose build --no-cache

echo "==> Restarting container..."
docker compose down --remove-orphans
docker compose up -d

echo ""
echo "✅ App is running at http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo 'YOUR_EC2_IP'):5000"
echo ""
echo "Useful commands:"
echo "  docker compose logs -f       # stream logs"
echo "  docker compose down          # stop the app"
