#!/usr/bin/env bash
# ec2-setup.sh — run ONCE on a fresh EC2 instance (Amazon Linux 2023 / Ubuntu)
# Usage: bash ec2-setup.sh
set -euo pipefail

echo "==> Installing Docker..."
if command -v apt-get &>/dev/null; then
  # Ubuntu
  sudo apt-get update -y
  sudo apt-get install -y docker.io docker-compose-plugin git
  sudo systemctl enable --now docker
else
  # Amazon Linux 2023
  sudo dnf install -y docker git
  sudo systemctl enable --now docker
  # docker compose v2 plugin
  COMPOSE_VER="2.27.0"
  sudo mkdir -p /usr/local/lib/docker/cli-plugins
  sudo curl -SL "https://github.com/docker/compose/releases/download/v${COMPOSE_VER}/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

# Allow current user to run docker without sudo
sudo usermod -aG docker "$USER"

echo "==> Cloning repo..."
REPO_URL="${REPO_URL:-https://github.com/sumitsks1312/job-copilot.git}"
git clone "$REPO_URL" /opt/job-copilot || (cd /opt/job-copilot && git pull)

echo "==> Creating data directory..."
mkdir -p /opt/job-copilot/data/uploads

echo "==> Creating .env from example..."
if [ ! -f /opt/job-copilot/.env ]; then
  cp /opt/job-copilot/.env.example /opt/job-copilot/.env
  echo ""
  echo "⚠️  Edit /opt/job-copilot/.env and fill in your API keys, then run deploy.sh"
else
  echo ".env already exists — skipping."
fi

echo ""
echo "✅ Setup complete. Next steps:"
echo "   1. Log out and back in (to apply docker group)"
echo "   2. Edit /opt/job-copilot/.env with your real API keys"
echo "   3. Run: cd /opt/job-copilot && bash deploy.sh"
