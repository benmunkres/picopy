#! /bin/sh

set -e

cd "$(dirname "$0")/.."

echo "=> Stopping picopy...\n"
sudo systemctl stop picopy.service
sudo systemctl disable picopy.service

echo "=> Stopping listen-for-shutdown...\n"
sudo systemctl stop listen-for-shutdown.service
sudo systemctl disable listen-for-shutdown.service

echo "=> Removing picopy...\n"
sudo rm -f /usr/local/bin/picopy.py
sudo rm -f /etc/systemd/system/picopy.service
sudo rm -f /etc/picopy.conf

echo "=> Removing listen-for-shutdown...\n"
sudo rm -f /usr/local/bin/listen-for-shutdown.py
sudo rm -f /etc/systemd/system/listen-for-shutdown.service

sudo systemctl daemon-reload

echo "picopy uninstalled.\n"
