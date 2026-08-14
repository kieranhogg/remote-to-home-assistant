#!/bin/bash
set -euo pipefail

sudo useradd -r -s /bin/false -G input remote
sudo mkdir -p /opt/remote
sudo cp main.py /opt/remote
sudo cp remote.service /etc/systemd/system/

python3 -m venv /opt/remote/.venv
/opt/remote/.venv/bin/pip install -r requirements.txt

sudo chown -R remote:remote /opt/remote

sudo systemctl daemon-reload
sudo systemctl enable --now remote