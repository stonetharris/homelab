#!/usr/bin/env bash
set -e

echo "Updating Ubuntu packages..."
sudo apt update
sudo apt upgrade -y
sudo apt autoremove -y

echo "Updating Jellyfin container..."
cd ~/homelab/docker/jellyfin
docker compose pull
docker compose up -d

echo "Cleanup unused Docker images..."
docker image prune -f

echo "Done."
